"""Account management — FastAPI router."""
from __future__ import annotations
import threading
import time
import uuid

from pathlib import Path

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from api.deps import get_accounts_service, get_tasks_service, get_browser_session_service, get_account_health_service
from application.accounts_service import AccountsService
from application.account_health_service import AccountHealthService
from application.browser_session_service import BrowserSessionService
from application.tasks_service import TasksService
from core import account_store
from infrastructure import db


_PLUS_VERIFY_TASKS: dict[str, dict] = {}
_PLUS_VERIFY_TASKS_LOCK = threading.Lock()

# Short TTL list cache: Accounts page reloads after almost every bulk action.
# Full list is ~10k rows; recomputing every time dominates wall time.
_ACCOUNTS_LIST_CACHE: dict[str, object] = {"at": 0.0, "items": None}
_ACCOUNTS_LIST_CACHE_LOCK = threading.Lock()
_ACCOUNTS_LIST_CACHE_TTL_S = 3.0

def _plus_task_snapshot(task: dict) -> dict:
    return {key: value for key, value in task.items() if key != "cancel_event"}


def _update_plus_task(task_id: str, **values) -> dict:
    with _PLUS_VERIFY_TASKS_LOCK:
        task = _PLUS_VERIFY_TASKS.get(task_id)
        if not task:
            return {}
        task.update(values)
        return _plus_task_snapshot(task)


def _run_plus_verify_task(task_id: str, keys: list[str], proxy_region: str, svc: AccountsService) -> None:
    """Progressive Plus verify with live progress + cancel.

    Model matches the old 8-worker UI (complete → bar moves → cancel works), but
    each chunk is executed by Go multi-worker over ONE shared local bridge.

    Why not 32× Python verify_plus:
      each call starts its own SOCKS bridge → 32 bridges thrash and stall at 0/N.
    Why not one giant Go batch for all keys:
      UI stays 0/N until the whole batch returns (looks frozen for minutes).
    """
    try:
        try:
            cfg = svc.config_service.merged_config() if getattr(svc, "config_service", None) else {}
        except Exception:
            cfg = {}
        # Plus verify concurrency: default 64 Go goroutines.
        # Chunk size intentionally <= 48 so each Go call returns fast (UI moves)
        # and we do not pile 64 concurrent requests onto 1-3 bridges forever.
        workers = max(1, min(100, len(keys), int(cfg.get("max_plus_verify_workers") or 64) or 64))
        chunk_size = max(8, min(48, workers, len(keys)))

        def is_cancelled() -> bool:
            with _PLUS_VERIFY_TASKS_LOCK:
                task = _PLUS_VERIFY_TASKS.get(task_id)
                if not task:
                    return True
                if task.get("cancelled"):
                    return True
                ev = task.get("cancel_event")
                return bool(ev and ev.is_set())

        def cancelled_result(key: str) -> dict:
            return {
                "key": key,
                "ok": False,
                "status_code": 499,
                "message": "已取消",
                "error_code": "cancelled",
            }

        with _PLUS_VERIFY_TASKS_LOCK:
            task = _PLUS_VERIFY_TASKS.get(task_id)
            if task is not None:
                task["pending_keys"] = list(keys)
                task["in_flight_keys"] = []
                task["workers"] = workers
                task["backend"] = "go"
                task["message"] = f"校验中… Go {workers} 并发"

        all_results: list[dict] = []
        backend_used = "go"
        workers_used = workers

        # Build bridges ONCE for the whole progressive task.
        # Rebuilding SOCKS→HTTP bridge every 64-item chunk was the main stall
        # (minutes of idle UI + eventual python sequential fallback).
        shared_bridge_urls: list[str] = []
        shared_bridge_runtimes: list = []
        try:
            from core.proxy.credential_runtime import CredentialProxyRuntime
            from core.proxy.seed_session import build_session, seed_from_payload

            region = str(proxy_region or "JP").split(",")[0].strip().upper() or "JP"
            pool_proxies: list[str] = []
            if hasattr(svc, "_pool_proxy_candidates"):
                try:
                    pool_proxies = list(svc._pool_proxy_candidates(proxy_region=region, limit=8) or [])
                except Exception:
                    pool_proxies = []
            # Always try bestgo seed mint if pool empty / filtered.
            if len(pool_proxies) < 8:
                try:
                    from application.resource_pool_service import ResourcePoolService

                    pool = ResourcePoolService()
                    need = 8 - len(pool_proxies)
                    for i in range(need):
                        u, _meta = pool._lease_proxy_session(
                            f"plus-verify-{task_id}-{i}",
                            region=region,
                            config={"proxy_seed_styles": "bestgo"},
                        )
                        if u and u not in pool_proxies:
                            pool_proxies.append(u)
                except Exception:
                    pass

            seen_up: set[str] = set()
            for up in pool_proxies:
                up = str(up or "").strip()
                if not up or up in seen_up:
                    continue
                seen_up.add(up)
                runtime = CredentialProxyRuntime({"lajiao_proxy_credential_protocol": "socks5"}, log_fn=lambda _m: None)
                try:
                    runtime_proxy = runtime.runtime_url(up) if hasattr(runtime, "runtime_url") else up
                    bridge_url = runtime.start_browser_bridge(runtime_proxy)
                    if bridge_url and str(bridge_url).startswith("http://127.0.0.1:") and bridge_url not in shared_bridge_urls:
                        shared_bridge_runtimes.append(runtime)
                        shared_bridge_urls.append(str(bridge_url))
                    else:
                        try:
                            runtime.cleanup()
                        except Exception:
                            pass
                except Exception:
                    try:
                        runtime.cleanup()
                    except Exception:
                        pass
                if len(shared_bridge_urls) >= 8:
                    break
        except Exception:
            shared_bridge_urls = []
            shared_bridge_runtimes = []

        try:
            for offset in range(0, len(keys), chunk_size):
                if is_cancelled():
                    for key in keys[offset:]:
                        all_results.append(cancelled_result(key))
                    break

                chunk = keys[offset: offset + chunk_size]
                with _PLUS_VERIFY_TASKS_LOCK:
                    task = _PLUS_VERIFY_TASKS.get(task_id)
                    if task is None:
                        return
                    task["in_flight_keys"] = list(chunk)
                    task["pending_keys"] = keys[offset + len(chunk):]
                    task["message"] = (
                        f"进行中 {len(all_results)}/{len(keys)}，本批 {len(chunk)}，"
                        f"Go workers={workers}"
                        + (f"，bridges={len(shared_bridge_urls)}" if shared_bridge_urls else "")
                    )

                chunk_results: list[dict] = []
                go_result = None
                if hasattr(svc, "_verify_plus_batch_via_go"):
                    try:
                        go_result = svc._verify_plus_batch_via_go(
                            chunk,
                            proxy_region=proxy_region,
                            bridge_urls=shared_bridge_urls or None,
                            bridge_runtimes=shared_bridge_runtimes or None,
                            own_bridges=False if shared_bridge_urls else True,
                            timeout_ms=8000,
                        )
                    except Exception:
                        go_result = None

                if isinstance(go_result, dict) and isinstance(go_result.get("results"), list):
                    chunk_results = list(go_result.get("results") or [])
                    backend_used = str(go_result.get("backend") or "go")
                    try:
                        workers_used = int(go_result.get("workers") or workers_used)
                    except Exception:
                        pass
                else:
                    # Do NOT fall back to sequential Python verify_plus (N bridges, stalls).
                    # Mark the whole chunk failed so UI moves and user can retry.
                    backend_used = "go_unavailable"
                    for key in chunk:
                        chunk_results.append({
                            "key": key,
                            "ok": False,
                            "status_code": 503,
                            "message": "Go Plus 校验不可用（无 bridge / worker 失败）；请检查 bestgo 代理与 Go worker",
                            "error_code": "go_plus_verify_unavailable",
                        })

                # Preserve input order for missing keys in chunk results.
                by_key = {str(item.get("key") or ""): item for item in chunk_results if isinstance(item, dict)}
                ordered = []
                for key in chunk:
                    ordered.append(by_key.get(key) or {"key": key, "ok": False, "status_code": 500, "message": "无结果"})
                all_results.extend(ordered)

                with _PLUS_VERIFY_TASKS_LOCK:
                    task = _PLUS_VERIFY_TASKS.get(task_id)
                    if task is None:
                        return
                    task["results"] = list(all_results)
                    task["completed"] = len(all_results)
                    task["paid"] = sum(1 for item in all_results if item.get("paid"))
                    task["failed"] = sum(1 for item in all_results if not item.get("ok"))
                    task["in_flight_keys"] = []
                    task["pending_keys"] = keys[offset + len(chunk):]
                    task["backend"] = backend_used
                    task["workers"] = workers_used
                    task["message"] = (
                        f"进行中 {len(all_results)}/{len(keys)}，"
                        f"Plus/Team {task['paid']}，失败 {task['failed']}，"
                        f"backend={backend_used} workers={workers_used}"
                    )

            # If cancelled mid-loop already filled remaining; ensure full coverage.
            done = {str(item.get("key") or "") for item in all_results}
            for key in keys:
                if key not in done:
                    all_results.append(cancelled_result(key))

            with _PLUS_VERIFY_TASKS_LOCK:
                task = _PLUS_VERIFY_TASKS.get(task_id)
                if task is None:
                    return
                # Do NOT call is_cancelled() here — it re-acquires the same non-reentrant lock (deadlock).
                cancelled = bool(task.get("cancelled"))
                ev = task.get("cancel_event")
                if ev is not None and ev.is_set():
                    cancelled = True
                task["results"] = all_results
                task["completed"] = len(all_results)
                task["paid"] = sum(1 for item in all_results if item.get("paid"))
                task["failed"] = sum(1 for item in all_results if not item.get("ok"))
                task["pending_keys"] = []
                task["in_flight_keys"] = []
                task["running"] = False
                task["workers"] = workers_used
                task["backend"] = backend_used
                task["cancelled"] = cancelled
                task["ok"] = (not cancelled) and all(item.get("ok") for item in all_results) if all_results else False
                task["message"] = (
                    f"{'已取消' if cancelled else '完成'}：{len(all_results)}/{len(keys)}，"
                    f"Plus/Team {task['paid']}，失败 {task['failed']}，"
                    f"backend={backend_used} workers={workers_used}"
                )
        finally:
            for runtime in shared_bridge_runtimes:
                try:
                    runtime.cleanup()
                except Exception:
                    pass
    except Exception as exc:
        _update_plus_task(task_id, running=False, ok=False, error=str(exc))
router = APIRouter()
PROJECT_ROOT = Path(__file__).resolve().parents[1]


class MarkPlusRequest(BaseModel):
    """标记账号为已确认 Plus。"""

class VerifyPlusRequest(BaseModel):
    proxy_region: str = "JP"


class VerifyPlusBatchRequest(VerifyPlusRequest):
    keys: list[str] = Field(default_factory=list)
    async_mode: bool = False
class ArchiveAccountsRequest(BaseModel):
    keys: list[str] = Field(default_factory=list)


class ImportAtAccountsRequest(BaseModel):
    text: str = Field(min_length=1)


class OpenBrowserRequest(BaseModel):
    target_url: str = "https://chatgpt.com/"
    use_saved_proxy: bool = True
    browser_engine: str = "auto"
    headed: bool = True
    save_on_close: bool = False


class CloseBrowserSessionRequest(BaseModel):
    save: bool = False

class RefreshAccessTokenRequest(BaseModel):
    use_saved_proxy: bool = True
    save_storage: bool = True

class BillingEmailBindRequest(BaseModel):
    headed: bool = True
    mailbox_provider: str = "icloud_api"
    resume_file: str = ""
    proxy_region: str = "JP"
    lajiao_proxy_credential_protocol: str = "socks5"

class BillingEmailBindBatchRequest(BaseModel):
    keys: list[str] = Field(default_factory=list)
    headed: bool = True
    mailbox_provider: str = "icloud_api"
    proxy_region: str = "JP"
    lajiao_proxy_credential_protocol: str = "socks5"







class ResumeOAuthRequest(BaseModel):
    headed: bool = True
    oauth_callback_mode: str = ""
    cpa_base_url: str = ""
    cpa_management_key: str = ""
    sms_provider: str = ""
    sms_phone_url: str = ""
    sms_country: str = ""
    sms_service: str = ""
    bind_sms_provider: str = ""
    bind_sms_phone_url: str = ""
    bind_sms_country: str = ""
    bind_sms_service: str = ""
    bind_country_code: str = ""

class ExportAccountRequest(BaseModel):
    fields: list[str] = Field(default_factory=list)

class ExportAccountsRequest(BaseModel):
    keys: list[str] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=list)


class ExportPlusProductsRequest(BaseModel):
    keys: list[str] = Field(default_factory=list)
    only_verified: bool = True
    archive_after_export: bool = False

class ExportAtProductsRequest(BaseModel):
    keys: list[str] = Field(default_factory=list)
    archive_after_export: bool = False

class ActivatePlusBatchRequest(BaseModel):
    keys: list[str] = Field(default_factory=list)
    channel: str = "upi"
    force: bool = False
    provider: str = "upi"


class PlusActivationBatchCreateRequest(BaseModel):
    keys: list[str] = Field(default_factory=list)
    name: str = ""
    channel: str = "upi"
    dry_run: bool = False
    submit_rate_per_min: int = 49
    max_in_flight: int = 16


class PlusActivationBatchActionRequest(BaseModel):
    keys: list[str] = Field(default_factory=list)
    statuses: list[str] = Field(default_factory=list)
    channel: str = "upi"
    force: bool = False


class PlusActivationBatchExportRequest(BaseModel):
    format: str = "txt"
    include_already_exported: bool = False
    archive_after_export: bool = True

class ActivationTasksRefreshRequest(BaseModel):
    keys: list[str] = Field(default_factory=list)
    statuses: list[str] = Field(default_factory=lambda: ["submitted", "processing", "submit_unknown"])


class ActivationTasksRetryRequest(BaseModel):
    keys: list[str] = Field(default_factory=list)
    statuses: list[str] = Field(default_factory=lambda: ["failed", "cancelled", "released"])
    channel: str = "upi"

class IssueClientKeyRequest(BaseModel):
    cdk: str = Field(min_length=1)
    note: str = "gpt-register"
    rotate: bool = False



def _account_list_item(item: dict) -> dict:
    tokens = token_summary(item)
    proxy = item.get("proxy") if isinstance(item.get("proxy"), dict) else {}
    return {
        "id": item.get("id"),
        "key": item.get("account_key") or item.get("key") or item.get("account_id") or "",
        "account_key": item.get("account_key") or item.get("key") or item.get("account_id") or "",
        "account_id": item.get("account_id") or "",
        "email": item.get("email") or "",
        "billing_email": item.get("billing_email") or "",
        # Collection responses must not carry credentials; use detail/export paths.
        "has_password": bool(str(item.get("password") or "").strip()),
        "phone_number": item.get("phone_number") or "",
        "sms_phone": item.get("phone_number") or item.get("sms_phone") or "",
        "plan_type": item.get("plan_type") or "",
        "status": item.get("status") or "",
        "stage": item.get("stage") or "",
        "registration_mode": item.get("registration_mode") or "",
        "login_identifier": item.get("login_identifier") or item.get("email") or item.get("phone_number") or "",
        "registration_status": item.get("registration_status") or "",
        "registration_task_id": item.get("registration_task_id") or "",
        "registration_started_at": item.get("registration_started_at") or "",
        "registration_completed_at": item.get("registration_completed_at") or "",
        "registration_error": item.get("registration_error") or "",
        "display_name": item.get("display_name") or item.get("nickname") or "",
        "plus_status": item.get("plus_status") or "",
        "plus_verified_at": item.get("plus_verified_at") or "",
        "plus_check_source": item.get("plus_check_source") or "",
        "plus_check_error": item.get("plus_check_error") or "",
        "cpa_auth_file_name": item.get("cpa_auth_file_name") or "",
        # cpa_auth_file_json is credential material; never include in list DTOs.
        "cpa_synced_at": item.get("cpa_synced_at") or "",
        "cpa_sync_error": item.get("cpa_sync_error") or "",
        "binding_status": item.get("binding_status") or "",
        "binding_task_id": item.get("binding_task_id") or "",
        "binding_provider": item.get("binding_provider") or "",
        "binding_started_at": item.get("binding_started_at") or "",
        "binding_phone_number": item.get("binding_phone_number") or "",
        "binding_completed_at": item.get("binding_completed_at") or "",
        "binding_error": item.get("binding_error") or "",
        "oauth_callback_mode": item.get("oauth_callback_mode") or "",
        "cpa_base_url": item.get("cpa_base_url") or "",
        "cpa_submitted_at": item.get("cpa_submitted_at") or "",
        "cpa_submit_status": item.get("cpa_submit_status") or "",
        "cpa_submit_error": item.get("cpa_submit_error") or "",
        "registration_phone_resource_id": item.get("registration_phone_resource_id") or 0,
        "binding_phone_resource_id": item.get("binding_phone_resource_id") or 0,
        "email_resource_id": item.get("email_resource_id") or 0,
        "proxy_resource_id": item.get("proxy_resource_id") or 0,
        "registration_proxy_exit_ip": item.get("registration_proxy_exit_ip") or "",
        "registration_proxy_region": item.get("registration_proxy_region") or "",
        "resume_file": item.get("resume_file") or "",
        "storage_file": item.get("storage_file") or "",
        "account_file": item.get("account_file") or "",
        "account_health_status": item.get("account_health_status") or "",
        "account_health_checked_at": item.get("account_health_checked_at") or "",
        "account_health_source": item.get("account_health_source") or "",
        "account_health_error": item.get("account_health_error") or "",
        "health_status": item.get("account_health_status") or "",
        "export_status": item.get("export_status") or "",
        "export_kind": item.get("export_kind") or "",
        "exported_at": item.get("exported_at") or "",
        "activation_provider": item.get("activation_provider") or "",
        "activation_status": item.get("activation_status") or "",
        "activation_channel": item.get("activation_channel") or "",
        "activation_task_id": item.get("activation_task_id") or "",
        "activation_error": item.get("activation_error") or "",
        "activation_display": item.get("activation_display") or "",
        "activation_can_release": int(item.get("activation_can_release") or 0),
        "activation_cdk_consumed": int(item.get("activation_cdk_consumed") or 0),
        "activation_submitted_at": item.get("activation_submitted_at") or "",
        "activation_finished_at": item.get("activation_finished_at") or "",
        "activation_updated_at": item.get("activation_updated_at") or "",
        "active_plus_batch_id": item.get("active_plus_batch_id") or 0,
        "active_plus_batch_key": item.get("active_plus_batch_key") or "",
        "active_plus_item_id": item.get("active_plus_item_id") or 0,
        "plus_batch_status": item.get("plus_batch_status") or "",
        "plus_reserved_at": item.get("plus_reserved_at") or "",
        "plus_archived_at": item.get("plus_archived_at") or "",
        "plus_export_batch_key": item.get("plus_export_batch_key") or "",
        "plus_export_key": item.get("plus_export_key") or "",
        "created_at": item.get("created_at") or "",
        "updated_at": item.get("updated_at") or "",
        "tokens": tokens,
        "proxy": {
            "registration_exit_ip": proxy.get("registration_exit_ip") or "",
            "registration_country": proxy.get("registration_country") or "",
        },
        "proxy_region": proxy.get("registration_country") or proxy.get("registration_exit_ip") or "",
    }

@router.get("/accounts")
async def list_accounts(
    status: str = "",
    stage: str = "",
    search: str = "",
    limit: int = 5000,
    include_archived: bool = False,
):
    def _load() -> list:
        now = time.monotonic()
        with _ACCOUNTS_LIST_CACHE_LOCK:
            cached = _ACCOUNTS_LIST_CACHE.get("items")
            at = float(_ACCOUNTS_LIST_CACHE.get("at") or 0.0)
            if cached is not None and (now - at) < _ACCOUNTS_LIST_CACHE_TTL_S:
                return list(cached)  # type: ignore[arg-type]
        items = account_store.list_accounts(refresh_legacy=False)
        with _ACCOUNTS_LIST_CACHE_LOCK:
            _ACCOUNTS_LIST_CACHE["items"] = items
            _ACCOUNTS_LIST_CACHE["at"] = time.monotonic()
        return items

    items = await run_in_threadpool(_load)
    # Default: hide archived from the main accounts page for speed + clarity.
    want_archived = bool(include_archived) or str(status or "").lower() == "archived" or str(stage or "").lower() == "archived"
    if not want_archived:
        items = [
            item for item in items
            if not any(
                str(item.get(field) or "").strip().lower() == "archived"
                for field in ("stage", "status", "registration_status")
            )
        ]
    if status:
        items = [item for item in items if str(item.get("status") or "") == status]
    if stage:
        items = [item for item in items if str(item.get("stage") or "") == stage]
    if search:
        needle = search.lower()
        items = [item for item in items if needle in " ".join(str(item.get(k) or "") for k in ("account_key", "account_id", "email", "phone_number", "login_identifier")).lower()]
    total = len(items)
    effective_limit = max(1, min(int(limit), 100000))
    truncated = total > effective_limit
    return {
        "ok": True,
        "items": [_account_list_item(item) for item in items[:effective_limit]],
        "total": total,
        "truncated": truncated,
        "include_archived": want_archived,
    }


@router.get("/accounts/export-fields")
async def account_export_fields(svc: AccountsService = Depends(get_accounts_service)):
    return {"ok": True, "fields": await run_in_threadpool(svc.available_export_fields)}


@router.post("/accounts/import-at")
async def import_at_accounts(req: ImportAtAccountsRequest, svc: AccountsService = Depends(get_accounts_service)):
    result = await run_in_threadpool(svc.import_at_accounts, req.text)
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    _invalidate_accounts_list_cache()
    return result


@router.post("/accounts-bulk/archive")
@router.post("/accounts/archive")
async def archive_accounts_legacy(req: ArchiveAccountsRequest, svc: AccountsService = Depends(get_accounts_service)):
    keys = [str(key or "").strip() for key in req.keys if str(key or "").strip()]
    if not keys:
        return JSONResponse({"ok": False, "message": "请至少选择一个账号"}, status_code=400)
    result = await run_in_threadpool(svc.archive_many, keys)
    _invalidate_accounts_list_cache()
    return result

def _invalidate_accounts_list_cache() -> None:
    with _ACCOUNTS_LIST_CACHE_LOCK:
        _ACCOUNTS_LIST_CACHE["items"] = None
        _ACCOUNTS_LIST_CACHE["at"] = 0.0


class ArchiveOlderThanRequest(BaseModel):
    days: int = 3
    name: str = ""
    reason: str = "older_than_days"


@router.get("/archive-batches")
async def list_archive_batches(limit: int = 100):
    items = await run_in_threadpool(db.list_archive_batches, limit=limit)
    return {"ok": True, "items": items, "total": len(items)}


@router.get("/archive-batches/{batch_key}")
async def get_archive_batch_api(batch_key: str):
    batch = await run_in_threadpool(db.get_archive_batch, batch_key)
    if not batch:
        return JSONResponse({"ok": False, "message": "归档批次不存在"}, status_code=404)
    return {"ok": True, "batch": batch}


@router.post("/archive-batches/archive-older-than")
async def archive_older_than(req: ArchiveOlderThanRequest):
    result = await run_in_threadpool(
        db.archive_accounts_older_than,
        days=int(req.days or 3),
        reason=str(req.reason or "older_than_days"),
        name=str(req.name or ""),
    )
    _invalidate_accounts_list_cache()
    return result


@router.post("/archive-batches/{batch_key}/restore")
async def restore_archive_batch_api(batch_key: str):
    result = await run_in_threadpool(db.restore_archive_batch, batch_key)
    if not result.get("ok"):
        return JSONResponse(result, status_code=404 if "不存在" in str(result.get("message") or "") else 400)
    _invalidate_accounts_list_cache()
    return result



@router.get("/accounts/{key}/events")
async def account_events(key: str):
    return {"ok": True, "items": await run_in_threadpool(db.list_account_events, key)}



@router.post("/accounts/{key}/sync-cpa-token")
async def sync_cpa_token(key: str, svc: AccountsService = Depends(get_accounts_service)):
    payload, status_code = await run_in_threadpool(svc.sync_cpa_token, key)
    if status_code != 200:
        return JSONResponse(payload, status_code=status_code)
    return payload

@router.post("/accounts-bulk/sync-cpa-token")
async def sync_cpa_tokens(req: VerifyPlusBatchRequest, svc: AccountsService = Depends(get_accounts_service)):
    keys = [str(key or "").strip() for key in req.keys if str(key or "").strip()]
    if not keys:
        return JSONResponse({"ok": False, "message": "请至少选择一个账号"}, status_code=400)
    results = await run_in_threadpool(lambda: [
        {"key": key, "status_code": status_code, **payload}
        for key in keys
        for payload, status_code in [svc.sync_cpa_token(key)]
    ])
    synced = sum(1 for item in results if item.get("ok"))
    return {"ok": all(item.get("ok") for item in results), "synced": synced, "results": results}


@router.post("/accounts/cleanup-invalid")
async def cleanup_invalid_accounts(svc: AccountsService = Depends(get_accounts_service)):
    return await run_in_threadpool(svc.archive_invalid_accounts)

@router.post("/accounts/{key}/mark-plus")
async def mark_plus(key: str, _req: MarkPlusRequest = MarkPlusRequest(),
                    svc: AccountsService = Depends(get_accounts_service)):
    account = await run_in_threadpool(svc.mark_plus, key)
    if not account:
        return JSONResponse({"ok": False, "message": "未找到账号"}, status_code=404)
    return {"ok": True, "account": account}

@router.post("/accounts-bulk/verify-plus")
async def verify_plus_batch(req: VerifyPlusBatchRequest, svc: AccountsService = Depends(get_accounts_service)):
    keys = []
    seen = set()
    for raw_key in req.keys:
        key = str(raw_key or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        keys.append(key)
    if not keys:
        return JSONResponse({"ok": False, "message": "请至少选择一个账号"}, status_code=400)
    if req.async_mode:
        task_id = f"plus-verify-{uuid.uuid4().hex[:12]}"
        try:
            cfg = svc.config_service.merged_config() if getattr(svc, "config_service", None) else {}
        except Exception:
            cfg = {}
        workers_hint = max(1, min(100, len(keys), int(cfg.get("max_plus_verify_workers") or 64) or 64))
        task = {
            "ok": True,
            "task_id": task_id,
            "total": len(keys),
            "completed": 0,
            "paid": 0,
            "failed": 0,
            "running": True,
            "cancelled": False,
            "results": [],
            "pending_keys": list(keys),
            "in_flight_keys": [],
            "workers": workers_hint,
            "backend": "go",
            "message": f"校验中… Go {workers_hint} 并发；网络失败换代理/sid 重试 2 次；可取消",
            "cancel_event": threading.Event(),
        }
        with _PLUS_VERIFY_TASKS_LOCK:
            _PLUS_VERIFY_TASKS[task_id] = task
        thread = threading.Thread(target=_run_plus_verify_task, args=(task_id, keys, req.proxy_region, svc), daemon=True)
        thread.start()
        return JSONResponse(_plus_task_snapshot(task), status_code=202)
    return await run_in_threadpool(svc.verify_plus_batch, keys, proxy_region=req.proxy_region)


@router.get("/accounts-bulk/verify-plus/{task_id}")
async def verify_plus_batch_status(task_id: str):
    with _PLUS_VERIFY_TASKS_LOCK:
        task = _PLUS_VERIFY_TASKS.get(task_id)
        if not task:
            return JSONResponse({"ok": False, "message": "未找到 Plus 校验任务"}, status_code=404)
        return _plus_task_snapshot(task)


@router.post("/accounts-bulk/verify-plus/{task_id}/cancel")
async def verify_plus_batch_cancel(task_id: str):
    with _PLUS_VERIFY_TASKS_LOCK:
        task = _PLUS_VERIFY_TASKS.get(task_id)
        if not task:
            return JSONResponse({"ok": False, "message": "未找到 Plus 校验任务"}, status_code=404)
        task["cancelled"] = True
        cancel_event = task.get("cancel_event")
        if cancel_event:
            cancel_event.set()
        task["message"] = "正在取消…等待运行中的请求结束"
        return _plus_task_snapshot(task)


@router.post("/accounts-bulk/check-health")
async def check_health_batch(req: VerifyPlusBatchRequest, svc: AccountHealthService = Depends(get_account_health_service)):
    keys = [str(key or "").strip() for key in req.keys if str(key or "").strip()]
    if not keys:
        return JSONResponse({"ok": False, "message": "请至少选择一个账号"}, status_code=400)
    return await run_in_threadpool(svc.check_batch, keys)


@router.post("/accounts/{key}/check-health")
async def check_health(key: str, svc: AccountHealthService = Depends(get_account_health_service)):
    payload, status_code = await run_in_threadpool(svc.check_account, key)
    if status_code != 200:
        return JSONResponse(payload, status_code=status_code)
    return payload


@router.post("/accounts/verify-plus")
async def verify_plus_batch_legacy(req: VerifyPlusBatchRequest, svc: AccountsService = Depends(get_accounts_service)):
    keys = [str(key or "").strip() for key in req.keys if str(key or "").strip()]
    if not keys:
        return JSONResponse({"ok": False, "message": "请至少选择一个账号"}, status_code=400)
    return await run_in_threadpool(svc.verify_plus_batch, keys, proxy_region=req.proxy_region)

@router.post("/accounts-bulk/refresh-access-token")
def refresh_access_token_batch(req: VerifyPlusBatchRequest, browser_svc: BrowserSessionService = Depends(get_browser_session_service)):
    keys = [str(key or "").strip() for key in req.keys if str(key or "").strip()]
    if not keys:
        return JSONResponse({"ok": False, "message": "请至少选择一个账号"}, status_code=400)
    results = []
    for key in keys:
        account = account_store.get_account(key)
        if not account:
            results.append({"key": key, "ok": False, "message": "未找到账号"})
            continue
        payload, status_code = browser_svc.refresh_access_token_for_account(account)
        results.append({"key": key, "status_code": status_code, **payload})
    return {"ok": all(item.get("ok") for item in results), "refreshed": sum(1 for item in results if item.get("ok")), "results": results}



@router.post("/accounts/{key}/verify-plus")
async def verify_plus(key: str, req: VerifyPlusRequest = VerifyPlusRequest(), svc: AccountsService = Depends(get_accounts_service)):
    payload, status_code = await run_in_threadpool(svc.verify_plus, key, proxy_region=req.proxy_region)
    if status_code != 200:
        return JSONResponse(payload, status_code=status_code)
    return payload




@router.post("/accounts/{key}/resume-oauth")
async def resume_oauth(key: str, req: ResumeOAuthRequest,
                       svc: AccountsService = Depends(get_accounts_service),
                       tasks_svc: TasksService = Depends(get_tasks_service)):
    payload, status_code = await run_in_threadpool(svc.resume_oauth, key, req.headed, tasks_svc, oauth_callback_mode=req.oauth_callback_mode, cpa_base_url=req.cpa_base_url, cpa_management_key=req.cpa_management_key, sms_provider=req.sms_provider, sms_phone_url=req.sms_phone_url, sms_country=req.sms_country, sms_service=req.sms_service, bind_sms_provider=req.bind_sms_provider, bind_sms_phone_url=req.bind_sms_phone_url, bind_sms_country=req.bind_sms_country, bind_sms_service=req.bind_sms_service, bind_country_code=req.bind_country_code)
    if status_code != 200:
        return JSONResponse(payload, status_code=status_code)
    return payload

@router.post("/accounts/{key}/protocol-bind")
async def protocol_bind(key: str, req: ResumeOAuthRequest,
                        svc: AccountsService = Depends(get_accounts_service),
                        tasks_svc: TasksService = Depends(get_tasks_service)):
    payload, status_code = await run_in_threadpool(svc.protocol_cpa_bind, key, tasks_svc, oauth_callback_mode=req.oauth_callback_mode or "cpa", cpa_base_url=req.cpa_base_url, cpa_management_key=req.cpa_management_key, sms_provider=req.sms_provider, sms_phone_url=req.sms_phone_url, sms_country=req.sms_country, sms_service=req.sms_service, bind_sms_provider=req.bind_sms_provider, bind_sms_phone_url=req.bind_sms_phone_url, bind_sms_country=req.bind_sms_country, bind_sms_service=req.bind_sms_service, bind_country_code=req.bind_country_code)
    if status_code != 200:
        return JSONResponse(payload, status_code=status_code)
    return payload


def check_plus(key: str) -> tuple[dict, int]:
    account = account_store.get_account(key)
    if not account:
        return {"ok": False, "message": "未找到账号"}, 404
    return {"ok": True, "message": "Plus 检查已并入 resume-oauth 流程。", "account": account}, 200



@router.get("/accounts/{key}/export")
async def export_product(key: str, fields: list[str] = Query(default=[]),
                         svc: AccountsService = Depends(get_accounts_service)):
    product = await run_in_threadpool(svc.export_product, key, fields)
    if not product:
        return JSONResponse({"ok": False, "message": "未找到账号"}, status_code=404)
    return {"ok": True, "product": product}


@router.post("/accounts/{key}/export")
async def export_product_selected(key: str, req: ExportAccountRequest,
                                  svc: AccountsService = Depends(get_accounts_service)):
    product = await run_in_threadpool(svc.export_product, key, req.fields)
    if not product:
        return JSONResponse({"ok": False, "message": "未找到账号"}, status_code=404)
    return {"ok": True, "product": product}

@router.post("/accounts-bulk/export")
async def export_products_selected(req: ExportAccountsRequest,
                                   svc: AccountsService = Depends(get_accounts_service)):
    keys = [str(key or "").strip() for key in req.keys if str(key or "").strip()]
    if not keys:
        return JSONResponse({"ok": False, "message": "请至少选择一个账号"}, status_code=400)
    result = await run_in_threadpool(svc.export_products, keys, req.fields)
    return {"ok": True, **result}


@router.post("/accounts-bulk/export-plus-txt")
async def export_plus_products_txt(req: ExportPlusProductsRequest,
                                   svc: AccountsService = Depends(get_accounts_service)):
    keys = [str(key or "").strip() for key in (req.keys or []) if str(key or "").strip()]
    result = await run_in_threadpool(
        svc.export_plus_products_txt,
        keys or None,
        only_verified=bool(req.only_verified),
        archive_after_export=bool(req.archive_after_export),
    )
    if int(result.get("count") or 0) <= 0:
        return JSONResponse({
            "ok": False,
            "message": "没有可导出的 Plus 成品号",
            **{k: v for k, v in result.items() if k != "ok"},
        }, status_code=400)
    return result

@router.post("/accounts-bulk/export-at-txt")
async def export_at_products_txt(req: ExportAtProductsRequest,
                                 svc: AccountsService = Depends(get_accounts_service)):
    keys = [str(key or "").strip() for key in (req.keys or []) if str(key or "").strip()]
    result = await run_in_threadpool(
        svc.export_at_products_txt,
        keys or None,
        archive_after_export=bool(req.archive_after_export),
    )
    if int(result.get("count") or 0) <= 0:
        return JSONResponse({
            "ok": False,
            "message": "没有可导出的 AT 账号（需有 access_token 与邮箱四段）",
            **{k: v for k, v in result.items() if k != "ok"},
        }, status_code=400)
    return result

@router.post("/accounts-bulk/activate-plus")
async def activate_plus_batch(req: ActivatePlusBatchRequest):
    keys = [str(key or "").strip() for key in (req.keys or []) if str(key or "").strip()]
    if not keys:
        return JSONResponse({"ok": False, "message": "请至少选择一个账号"}, status_code=400)
    provider = str(req.provider or "upi").strip().lower() or "upi"
    if provider != "upi":
        return JSONResponse({"ok": False, "message": f"暂不支持激活 provider: {provider}"}, status_code=400)
    from services.plus_activation_batch_service import get_plus_activation_batch_service
    svc = get_plus_activation_batch_service()
    result = await run_in_threadpool(
        svc.create_batch,
        keys,
        channel=str(req.channel or "upi"),
        dry_run=False,
    )
    status = 200 if result.get("ok") or int(result.get("accepted") or result.get("queued") or 0) > 0 else 400
    if status >= 400:
        return JSONResponse(result, status_code=status)
    return result


@router.post("/plus-activation/batches")
async def create_plus_activation_batch(req: PlusActivationBatchCreateRequest):
    from services.plus_activation_batch_service import get_plus_activation_batch_service
    svc = get_plus_activation_batch_service()
    result = await run_in_threadpool(
        svc.create_batch,
        [str(key or "").strip() for key in (req.keys or []) if str(key or "").strip()],
        name=req.name,
        channel=req.channel,
        dry_run=bool(req.dry_run),
        submit_rate_per_min=req.submit_rate_per_min,
        max_in_flight=req.max_in_flight,
    )
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return result


@router.get("/plus-activation/batches")
async def list_plus_activation_batches(status: str = "active", limit: int = 50, offset: int = 0):
    from services.plus_activation_batch_service import get_plus_activation_batch_service
    svc = get_plus_activation_batch_service()
    return await run_in_threadpool(svc.list_batches, status=status, limit=limit, offset=offset)


@router.get("/plus-activation/batches/{batch_key}")
async def get_plus_activation_batch(batch_key: str):
    from services.plus_activation_batch_service import get_plus_activation_batch_service
    svc = get_plus_activation_batch_service()
    result = await run_in_threadpool(svc.get_batch, batch_key)
    if not result.get("ok"):
        return JSONResponse(result, status_code=404)
    return result


@router.get("/plus-activation/batches/{batch_key}/items")
async def list_plus_activation_batch_items(
    batch_key: str,
    status: str = "",
    search: str = "",
    error: str = "",
    include_exported: bool = True,
    limit: int = 80,
    offset: int = 0,
):
    from services.plus_activation_batch_service import get_plus_activation_batch_service
    svc = get_plus_activation_batch_service()
    return await run_in_threadpool(
        svc.list_items,
        batch_key,
        status=status,
        search=search,
        error=error,
        include_exported=include_exported,
        limit=limit,
        offset=offset,
    )


@router.post("/plus-activation/batches/{batch_key}/refresh")
async def refresh_plus_activation_batch(batch_key: str):
    from services.plus_activation_batch_service import get_plus_activation_batch_service
    svc = get_plus_activation_batch_service()
    result = await run_in_threadpool(svc.refresh, batch_key)
    if not result.get("ok"):
        return JSONResponse(result, status_code=404)
    return result


@router.post("/plus-activation/batches/{batch_key}/retry")
async def retry_plus_activation_batch(batch_key: str, req: PlusActivationBatchActionRequest):
    from services.plus_activation_batch_service import get_plus_activation_batch_service
    svc = get_plus_activation_batch_service()
    result = await run_in_threadpool(svc.retry_items, batch_key, keys=req.keys, statuses=req.statuses or None, channel=req.channel)
    status = 200 if result.get("ok") else 400
    if status >= 400:
        return JSONResponse(result, status_code=status)
    return result


@router.post("/plus-activation/batches/{batch_key}/release")
async def release_plus_activation_batch(batch_key: str, req: PlusActivationBatchActionRequest):
    from services.plus_activation_batch_service import get_plus_activation_batch_service
    svc = get_plus_activation_batch_service()
    result = await run_in_threadpool(svc.release_items, batch_key, keys=req.keys, statuses=req.statuses or None)
    status = 200 if result.get("ok") else 400
    if status >= 400:
        return JSONResponse(result, status_code=status)
    return result

@router.post("/plus-activation/batches/{batch_key}/show-accounts")
async def show_plus_activation_batch_accounts(batch_key: str, req: PlusActivationBatchActionRequest):
    from services.plus_activation_batch_service import get_plus_activation_batch_service
    svc = get_plus_activation_batch_service()
    result = await run_in_threadpool(svc.show_accounts_in_account_list, batch_key, keys=req.keys or None)
    status = 200 if result.get("ok") else 400
    if status >= 400:
        return JSONResponse(result, status_code=status)
    return result


@router.post("/plus-activation/batches/{batch_key}/export-plus")
async def export_plus_activation_batch(batch_key: str, req: PlusActivationBatchExportRequest):
    from services.plus_activation_batch_service import get_plus_activation_batch_service
    svc = get_plus_activation_batch_service()
    result = await run_in_threadpool(
        svc.export_plus,
        batch_key,
        fmt=req.format,
        include_already_exported=bool(req.include_already_exported),
        archive_after_export=bool(req.archive_after_export),
    )
    status = 200 if result.get("ok") else 400
    if status >= 400:
        return JSONResponse(result, status_code=status)
    return result


@router.post("/plus-activation/batches/{batch_key}/archive")
async def archive_plus_activation_batch(batch_key: str, req: PlusActivationBatchActionRequest):
    from services.plus_activation_batch_service import get_plus_activation_batch_service
    svc = get_plus_activation_batch_service()
    result = await run_in_threadpool(svc.archive_batch, batch_key, force=bool(req.force))
    status = 200 if result.get("ok") else 400
    if status >= 400:
        return JSONResponse(result, status_code=status)
    return result


@router.get("/plus-activation/exports/{export_key}/download")
async def download_plus_activation_export(export_key: str):
    from services.plus_activation_batch_service import PROJECT_ROOT
    from infrastructure.repositories.plus_activation_repository import PlusActivationRepository
    export = await run_in_threadpool(PlusActivationRepository().get_export, export_key)
    if not export:
        return JSONResponse({"ok": False, "message": "导出文件不存在"}, status_code=404)
    file_path = PROJECT_ROOT / str(export.get("file_path") or "")
    if not file_path.exists() or not file_path.is_file():
        return JSONResponse({"ok": False, "message": "导出文件不存在"}, status_code=404)
    return FileResponse(str(file_path), filename=str(export.get("file_name") or file_path.name))

@router.get("/activation/tasks")
async def list_activation_tasks(status: str = "", limit: int = 100000):
    from services.upi_activation_service import get_upi_activation_service
    svc = get_upi_activation_service()
    statuses = [value.strip() for value in str(status or "").split(",") if value.strip()]
    return await run_in_threadpool(svc.list_activation_tasks, statuses=statuses or None, limit=limit)


@router.post("/activation/tasks/refresh")
async def refresh_activation_tasks(req: ActivationTasksRefreshRequest):
    from services.upi_activation_service import get_upi_activation_service
    svc = get_upi_activation_service()
    result = await run_in_threadpool(svc.refresh_activation_tasks, req.keys, statuses=req.statuses)
    status = 200 if result.get("ok") else 400
    if status >= 400:
        return JSONResponse(result, status_code=status)
    return result


@router.post("/activation/tasks/retry")
async def retry_activation_tasks(req: ActivationTasksRetryRequest):
    from services.upi_activation_service import get_upi_activation_service
    svc = get_upi_activation_service()
    result = await run_in_threadpool(svc.retry_activation_tasks, req.keys, statuses=req.statuses, channel=req.channel)
    status = 200 if result.get("ok") or int(result.get("accepted") or result.get("queued") or 0) > 0 else 400
    if status >= 400:
        return JSONResponse(result, status_code=status)
    return result


@router.post("/accounts-bulk/activation/release")
async def release_activation_batch(req: ActivatePlusBatchRequest):
    """Batch cancel local UPI queue / release remote tasks to free client-key capacity."""
    keys = [str(key or "").strip() for key in (req.keys or []) if str(key or "").strip()]
    if not keys:
        return JSONResponse({"ok": False, "message": "请至少选择一个账号", "released": 0, "failed": 0, "results": []}, status_code=400)
    from services.upi_activation_service import get_upi_activation_service
    svc = get_upi_activation_service()
    result = await run_in_threadpool(svc.release_accounts, keys)
    status = 200 if int(result.get("released") or 0) > 0 else 400
    if status >= 400:
        return JSONResponse(result, status_code=status)
    return result



@router.post("/accounts/{key}/activation/release")
async def release_activation(key: str):
    from services.upi_activation_service import get_upi_activation_service
    svc = get_upi_activation_service()
    payload, status_code = await run_in_threadpool(svc.release_account, key)
    if status_code >= 400:
        return JSONResponse(payload, status_code=status_code)
    return payload


@router.post("/accounts/{key}/activation/cancel")
async def cancel_activation(key: str):
    from services.upi_activation_service import get_upi_activation_service
    svc = get_upi_activation_service()
    payload, status_code = await run_in_threadpool(svc.cancel_account, key)
    if status_code >= 400:
        return JSONResponse(payload, status_code=status_code)
    return payload


@router.post("/activation/client-key/issue")
async def issue_activation_client_key(req: IssueClientKeyRequest):
    from services.upi_activation_service import get_upi_activation_service
    svc = get_upi_activation_service()
    payload, status_code = await run_in_threadpool(
        svc.issue_client_key,
        req.cdk,
        note=req.note,
        rotate=bool(req.rotate),
    )
    if status_code >= 400:
        return JSONResponse(payload, status_code=status_code)
    return payload

@router.get("/activation/queue-stats")
async def activation_queue_stats():
    from services.upi_activation_service import get_upi_activation_service
    svc = get_upi_activation_service()
    return await run_in_threadpool(svc.queue_stats)



def token_summary(account: dict) -> dict:
    """Return presence-only credential flags for account list responses."""
    tokens = account.get("tokens") if isinstance(account.get("tokens"), dict) else {}

    def has_token(name: str) -> bool:
        return bool(_token_string(tokens.get(name) or account.get(name))) or bool(tokens.get(f"has_{name}"))

    return {
        "access_token": has_token("access_token"),
        "refresh_token": has_token("refresh_token"),
        "id_token": has_token("id_token"),
    }


def _token_string(value: object) -> str:
    if isinstance(value, str):
        text = value.strip()
        if text and text.lower() not in {"true", "false"}:
            return text
    return ""


@router.get("/accounts/{key}/tokens")
async def reveal_tokens(key: str):
    account = await run_in_threadpool(account_store.get_account, key)
    if not account:
        return JSONResponse({"ok": False, "message": "未找到账号"}, status_code=404)
    raw_tokens = account.get("tokens") if isinstance(account.get("tokens"), dict) else {}
    return {"ok": True, "tokens": dict(raw_tokens)}

@router.post("/accounts/{key}/refresh-access-token")
def refresh_account_access_token(key: str, req: RefreshAccessTokenRequest, browser_svc: BrowserSessionService = Depends(get_browser_session_service)):
    account = account_store.get_account(key)
    if not account:
        return JSONResponse({"ok": False, "message": "未找到账号"}, status_code=404)
    payload, status_code = browser_svc.refresh_access_token_for_account(account, use_saved_proxy=req.use_saved_proxy, save_storage=req.save_storage)
    if status_code >= 400:
        return JSONResponse(payload, status_code=status_code)
    return payload

@router.post("/accounts/{key}/bind-billing-email")
def bind_billing_email(key: str, req: BillingEmailBindRequest, tasks_svc: TasksService = Depends(get_tasks_service)):
    account = account_store.get_account(key)
    if not account:
        return JSONResponse({"ok": False, "message": "未找到账号"}, status_code=404)
    resume_file = str(req.resume_file or account.get("resume_file") or "").strip()
    if not resume_file:
        return JSONResponse({"ok": False, "message": "该账号缺少 resume_file，无法启动账单邮箱绑定"}, status_code=400)
    try:
        task = tasks_svc.start_billing_email_bind({
            "resume_file": resume_file,
            "headed": req.headed,
            "mailbox_provider": req.mailbox_provider,
            "proxy_region": req.proxy_region,
            "lajiao_proxy_credential_protocol": req.lajiao_proxy_credential_protocol,
        })
    except Exception as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=500)
    return {"ok": True, "task": task}

@router.post("/accounts-bulk/bind-billing-email")
def bind_billing_email_batch(req: BillingEmailBindBatchRequest, tasks_svc: TasksService = Depends(get_tasks_service)):
    keys = [str(key or "").strip() for key in req.keys if str(key or "").strip()]
    if not keys:
        return JSONResponse({"ok": False, "message": "请至少选择一个账号"}, status_code=400)
    results = []
    for key in keys:
        account = account_store.get_account(key)
        if not account:
            results.append({"ok": False, "key": key, "message": "未找到账号"})
            continue
        resume_file = str(account.get("resume_file") or "").strip()
        if not resume_file:
            results.append({"ok": False, "key": key, "message": "缺少 resume_file"})
            continue
        try:
            task = tasks_svc.start_billing_email_bind({
                "resume_file": resume_file,
                "headed": req.headed,
                "mailbox_provider": req.mailbox_provider,
                "proxy_region": req.proxy_region,
                "lajiao_proxy_credential_protocol": req.lajiao_proxy_credential_protocol,
            })
            results.append({"ok": True, "key": key, "task": task})
        except Exception as exc:
            results.append({"ok": False, "key": key, "message": str(exc)})
    return {"ok": all(item.get("ok") for item in results), "started": sum(1 for item in results if item.get("ok")), "results": results}


@router.get("/account-browser-sessions")
async def list_browser_sessions(browser_svc: BrowserSessionService = Depends(get_browser_session_service)):
    return await run_in_threadpool(browser_svc.list_sessions)


@router.post("/accounts/{key}/open-browser")
async def open_account_browser(key: str, req: OpenBrowserRequest, browser_svc: BrowserSessionService = Depends(get_browser_session_service)):
    account = await run_in_threadpool(account_store.get_account, key)
    if not account:
        return JSONResponse({"ok": False, "message": "未找到账号"}, status_code=404)
    payload, status_code = await run_in_threadpool(
        browser_svc.open_for_account,
        account,
        target_url=req.target_url,
        use_saved_proxy=req.use_saved_proxy,
        engine=req.browser_engine,
        headed=req.headed,
        save_on_close=req.save_on_close,
    )
    if status_code >= 400:
        return JSONResponse(payload, status_code=status_code)
    return payload


@router.post("/account-browser-sessions/{session_id}/save")
async def save_browser_session(session_id: str, browser_svc: BrowserSessionService = Depends(get_browser_session_service)):
    payload, status_code = await run_in_threadpool(browser_svc.save_session, session_id)
    if status_code >= 400:
        return JSONResponse(payload, status_code=status_code)
    return payload


@router.post("/account-browser-sessions/{session_id}/close")
async def close_browser_session(session_id: str, req: CloseBrowserSessionRequest, browser_svc: BrowserSessionService = Depends(get_browser_session_service)):
    payload, status_code = await run_in_threadpool(browser_svc.close_session, session_id, save=req.save)
    if status_code >= 400:
        return JSONResponse(payload, status_code=status_code)
    return payload


@router.get("/accounts/{key}/artifact/{artifact_type}")
async def artifact(key: str, artifact_type: str):
    def read_artifact() -> dict:
        account = account_store.get_account(key)
        if not account:
            return {"missing_account": True}
        paths = account.get("paths") if isinstance(account.get("paths"), dict) else {}
        artifact_path_str = str(paths.get(artifact_type) or "")
        if not artifact_path_str:
            return {"missing_artifact": True}
        file_path = Path(artifact_path_str)
        if not file_path.is_absolute():
            file_path = PROJECT_ROOT / file_path
        if not file_path.exists() or not file_path.is_file():
            return {"missing_file": True}
        return {"ok": True, "artifact_type": artifact_type, "path": str(file_path), "content": file_path.read_text(encoding="utf-8", errors="replace")}

    payload = await run_in_threadpool(read_artifact)
    if payload.pop("missing_account", False):
        return JSONResponse({"ok": False, "message": "未找到账号"}, status_code=404)
    if payload.pop("missing_artifact", False):
        return JSONResponse({"ok": False, "message": "未找到产物"}, status_code=404)
    if payload.pop("missing_file", False):
        return JSONResponse({"ok": False, "message": "产物文件不存在"}, status_code=404)
    return payload
