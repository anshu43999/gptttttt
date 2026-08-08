from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from domain.tasks import TaskEvent, TaskSummary
from infrastructure import db


class TasksRepository:
    def __init__(self, db_path: str | Path | None = None):
        self.db_path = db_path
        db.init_db(db_path)

    def create(self, task: dict[str, Any]) -> TaskSummary:
        return TaskSummary.from_dict(db.create_task(task, path=self.db_path))

    def create_many(self, tasks: list[dict[str, Any]]) -> int:
        return db.create_tasks_bulk(tasks, path=self.db_path)

    def update(self, task_id: str, **patch: Any) -> TaskSummary:
        return TaskSummary.from_dict(db.update_task(task_id, path=self.db_path, **patch))

    def get(self, task_id: str) -> TaskSummary:
        return TaskSummary.from_dict(db.get_task(task_id, path=self.db_path))

    def list(self, *, status: str = "", limit: int = 50, offset: int = 0, order: str = "desc") -> list[TaskSummary]:
        return [TaskSummary.from_dict(item) for item in db.list_tasks(status=status, limit=limit, offset=offset, order=order, path=self.db_path)]

    def claim_next_queued_task(
        self,
        *,
        max_parallel: int,
        bucket_limits: Mapping[str, int],
        bucket_for_type: Callable[[str], str],
        orphan_grace_seconds: int = 45,
    ) -> TaskSummary | None:
        """Atomically claim the oldest queued task into status=starting.

        Capacity (live seats):
        - ``running`` with non-zero result pid (process exists)
        - ``starting`` younger than grace (claim → lease → spawn in flight)

        Aged ``starting`` and aged nopid ``running`` rows are requeued in the same
        transaction so they never permanently fill max_parallel.
        """
        try:
            global_limit = int(max_parallel)
        except (TypeError, ValueError):
            return None
        if global_limit < 1:
            return None

        try:
            grace = max(10, min(int(orphan_grace_seconds or 20), 300))
        except (TypeError, ValueError):
            grace = 20

        parsed_bucket_limits: dict[str, int] = {}
        for bucket, limit in bucket_limits.items():
            try:
                parsed_bucket_limits[str(bucket)] = int(limit)
            except (TypeError, ValueError):
                continue

        from infrastructure.db_backend import resolve_backend

        db.init_db(self.db_path)
        backend = resolve_backend()
        with db.connect(self.db_path) as conn:
            conn.execute("BEGIN" if backend == "postgres" else "BEGIN IMMEDIATE")
            try:
                if backend == "postgres":
                    conn.execute("SELECT pg_advisory_xact_lock(?, ?)", (73_241, 8_291))

                now = db.now_iso()
                self._requeue_orphan_seats_locked(conn, backend=backend, grace=grace, now=now)

                live_count = self._count_live_seats_locked(conn, backend=backend, grace=grace)
                if live_count >= global_limit:
                    return None

                running_by_bucket = self._live_seats_by_bucket_locked(
                    conn,
                    backend=backend,
                    grace=grace,
                    bucket_for_type=bucket_for_type,
                )

                candidate_window = max(global_limit * 4, 64)
                # NOTE: psycopg treats % as placeholder; LIKE wildcards must be %%.
                candidates = conn.execute(
                    """
                    SELECT id, task_type
                    FROM tasks
                    WHERE status='queued'
                      AND COALESCE(params_json, '') NOT LIKE '%%go_managed%%'
                      AND COALESCE(result_json, '') NOT LIKE '%%go_managed%%'
                      AND COALESCE(params_json, '') NOT LIKE '%%go_batch_id%%'
                      AND COALESCE(result_json, '') NOT LIKE '%%go_batch_id%%'
                    ORDER BY created_at ASC, id ASC
                    LIMIT ?
                    """,
                    (int(candidate_window),),
                ).fetchall()
                for queued in candidates:
                    candidate_id = str(queued["id"])
                    candidate_bucket = str(bucket_for_type(str(queued["task_type"] or "")))
                    candidate_limit = parsed_bucket_limits.get(candidate_bucket, global_limit)
                    if candidate_limit < 1 or running_by_bucket.get(candidate_bucket, 0) >= candidate_limit:
                        continue

                    if backend == "postgres":
                        candidate = conn.execute(
                            "SELECT * FROM tasks WHERE id=? AND status='queued' FOR UPDATE SKIP LOCKED",
                            (candidate_id,),
                        ).fetchone()
                    else:
                        candidate = conn.execute(
                            "SELECT * FROM tasks WHERE id=? AND status='queued'",
                            (candidate_id,),
                        ).fetchone()
                    if not candidate:
                        continue

                    candidate_bucket = str(bucket_for_type(str(candidate["task_type"] or "")))
                    candidate_limit = parsed_bucket_limits.get(candidate_bucket, global_limit)
                    if candidate_limit < 1 or running_by_bucket.get(candidate_bucket, 0) >= candidate_limit:
                        continue

                    updated = conn.execute(
                        """
                        UPDATE tasks
                        SET status='starting', started_at=?, updated_at=?
                        WHERE id=? AND status='queued'
                        """,
                        (now, now, candidate_id),
                    )
                    if updated.rowcount != 1:
                        continue

                    claimed = conn.execute("SELECT * FROM tasks WHERE id=?", (candidate_id,)).fetchone()
                    return TaskSummary.from_dict(db._task_from_row(claimed)) if claimed else None
                return None
            except Exception:
                conn.rollback()
                raise

    def requeue_orphan_seats(self, *, orphan_grace_seconds: int = 45) -> int:
        """Requeue aged starting / nopid-running seats outside a claim (startup/daemon)."""
        try:
            grace = max(15, min(int(orphan_grace_seconds or 45), 300))
        except (TypeError, ValueError):
            grace = 45
        from infrastructure.db_backend import resolve_backend

        db.init_db(self.db_path)
        backend = resolve_backend()
        with db.connect(self.db_path) as conn:
            conn.execute("BEGIN" if backend == "postgres" else "BEGIN IMMEDIATE")
            try:
                if backend == "postgres":
                    conn.execute("SELECT pg_advisory_xact_lock(?, ?)", (73_241, 8_292))
                cur = self._requeue_orphan_seats_locked(
                    conn,
                    backend=backend,
                    grace=grace,
                    now=db.now_iso(),
                )
                return int(getattr(cur, "rowcount", 0) or 0)
            except Exception:
                conn.rollback()
                raise

    def count_live_seats(self, *, orphan_grace_seconds: int = 45) -> int:
        try:
            grace = max(15, min(int(orphan_grace_seconds or 45), 300))
        except (TypeError, ValueError):
            grace = 45
        from infrastructure.db_backend import resolve_backend

        db.init_db(self.db_path)
        backend = resolve_backend()
        with db.connect(self.db_path) as conn:
            return self._count_live_seats_locked(conn, backend=backend, grace=grace)

    def _requeue_orphan_seats_locked(self, conn: Any, *, backend: str, grace: int, now: str) -> Any:
        # Go daemon tasks have no local pid; never requeue them as Python orphans.
        # psycopg: literal % in LIKE must be written as %%.
        go_guard = """
                      AND COALESCE(result_json, '') NOT LIKE '%%go_managed%%'
                      AND COALESCE(params_json, '') NOT LIKE '%%go_managed%%'
                      AND COALESCE(result_json, '') NOT LIKE '%%go_batch_id%%'
                      AND COALESCE(params_json, '') NOT LIKE '%%go_batch_id%%'
        """
        if backend == "postgres":
            return conn.execute(
                f"""
                UPDATE tasks
                SET status='queued',
                    started_at='',
                    error='',
                    updated_at=?
                WHERE (
                    status='starting'
                    OR (
                      status='running'
                      AND COALESCE(NULLIF(result_json::jsonb ->> 'pid', ''), '0') IN ('', '0')
                      AND COALESCE(result_json::jsonb ->> 'inline', '') NOT IN ('1', 'true', 'True')
                      AND COALESCE(result_json::jsonb ->> 'spawn_mode', '') <> 'inline'
                    )
                )
                  AND started_at IS NOT NULL
                  AND started_at <> ''
                  AND started_at::timestamptz < (NOW() - (? || ' seconds')::interval)
                  {go_guard}
                """,
                (now, str(grace)),
            )
        return conn.execute(
            f"""
            UPDATE tasks
            SET status='queued',
                started_at='',
                error='',
                updated_at=?
            WHERE (
                status='starting'
                OR (
                  status='running'
                  AND COALESCE(NULLIF(json_extract(result_json, '$.pid'), ''), 0) IN ('', 0, '0')
                  AND COALESCE(json_extract(result_json, '$.inline'), 0) NOT IN (1, '1', 'true')
                  AND COALESCE(json_extract(result_json, '$.spawn_mode'), '') <> 'inline'
                )
            )
              AND started_at IS NOT NULL
              AND started_at <> ''
              AND (julianday('now') - julianday(replace(substr(started_at, 1, 19), 'T', ' '))) * 86400.0 > ?
              {go_guard}
            """,
            (now, float(grace)),
        )
    def _count_live_seats_locked(self, conn: Any, *, backend: str, grace: int) -> int:
        if backend == "postgres":
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM tasks
                WHERE
                  (
                    status='running'
                    AND (
                      COALESCE(NULLIF(result_json::jsonb ->> 'pid', ''), '0') NOT IN ('', '0')
                      OR COALESCE(result_json::jsonb ->> 'inline', '') IN ('1', 'true', 'True')
                      OR COALESCE(result_json::jsonb ->> 'spawn_mode', '') = 'inline'
                    )
                  )
                  OR (
                    status='starting'
                    AND started_at IS NOT NULL AND started_at <> ''
                    AND started_at::timestamptz >= (NOW() - (? || ' seconds')::interval)
                  )
                """,
                (str(grace),),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM tasks
                WHERE
                  (
                    status='running'
                    AND (
                      COALESCE(NULLIF(json_extract(result_json, '$.pid'), ''), 0) NOT IN ('', 0, '0')
                      OR COALESCE(json_extract(result_json, '$.inline'), 0) IN (1, '1', 'true')
                      OR COALESCE(json_extract(result_json, '$.spawn_mode'), '') = 'inline'
                    )
                  )
                  OR (
                    status='starting'
                    AND started_at IS NOT NULL AND started_at <> ''
                    AND (julianday('now') - julianday(replace(substr(started_at, 1, 19), 'T', ' '))) * 86400.0 <= ?
                  )
                """,
                (float(grace),),
            ).fetchone()
        return int(row["count"] or 0)
    def _live_seats_by_bucket_locked(
        self,
        conn: Any,
        *,
        backend: str,
        grace: int,
        bucket_for_type: Callable[[str], str],
    ) -> dict[str, int]:
        if backend == "postgres":
            type_rows = conn.execute(
                """
                SELECT task_type
                FROM tasks
                WHERE
                  (
                    status='running'
                    AND (
                      COALESCE(NULLIF(result_json::jsonb ->> 'pid', ''), '0') NOT IN ('', '0')
                      OR COALESCE(result_json::jsonb ->> 'inline', '') IN ('1', 'true', 'True')
                      OR COALESCE(result_json::jsonb ->> 'spawn_mode', '') = 'inline'
                    )
                  )
                  OR (
                    status='starting'
                    AND started_at IS NOT NULL AND started_at <> ''
                    AND started_at::timestamptz >= (NOW() - (? || ' seconds')::interval)
                  )
                """,
                (str(grace),),
            ).fetchall()
        else:
            type_rows = conn.execute(
                """
                SELECT task_type
                FROM tasks
                WHERE
                  (
                    status='running'
                    AND (
                      COALESCE(NULLIF(json_extract(result_json, '$.pid'), ''), 0) NOT IN ('', 0, '0')
                      OR COALESCE(json_extract(result_json, '$.inline'), 0) IN (1, '1', 'true')
                      OR COALESCE(json_extract(result_json, '$.spawn_mode'), '') = 'inline'
                    )
                  )
                  OR (
                    status='starting'
                    AND started_at IS NOT NULL AND started_at <> ''
                    AND (julianday('now') - julianday(replace(substr(started_at, 1, 19), 'T', ' '))) * 86400.0 <= ?
                  )
                """,
                (float(grace),),
            ).fetchall()
        running_by_bucket: dict[str, int] = {}
        for row in type_rows:
            bucket = str(bucket_for_type(str(row["task_type"] or "")))
            running_by_bucket[bucket] = running_by_bucket.get(bucket, 0) + 1
        return running_by_bucket

    def add_event(self, task_id: str, level: str, event_type: str, message: str, data: Any = None) -> None:
        db.add_task_event(task_id, level, event_type, message, data, path=self.db_path)

    def events(self, task_id: str, since_id: int = 0) -> list[TaskEvent]:
        return [
            TaskEvent(
                id=int(item.get("id") or 0),
                task_id=str(item.get("task_id") or ""),
                timestamp=str(item.get("timestamp") or ""),
                level=str(item.get("level") or ""),
                event_type=str(item.get("event_type") or ""),
                message=str(item.get("message") or ""),
                data=item.get("data") if isinstance(item.get("data"), dict) else {},
            )
            for item in db.list_task_events(task_id, since_id=since_id, path=self.db_path)
        ]
