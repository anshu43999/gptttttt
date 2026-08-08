from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any

from infrastructure import db
from infrastructure.repositories.plus_activation_repository import PlusActivationRepository

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPORT_ROOT = PROJECT_ROOT / "output" / "plus_exports"


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _normalize_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _safe_name(value: str, fallback: str) -> str:
    raw = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(value or "").strip())
    raw = raw.strip("_")[:80]
    return raw or fallback


class PlusActivationBatchService:
    def __init__(self, repo: PlusActivationRepository | None = None):
        self.repo = repo or PlusActivationRepository()

    def create_batch(
        self,
        keys: list[str],
        *,
        name: str = "",
        channel: str = "upi",
        dry_run: bool = False,
        submit_rate_per_min: int = 49,
        max_in_flight: int = 16,
    ) -> dict[str, Any]:
        clean_keys = [str(key or "").strip() for key in keys if str(key or "").strip()]
        if not clean_keys:
            return {"ok": False, "message": "请至少选择一个账号", "accepted": 0, "queued": 0, "skipped": 0, "results": []}
        result = self.repo.create_batch_with_items(
            clean_keys,
            name=name,
            provider="upi",
            channel=channel or "upi",
            dry_run=dry_run,
            submit_rate_per_min=submit_rate_per_min,
            max_in_flight=max_in_flight,
        )
        queued = 0
        enqueue_payload: dict[str, Any] | None = None
        if result.accepted_keys and not dry_run:
            from services.upi_activation_service import get_upi_activation_service

            upi = get_upi_activation_service()
            enqueue_payload = upi.enqueue_accounts_async(
                result.accepted_keys,
                channel=channel or "upi",
                force=False,
            )
            queued = _safe_int(enqueue_payload.get("queued"), len(result.accepted_keys) if enqueue_payload.get("ok") else 0)
            self.repo.sync_items_from_accounts(str(result.batch.get("batch_key") if result.batch else ""))
        skipped_count = len(result.skipped) + max(0, len(result.accepted_keys) - queued if not dry_run else 0)
        payload = {
            "ok": True,
            "dry_run": bool(dry_run),
            "batch": result.batch,
            "batch_key": result.batch.get("batch_key") if result.batch else "",
            "requested": len(clean_keys),
            "accepted": len(result.accepted_keys),
            "queued": queued if not dry_run else 0,
            "skipped": skipped_count,
            "skip_counts": result.skip_counts,
            "results": result.skipped,
            "enqueue": enqueue_payload,
        }
        if dry_run:
            payload["message"] = f"预检完成：可加入 {len(result.accepted_keys)} 个，跳过 {len(result.skipped)} 个"
        else:
            payload["message"] = f"已创建 Plus 批次：{queued}/{len(result.accepted_keys)} 个进入 UPI 队列，跳过 {skipped_count} 个"
        return payload

    def list_batches(self, *, status: str = "active", limit: int = 50, offset: int = 0) -> dict[str, Any]:
        data = self.repo.list_batches(status=status, limit=limit, offset=offset)
        for item in data.get("items") or []:
            batch_key = str(item.get("batch_key") or "")
            if batch_key and str(item.get("status") or "") not in {"archived"}:
                self.repo.sync_items_from_accounts(batch_key)
        return self.repo.list_batches(status=status, limit=limit, offset=offset)

    def get_batch(self, batch_key: str) -> dict[str, Any]:
        self.repo.sync_items_from_accounts(batch_key)
        batch = self.repo.get_batch(batch_key)
        if not batch:
            return {"ok": False, "message": "批次不存在"}
        return {"ok": True, "batch": batch, "exports": self.repo.list_exports(batch_key)}

    def list_items(self, batch_key: str, **filters: Any) -> dict[str, Any]:
        self.repo.sync_items_from_accounts(batch_key)
        return {"ok": True, **self.repo.list_items(batch_key, **filters)}

    def _refresh_remote_active_items(self, batch_key: str) -> dict[str, Any]:
        data = self.repo.list_items(
            batch_key,
            status="submitted,processing",
            include_exported=False,
            limit=500,
        )
        keys = [str(item.get("account_key") or "").strip() for item in data.get("items") or []]
        keys = [key for key in keys if key]
        if not keys:
            return {"checked": 0, "updated": 0}
        from services.upi_activation_service import get_upi_activation_service

        result = get_upi_activation_service().refresh_activation_tasks(keys, statuses=["submitted", "processing"])
        return {
            "checked": _safe_int(result.get("checked"), len(keys)) if isinstance(result, dict) else len(keys),
            "updated": _safe_int(result.get("updated"), 0) if isinstance(result, dict) else 0,
        }

    def refresh(self, batch_key: str) -> dict[str, Any]:
        self.repo.sync_items_from_accounts(batch_key)
        remote = self._refresh_remote_active_items(batch_key)
        self.repo.sync_items_from_accounts(batch_key)
        batch = self.repo.get_batch(batch_key)
        if not batch:
            return {"ok": False, "message": "批次不存在"}
        return {"ok": True, "batch": batch, "remote_refresh": remote}

    def retry_items(self, batch_key: str, *, keys: list[str] | None = None, statuses: list[str] | None = None, channel: str = "upi") -> dict[str, Any]:
        self.repo.sync_items_from_accounts(batch_key)
        data = self.repo.list_items(batch_key, status=",".join(statuses or ["failed", "releasable", "released", "submit_unknown"]), limit=100000)
        allowed = {str(key) for key in (keys or []) if str(key or "").strip()}
        retry_keys = [str(item["account_key"]) for item in data.get("items") or [] if not allowed or str(item["account_key"]) in allowed]
        if not retry_keys:
            return {"ok": False, "message": "没有可重试的账号", "retried": 0}
        self.repo.mark_batch_items_for_retry(batch_key, retry_keys, channel=channel or "upi")
        from services.upi_activation_service import get_upi_activation_service

        enqueue = get_upi_activation_service().enqueue_accounts_async(retry_keys, channel=channel or "upi", force=True)
        self.repo.sync_items_from_accounts(batch_key)
        return {"ok": True, "message": f"已重试 {len(retry_keys)} 个账号", "retried": len(retry_keys), "enqueue": enqueue, "batch": self.repo.get_batch(batch_key)}

    def release_items(self, batch_key: str, *, keys: list[str] | None = None, statuses: list[str] | None = None) -> dict[str, Any]:
        self.repo.sync_items_from_accounts(batch_key)
        data = self.repo.list_items(batch_key, status=",".join(statuses or ["failed", "releasable", "released", "submit_unknown", "submitted", "processing"]), limit=100000)
        allowed = {str(key) for key in (keys or []) if str(key or "").strip()}
        release_keys = [str(item["account_key"]) for item in data.get("items") or [] if not allowed or str(item["account_key"]) in allowed]
        if not release_keys:
            return {"ok": False, "message": "没有可释放/取消的账号", "released": 0}
        from services.upi_activation_service import get_upi_activation_service

        release = get_upi_activation_service().release_accounts(release_keys)
        ok_keys = [str(item.get("key") or item.get("account_key") or "") for item in release.get("results") or [] if item.get("ok")]
        if not ok_keys and release.get("ok"):
            ok_keys = release_keys
        self.repo.mark_released_accounts(batch_key, ok_keys)
        return {"ok": bool(ok_keys), "message": release.get("message") or f"已释放 {len(ok_keys)} 个账号", "released": len(ok_keys), "results": release.get("results") or [], "batch": self.repo.get_batch(batch_key)}

    def export_plus(
        self,
        batch_key: str,
        *,
        fmt: str = "txt",
        include_already_exported: bool = False,
        archive_after_export: bool = True,
    ) -> dict[str, Any]:
        self.repo.sync_items_from_accounts(batch_key)
        batch = self.repo.get_batch(batch_key)
        if not batch:
            return {"ok": False, "message": "批次不存在"}
        statuses = ["verified"]
        if include_already_exported:
            statuses.extend(["exported", "archived"])
        data = self.repo.list_items(batch_key, status=",".join(statuses), limit=100000)
        item_keys = [str(item["account_key"]) for item in data.get("items") or []]
        if not item_keys:
            return {"ok": False, "message": "没有可导出的 Plus 成品号", "count": 0}
        placeholders = ",".join("?" for _ in item_keys)
        with db.connect(getattr(self.repo, "db_path", None)) as conn:
            rows = conn.execute(
                f"""
                SELECT a.account_key, a.email, a.password, a.billing_email, a.codex_email, a.plan_type,
                       a.plus_status, a.plus_verified_at, c.access_token, c.refresh_token
                FROM accounts a
                LEFT JOIN account_credentials c ON c.account_id_ref=a.id
                WHERE a.account_key IN ({placeholders})
                ORDER BY a.id
                """,
                item_keys,
            ).fetchall()
        accounts = [dict(row) for row in rows]
        EXPORT_ROOT.mkdir(parents=True, exist_ok=True)
        fmt_norm = fmt if fmt in {"txt", "csv", "json"} else "txt"
        file_name = f"{_safe_name(batch_key, 'plus_batch')}_plus_{len(accounts)}.{fmt_norm}"
        file_path = EXPORT_ROOT / file_name
        text_content = ""
        if fmt_norm == "json":
            content = json.dumps(accounts, ensure_ascii=False, indent=2)
            file_path.write_text(content + "\n", encoding="utf-8")
        elif fmt_norm == "csv":
            fields = ["email", "password", "billing_email", "codex_email", "account_key", "plus_status", "plus_verified_at"]
            with file_path.open("w", newline="", encoding="utf-8-sig") as fh:
                writer = csv.DictWriter(fh, fieldnames=fields)
                writer.writeheader()
                for account in accounts:
                    writer.writerow({field: account.get(field, "") for field in fields})
        else:
            # Full product line: email----Outlook密码----client_id----refresh_token
            # Must match Accounts page export_plus_products_txt (resource pool OT).
            from application.accounts_service import AccountsService
            from infrastructure.repositories.accounts_repository import AccountsRepository

            db_path = getattr(self.repo, "db_path", None)
            accounts_svc = AccountsService(repo=AccountsRepository(db_path))
            resource_cache = accounts_svc._email_resource_cache()
            lines: list[str] = []
            skipped: list[dict[str, str]] = []
            for account in accounts:
                line, kind, error = accounts_svc._format_plus_product_line(
                    account,
                    resource_cache=resource_cache,
                )
                if line:
                    lines.append(line)
                    continue
                # No short ChatGPT-password fallback — OT missing means skip.
                email = str(account.get("email") or account.get("account_key") or "").strip()
                if email:
                    skipped.append({
                        "email": email,
                        "kind": kind or "unknown",
                        "error": error or "无法组装成品号行",
                    })
            if not lines:
                return {
                    "ok": False,
                    "message": f"没有可导出的完整成品号（跳过 {len(skipped)}）",
                    "count": 0,
                    "skipped": skipped[:50],
                }
            text_content = "\n".join(lines) + "\n"
            file_name = f"{_safe_name(batch_key, 'plus_batch')}_plus_{len(lines)}.{fmt_norm}"
            file_path = EXPORT_ROOT / file_name
            file_path.write_text(text_content, encoding="utf-8")
            # Track keys by email/account_key from successful lines.
            key_by_email = {
                str(a.get("email") or "").strip().lower(): str(a.get("account_key") or a.get("email") or "")
                for a in accounts
            }
            key_by_email.update({
                str(a.get("account_key") or "").strip().lower(): str(a.get("account_key") or "")
                for a in accounts
            })
            exported_account_keys: list[str] = []
            for ln in lines:
                head = ln.split("----", 1)[0].strip()
                exported_account_keys.append(key_by_email.get(head.lower()) or head)
            accounts = [{"account_key": k} for k in exported_account_keys]
        checksum = hashlib.sha256(file_path.read_bytes()).hexdigest()
        export = self.repo.create_export_record(
            batch_key,
            fmt=fmt_norm,
            file_path=str(file_path.relative_to(PROJECT_ROOT)),
            file_name=file_name,
            count=len(accounts),
            checksum=checksum,
            include_already_exported=include_already_exported,
            archive_after_export=archive_after_export,
            account_keys=[str(account["account_key"]) for account in accounts],
        )
        skipped_items = locals().get("skipped") or []
        return {
            "ok": True,
            "message": f"已导出 {len(accounts)} 个 Plus 成品号"
            + (f"（跳过 {len(skipped_items)}）" if fmt_norm == "txt" and skipped_items else ""),
            "export": export,
            "count": len(accounts),
            "file_name": file_name,
            "download_url": f"/api/plus-activation/exports/{export.get('export_key')}/download",
            "text": text_content,
            "archived": len(accounts) if archive_after_export else 0,
            "skipped": skipped_items[:50] if fmt_norm == "txt" else [],
        }

    def show_accounts_in_account_list(self, batch_key: str, *, keys: list[str] | None = None) -> dict[str, Any]:
        return self.repo.show_accounts_in_account_list(batch_key, keys=keys)

    def archive_batch(self, batch_key: str, *, force: bool = False) -> dict[str, Any]:
        return self.repo.archive_batch(batch_key, force=force)


def get_plus_activation_batch_service() -> PlusActivationBatchService:
    return PlusActivationBatchService()
