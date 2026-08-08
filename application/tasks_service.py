from __future__ import annotations

import concurrent.futures
import os
import json
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from infrastructure.repositories.tasks_repository import TasksRepository
from infrastructure.repositories.resource_pool_repository import ResourcePoolRepository
from application.config_service import ConfigService
from application.resource_pool_service import ResourcePoolService
from platforms.chatgpt.pipeline_adapter import ChatGptPipelineAdapter
from services.task_runtime import ManagedTask, is_pid_running, terminate_process_tree
from core import account_store
from infrastructure import db
from services.mailat_protocol_bind_runner import normalize_oauth_callback_mode

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TASK_ROOT = PROJECT_ROOT / "data" / "tasks"


def now_id() -> str:
    return f"task_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _task_oauth_callback_mode(task: dict[str, Any]) -> str:
    params = task.get("params") if isinstance(task.get("params"), dict) else {}
    overrides = params.get("overrides") if isinstance(params.get("overrides"), dict) else {}
    return normalize_oauth_callback_mode(overrides.get("oauth_callback_mode"))


class TasksService:
    def __init__(self, repo: TasksRepository | None = None, adapter: ChatGptPipelineAdapter | None = None, config_service: ConfigService | None = None, resource_pool: ResourcePoolService | None = None):
        self.repo = repo or TasksRepository()
        self.adapter = adapter or ChatGptPipelineAdapter()
        self.config_service = config_service or ConfigService()
        self.resource_pool = resource_pool or ResourcePoolService(ResourcePoolRepository(getattr(self.repo, "db_path", None)))
        self.running: dict[str, ManagedTask] = {}
        self._lock = threading.Lock()
        self._drain_lock = threading.Lock()
        # Lifecycle: queued -> starting (claim) -> running (pid or inline).
        # Claim burst / start workers scale with register bucket so 200 concurrency
        # can fill in a few drain cycles instead of crawling at 16.
        self._claim_burst = 64
        self._start_workers = 64
        # starting seats block capacity; reclaim quickly if prep stalls
        self._orphan_grace_seconds = 20
        self._last_orphan_reconcile_at = 0.0
        self._reconcile_thread: threading.Thread | None = None
        self._reconcile_stop = threading.Event()

        # SQLite: keep a global lock (BEGIN IMMEDIATE single-writer).
        # Postgres: leases use FOR UPDATE SKIP LOCKED → fan-out with a semaphore.
        self._resource_lease_lock = threading.Lock()
        self._resource_lease_sem = threading.Semaphore(64)
        self._lease_backend = ""
        self.max_parallel = 20
        self.bucket_limits: dict[str, int] = {}
        self.reload_limits()
        try:
            self.reconcile_orphan_running_tasks(grace_seconds=self._orphan_grace_seconds)
        except Exception:
            pass



    def reload_limits(self) -> None:
        cfg = self.config_service.merged_config()
        self.set_max_parallel(cfg.get("max_parallel_tasks", self.max_parallel))
        # No artificial 200 ceiling — follow config / register page threads.
        register_limit = max(1, int(cfg.get("max_register_tasks", self.max_parallel) or self.max_parallel))
        oauth_limit = max(1, int(cfg.get("max_oauth_tasks", 1) or 1))
        maintenance_default = min(self.max_parallel, 8)
        maintenance_limit = max(1, int(cfg.get("max_maintenance_tasks", maintenance_default) or maintenance_default))
        self.bucket_limits = {
            "register": register_limit,
            "oauth": oauth_limit,
            "maintenance": maintenance_limit,
        }
        needed = max(self.max_parallel, register_limit, oauth_limit)
        if needed > self.max_parallel:
            self.max_parallel = needed
        # Claim/start/lease fan-out tracks register_limit (bounded only by OS practicality).
        self._claim_burst = max(48, int(register_limit))
        self._start_workers = max(48, int(register_limit))
        self._resource_lease_sem = threading.Semaphore(max(32, int(register_limit)))
        try:
            from services.task_runtime import ensure_inline_pool

            ensure_inline_pool(max(int(register_limit), 32))
        except Exception:
            pass

    def set_max_parallel(self, value: Any) -> None:
        try:
            parsed = int(value)
        except Exception:
            parsed = 20
        self.max_parallel = max(1, parsed)

    def _lease_backend_name(self) -> str:
        if self._lease_backend:
            return self._lease_backend
        try:
            from infrastructure.db_backend import resolve_backend

            self._lease_backend = str(resolve_backend() or "sqlite")
        except Exception:
            self._lease_backend = "sqlite"
        return self._lease_backend

    def _with_resource_lease_guard(self):
        """Serialize only on SQLite; Postgres leases skip-lock in parallel."""
        if self._lease_backend_name() == "postgres":
            return self._resource_lease_sem
        return self._resource_lease_lock


    def _bucket_for_type(self, task_type: str) -> str:
        if task_type in {"register-token", "email-register-token", "protocol-register-token", "email-protocol-register-token"}:
            return "register"
        if task_type in {"resume-oauth", "protocol-cpa-bind", "billing-email-bind"}:
            return "oauth"
        return "maintenance"

    def _active_bucket_count(self, bucket: str) -> int:
        count = 0
        for task_id in self.running:
            task = self.repo.get(task_id).to_dict()
            if self._bucket_for_type(str(task.get("type") or task.get("task_type") or "")) == bucket:
                count += 1
        return count

    def _can_start_locked(self, task_type: str) -> tuple[bool, str]:
        if len(self.running) >= self.max_parallel:
            return False, f"active={len(self.running)} max_parallel={self.max_parallel}"
        bucket = self._bucket_for_type(task_type)
        active_bucket = self._active_bucket_count(bucket)
        bucket_limit = self.bucket_limits.get(bucket, self.max_parallel)
        if active_bucket >= bucket_limit:
            return False, f"bucket={bucket} active={active_bucket} limit={bucket_limit}"
        return True, ""

    def list_tasks(self, *, status: str = "", limit: int = 50, offset: int = 0, drain_queue: bool = True, reconcile_stale: bool = False) -> list[dict[str, Any]]:
        if drain_queue:
            self._drain_queue()
        parsed_limit = max(1, min(int(limit or 50), 500))
        parsed_offset = max(0, int(offset or 0))
        status_filter = str(status or "").strip()

        # Unfiltered list used to be pure created_at DESC. After bulk-queue of 200+,
        # every page is only "queued" and the UI cards show running=0 even while
        # dozens of older tasks are actively running. Prefer active work first.
        if not status_filter and parsed_offset == 0:
            running_items = [task.to_dict() for task in self.repo.list(status="running", limit=parsed_limit, offset=0, order="desc")]
            remaining = max(0, parsed_limit - len(running_items))
            other_items: list[dict[str, Any]] = []
            if remaining:
                # Pull a wider newest window, then drop already-included running rows.
                seen = {str(item.get("id") or "") for item in running_items}
                for task in self.repo.list(status="", limit=max(parsed_limit, remaining * 2), offset=0, order="desc"):
                    item = task.to_dict()
                    tid = str(item.get("id") or "")
                    if not tid or tid in seen:
                        continue
                    other_items.append(item)
                    seen.add(tid)
                    if len(other_items) >= remaining:
                        break
            raw_items = running_items + other_items
        else:
            raw_items = [task.to_dict() for task in self.repo.list(status=status_filter, limit=parsed_limit, offset=parsed_offset, order="desc")]

        items: list[dict[str, Any]] = []
        for item in raw_items:
            task_id = str(item.get("id") or "")
            if reconcile_stale and item.get("status") == "running" and task_id not in self.running:
                result = item.get("result") if isinstance(item.get("result"), dict) else {}
                try:
                    pid = int(result.get("pid") or 0)
                except Exception:
                    pid = 0
                if pid and is_pid_running(pid):
                    item["external_running"] = True
                elif pid:
                    # Only mark interrupted when we know the child died.
                    item = self.repo.update(
                        task_id,
                        status="interrupted",
                        error=item.get("error") or "任务进程不在当前运行队列，可能是服务重启或子进程已退出。",
                        retryable=True,
                    ).to_dict()
                    self.repo.add_event(task_id, "warning", "stale_running", "运行中任务已标记为中断：子进程 pid 已不存在")
                else:
                    # No pid yet: either still starting, or an abandoned claim seat.
                    # Keep visible as external for the first grace window; durable
                    # cleanup happens in reconcile_orphan_running_tasks on drain.
                    item["external_running"] = True
            items.append(item)
        return items

    def task_status_counts(self) -> dict[str, int]:
        """Global status counts for UI cards — not limited by list window."""
        counts: dict[str, int] = {}
        # Prefer a direct SQL aggregate; fall back to scanning known statuses.
        try:
            from infrastructure import db as _db
            with _db.connect(getattr(self.repo, "db_path", None)) as conn:
                for row in conn.execute("SELECT status, COUNT(*) AS c FROM tasks GROUP BY status"):
                    counts[str(row["status"] or "")] = int(row["c"] or 0)
            return counts
        except Exception:
            pass
        for status in ("running", "starting", "queued", "pending", "succeeded", "failed", "cancelled", "interrupted"):
            counts[status] = len(self.repo.list(status=status, limit=500, offset=0, order="desc"))
        return counts

    def task_batch_summaries(self, *, limit: int = 20, since: str = "") -> list[dict[str, Any]]:
        """Aggregate tasks by batch_id (or by minute bucket for legacy rows)."""
        parsed_limit = max(1, min(int(limit or 20), 100))
        since_value = str(since or "").strip()
        try:
            from infrastructure import db as _db

            with _db.connect(getattr(self.repo, "db_path", None)) as conn:
                # Prefer explicit batch_id stamped into params_json.overrides / params_json.batch_id.
                # Legacy rows without batch_id fall back to minute buckets so the page still works.
                # psycopg style uses ? via adapter; keep dialect-neutral placeholders used by this project.
                rows = conn.execute(
                    """
                    WITH labeled AS (
                      SELECT
                        id,
                        status,
                        task_type,
                        created_at,
                        COALESCE(
                          NULLIF(params_json::jsonb ->> 'batch_id', ''),
                          NULLIF(params_json::jsonb -> 'overrides' ->> 'batch_id', ''),
                          'legacy_' || left(created_at, 16)
                        ) AS batch_id
                      FROM tasks
                      WHERE (? = '' OR created_at >= ?)
                    )
                    SELECT
                      batch_id,
                      MIN(created_at) AS started_at,
                      MAX(created_at) AS latest_at,
                      COUNT(*) AS total,
                      COUNT(*) FILTER (WHERE status = 'succeeded') AS succeeded,
                      COUNT(*) FILTER (WHERE status = 'failed') AS failed,
                      COUNT(*) FILTER (WHERE status = 'interrupted') AS interrupted,
                      COUNT(*) FILTER (WHERE status = 'cancelled') AS cancelled,
                      COUNT(*) FILTER (WHERE status IN ('running', 'starting')) AS running,
                      COUNT(*) FILTER (WHERE status IN ('queued', 'pending')) AS queued,
                      MIN(task_type) AS task_type
                    FROM labeled
                    GROUP BY batch_id
                    ORDER BY MAX(created_at) DESC
                    LIMIT ?
                    """,
                    (since_value, since_value, parsed_limit),
                ).fetchall()
                batches: list[dict[str, Any]] = []
                for row in rows:
                    item = dict(row)
                    total = int(item.get("total") or 0)
                    succeeded = int(item.get("succeeded") or 0)
                    failed = int(item.get("failed") or 0)
                    interrupted = int(item.get("interrupted") or 0)
                    cancelled = int(item.get("cancelled") or 0)
                    running = int(item.get("running") or 0)
                    queued = int(item.get("queued") or 0)
                    active = running + queued
                    finished = succeeded + failed + interrupted + cancelled
                    success_rate_pct = round((succeeded / total) * 100, 1) if total else 0.0
                    batches.append(
                        {
                            "batch_id": str(item.get("batch_id") or ""),
                            "task_type": str(item.get("task_type") or ""),
                            "started_at": str(item.get("started_at") or ""),
                            "latest_at": str(item.get("latest_at") or ""),
                            "total": total,
                            "succeeded": succeeded,
                            "failed": failed + interrupted,
                            "failed_raw": failed,
                            "interrupted": interrupted,
                            "cancelled": cancelled,
                            "running": running,
                            "queued": queued,
                            "active": active,
                            "finished": finished,
                            "progress_pct": round((finished / total) * 100, 1) if total else 0.0,
                            "completion_rate_pct": round((finished / total) * 100, 1) if total else 0.0,
                            "success_rate_pct": success_rate_pct,
                        }
                    )
                return batches
        except Exception as exc:
            # Fallback: cheap global counts only, presented as one pseudo-batch.
            counts = self.task_status_counts()
            total = sum(int(v or 0) for v in counts.values())
            return [
                {
                    "batch_id": "all",
                    "task_type": "",
                    "started_at": "",
                    "latest_at": "",
                    "total": total,
                    "succeeded": int(counts.get("succeeded") or 0),
                    "failed": int(counts.get("failed") or 0) + int(counts.get("interrupted") or 0),
                    "failed_raw": int(counts.get("failed") or 0),
                    "interrupted": int(counts.get("interrupted") or 0),
                    "cancelled": int(counts.get("cancelled") or 0),
                    "running": int(counts.get("running") or 0),
                    "queued": int(counts.get("queued") or 0) + int(counts.get("pending") or 0),
                    "active": int(counts.get("running") or 0) + int(counts.get("queued") or 0) + int(counts.get("pending") or 0),
                    "finished": int(counts.get("succeeded") or 0) + int(counts.get("failed") or 0) + int(counts.get("interrupted") or 0) + int(counts.get("cancelled") or 0),
                    "progress_pct": 0.0,
                    "completion_rate_pct": 0.0,
                    "success_rate_pct": 0.0,
                    "error": str(exc)[:200],
                }
            ]

    def get_task(self, task_id: str) -> dict[str, Any]:
        return self.repo.get(task_id).to_dict()

    def task_events(self, task_id: str, since_id: int = 0) -> list[dict[str, Any]]:
        return [event.__dict__ for event in self.repo.events(task_id, since_id)]

    def _batch_id_sql_expr(self, table_alias: str = "t") -> str:
        """Batch id projection shared by summaries/export (PG + SQLite)."""
        alias = str(table_alias or "t").strip() or "t"
        try:
            from infrastructure.db_backend import resolve_backend

            backend = resolve_backend()
        except Exception:
            backend = "sqlite"
        if backend == "postgres":
            return f"""
                COALESCE(
                  NULLIF({alias}.params_json::jsonb ->> 'batch_id', ''),
                  NULLIF({alias}.params_json::jsonb -> 'overrides' ->> 'batch_id', ''),
                  'legacy_' || left({alias}.created_at, 16)
                )
            """
        return f"""
            COALESCE(
              NULLIF(json_extract({alias}.params_json, '$.batch_id'), ''),
              NULLIF(json_extract({alias}.params_json, '$.overrides.batch_id'), ''),
              'legacy_' || substr({alias}.created_at, 1, 16)
            )
        """

    def list_account_keys_for_batches(
        self,
        batch_ids: list[str] | tuple[str, ...] | None,
        *,
        only_succeeded: bool = True,
    ) -> dict[str, Any]:
        """Resolve account_keys created by tasks in the given register batches."""
        clean_ids: list[str] = []
        seen: set[str] = set()
        for raw in batch_ids or []:
            bid = str(raw or "").strip()
            if not bid or bid in {"all", "legacy"} or bid in seen:
                continue
            seen.add(bid)
            clean_ids.append(bid)
        if not clean_ids:
            return {"batch_ids": [], "account_keys": [], "by_batch": {}, "task_counts": {}}

        from infrastructure import db as _db

        batch_expr = self._batch_id_sql_expr()
        placeholders = ",".join("?" for _ in clean_ids)
        status_clause = "AND t.status = 'succeeded'" if only_succeeded else ""
        by_batch: dict[str, list[str]] = {bid: [] for bid in clean_ids}
        task_counts: dict[str, int] = {bid: 0 for bid in clean_ids}
        account_keys: list[str] = []
        key_seen: set[str] = set()

        with _db.connect(getattr(self.repo, "db_path", None)) as conn:
            # Task counts per batch (for response stats even when no accounts linked yet).
            rows = conn.execute(
                f"""
                SELECT {batch_expr} AS batch_id, COUNT(*) AS c
                FROM tasks t
                WHERE {batch_expr} IN ({placeholders})
                GROUP BY 1
                """,
                tuple(clean_ids),
            ).fetchall()
            for row in rows:
                bid = str(dict(row).get("batch_id") or "")
                if bid in task_counts:
                    task_counts[bid] = int(dict(row).get("c") or 0)

            rows = conn.execute(
                f"""
                SELECT
                  {batch_expr} AS batch_id,
                  a.account_key AS account_key
                FROM tasks t
                JOIN accounts a ON a.registration_task_id = t.id
                WHERE {batch_expr} IN ({placeholders})
                  {status_clause}
                  AND COALESCE(a.account_key, '') <> ''
                ORDER BY a.created_at ASC, a.id ASC
                """,
                tuple(clean_ids),
            ).fetchall()
            for row in rows:
                item = dict(row)
                bid = str(item.get("batch_id") or "")
                key = str(item.get("account_key") or "").strip()
                if not key:
                    continue
                if bid in by_batch and key not in by_batch[bid]:
                    by_batch[bid].append(key)
                if key not in key_seen:
                    key_seen.add(key)
                    account_keys.append(key)

        return {
            "batch_ids": clean_ids,
            "account_keys": account_keys,
            "by_batch": by_batch,
            "task_counts": task_counts,
        }

    def export_batch_accounts(
        self,
        batch_ids: list[str] | tuple[str, ...] | None,
        fields: list[str] | None = None,
        *,
        only_succeeded: bool = True,
        archive_after_export: bool = False,
    ) -> dict[str, Any]:
        """Export accounts belonging to register task batches; mark export_status.

        Mirrors Accounts bulk export:
        - downloads the same product JSON shape
        - sets accounts.export_status / export_kind / exported_at
        - optional archive_after_export via archive_many
        """
        from application.accounts_service import AccountsService

        resolved = self.list_account_keys_for_batches(list(batch_ids or []), only_succeeded=only_succeeded)
        clean_ids = list(resolved.get("batch_ids") or [])
        account_keys = list(resolved.get("account_keys") or [])
        by_batch = resolved.get("by_batch") if isinstance(resolved.get("by_batch"), dict) else {}
        task_counts = resolved.get("task_counts") if isinstance(resolved.get("task_counts"), dict) else {}

        empty = {
            "ok": True,
            "count": 0,
            "products": [],
            "exported_keys": [],
            "missing": [],
            "batch_ids": clean_ids,
            "by_batch": {bid: {"account_count": len(by_batch.get(bid) or []), "task_count": int(task_counts.get(bid) or 0)} for bid in clean_ids},
            "archived": 0,
            "archived_keys": [],
            "archive_missing": [],
            "message": "所选批次没有可导出的账号（需 registration_task_id 关联且任务成功）" if clean_ids else "请至少选择一个批次",
        }
        if not clean_ids:
            empty["ok"] = False
            return empty
        if not account_keys:
            return empty

        accounts_svc = AccountsService(repo=None)
        # Reuse same DB path when TasksRepository is pointed at a test db.
        try:
            from infrastructure.repositories.accounts_repository import AccountsRepository

            accounts_svc = AccountsService(repo=AccountsRepository(getattr(self.repo, "db_path", None)))
        except Exception:
            accounts_svc = AccountsService()

        result = accounts_svc.export_products(account_keys, fields)
        exported_keys = list(result.get("exported_keys") or [])
        archived_keys: list[str] = []
        archive_missing: list[str] = []
        archived = 0
        if archive_after_export and exported_keys:
            archive_result = accounts_svc.archive_many(exported_keys)
            archived_keys = list(archive_result.get("keys") or archive_result.get("archived") or [])
            if not archived_keys and isinstance(archive_result.get("archived"), int):
                # older shape may only return count
                archived = int(archive_result.get("archived") or 0)
            else:
                archived = len(archived_keys)
            archive_missing = list(archive_result.get("missing") or [])

        message = f"已导出 {int(result.get('count') or 0)} 个账号（{len(clean_ids)} 个批次）"
        if archive_after_export:
            message += f"；已归档 {archived}"
            if archive_missing:
                message += f"，归档失败 {len(archive_missing)}"

        return {
            "ok": True,
            "count": int(result.get("count") or 0),
            "products": list(result.get("products") or []),
            "exported_keys": exported_keys,
            "missing": list(result.get("missing") or []),
            "batch_ids": clean_ids,
            "by_batch": {
                bid: {
                    "account_count": len(by_batch.get(bid) or []),
                    "task_count": int(task_counts.get(bid) or 0),
                    "exported": sum(1 for k in (by_batch.get(bid) or []) if k in set(exported_keys)),
                }
                for bid in clean_ids
            },
            "archived": archived,
            "archived_keys": archived_keys,
            "archive_missing": archive_missing,
            "message": message,
        }

    def export_batch_at_products_txt(
        self,
        batch_ids: list[str] | tuple[str, ...] | None,
        *,
        only_succeeded: bool = True,
        archive_after_export: bool = False,
        chunk_size: int = 0,
        write_dir: str | Path | None = None,
        stamp: str | None = None,
        only_unexported: bool = True,
    ) -> dict[str, Any]:
        """Export AT lines for register batches; realtime-friendly incremental append.

        Line format (same as Accounts AT export):
          email----password----client_id----refresh_token----access_token

        Files under:
          {write_dir}/{stamp}/at-products-batch-{n}-{stamp}.txt
          (or ...-p{part}-... when split by chunk_size)

        When only_unexported=True (default), accounts already marked export_kind=at
        are skipped so callers can poll during a live batch and only flush new ATs.
        """
        from application.accounts_service import AccountsService
        from infrastructure.repositories.accounts_repository import AccountsRepository

        resolved = self.list_account_keys_for_batches(list(batch_ids or []), only_succeeded=only_succeeded)
        clean_ids = list(resolved.get("batch_ids") or [])
        account_keys = list(resolved.get("account_keys") or [])
        by_batch = resolved.get("by_batch") if isinstance(resolved.get("by_batch"), dict) else {}
        task_counts = resolved.get("task_counts") if isinstance(resolved.get("task_counts"), dict) else {}

        empty = {
            "ok": True,
            "count": 0,
            "new_count": 0,
            "total_ready": 0,
            "text": "",
            "skipped_count": 0,
            "kind_counts": {},
            "items": [],
            "skipped": [],
            "exported_keys": [],
            "batch_ids": clean_ids,
            "by_batch": {
                bid: {
                    "account_count": len(by_batch.get(bid) or []),
                    "task_count": int(task_counts.get(bid) or 0),
                    "exported": 0,
                }
                for bid in clean_ids
            },
            "files": [],
            "dir": "",
            "stamp": "",
            "chunk_size": int(chunk_size or 0),
            "archived": 0,
            "archived_keys": [],
            "archive_missing": [],
            "message": "所选批次没有可导出的账号" if clean_ids else "请至少选择一个批次",
        }
        if not clean_ids:
            empty["ok"] = False
            return empty
        if not account_keys:
            return empty

        try:
            accounts_svc = AccountsService(repo=AccountsRepository(getattr(self.repo, "db_path", None)))
        except Exception:
            accounts_svc = AccountsService()

        # Filter already-exported ATs so realtime polling only flushes new successes.
        pending_keys = list(account_keys)
        already_exported = 0
        if only_unexported and pending_keys:
            try:
                from infrastructure import db as _db

                keep: list[str] = []
                with _db.connect(getattr(self.repo, "db_path", None)) as conn:
                    chunk = 400
                    for offset in range(0, len(pending_keys), chunk):
                        part = pending_keys[offset : offset + chunk]
                        placeholders = ",".join("?" for _ in part)
                        rows = conn.execute(
                            f"""
                            SELECT account_key, COALESCE(export_status,'') AS export_status,
                                   COALESCE(export_kind,'') AS export_kind
                            FROM accounts
                            WHERE account_key IN ({placeholders})
                            """,
                            tuple(part),
                        ).fetchall()
                        status_map = {
                            str(dict(r).get("account_key") or ""): (
                                str(dict(r).get("export_status") or ""),
                                str(dict(r).get("export_kind") or ""),
                            )
                            for r in rows
                        }
                        for key in part:
                            st, kind = status_map.get(key, ("", ""))
                            if st == "at_exported" or kind == "at":
                                already_exported += 1
                                continue
                            keep.append(key)
                pending_keys = keep
            except Exception:
                # Fail open: export all resolved keys if status filter breaks.
                pending_keys = list(account_keys)
                already_exported = 0

        total_ready = len(account_keys)
        if not pending_keys:
            stamp_value = str(stamp or "").strip()
            if not stamp_value:
                stamp_value = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
            stamp_value = re.sub(r"[^\w.\-]+", "-", stamp_value).strip("-") or datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
            out_dir = ""
            if write_dir is not None and str(write_dir).strip():
                out_dir = str(Path(str(write_dir).strip()) / stamp_value)
            empty.update(
                {
                    "message": f"暂无新增 AT（批次已就绪 {total_ready}，已导出 {already_exported}）",
                    "total_ready": total_ready,
                    "new_count": 0,
                    "dir": out_dir,
                    "stamp": stamp_value,
                    "by_batch": {
                        bid: {
                            "account_count": len(by_batch.get(bid) or []),
                            "task_count": int(task_counts.get(bid) or 0),
                            "exported": already_exported if bid in clean_ids else 0,
                        }
                        for bid in clean_ids
                    },
                }
            )
            return empty

        # Build AT lines without marking yet; mark only after successful disk write.
        result = accounts_svc.export_at_products_txt(
            pending_keys,
            archive_after_export=False,
        )
        # export_at_products_txt already marks exported; that's OK for incremental
        # (next poll skips them). If write fails we still marked — acceptable vs re-export dupes.

        text = str(result.get("text") or "")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        count = len(lines)
        exported_keys = list(result.get("exported_keys") or [])
        exported_set = set(exported_keys)

        stamp_value = str(stamp or "").strip()
        if not stamp_value:
            stamp_value = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
        stamp_value = re.sub(r"[^\w.\-]+", "-", stamp_value).strip("-") or datetime.now().strftime("%Y-%m-%d-%H-%M-%S")

        files: list[dict[str, Any]] = []
        out_dir = ""
        if write_dir is not None and str(write_dir).strip() and count > 0:
            root = Path(str(write_dir).strip())
            target = root / stamp_value
            target.mkdir(parents=True, exist_ok=True)
            out_dir = str(target)
            size = int(chunk_size or 0)
            if size < 1:
                size = 10**9  # effectively one growing file
            size = max(1, min(size, 100000))

            # Rolling state so realtime polls append into the same part file.
            state_path = target / ".at_export_state.json"
            state: dict[str, Any] = {"part": 1, "in_part": 0, "files": []}
            if state_path.is_file():
                try:
                    loaded = json.loads(state_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        state.update(loaded)
                except Exception:
                    pass
            part = max(1, int(state.get("part") or 1))
            in_part = max(0, int(state.get("in_part") or 0))
            known_files = list(state.get("files") or []) if isinstance(state.get("files"), list) else []

            remaining = list(lines)
            while remaining:
                room = size - in_part if size < 10**8 else len(remaining)
                if room <= 0:
                    part += 1
                    in_part = 0
                    room = size if size < 10**8 else len(remaining)
                take = remaining[:room]
                remaining = remaining[room:]
                n_in_file_after = in_part + len(take)
                # Name uses final count in this part so far (updates as file grows).
                if size >= 10**8:
                    name = f"at-products-batch-{n_in_file_after}-{stamp_value}.txt"
                    # single-file mode: stable name without changing count mid-run
                    name = f"at-products-batch-{stamp_value}.txt"
                else:
                    name = f"at-products-batch-{size}-p{part}-{stamp_value}.txt"
                path = target / name
                with path.open("a", encoding="utf-8", newline="\n") as fh:
                    fh.write("\n".join(take) + "\n")
                in_part = n_in_file_after
                # track file meta
                meta = {"path": str(path), "name": path.name, "count": in_part, "part": part}
                # replace existing part entry
                known_files = [f for f in known_files if not (isinstance(f, dict) and int(f.get("part") or 0) == part)]
                known_files.append(meta)
                files.append(meta)
                if size < 10**8 and in_part >= size and remaining:
                    part += 1
                    in_part = 0

            state = {"part": part, "in_part": in_part, "files": known_files}
            state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            files = known_files

        if archive_after_export and exported_keys:
            try:
                arch = accounts_svc.archive_many(exported_keys)
                result["archived"] = len(list(arch.get("keys") or []))
                result["archived_keys"] = list(arch.get("keys") or [])
                result["archive_missing"] = list(arch.get("missing") or [])
            except Exception:
                pass

        message = f"本次新增导出 {count} 条 AT（批次就绪 {total_ready}，历史已导出约 {already_exported}）"
        if files:
            message += f" → {out_dir}"
        if result.get("archived"):
            message += f"；已归档 {int(result.get('archived') or 0)}"

        return {
            "ok": True,
            "count": count,
            "new_count": count,
            "total_ready": total_ready,
            "text": "",
            "skipped_count": int(result.get("skipped_count") or 0) + already_exported,
            "kind_counts": dict(result.get("kind_counts") or {}),
            "items": list(result.get("items") or []),
            "skipped": list(result.get("skipped") or []),
            "exported_keys": exported_keys,
            "batch_ids": clean_ids,
            "by_batch": {
                bid: {
                    "account_count": len(by_batch.get(bid) or []),
                    "task_count": int(task_counts.get(bid) or 0),
                    "exported": sum(1 for k in (by_batch.get(bid) or []) if k in exported_set),
                }
                for bid in clean_ids
            },
            "files": files,
            "dir": out_dir,
            "stamp": stamp_value,
            "chunk_size": int(chunk_size or 0),
            "archived": int(result.get("archived") or 0),
            "archived_keys": list(result.get("archived_keys") or []),
            "archive_missing": list(result.get("archive_missing") or []),
            "message": message,
        }


    def _write_task_config(self, task_id: str, base_config: str, overrides: dict[str, Any], *, skip_phone_leases: bool = False, prepare_resources: bool = False) -> str:
        config_service = ConfigService(base_config=base_config, repo=self.config_service.repo)
        config = config_service.merged_config()
        config.update(overrides)
        if skip_phone_leases:
            config["_skip_phone_leases"] = True
        if prepare_resources:
            pool_overrides, leases = self.resource_pool.lease_for_task(task_id, config)
            config.update(pool_overrides)
            if leases:
                config["resource_leases"] = [{"type": item.resource_type, "provider": item.provider, "key": item.resource_key} for item in leases]
            config["_resources_prepared"] = True
        else:
            config["_resources_prepared"] = False
        if skip_phone_leases:
            for key in ("sms_phone_url", "sms_phone_urls", "sms_phone_url_file", "bind_sms_phone_url", "bind_sms_phone_urls", "bind_sms_phone_url_file"):
                config[key] = ""
        if not skip_phone_leases:
            config.pop("_skip_phone_leases", None)
        config["dashboard_task_id"] = task_id
        config["_task_config_path"] = str(TASK_ROOT / f"{task_id}_config.yaml")
        TASK_ROOT.mkdir(parents=True, exist_ok=True)
        path = TASK_ROOT / f"{task_id}_config.yaml"
        path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return str(path)

    def _prepare_task_resources(self, task_id: str) -> bool:
        task = self.repo.get(task_id).to_dict()
        params = task.get("params") if isinstance(task.get("params"), dict) else {}
        config_path = str(params.get("config_path") or "")
        if not config_path:
            return True
        path = Path(config_path)
        config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if config.get("_resources_prepared"):
            return True
        pool_overrides, leases = self.resource_pool.lease_for_task(task_id, config)
        config.update(pool_overrides)
        if leases:
            config["resource_leases"] = [{"type": item.resource_type, "provider": item.provider, "key": item.resource_key} for item in leases]
        config["_resources_prepared"] = True
        path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
        self.repo.add_event(task_id, "info", "resource_leased", "任务启动前资源租约已准备", {"leases": config.get("resource_leases") or []})
        return True
    def drain_queue_async(self) -> None:
        threading.Thread(target=self._drain_queue, daemon=True).start()

    def _queue_for_async_start(self, task_id: str, reason: str = "后台启动") -> None:
        self.repo.update(task_id, status="queued")
        self.repo.add_event(task_id, "info", "queued", f"任务已排队: {reason}")


    def start_register(self, data: dict[str, Any], overrides: dict[str, Any], *, defer_start: bool = False) -> dict[str, Any]:
        task_id = now_id()
        config_path = self._write_task_config(task_id, str(data.get("config") or "config.yaml"), overrides)
        headed = bool(data.get("headed", True))
        command = self.adapter.register_token_command(config_path, headed=headed)
        task = self.repo.create(
            {
                "id": task_id,
                "type": "register-token",
                "status": "pending",
                "created_at": now_iso(),
                "updated_at": now_iso(),
                "params": {"config_path": config_path, "overrides": overrides, "headed": headed},
                "command": command,
                "log_file": str(TASK_ROOT / f"{task_id}.log"),
            }
        ).to_dict()
        db.add_account_event(task_id, "registration_started", task_id=task_id, status="pending", message="手机号注册任务已创建")
        if defer_start:
            self._queue_for_async_start(task_id)
        else:
            self._schedule_or_queue(task_id, command, task["log_file"])
        return self.get_task(task_id)

    def start_protocol_register(self, data: dict[str, Any], overrides: dict[str, Any], *, defer_start: bool = False) -> dict[str, Any]:
        task_id = now_id()
        config_path = self._write_task_config(task_id, str(data.get("config") or "config.yaml"), overrides)
        headed = bool(data.get("headed", True))
        command = self.adapter.protocol_register_token_command(config_path, headed=headed, task_id=task_id)
        task = self.repo.create(
            {
                "id": task_id,
                "type": "protocol-register-token",
                "status": "pending",
                "created_at": now_iso(),
                "updated_at": now_iso(),
                "params": {"config_path": config_path, "overrides": overrides, "headed": headed},
                "command": command,
                "log_file": str(TASK_ROOT / f"{task_id}.log"),
            }
        ).to_dict()
        db.add_account_event(task_id, "registration_started", task_id=task_id, status="pending", message="协议手机号注册任务已创建")
        if defer_start:
            self._queue_for_async_start(task_id)
        else:
            self._schedule_or_queue(task_id, command, task["log_file"])
        return self.get_task(task_id)

    def _email_protocol_backend_is_go(self, data: dict[str, Any], overrides: dict[str, Any] | None = None) -> bool:
        """True unless caller explicitly selects python/mailat/node protocol backend."""
        ov = overrides or {}
        raw = str(
            ov.get("email_protocol_backend")
            or ov.get("protocol_backend")
            or (data or {}).get("email_protocol_backend")
            or (data or {}).get("protocol_backend")
            or ""
        ).strip().lower().replace("-", "_")
        if not raw:
            try:
                cfg = ConfigService(base_config=str((data or {}).get("config") or "config.yaml"), repo=self.config_service.repo).merged_config()
                raw = str(cfg.get("email_protocol_backend") or cfg.get("protocol_backend") or "go").strip().lower().replace("-", "_")
            except Exception:
                raw = "go"
        if raw in {"python", "mailat", "node", "tsx", "codex"}:
            return False
        # go / golang / empty / pure defaults to Go batch hot path
        return True

    def _go_batch_config(self, data: dict[str, Any], overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            cfg = ConfigService(base_config=str((data or {}).get("config") or "config.yaml"), repo=self.config_service.repo).merged_config()
        except Exception:
            cfg = {}
        out = dict(cfg)
        out.update(overrides or {})
        return out

    def start_email_protocol_register(self, data: dict[str, Any], overrides: dict[str, Any], *, defer_start: bool = False) -> dict[str, Any]:
        # Software hot path: Go batch only when backend=go (default). Never silently fall
        # back to Python inline / mailat — that path diverges from canary recovery policy.
        if not defer_start:
            wants_go = self._email_protocol_backend_is_go(data, overrides)
            if wants_go:
                from services.go_registration_batch import try_start_go_batch_register, get_go_registration_batch

                ov = dict(overrides or {})
                if not str(ov.get("batch_id") or "").strip():
                    ov["batch_id"] = f"go_single_{now_id()}"
                ov["go_batch_required"] = "1"
                go_n = try_start_go_batch_register(data, ov, 1, force=True)
                if go_n is None:
                    raise RuntimeError(
                        "Go batch registration required but worker did not accept the batch "
                        "(is email-protocol-worker pure-go with email-register-batches?)"
                    )
                try:
                    view = get_go_registration_batch(str(ov["batch_id"]), self._go_batch_config(data, ov))
                    ids = view.get("task_ids") if isinstance(view, dict) else None
                    if isinstance(ids, list) and ids:
                        return self.get_task(str(ids[0]))
                except Exception:
                    pass
                return {
                    "id": "",
                    "status": "running",
                    "params": {"overrides": ov, "go_managed": True},
                    "result": {"go_managed": True, "go_batch_id": ov["batch_id"]},
                }
        task_id = now_id()
        # Bulk create (defer_start=True) must NOT lease resources yet.
        # Leasing 300 tasks serially blocks the HTTP request for minutes and pins
        # proxy/email seats to queued jobs that are not running.
        # Real start path (_start_prepared -> _prepare_task_resources) leases under
        # DB atomic BEGIN IMMEDIATE when the job actually dequeues.
        config_path = self._write_task_config(
            task_id,
            str(data.get("config") or "config.yaml"),
            overrides,
            skip_phone_leases=True,
            prepare_resources=not defer_start,
        )
        command = self.adapter.email_protocol_register_command(config_path, task_id=task_id)
        params: dict[str, Any] = {"config_path": config_path, "overrides": overrides, "headed": False}
        batch_id = str(overrides.get("batch_id") or "").strip()
        if batch_id:
            params["batch_id"] = batch_id
        task = self.repo.create(
            {
                "id": task_id,
                "type": "email-protocol-register-token",
                "status": "pending",
                "created_at": now_iso(),
                "updated_at": now_iso(),
                "params": params,
                "command": command,
                "log_file": str(TASK_ROOT / f"{task_id}.log"),
            }
        ).to_dict()
        db.add_account_event(task_id, "registration_started", task_id=task_id, status="pending", message="邮箱协议注册任务已创建")
        if defer_start:
            self._queue_for_async_start(task_id)
        else:
            self._schedule_or_queue(task_id, command, task["log_file"])
        return self.get_task(task_id)

    def start_email_protocol_register_many(self, data: dict[str, Any], overrides: dict[str, Any], count: int) -> int:
        """Create many email-protocol tasks. Go backend: Go-owned batch only (fail closed)."""
        try:
            total = max(0, int(count or 0))
        except Exception:
            total = 0
        if total <= 0:
            return 0
        wants_go = self._email_protocol_backend_is_go(data, overrides)
        if wants_go:
            from services.go_registration_batch import try_start_go_batch_register

            ov = dict(overrides or {})
            ov["go_batch_required"] = "1"
            go_n = try_start_go_batch_register(data, ov, total, force=True)
            if go_n is None:
                raise RuntimeError(
                    "Go batch registration required but worker did not accept the batch "
                    "(is email-protocol-worker pure-go with email-register-batches?)"
                )
            return int(go_n)

        # Explicit python/mailat backend only — legacy inline fan-out.
        config_service = ConfigService(base_config=str(data.get("config") or "config.yaml"), repo=self.config_service.repo)
        base_config = config_service.merged_config()
        base_config.update(overrides)
        base_config["_skip_phone_leases"] = True
        base_config["_resources_prepared"] = False
        for key in ("sms_phone_url", "sms_phone_urls", "sms_phone_url_file", "bind_sms_phone_url", "bind_sms_phone_urls", "bind_sms_phone_url_file"):
            base_config[key] = ""

        TASK_ROOT.mkdir(parents=True, exist_ok=True)
        tasks: list[dict[str, Any]] = []
        for _ in range(total):
            task_id = now_id()
            config_path = str(TASK_ROOT / f"{task_id}_config.yaml")
            config = dict(base_config)
            config["dashboard_task_id"] = task_id
            config["_task_config_path"] = config_path
            Path(config_path).write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
            command = self.adapter.email_protocol_register_command(config_path, task_id=task_id)
            params: dict[str, Any] = {"config_path": config_path, "overrides": overrides, "headed": False}
            batch_id = str(overrides.get("batch_id") or "").strip()
            if batch_id:
                params["batch_id"] = batch_id
            tasks.append(
                {
                    "id": task_id,
                    "type": "email-protocol-register-token",
                    "status": "queued",
                    "created_at": now_iso(),
                    "updated_at": now_iso(),
                    "params": params,
                    "command": command,
                    "log_file": str(TASK_ROOT / f"{task_id}.log"),
                }
            )
        return self.repo.create_many(tasks)

    def start_email_register(self, data: dict[str, Any], overrides: dict[str, Any], *, defer_start: bool = False) -> dict[str, Any]:
        task_id = now_id()
        config_path = self._write_task_config(task_id, str(data.get("config") or "config.yaml"), overrides, skip_phone_leases=True)
        headed = bool(data.get("headed", True))
        command = self.adapter.email_register_token_command(config_path, headed=headed)
        task = self.repo.create(
            {
                "id": task_id,
                "type": "email-register-token",
                "status": "pending",
                "created_at": now_iso(),
                "updated_at": now_iso(),
                "params": {"config_path": config_path, "overrides": overrides, "headed": headed},
                "command": command,
                "log_file": str(TASK_ROOT / f"{task_id}.log"),
            }
        ).to_dict()
        db.add_account_event(task_id, "registration_started", task_id=task_id, status="pending", message="邮箱注册任务已创建")
        if defer_start:
            self._queue_for_async_start(task_id)
        else:
            self._schedule_or_queue(task_id, command, task["log_file"])
        return self.get_task(task_id)

    def start_resume(self, data: dict[str, Any]) -> dict[str, Any]:
        resume_file = str(data.get("resume_file") or "").strip()
        if not resume_file:
            raise ValueError("缺少 resume_file")
        task_id = now_id()
        overrides = {key: value for key, value in {
            "oauth_callback_mode": data.get("oauth_callback_mode"),
            "cpa_base_url": data.get("cpa_base_url"),
            "cpa_management_key": data.get("cpa_management_key"),
            "sms_provider": data.get("sms_provider"),
            "sms_phone_url": data.get("sms_phone_url"),
            "sms_phone_urls": data.get("sms_phone_url"),
            "sms_country": data.get("sms_country"),
            "sms_service": data.get("sms_service"),
            "bind_sms_provider": data.get("bind_sms_provider"),
            "bind_sms_phone_url": data.get("bind_sms_phone_url"),
            "bind_sms_phone_urls": data.get("bind_sms_phone_url"),
            "bind_sms_country": data.get("bind_sms_country"),
            "bind_sms_service": data.get("bind_sms_service"),
            "bind_country_code": data.get("bind_country_code"),
        }.items() if str(value or "").strip()}
        overrides["_resume_oauth_task"] = True
        overrides["skip_plus_check_for_binding"] = True
        config_path = self._write_task_config(task_id, str(data.get("config") or "config.yaml"), overrides)
        headed = bool(data.get("headed", True))
        command = self.adapter.resume_oauth_command(config_path, resume_file, headed=headed)
        task = self.repo.create(
            {
                "id": task_id,
                "type": "resume-oauth",
                "status": "pending",
                "created_at": now_iso(),
                "updated_at": now_iso(),
                "params": {"config_path": config_path, "resume_file": resume_file, "headed": headed, "overrides": overrides},
                "command": command,
                "log_file": str(TASK_ROOT / f"{task_id}.log"),
            }
        ).to_dict()
        self._schedule_or_queue(task_id, command, task["log_file"])
        return self.get_task(task_id)

    def start_protocol_cpa_bind(self, data: dict[str, Any], *, defer_start: bool = False) -> dict[str, Any]:
        account_key = str(data.get("account_key") or "").strip()
        if not account_key:
            raise ValueError("缺少 account_key")
        task_id = now_id()
        callback_mode = normalize_oauth_callback_mode(data.get("oauth_callback_mode"))
        overrides = {key: value for key, value in {
            "oauth_callback_mode": callback_mode,
            "cpa_base_url": data.get("cpa_base_url") if callback_mode == "cpa" else "",
            "cpa_management_key": data.get("cpa_management_key") if callback_mode == "cpa" else "",
            "sms_provider": data.get("sms_provider"),
            "sms_phone_url": data.get("sms_phone_url"),
            "sms_phone_urls": data.get("sms_phone_url"),
            "sms_country": data.get("sms_country"),
            "sms_service": data.get("sms_service"),
            "bind_sms_provider": data.get("bind_sms_provider"),
            "bind_sms_phone_url": data.get("bind_sms_phone_url"),
            "bind_sms_phone_urls": data.get("bind_sms_phone_url"),
            "bind_sms_country": data.get("bind_sms_country"),
            "bind_sms_service": data.get("bind_sms_service"),
            "bind_country_code": data.get("bind_country_code"),
        }.items() if str(value or "").strip()}
        overrides["_protocol_cpa_bind_task"] = True
        overrides["skip_plus_check_for_binding"] = True
        config_path = self._write_task_config(task_id, str(data.get("config") or "config.yaml"), overrides)
        command = self.adapter.protocol_cpa_bind_command(config_path, account_key, task_id=task_id)
        task = self.repo.create(
            {
                "id": task_id,
                "type": "protocol-cpa-bind",
                "status": "pending",
                "created_at": now_iso(),
                "updated_at": now_iso(),
                "params": {"config_path": config_path, "account_key": account_key, "overrides": overrides},
                "command": command,
                "log_file": str(TASK_ROOT / f"{task_id}.log"),
            }
        ).to_dict()
        if defer_start:
            self._queue_for_async_start(task_id)
        else:
            self._schedule_or_queue(task_id, command, task["log_file"])
        return self.get_task(task_id)

    def start_billing_email_bind(self, data: dict[str, Any]) -> dict[str, Any]:
        resume_file = str(data.get("resume_file") or "").strip()
        if not resume_file:
            raise ValueError("缺少 resume_file")
        provider = str(data.get("mailbox_provider") or "icloud_api").strip() or "icloud_api"
        task_id = now_id()
        overrides = {key: value for key, value in {
            "mailbox_provider": provider,
            "proxy_region": data.get("proxy_region"),
            "lajiao_proxy_regions": data.get("proxy_region"),
            "lajiao_proxy_expected_country": data.get("proxy_region"),
            "lajiao_proxy_credential_protocol": data.get("lajiao_proxy_credential_protocol"),
        }.items() if str(value or "").strip()}
        config_path = self._write_task_config(task_id, str(data.get("config") or "config.yaml"), overrides, skip_phone_leases=True)
        headed = bool(data.get("headed", True))
        command = self.adapter.billing_email_bind_command(config_path, resume_file, headed=headed)
        task = self.repo.create(
            {
                "id": task_id,
                "type": "billing-email-bind",
                "status": "pending",
                "created_at": now_iso(),
                "updated_at": now_iso(),
                "params": {"config_path": config_path, "resume_file": resume_file, "provider": provider, "headed": headed},
                "command": command,
                "log_file": str(TASK_ROOT / f"{task_id}.log"),
            }
        ).to_dict()
        db.add_account_event(task_id, "billing_email_bind_started", task_id=task_id, status="pending", message=f"账单邮箱绑定任务已创建 provider={provider}")
        self._schedule_or_queue(task_id, command, task["log_file"])
        return self.get_task(task_id)

    def retry(self, task_id: str) -> dict[str, Any]:
        original = self.repo.get(task_id).to_dict()
        if not original.get("id"):
            return {}
        params = original.get("params") if isinstance(original.get("params"), dict) else {}
        task_type = str(original.get("type") or original.get("task_type") or "")
        if task_type == "register-token":
            data = {"config": params.get("config_path") or "config.yaml", "headed": params.get("headed", True)}
            overrides = params.get("overrides") if isinstance(params.get("overrides"), dict) else {}
            return self.start_register(data, overrides)
        if task_type == "protocol-register-token":
            data = {"config": params.get("config_path") or "config.yaml", "headed": params.get("headed", True)}
            overrides = params.get("overrides") if isinstance(params.get("overrides"), dict) else {}
            return self.start_protocol_register(data, overrides)
        if task_type == "email-register-token":
            data = {"config": params.get("config_path") or "config.yaml", "headed": params.get("headed", True)}
            overrides = params.get("overrides") if isinstance(params.get("overrides"), dict) else {}
            return self.start_email_register(data, overrides)
        if task_type == "email-protocol-register-token":
            data = {"config": params.get("config_path") or "config.yaml", "headed": False}
            overrides = params.get("overrides") if isinstance(params.get("overrides"), dict) else {}
            return self.start_email_protocol_register(data, overrides)
        if task_type == "resume-oauth":
            overrides = params.get("overrides") if isinstance(params.get("overrides"), dict) else {}
            return self.start_resume({"resume_file": params.get("resume_file"), "headed": params.get("headed", True), **overrides})
        if task_type == "protocol-cpa-bind":
            overrides = params.get("overrides") if isinstance(params.get("overrides"), dict) else {}
            account_key = str(params.get("account_key") or "").strip()
            callback_mode = normalize_oauth_callback_mode(overrides.get("oauth_callback_mode"))
            task = self.start_protocol_cpa_bind({"account_key": account_key, **overrides}, defer_start=True)
            account = db.get_account(account_key)
            if account:
                account["binding_status"] = "binding_queued"
                account["binding_task_id"] = str(task.get("id") or "")
                account["binding_provider"] = str(overrides.get("bind_sms_provider") or "")
                account["binding_started_at"] = now_iso()
                account["binding_error"] = ""
                account["oauth_callback_mode"] = callback_mode
                if callback_mode == "cpa" and overrides.get("cpa_base_url"):
                    account["cpa_base_url"] = str(overrides.get("cpa_base_url") or "")
                db.upsert_account(account)
                mode_label = "本地 OAuth" if callback_mode == "local" else "CPA"
                db.add_account_event(account["account_key"], "binding_queued", task_id=str(task.get("id") or ""), status="binding_queued", message=f"协议 {mode_label} 绑定重试任务已排队", payload={"provider": account["binding_provider"], "oauth_callback_mode": callback_mode, "retry_of": task_id})
            self.drain_queue_async()
            return self.get_task(str(task.get("id") or ""))
        return {}

    def _read_resource_report_log(self, log_file: str, *, max_bytes: int = 200_000) -> str:
        path = Path(log_file)
        if not path.exists():
            return ""
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            return handle.read(max_bytes).decode("utf-8", errors="replace")

    def _extract_binding_phone_report(self, log_text: str) -> tuple[str, bool]:
        phone = ""
        verified = False
        for line in str(log_text or "").splitlines():
            match = re.search(r"已成功租到号码\([^)]*\):\s*(\+?\d{6,20})", line)
            if not match:
                match = re.search(r"(?:使用绑定手机号 API|取号成功).*phone=\+?(\d{6,20})", line)
            if match:
                phone = match.group(1)
                if not phone.startswith("+"):
                    phone = f"+{phone}"
                continue
            if ("短信验证成功" in line or "提交短信验证" in line or "phone-otp/validate" in line) and phone:
                verified = True
        return phone, verified

    def _extract_successful_binding_phone(self, log_text: str) -> str:
        phone, verified = self._extract_binding_phone_report(log_text)
        return phone if verified else ""

    def _extract_resume_file(self, log_text: str) -> str:
        for line in reversed(str(log_text or "").splitlines()):
            match = re.search(r"交接文件:\s*(.+?\.json)\s*$", line)
            if match:
                return match.group(1).strip()
        return ""


    def _email_protocol_spawn_mode(self, task: dict[str, Any] | None = None) -> str:
        """process (default legacy) | inline (thread-pool → Go HTTP, no per-task python process)."""
        from services.task_runtime import normalize_spawn_mode

        params = (task or {}).get("params") if isinstance((task or {}).get("params"), dict) else {}
        overrides = params.get("overrides") if isinstance(params.get("overrides"), dict) else {}
        cfg = {}
        try:
            cfg = self.config_service.merged_config()
        except Exception:
            cfg = {}
        raw = (
            overrides.get("email_protocol_spawn_mode")
            or overrides.get("spawn_mode")
            or params.get("spawn_mode")
            or cfg.get("email_protocol_spawn_mode")
            or cfg.get("email_protocol_spawn")
            or os.environ.get("EMAIL_PROTOCOL_SPAWN_MODE")
            or "inline"
        )
        return normalize_spawn_mode(raw)

    def _make_managed(self, task_id: str, command: list[str], log_file: str) -> ManagedTask:
        def finish(_: int) -> None:
            task = self.repo.get(task_id).to_dict()
            params = task.get("params") if isinstance(task.get("params"), dict) else {}
            config_path = str(params.get("config_path") or "")
            if config_path:
                try:
                    log_text = self._read_resource_report_log(log_file)
                    config = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
                    reports = self.resource_pool.report_for_task(task_id, str(task.get("status") or ""), config, error=str(task.get("error") or ""), log_text=log_text)
                    self.repo.add_event(task_id, "info", "resource_reported", "资源租约已回写", {"reports": reports})
                except Exception as exc:
                    self.repo.add_event(task_id, "warning", "resource_report_failed", f"资源租约回写失败: {exc}")

            task_type = str(task.get("type") or task.get("task_type") or "")
            if task_type in {"resume-oauth", "protocol-cpa-bind"}:
                task_status = str(task.get("status") or "")
                callback_mode = _task_oauth_callback_mode(task)
                mode_label = "本地 OAuth" if callback_mode == "local" else "CPA"
                for account in account_store.list_accounts(refresh_legacy=False):
                    if str(account.get("binding_task_id") or "") != task_id:
                        continue
                    log_text = self._read_resource_report_log(log_file)
                    binding_phone, binding_phone_verified = self._extract_binding_phone_report(log_text)
                    account["oauth_callback_mode"] = callback_mode
                    if binding_phone:
                        account["binding_phone_number"] = binding_phone
                    if binding_phone_verified and binding_phone:
                        account["phone_number"] = account.get("phone_number") or binding_phone
                        account["binding_phone_verified"] = True
                    if task_status == "succeeded":
                        account["binding_completed_at"] = now_iso()
                        account["binding_error"] = ""
                        if callback_mode == "local":
                            account["binding_status"] = "bound"
                            account["cpa_submitted_at"] = ""
                            account["cpa_submit_status"] = ""
                            account["cpa_submit_error"] = ""
                            db.add_account_event(account["account_key"], "protocol_local_bound", task_id=task_id, status="bound", message="协议本地 OAuth 绑定任务成功", payload={"oauth_callback_mode": callback_mode})
                        else:
                            account["binding_status"] = "cpa_submitted"
                            account["cpa_submitted_at"] = now_iso()
                            account["cpa_submit_status"] = "submitted"
                            db.add_account_event(account["account_key"], "cpa_callback_submitted", task_id=task_id, status="cpa_submitted", message="协议/浏览器 CPA 绑定任务成功", payload={"oauth_callback_mode": callback_mode})
                    else:
                        account["binding_status"] = "failed"
                        account["binding_error"] = str(task.get("error") or "绑定任务失败")
                        db.add_account_event(account["account_key"], "binding_failed", task_id=task_id, status="failed", message=f"{mode_label} 绑定任务失败: {account['binding_error']}", payload={"oauth_callback_mode": callback_mode})
                    db.upsert_account(account)


            with self._lock:
                self.running.pop(task_id, None)
            self._drain_queue()

        task = self.repo.get(task_id).to_dict()
        task_type = str(task.get("type") or task.get("task_type") or "")
        params = task.get("params") if isinstance(task.get("params"), dict) else {}
        config_path = str(params.get("config_path") or "")
        spawn_mode = "process"
        if task_type in {"email-protocol-register-token", "email_protocol_register", "email-protocol-register"}:
            spawn_mode = self._email_protocol_spawn_mode(task)
        pool_size = int(self.bucket_limits.get("register") or self.max_parallel or 64)
        managed = ManagedTask(
            task_id,
            command,
            log_file,
            self.repo,
            spawn_mode=spawn_mode,
            config_path=config_path,
            inline_pool_size=pool_size,
        )
        managed._omp_on_finish = finish  # type: ignore[attr-defined]
        return managed

    def _start_prepared(self, managed: ManagedTask) -> None:
        current = self.repo.get(managed.task_id).to_dict()
        current_status = str(current.get("status") or "")
        # Claim now leaves status=starting; running is only after pid is written.
        if current_status not in {"starting", "running"} or str(current.get("finished_at") or ""):
            self.repo.add_event(
                managed.task_id,
                "info",
                "start_skipped",
                f"任务已是 {current_status or 'unknown'}，跳过迟到的启动请求",
            )
            with self._lock:
                self.running.pop(managed.task_id, None)
            self._drain_queue()
            return
        task = self.repo.get(managed.task_id).to_dict()
        if str(task.get("task_type") or task.get("type") or "") in {"resume-oauth", "protocol-cpa-bind"}:
            callback_mode = _task_oauth_callback_mode(task)
            mode_label = "本地 OAuth" if callback_mode == "local" else "CPA"
            for account in account_store.list_accounts(refresh_legacy=False):
                if str(account.get("binding_task_id") or "") == managed.task_id and str(account.get("binding_status") or "") == "binding_queued":
                    account["binding_status"] = "binding_started"
                    account["binding_started_at"] = account.get("binding_started_at") or now_iso()
                    account["oauth_callback_mode"] = callback_mode
                    db.add_account_event(account["account_key"], "binding_started", task_id=managed.task_id, status="binding_started", message=f"协议/浏览器 {mode_label} 绑定任务开始执行", payload={"oauth_callback_mode": callback_mode})
                    db.upsert_account(account)
                    break
        try:
            # SQLite: process lock (single-writer). Postgres: bounded semaphore +
            # FOR UPDATE SKIP LOCKED so 64 tasks can lease outlook/proxy together.
            with self._with_resource_lease_guard():
                self._prepare_task_resources(managed.task_id)
        except Exception as exc:
            err = str(exc)
            if "database is locked" in err.lower() or "database is busy" in err.lower():
                import time

                time.sleep(0.35)
                try:
                    with self._with_resource_lease_guard():
                        self._prepare_task_resources(managed.task_id)
                except Exception as exc2:
                    err = str(exc2)
                else:
                    err = ""
            latest = self.repo.get(managed.task_id).to_dict()
            latest_status = str(latest.get("status") or "")
            if latest_status not in {"starting", "running"} or str(latest.get("finished_at") or ""):
                self.repo.add_event(managed.task_id, "info", "start_skipped", f"任务已是 {latest_status or 'unknown'}，资源准备后跳过启动")
                with self._lock:
                    self.running.pop(managed.task_id, None)
                self._drain_queue()
                return
            if not err:
                managed.start(on_finish=getattr(managed, "_omp_on_finish"))
                return
            self.repo.update(managed.task_id, status="failed", error=err, retryable=True)
            self.repo.add_event(managed.task_id, "error", "resource_lease_failed", f"任务启动前资源租赁失败: {err}")
            task = self.repo.get(managed.task_id).to_dict()
            if str(task.get("task_type") or task.get("type") or "") in {"resume-oauth", "protocol-cpa-bind"}:
                callback_mode = _task_oauth_callback_mode(task)
                mode_label = "本地 OAuth" if callback_mode == "local" else "CPA"
                for account in account_store.list_accounts(refresh_legacy=False):
                    if str(account.get("binding_task_id") or "") != managed.task_id:
                        continue
                    account["binding_status"] = "failed"
                    account["binding_error"] = err
                    account["oauth_callback_mode"] = callback_mode
                    db.add_account_event(account["account_key"], "binding_failed", task_id=managed.task_id, status="failed", message=f"{mode_label} 绑定任务失败: {err}", payload={"oauth_callback_mode": callback_mode})
                    db.upsert_account(account)
            with self._lock:
                self.running.pop(managed.task_id, None)
            self._drain_queue()
            return
        latest = self.repo.get(managed.task_id).to_dict()
        latest_status = str(latest.get("status") or "")
        if latest_status not in {"starting", "running"} or str(latest.get("finished_at") or ""):
            self.repo.add_event(managed.task_id, "info", "start_skipped", f"任务已是 {latest_status or 'unknown'}，资源准备后跳过启动")
            with self._lock:
                self.running.pop(managed.task_id, None)
            self._drain_queue()
            return
        managed.start(on_finish=getattr(managed, "_omp_on_finish"))

    def _schedule_or_queue(self, task_id: str, command: list[str], log_file: str) -> None:
        # Every launch flows through the durable queue claim.  A TasksService
        # instance owns only its local process handles; PostgreSQL owns global
        # admission across every dashboard process.
        self.repo.update(task_id, status="queued", updated_at=now_iso())
        self.repo.add_event(task_id, "info", "queued", "任务已排队: 等待数据库全局调度")
        self._drain_queue()

    def ensure_reconcile_loop(self) -> None:
        """Background reaper so orphan seats free even when UI is idle."""
        if self._reconcile_thread and self._reconcile_thread.is_alive():
            return

        def _loop() -> None:
            while not self._reconcile_stop.wait(20.0):
                try:
                    self.reconcile_orphan_running_tasks(grace_seconds=int(self._orphan_grace_seconds or 45))
                    # Keep the queue moving after free seats appear.
                    self.drain_queue_async()
                except Exception:
                    pass

        self._reconcile_thread = threading.Thread(target=_loop, name="task-orphan-reaper", daemon=True)
        self._reconcile_thread.start()

    def scheduler_health(self) -> dict[str, Any]:
        """Expose real concurrency so UI/ops can detect fake running seats."""
        import json as _json

        grace = int(self._orphan_grace_seconds or 45)
        counts = self.task_status_counts()
        running_rows: list[dict[str, Any]] = []
        try:
            running_rows = [task.to_dict() for task in self.repo.list(status="running", limit=2000, offset=0, order="asc")]
        except Exception:
            running_rows = []
        starting_count = int(counts.get("starting") or 0)
        alive = dead = nopid = inline = 0
        for item in running_rows:
            result = item.get("result") if isinstance(item.get("result"), dict) else {}
            if not result:
                raw = item.get("result_json")
                if isinstance(raw, str) and raw:
                    try:
                        result = _json.loads(raw) or {}
                    except Exception:
                        result = {}
            try:
                pid = int(result.get("pid") or 0)
            except Exception:
                pid = 0
            is_inline = bool(result.get("inline")) or str(result.get("spawn_mode") or "") == "inline"
            if is_inline:
                inline += 1
                alive += 1
            elif pid > 0 and is_pid_running(pid):
                alive += 1
            elif pid > 0:
                dead += 1
            else:
                nopid += 1
        live_seats = 0
        if hasattr(self.repo, "count_live_seats"):
            try:
                live_seats = int(self.repo.count_live_seats(orphan_grace_seconds=grace) or 0)
            except Exception:
                live_seats = alive + starting_count
        else:
            live_seats = alive + starting_count
        with self._lock:
            local_handles = len(self.running)
        go: dict[str, Any] = {}
        try:
            import urllib.request

            with urllib.request.urlopen("http://127.0.0.1:18765/diagnostics", timeout=1.5) as resp:
                go = _json.loads(resp.read().decode("utf-8", "replace") or "{}")
        except Exception as exc:
            go = {"ok": False, "error": str(exc)[:160]}
        healthy = nopid == 0 and dead == 0 and int(counts.get("running") or 0) == alive
        spawn_mode = "inline"
        try:
            spawn_mode = self._email_protocol_spawn_mode()
        except Exception:
            spawn_mode = "inline"
        return {
            "ok": True,
            "healthy": healthy,
            "max_parallel": int(self.max_parallel),
            "register_limit": int(self.bucket_limits.get("register") or 0),
            "local_handles": local_handles,
            "live_seats": live_seats,
            "running": int(counts.get("running") or 0),
            "starting": starting_count,
            "queued": int(counts.get("queued") or 0) + int(counts.get("pending") or 0),
            "alive_pid": alive,
            "inline_workers": inline,
            "dead_pid": dead,
            "nopid_running": nopid,
            "spawn_mode": spawn_mode,
            "orphan_grace_seconds": grace,
            "claim_burst": int(self._claim_burst or 16),
            "go": go,
            "message": (
                "调度健康"
                if healthy
                else f"假并发: running={counts.get('running') or 0} alive={alive} nopid={nopid} starting={starting_count}"
            ),
        }

    def drain_queue(self) -> None:
        self._drain_queue()

    def reconcile_orphan_running_tasks(self, *, grace_seconds: int | None = None, limit: int = 500) -> dict[str, int]:
        """Release durable seats that have no live process handle.

        Prefer the atomic DB path (requeue aged starting / nopid running).
        Also interrupt dead-pid running rows not owned by this process.
        """
        grace = int(self._orphan_grace_seconds if grace_seconds is None else grace_seconds)
        grace = max(15, grace)
        requeued = interrupted = scanned = 0

        # Fast path: SQL requeue under admission semantics.
        if hasattr(self.repo, "requeue_orphan_seats"):
            try:
                requeued = int(self.repo.requeue_orphan_seats(orphan_grace_seconds=grace) or 0)
            except Exception:
                requeued = 0

        # Dead-pid cleanup for rows already promoted to running.
        try:
            candidates = [task.to_dict() for task in self.repo.list(status="running", limit=max(1, min(int(limit or 500), 2000)), offset=0, order="asc")]
        except Exception:
            candidates = []
        with self._lock:
            local_ids = set(self.running.keys())
        for item in candidates:
            task_id = str(item.get("id") or "")
            if not task_id or task_id in local_ids:
                continue
            scanned += 1
            result = item.get("result") if isinstance(item.get("result"), dict) else {}
            try:
                pid = int(result.get("pid") or 0)
            except Exception:
                pid = 0
            if pid > 0:
                if is_pid_running(pid):
                    continue
                self.repo.update(
                    task_id,
                    status="interrupted",
                    error=item.get("error") or "任务进程已退出，运行槽已释放",
                    retryable=True,
                    updated_at=now_iso(),
                )
                self.repo.add_event(task_id, "warning", "stale_running", f"运行中任务已中断：pid={pid} 不存在")
                interrupted += 1
                continue
            # Go daemon owns these seats (no local pid). Never requeue/interrupt.
            result = item.get("result") if isinstance(item.get("result"), dict) else {}
            params = item.get("params") if isinstance(item.get("params"), dict) else {}
            if result.get("go_managed") or params.get("go_managed") or str(result.get("go_batch_id") or params.get("go_batch_id") or "").strip():
                continue
            # nopid running should already be handled by requeue_orphan_seats;
            # keep a soft age-based requeue for backends without that helper.
            started_raw = str(item.get("started_at") or item.get("updated_at") or "").strip()
            age = None
            if started_raw:
                try:
                    started_dt = datetime.fromisoformat(started_raw.replace("Z", "+00:00"))
                    age = __import__("time").time() - started_dt.timestamp()
                except Exception:
                    age = None
            if age is not None and age < grace:
                continue
            self.repo.update(
                task_id,
                status="queued",
                started_at="",
                error="",
                updated_at=now_iso(),
            )
            self.repo.add_event(
                task_id,
                "warning",
                "orphan_requeued",
                f"无进程 running 已回收并重新排队（grace={grace}s, age={int(age) if age is not None else -1}s）",
            )
            requeued += 1
        return {"scanned": scanned, "requeued": requeued, "interrupted": interrupted}


    def _drain_queue(self) -> None:
        # The local lock avoids duplicate work inside one dashboard process.
        # TasksRepository performs the durable cross-process claim.
        if not self._drain_lock.acquire(blocking=False):
            return
        to_start: list[ManagedTask] = []
        try:
            import time

            # Always reclaim aged nopid seats before claiming (not just every 15s).
            # claim_next_queued_task also does this atomically under the DB lock.
            try:
                self.reconcile_orphan_running_tasks(grace_seconds=int(self._orphan_grace_seconds or 45))
            except Exception:
                pass
            self._last_orphan_reconcile_at = time.monotonic()
            with self._lock:
                # Cap claim burst: only pull as many as we can start soon.
                # Local running is the true process handle set for this scheduler.
                free_local = max(0, int(self.max_parallel) - len(self.running))
                # Do not claim more than start workers can prepare — excess sits
                # as status=starting and blocks DB capacity for minutes.
                start_cap = max(16, int(self._start_workers or 64))
                burst = max(1, min(int(self._claim_burst or 16), free_local, start_cap, int(self.max_parallel)))
                claimed_count = 0
                while claimed_count < burst and len(self.running) < self.max_parallel:
                    claimed = self.repo.claim_next_queued_task(
                        max_parallel=self.max_parallel,
                        bucket_limits=self.bucket_limits,
                        bucket_for_type=self._bucket_for_type,
                        orphan_grace_seconds=int(self._orphan_grace_seconds or 20),
                    )
                    if claimed is None:
                        break
                    item = claimed.to_dict()
                    task_id = str(item.get("id") or "")
                    command = item.get("command") if isinstance(item.get("command"), list) else []
                    log_file = str(item.get("log_file") or "")
                    if not task_id or not command or not log_file:
                        self.repo.update(
                            task_id,
                            status="failed",
                            error="任务缺少启动命令或日志路径",
                            retryable=False,
                        )
                        self.repo.add_event(task_id, "error", "claim_invalid", "已领取任务缺少启动信息")
                        continue
                    self.repo.add_event(task_id, "info", "dequeued", "任务出队并开始执行")
                    managed = self._make_managed(task_id, command, log_file)
                    self.running[task_id] = managed
                    to_start.append(managed)
                    claimed_count += 1
        finally:
            self._drain_lock.release()
        # Resource preparation is slow; parallelize after the durable claim.
        if len(to_start) <= 1:
            for managed in to_start:
                self._start_prepared(managed)
            return
        workers = min(int(self._start_workers or 16), len(to_start))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="task-start") as pool:
            list(pool.map(self._start_prepared, to_start))



    def stop(self, task_id: str) -> bool:
        with self._lock:
            task = self.running.get(task_id)
        if task:
            return task.stop()
        current = self.repo.get(task_id).to_dict()
        status = str(current.get("status") or "")
        if status in {"queued", "pending", "starting"}:
            self.repo.update(task_id, status="cancelled", error="任务在启动前被取消", retryable=True)
            self.repo.add_event(task_id, "info", "cancelled", "等待中的任务已取消")
            return True
        if status == "running":
            result = current.get("result") if isinstance(current.get("result"), dict) else {}
            pid = int(result.get("pid") or 0)
            if pid and terminate_process_tree(pid):
                self.repo.update(task_id, status="cancelled", finished_at=now_iso(), updated_at=now_iso(), error="任务已取消", retryable=True)
                self.repo.add_event(task_id, "warning", "cancelled", f"已停止外部任务进程树: pid={pid}")
                return True
            self.repo.update(task_id, status="interrupted", error="任务未在当前进程运行，无法停止；已标记为中断。", retryable=True)
            self.repo.add_event(task_id, "warning", "stale_running", "停止失败：当前进程没有对应运行实例，已标记为中断")
        return False

    def stop_all(self) -> dict[str, int]:
        """Cancel every running/queued/pending task without scanning the whole history page-by-page.

        The old path listed *all* tasks (thousands) then stopped one-by-one; the UI hung on
        "正在结束…" because `/tasks/stop-all` never returned under load.
        """
        from services.task_runtime import terminate_process_tree
        from infrastructure import db as _db

        target_statuses = ("running", "starting", "queued", "pending")
        # Snapshot active tasks once via SQL (not full history scan).
        task_ids: list[str] = []
        pids: list[int] = []
        try:
            with _db.connect(getattr(self.repo, "db_path", None)) as conn:
                rows = conn.execute(
                    """
                    SELECT id, status, result_json
                    FROM tasks
                    WHERE status IN ('running', 'starting', 'queued', 'pending')
                    """
                ).fetchall()
            for row in rows:
                if isinstance(row, dict):
                    tid = str(row.get("id") or "")
                    status = str(row.get("status") or "")
                    result_raw = row.get("result_json")
                else:
                    tid = str(row[0] or "")
                    status = str(row[1] or "")
                    result_raw = row[2]
                if not tid:
                    continue
                task_ids.append(tid)
                if status == "running" and result_raw:
                    try:
                        import json as _json

                        result = _json.loads(result_raw) if isinstance(result_raw, str) else (result_raw or {})
                        pid = int((result or {}).get("pid") or 0)
                        if pid > 0:
                            pids.append(pid)
                    except Exception:
                        pass
        except Exception:
            # Fallback: only in-memory runners + status-filtered pages.
            with self._lock:
                task_ids.extend(list(self.running.keys()))
            for status in target_statuses:
                offset = 0
                while True:
                    page = self.repo.list(status=status, limit=500, offset=offset, order="asc")
                    if not page:
                        break
                    for task in page:
                        item = task.to_dict() if hasattr(task, "to_dict") else dict(task)
                        tid = str(item.get("id") or "")
                        if tid:
                            task_ids.append(tid)
                    if len(page) < 500:
                        break
                    offset += len(page)

        # Stop live ManagedTask runners first (best-effort, non-blocking-ish).
        with self._lock:
            live = list(self.running.items())
            self.running.clear()
        for task_id, managed in live:
            try:
                managed.stop()
            except Exception:
                pass
            if task_id not in task_ids:
                task_ids.append(task_id)

        for pid in sorted(set(pids)):
            try:
                terminate_process_tree(pid)
            except Exception:
                pass

        requested = len(set(task_ids))
        now = now_iso()
        stopped = 0
        failed = 0
        try:
            with _db.connect(getattr(self.repo, "db_path", None)) as conn:
                cur = conn.execute(
                    """
                    UPDATE tasks
                    SET status='cancelled',
                        error='批量结束：用户请求结束所有任务',
                        finished_at=CASE WHEN COALESCE(finished_at,'')='' THEN %s ELSE finished_at END,
                        updated_at=%s,
                        retryable=1
                    WHERE status IN ('running', 'starting', 'queued', 'pending')
                    """,
                    (now, now),
                )
                stopped = int(getattr(cur, "rowcount", 0) or 0)
                # Some backends report -1; treat as requested when no error.
                if stopped < 0:
                    stopped = requested
        except Exception:
            # Last-resort per-id cancel (still better than scanning whole history).
            for task_id in sorted(set(task_ids)):
                try:
                    if self.stop(task_id):
                        stopped += 1
                    else:
                        failed += 1
                except Exception:
                    failed += 1
        return {"requested": requested or stopped, "stopped": stopped, "failed": failed}
