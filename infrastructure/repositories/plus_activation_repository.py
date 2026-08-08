from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from infrastructure import db

ACTIVE_ITEM_STATUSES = frozenset({
    "reserved",
    "queued",
    "submitting",
    "submit_unknown",
    "submitted",
    "processing",
    "verifying",
    "verified",
    "failed",
    "releasable",
    "exported",
})
TERMINAL_BATCH_STATUSES = frozenset({"completed", "completed_with_failures", "cancelled", "archived"})
ACTIVE_BATCH_STATUSES = frozenset({"queued", "running", "paused"})


def _now() -> str:
    return db.now_iso()


def _rowdict(row: Any) -> dict[str, Any]:
    return dict(row or {})


def _batch_key() -> str:
    return f"plus_batch_{db.now_iso().replace('-', '').replace(':', '').replace('T', '_')}_{uuid.uuid4().hex[:8]}"


def _export_key() -> str:
    return f"plus_export_{db.now_iso().replace('-', '').replace(':', '').replace('T', '_')}_{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True)
class BatchCreateResult:
    batch: dict[str, Any] | None
    accepted_keys: list[str]
    skipped: list[dict[str, Any]]
    skip_counts: dict[str, int]


class PlusActivationRepository:
    def __init__(self, db_path: str | Path | None = None):
        self.db_path = db_path
        db.init_db(db_path)

    def precheck_keys(self, keys: Iterable[str]) -> dict[str, Any]:
        ordered: list[str] = []
        seen: set[str] = set()
        skipped: list[dict[str, Any]] = []
        skip_counts: dict[str, int] = {}
        for raw in keys or []:
            key = str(raw or "").strip()
            if not key:
                continue
            if key in seen:
                skipped.append({"key": key, "reason": "duplicate_input", "message": "请求内重复账号"})
                skip_counts["duplicate_input"] = skip_counts.get("duplicate_input", 0) + 1
                continue
            seen.add(key)
            ordered.append(key)
        if not ordered:
            return {"accepted": [], "skipped": skipped, "skip_counts": skip_counts, "requested": 0}
        placeholders = ",".join("?" for _ in ordered)
        with db.connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT a.id, a.account_key, a.email, a.status, a.stage, a.plus_status,
                       a.active_plus_batch_id, a.active_plus_batch_key, a.plus_archived_at,
                       a.export_status,
                       COALESCE(c.access_token, '') AS access_token,
                       COALESCE(c.chatgpt_access_token_initial, '') AS chatgpt_access_token_initial
                FROM accounts a
                LEFT JOIN account_credentials c ON c.account_id_ref=a.id
                WHERE a.account_key IN ({placeholders})
                """,
                ordered,
            ).fetchall()
            # Show-to-account-list clears accounts.active_plus_batch_* markers but leaves
            # plus_activation_batch_items rows in active statuses. The partial unique index
            # uq_plus_items_one_active_per_account still blocks a second active item.
            active_status_list = sorted(ACTIVE_ITEM_STATUSES)
            status_ph = ",".join("?" for _ in active_status_list)
            active_item_rows = conn.execute(
                f"""
                SELECT i.account_id_ref, i.account_key, i.batch_key, i.status
                FROM plus_activation_batch_items i
                WHERE i.status IN ({status_ph})
                  AND (
                    i.account_key IN ({placeholders})
                    OR i.account_id_ref IN (
                      SELECT a.id FROM accounts a WHERE a.account_key IN ({placeholders})
                    )
                  )
                """,
                (*active_status_list, *ordered, *ordered),
            ).fetchall()
        by_key = {str(row["account_key"]): _rowdict(row) for row in rows}
        active_by_account_id: dict[int, dict[str, Any]] = {}
        active_by_account_key: dict[str, dict[str, Any]] = {}
        for item in active_item_rows:
            payload = _rowdict(item)
            try:
                account_id = int(payload.get("account_id_ref") or 0)
            except Exception:
                account_id = 0
            account_key = str(payload.get("account_key") or "").strip()
            if account_id > 0:
                active_by_account_id[account_id] = payload
            if account_key:
                active_by_account_key[account_key] = payload
        accepted: list[dict[str, Any]] = []
        for key in ordered:
            row = by_key.get(key)
            reason = ""
            message = ""
            active_item = None
            if row:
                try:
                    account_id = int(row.get("id") or 0)
                except Exception:
                    account_id = 0
                active_item = active_by_account_id.get(account_id) or active_by_account_key.get(key)
            if not row:
                reason, message = "not_found", "未找到账号"
            elif row.get("active_plus_batch_id") or str(row.get("active_plus_batch_key") or ""):
                reason, message = "already_in_batch", f"已在 Plus 批次 {row.get('active_plus_batch_key') or row.get('active_plus_batch_id')}"
            elif active_item:
                batch_key = str(active_item.get("batch_key") or "").strip()
                reason, message = "already_in_batch", f"已在 Plus 批次 {batch_key or active_item.get('status') or ''}".strip()
            elif str(row.get("plus_archived_at") or "") or str(row.get("export_status") or "") == "exported_plus_archived":
                reason, message = "already_exported", "Plus 成品号已导出归档"
            elif str(row.get("plus_status") or "").lower() == "verified_plus":
                reason, message = "already_plus", "账号已是 Plus"
            elif str(row.get("stage") or row.get("status") or "").lower() == "archived":
                reason, message = "invalid_state", "账号已归档"
            elif not str(row.get("access_token") or row.get("chatgpt_access_token_initial") or "").strip():
                reason, message = "missing_token", "缺少 access_token"
            if reason:
                skipped.append({
                    "key": key,
                    "reason": reason,
                    "message": message,
                    "batch_key": (
                        (row.get("active_plus_batch_key") if row else "")
                        or (active_item.get("batch_key") if active_item else "")
                        or ""
                    ),
                })
                skip_counts[reason] = skip_counts.get(reason, 0) + 1
            else:
                accepted.append(row)
        return {"accepted": accepted, "skipped": skipped, "skip_counts": skip_counts, "requested": len(ordered)}

    def create_batch_with_items(
        self,
        keys: Iterable[str],
        *,
        name: str = "",
        provider: str = "upi",
        channel: str = "upi",
        dry_run: bool = False,
        submit_rate_per_min: int = 0,
        max_in_flight: int = 0,
    ) -> BatchCreateResult:
        precheck = self.precheck_keys(keys)
        accepted_rows = list(precheck["accepted"])
        skipped = list(precheck["skipped"])
        skip_counts = dict(precheck["skip_counts"])
        requested = int(precheck["requested"] or 0)
        if dry_run or not accepted_rows:
            return BatchCreateResult(None, [str(row["account_key"]) for row in accepted_rows], skipped, skip_counts)
        now = _now()
        batch_key = _batch_key()
        batch_name = name.strip() or f"UPI开通{len(accepted_rows)}单"
        with db.connect(self.db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO plus_activation_batches(
                  batch_key,name,provider,channel,status,requested_count,accepted_count,skipped_count,total_count,
                  queued_count,submit_rate_per_min,max_in_flight,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                RETURNING id
                """,
                (
                    batch_key,
                    batch_name,
                    provider,
                    channel,
                    "running",
                    requested,
                    len(accepted_rows),
                    len(skipped),
                    len(accepted_rows),
                    len(accepted_rows),
                    int(submit_rate_per_min or 0),
                    int(max_in_flight or 0),
                    now,
                    now,
                ),
            )
            row = cur.fetchone()
            batch_id = int(row["id"] if row else conn.execute("SELECT id FROM plus_activation_batches WHERE batch_key=?", (batch_key,)).fetchone()["id"])
            item_rows: list[tuple[Any, ...]] = []
            account_updates: list[tuple[Any, ...]] = []
            # Re-check occupancy inside the same transaction. Account list "show" clears
            # active_plus_batch_* markers without releasing items, so precheck alone can race.
            accepted_ids = [int(row["id"]) for row in accepted_rows]
            if accepted_ids:
                id_ph = ",".join("?" for _ in accepted_ids)
                status_list = sorted(ACTIVE_ITEM_STATUSES)
                status_ph = ",".join("?" for _ in status_list)
                occupied = {
                    int(item["account_id_ref"]): _rowdict(item)
                    for item in conn.execute(
                        f"""
                        SELECT account_id_ref, account_key, batch_key, status
                        FROM plus_activation_batch_items
                        WHERE account_id_ref IN ({id_ph})
                          AND status IN ({status_ph})
                        """,
                        (*accepted_ids, *status_list),
                    ).fetchall()
                }
            else:
                occupied = {}
            final_accepted: list[dict[str, Any]] = []
            for row in accepted_rows:
                account_id = int(row["id"])
                account_key = str(row["account_key"])
                if account_id in occupied:
                    existing = occupied[account_id]
                    skipped.append({
                        "key": account_key,
                        "reason": "already_in_batch",
                        "message": f"已在 Plus 批次 {existing.get('batch_key') or ''}",
                        "batch_key": existing.get("batch_key") or "",
                    })
                    skip_counts["already_in_batch"] = skip_counts.get("already_in_batch", 0) + 1
                    continue
                item_key = f"{batch_key}:{account_id}"
                item_rows.append((
                    batch_id,
                    batch_key,
                    item_key,
                    account_id,
                    account_key,
                    str(row.get("email") or account_key),
                    "queued",
                    provider,
                    channel,
                    f"upi-{batch_key}-{account_id}",
                    now,
                    now,
                ))
                final_accepted.append(row)
            if not item_rows:
                conn.execute("DELETE FROM plus_activation_batches WHERE id=?", (batch_id,))
                return BatchCreateResult(None, [], skipped, skip_counts)
            conn.executemany(
                """
                INSERT INTO plus_activation_batch_items(
                  batch_id,batch_key,item_key,account_id_ref,account_key,email,status,provider,channel,idempotency_key,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                item_rows,
            )
            # Keep batch counters honest after late skips.
            conn.execute(
                """
                UPDATE plus_activation_batches
                SET accepted_count=?, skipped_count=?, total_count=?, queued_count=?, updated_at=?
                WHERE id=?
                """,
                (len(final_accepted), len(skipped), len(final_accepted), len(final_accepted), now, batch_id),
            )
            item_refs = conn.execute(
                "SELECT id, account_id_ref FROM plus_activation_batch_items WHERE batch_id=?",
                (batch_id,),
            ).fetchall()
            for item in item_refs:
                account_updates.append((batch_id, batch_key, int(item["id"]), "queued", now, now, int(item["account_id_ref"])))
            conn.executemany(
                """
                UPDATE accounts
                SET active_plus_batch_id=?, active_plus_batch_key=?, active_plus_item_id=?, plus_batch_status=?, plus_reserved_at=?, updated_at=?
                WHERE id=? AND COALESCE(active_plus_batch_id, 0)=0 AND COALESCE(active_plus_batch_key, '')=''
                """,
                account_updates,
            )
        self.refresh_batch_summary(batch_key)
        batch = self.get_batch(batch_key) or {}
        return BatchCreateResult(batch, [str(row["account_key"]) for row in final_accepted], skipped, skip_counts)

    def get_batch(self, batch_key: str) -> dict[str, Any] | None:
        with db.connect(self.db_path) as conn:
            row = conn.execute("SELECT * FROM plus_activation_batches WHERE batch_key=?", (batch_key,)).fetchone()
        return _rowdict(row) if row else None

    def list_batches(self, *, status: str = "active", limit: int = 50, offset: int = 0) -> dict[str, Any]:
        where = "1=1"
        params: list[Any] = []
        if status == "active":
            where = "status NOT IN ('archived')"
        elif status and status != "all":
            values = [part.strip() for part in status.split(",") if part.strip()]
            if values:
                where = "status IN (" + ",".join("?" for _ in values) + ")"
                params.extend(values)
        safe_limit = max(1, min(int(limit or 50), 200))
        safe_offset = max(0, int(offset or 0))
        with db.connect(self.db_path) as conn:
            total = int(conn.execute(f"SELECT COUNT(*) AS n FROM plus_activation_batches WHERE {where}", params).fetchone()["n"] or 0)
            rows = conn.execute(
                f"SELECT * FROM plus_activation_batches WHERE {where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (*params, safe_limit, safe_offset),
            ).fetchall()
        return {"items": [_rowdict(row) for row in rows], "total": total, "limit": safe_limit, "offset": safe_offset}

    def list_items(
        self,
        batch_key: str,
        *,
        status: str = "",
        search: str = "",
        error: str = "",
        include_exported: bool = True,
        limit: int = 80,
        offset: int = 0,
    ) -> dict[str, Any]:
        where = "batch_key=?"
        params: list[Any] = [batch_key]
        statuses = [value.strip() for value in str(status or "").split(",") if value.strip()]
        if statuses:
            where += " AND status IN (" + ",".join("?" for _ in statuses) + ")"
            params.extend(statuses)
        if search:
            where += " AND (account_key LIKE ? OR email LIKE ? OR remote_task_id LIKE ?)"
            like = f"%{search}%"
            params.extend([like, like, like])
        if error:
            where += " AND (activation_error LIKE ? OR activation_display LIKE ?)"
            like = f"%{error}%"
            params.extend([like, like])
        if not include_exported:
            where += " AND COALESCE(exported_at,'')=''"
        safe_limit = max(1, min(int(limit or 80), 500))
        safe_offset = max(0, int(offset or 0))
        with db.connect(self.db_path) as conn:
            total = int(conn.execute(f"SELECT COUNT(*) AS n FROM plus_activation_batch_items WHERE {where}", params).fetchone()["n"] or 0)
            rows = conn.execute(
                f"""
                SELECT id,batch_id,batch_key,item_key,account_id_ref,account_key,email,status,provider,channel,
                       remote_task_id,client_key_hash,activation_attempt,retry_count,activation_error,
                       activation_error_code,activation_display,can_release,cdk_consumed,exported_at,export_key,
                       archived_at,submitted_at,finished_at,released_at,last_polled_at,created_at,updated_at
                FROM plus_activation_batch_items
                WHERE {where}
                ORDER BY CASE WHEN status IN ('queued','submitting','submit_unknown','submitted','processing','verifying') THEN 0 ELSE 1 END,
                         updated_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (*params, safe_limit, safe_offset),
            ).fetchall()
        return {"items": [_rowdict(row) for row in rows], "total": total, "limit": safe_limit, "offset": safe_offset}

    def refresh_batch_summary(self, batch_key: str) -> dict[str, Any] | None:
        now = _now()
        with db.connect(self.db_path) as conn:
            batch = conn.execute("SELECT id,status FROM plus_activation_batches WHERE batch_key=?", (batch_key,)).fetchone()
            if not batch:
                return None
            batch_id = int(batch["id"])
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM plus_activation_batch_items WHERE batch_id=? GROUP BY status",
                (batch_id,),
            ).fetchall()
            counts = {str(row["status"] or ""): int(row["n"] or 0) for row in rows}
            total = sum(counts.values())
            active = sum(counts.get(st, 0) for st in ("reserved", "queued", "submitting", "submit_unknown", "submitted", "processing", "verifying"))
            verified = counts.get("verified", 0) + counts.get("exported", 0) + counts.get("archived", 0)
            failed = counts.get("failed", 0) + counts.get("releasable", 0)
            released = counts.get("released", 0)
            skipped = counts.get("skipped", 0)
            status = str(batch["status"] or "running")
            if status != "archived":
                if active > 0:
                    status = "running"
                elif failed > 0:
                    status = "completed_with_failures"
                else:
                    status = "completed"
            progress = int(round(((total - active) / total) * 100)) if total else 0
            success_rate = int(round((verified / total) * 100)) if total else 0
            err_rows = conn.execute(
                """
                SELECT COALESCE(NULLIF(activation_display,''), activation_error) AS err, COUNT(*) AS n
                FROM plus_activation_batch_items
                WHERE batch_id=? AND COALESCE(NULLIF(activation_display,''), activation_error, '')<>''
                GROUP BY COALESCE(NULLIF(activation_display,''), activation_error)
                ORDER BY n DESC
                LIMIT 8
                """,
                (batch_id,),
            ).fetchall()
            errors = [{"message": str(row["err"] or ""), "count": int(row["n"] or 0)} for row in err_rows]
            conn.execute(
                """
                UPDATE plus_activation_batches
                SET status=?, total_count=?, reserved_count=?, queued_count=?, submitting_count=?, submit_unknown_count=?,
                    submitted_count=?, processing_count=?, verifying_count=?, verified_count=?, failed_count=?, releasable_count=?,
                    released_count=?, exported_count=?, archived_count=?, cdk_consumed_count=?, progress_percent=?, success_rate_percent=?,
                    error_summary_json=?, last_error=?, updated_at=?, finished_at=CASE WHEN ?=0 AND finished_at='' THEN ? ELSE finished_at END
                WHERE id=?
                """,
                (
                    status,
                    total,
                    counts.get("reserved", 0),
                    counts.get("queued", 0),
                    counts.get("submitting", 0),
                    counts.get("submit_unknown", 0),
                    counts.get("submitted", 0),
                    counts.get("processing", 0),
                    counts.get("verifying", 0),
                    counts.get("verified", 0),
                    counts.get("failed", 0),
                    counts.get("releasable", 0),
                    released,
                    counts.get("exported", 0),
                    counts.get("archived", 0),
                    int(conn.execute("SELECT COUNT(*) AS n FROM plus_activation_batch_items WHERE batch_id=? AND cdk_consumed<>0", (batch_id,)).fetchone()["n"] or 0),
                    progress,
                    success_rate,
                    json.dumps(errors, ensure_ascii=False),
                    errors[0]["message"] if errors else "",
                    now,
                    active,
                    now,
                    batch_id,
                ),
            )
        return self.get_batch(batch_key)

    def sync_items_from_accounts(self, batch_key: str) -> None:
        now = _now()
        active_values = {"queued", "submitting", "submit_unknown", "submitted", "processing", "verifying"}
        with db.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT i.id, i.status AS item_status, i.account_id_ref,
                       a.activation_status, a.activation_task_id, a.activation_idempotency_key,
                       a.activation_client_key_hash, a.activation_attempt, a.activation_error,
                       a.activation_display, a.activation_can_release, a.activation_cdk_consumed,
                       a.activation_submitted_at, a.activation_finished_at, a.activation_updated_at,
                       a.plus_status
                FROM plus_activation_batch_items i
                JOIN accounts a ON a.id=i.account_id_ref
                WHERE i.batch_key=? AND i.status NOT IN ('released','exported','archived','skipped')
                """,
                (batch_key,),
            ).fetchall()
            item_updates: list[tuple[Any, ...]] = []
            account_updates: list[tuple[Any, ...]] = []
            for row in rows:
                old = str(row["item_status"] or "")
                activation = str(row["activation_status"] or "").strip().lower()
                plus_status = str(row["plus_status"] or "").strip().lower()
                new_status = old
                if plus_status == "verified_plus" or activation in {"success", "verified", "active"}:
                    new_status = "verified"
                elif activation in {"queued", "submitting", "submit_unknown", "submitted", "processing", "verifying"}:
                    new_status = activation
                elif activation == "released":
                    new_status = "released"
                elif activation in {"failed", "expired", "replace_account", "cancelled", "submit_rejected"}:
                    new_status = "releasable" if int(row["activation_can_release"] or 0) else "failed"
                item_updates.append((
                    new_status,
                    str(row["activation_task_id"] or ""),
                    str(row["activation_idempotency_key"] or ""),
                    str(row["activation_client_key_hash"] or ""),
                    int(row["activation_attempt"] or 0),
                    str(row["activation_error"] or ""),
                    str(row["activation_display"] or ""),
                    int(row["activation_can_release"] or 0),
                    int(row["activation_cdk_consumed"] or 0),
                    str(row["activation_submitted_at"] or ""),
                    str(row["activation_finished_at"] or ""),
                    str(row["activation_updated_at"] or ""),
                    now,
                    int(row["id"]),
                ))
                if new_status == "released":
                    account_updates.append(("", None, "", None, "", int(row["account_id_ref"])))
                elif new_status != old or new_status in active_values:
                    account_updates.append((new_status, None, None, None, now, int(row["account_id_ref"])))
            if item_updates:
                conn.executemany(
                    """
                    UPDATE plus_activation_batch_items
                    SET status=?, remote_task_id=?, idempotency_key=COALESCE(NULLIF(?,''), idempotency_key), client_key_hash=?,
                        activation_attempt=?, activation_error=?, activation_display=?, can_release=?, cdk_consumed=?,
                        submitted_at=COALESCE(NULLIF(?,''), submitted_at), finished_at=COALESCE(NULLIF(?,''), finished_at),
                        last_polled_at=COALESCE(NULLIF(?,''), last_polled_at), updated_at=?
                    WHERE id=?
                    """,
                    item_updates,
                )
            for status, _b1, _b2, _b3, updated, account_id in account_updates:
                if status == "":
                    conn.execute(
                        """
                        UPDATE accounts
                        SET active_plus_batch_id=NULL, active_plus_batch_key='', active_plus_item_id=NULL, plus_batch_status='', updated_at=?
                        WHERE id=?
                        """,
                        (now, account_id),
                    )
                else:
                    conn.execute(
                        "UPDATE accounts SET plus_batch_status=?, updated_at=? WHERE id=?",
                        (status, updated or now, account_id),
                    )
        self.refresh_batch_summary(batch_key)

    def mark_batch_items_for_retry(self, batch_key: str, keys: list[str], *, channel: str = "upi") -> None:
        now = _now()
        if not keys:
            return
        placeholders = ",".join("?" for _ in keys)
        with db.connect(self.db_path) as conn:
            conn.execute(
                f"""
                UPDATE plus_activation_batch_items
                SET status='queued', channel=?, retry_count=retry_count+1, activation_error='', activation_display='',
                    can_release=0, released_at='', updated_at=?
                WHERE batch_key=? AND account_key IN ({placeholders})
                """,
                (channel, now, batch_key, *keys),
            )
            conn.execute(
                f"""
                UPDATE accounts
                SET active_plus_batch_id=(SELECT id FROM plus_activation_batches WHERE batch_key=?),
                    active_plus_batch_key=?,
                    active_plus_item_id=(SELECT id FROM plus_activation_batch_items WHERE batch_key=? AND plus_activation_batch_items.account_key=accounts.account_key),
                    plus_batch_status='queued', plus_reserved_at=COALESCE(NULLIF(plus_reserved_at,''), ?), updated_at=?
                WHERE account_key IN ({placeholders})
                """,
                (batch_key, batch_key, batch_key, now, now, *keys),
            )
        self.refresh_batch_summary(batch_key)

    def mark_released_accounts(self, batch_key: str, keys: list[str]) -> None:
        if not keys:
            return
        now = _now()
        placeholders = ",".join("?" for _ in keys)
        with db.connect(self.db_path) as conn:
            conn.execute(
                f"""
                UPDATE plus_activation_batch_items
                SET status='released', can_release=0, released_at=COALESCE(NULLIF(released_at,''), ?), updated_at=?
                WHERE batch_key=? AND account_key IN ({placeholders})
                """,
                (now, now, batch_key, *keys),
            )
            conn.execute(
                f"""
                UPDATE accounts
                SET active_plus_batch_id=NULL, active_plus_batch_key='', active_plus_item_id=NULL, plus_batch_status='', updated_at=?
                WHERE account_key IN ({placeholders})
                """,
                (now, *keys),
            )
        self.refresh_batch_summary(batch_key)

    def create_export_record(
        self,
        batch_key: str,
        *,
        fmt: str,
        file_path: str,
        file_name: str,
        count: int,
        checksum: str,
        include_already_exported: bool,
        archive_after_export: bool,
        account_keys: list[str],
    ) -> dict[str, Any]:
        now = _now()
        export_key = _export_key()
        with db.connect(self.db_path) as conn:
            batch = conn.execute("SELECT id FROM plus_activation_batches WHERE batch_key=?", (batch_key,)).fetchone()
            if not batch:
                raise ValueError("批次不存在")
            batch_id = int(batch["id"])
            conn.execute(
                """
                INSERT INTO plus_activation_exports(export_key,batch_id,batch_key,kind,format,file_path,file_name,count,checksum,include_already_exported,archive_after_export,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (export_key, batch_id, batch_key, "plus_verified", fmt, file_path, file_name, count, checksum, int(include_already_exported), int(archive_after_export), now),
            )
            if account_keys:
                placeholders = ",".join("?" for _ in account_keys)
                next_status = "archived" if archive_after_export else "exported"
                conn.execute(
                    f"""
                    UPDATE plus_activation_batch_items
                    SET status=?, exported_at=COALESCE(NULLIF(exported_at,''), ?), export_key=?, archived_at=CASE WHEN ?<>0 THEN ? ELSE archived_at END, updated_at=?
                    WHERE batch_key=? AND account_key IN ({placeholders})
                    """,
                    (next_status, now, export_key, int(archive_after_export), now, now, batch_key, *account_keys),
                )
                conn.execute(
                    f"""
                    UPDATE accounts
                    SET export_status='exported_plus_archived', export_kind='plus_batch', exported_at=?,
                        plus_export_batch_key=?, plus_export_key=?, plus_archived_at=CASE WHEN ?<>0 THEN ? ELSE plus_archived_at END,
                        active_plus_batch_id=NULL, active_plus_batch_key='', active_plus_item_id=NULL,
                        plus_batch_status=CASE WHEN ?<>0 THEN 'archived' ELSE 'exported' END,
                        updated_at=?
                    WHERE account_key IN ({placeholders})
                    """,
                    (now, batch_key, export_key, int(archive_after_export), now, int(archive_after_export), now, *account_keys),
                )
        self.refresh_batch_summary(batch_key)
        return self.get_export(export_key) or {}

    def get_export(self, export_key: str) -> dict[str, Any] | None:
        with db.connect(self.db_path) as conn:
            row = conn.execute("SELECT * FROM plus_activation_exports WHERE export_key=?", (export_key,)).fetchone()
        return _rowdict(row) if row else None

    def list_exports(self, batch_key: str) -> list[dict[str, Any]]:
        with db.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM plus_activation_exports WHERE batch_key=? ORDER BY created_at DESC, id DESC",
                (batch_key,),
            ).fetchall()
        return [_rowdict(row) for row in rows]

    def show_accounts_in_account_list(self, batch_key: str, keys: list[str] | None = None) -> dict[str, Any]:
        batch = self.get_batch(batch_key)
        if not batch:
            return {"ok": False, "message": "批次不存在", "visible": 0}
        now = _now()
        allowed = [str(key or "").strip() for key in (keys or []) if str(key or "").strip()]
        key_filter = ""
        params: list[Any] = [now, batch_key, int(batch["id"]), batch_key]
        if allowed:
            key_filter = f" AND account_key IN ({','.join('?' for _ in allowed)})"
            params.extend(allowed)
        with db.connect(self.db_path) as conn:
            cur = conn.execute(
                f"""
                UPDATE accounts
                SET active_plus_batch_id=NULL,
                    active_plus_batch_key='',
                    active_plus_item_id=NULL,
                    plus_batch_status='',
                    updated_at=?
                WHERE (active_plus_batch_key=? OR active_plus_batch_id=?)
                  AND EXISTS (
                    SELECT 1 FROM plus_activation_batch_items i
                    WHERE i.batch_key=? AND i.account_key=accounts.account_key
                  )
                  {key_filter}
                """,
                tuple(params),
            )
            visible = int(cur.rowcount or 0)
        return {"ok": True, "message": f"已允许 {visible} 个账号重新显示在账号列表", "visible": visible, "batch": self.get_batch(batch_key)}

    def archive_batch(self, batch_key: str, *, force: bool = False) -> dict[str, Any]:
        self.refresh_batch_summary(batch_key)
        batch = self.get_batch(batch_key)
        if not batch:
            return {"ok": False, "message": "批次不存在"}
        active = sum(int(batch.get(f"{name}_count") or 0) for name in ("reserved", "queued", "submitting", "submit_unknown", "submitted", "processing", "verifying"))
        if active and not force:
            return {"ok": False, "message": f"批次还有 {active} 个进行中 item，不能归档"}
        now = _now()
        with db.connect(self.db_path) as conn:
            conn.execute("UPDATE plus_activation_batches SET status='archived', archived_at=?, updated_at=? WHERE batch_key=?", (now, now, batch_key))
        return {"ok": True, "batch": self.get_batch(batch_key)}
