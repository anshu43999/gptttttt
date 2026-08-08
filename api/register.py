"""Registration API — FastAPI router."""
from __future__ import annotations

import logging
import threading
import uuid
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Any, Optional

from api.deps import get_tasks_service
from application.tasks_service import TasksService

router = APIRouter()
logger = logging.getLogger(__name__)

_BULK_CREATE_LOCK = threading.Lock()


class RegisterRequest(BaseModel):
    mode: str = "phone"
    registration_engine: str = "simulated"
    sms_provider: str = "herosms_api"
    sms_country: str = "BR"
    mailbox_provider: str = "icloud_api"
    proxy_mode: str = "credentials"
    proxy_region: str = "auto"
    lajiao_proxy_credential_protocol: str = "auto"
    lajiao_proxy_skip_check: bool = False
    codex_protocol_idle_timeout_seconds: Optional[int] = None
    codex_fetch_timeout_ms: Optional[int] = None
    codex_max_phone_tries: Optional[int] = None
    codex_protocol_skip_proxy_preflight: Optional[bool] = None
    codex_protocol_proxy_attempts: Optional[int] = None
    auto_bind_billing_email: bool = False
    billing_email_provider: str = "icloud_api"
    headed: bool = True
    skip_precheck: bool = False
    force_signup: bool = False
    register_count: int = 1
    register_threads: int = 1

    browser_engine: Optional[str] = None
    browser_channel: Optional[str] = None
    browser_profile_mode: Optional[str] = None
    browser_no_viewport: Optional[bool] = None
    email_register_flow: Optional[str] = None
    locale: Optional[str] = None
    timezone_id: Optional[str] = None
    accept_language: Optional[str] = None
    email_otp_timeout: Optional[int] = None
    email_otp_poll_interval: Optional[int] = None
    mailat_protocol_use_local_bridge: Optional[bool] = None
    mailat_protocol_timeout_seconds: Optional[int] = None
    mailat_protocol_proxy_attempts: Optional[int] = None
    mailat_protocol_proxy_preflight_timeout_seconds: Optional[int] = None
    mailat_protocol_proxy_precheck_enabled: Optional[bool] = None
    email_protocol_backend: Optional[str] = None
    go_email_protocol_url: Optional[str] = None
    go_email_protocol_timeout_seconds: Optional[int] = None


class OAuthBindRequest(BaseModel):
    mailbox_provider: str = "icloud_api"
    headed: bool = True


# ── sync utility (importable by tests) ──

def config_overrides(data: dict) -> dict:
    """Build config overrides from frontend form data. Always synchronous, importable."""
    overrides: dict = {}
    if data.get("registration_engine"):
        overrides["registration_engine"] = str(data.get("registration_engine") or "").strip().lower()

    mode = str(data.get("mode") or "").strip().lower()
    mailbox_provider = str(data.get("mailbox_provider") or "").strip()
    if mailbox_provider:
        overrides["mailbox_provider"] = mailbox_provider

    if mode in {"email", "email_phone", "email-register-token"}:
        overrides.setdefault("mailbox_provider", mailbox_provider or "icloud_api")
        overrides.setdefault("email_register_flow", str(data.get("email_register_flow") or "fast"))
        overrides.setdefault("browser_engine", str(data.get("browser_engine") or "patchright"))
        overrides.setdefault("browser_profile_mode", str(data.get("browser_profile_mode") or "per_task"))
        overrides.setdefault("browser_no_viewport", bool(data.get("browser_no_viewport", True)))
        overrides.setdefault("locale", str(data.get("locale") or "ja-JP"))
        overrides.setdefault("timezone_id", str(data.get("timezone_id") or "Asia/Tokyo"))
        overrides.setdefault("accept_language", str(data.get("accept_language") or "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7"))
        overrides.setdefault("email_otp_timeout", int(data.get("email_otp_timeout") or 200))
        overrides.setdefault("email_otp_poll_interval", int(data.get("email_otp_poll_interval") or 3))

    if mode in {"email_protocol", "email-protocol-register-token"}:
        overrides["registration_engine"] = "email_protocol"
        overrides.setdefault("mailbox_provider", mailbox_provider or "icloud_api")
        overrides.setdefault("email_otp_timeout", int(data.get("email_otp_timeout") or 200))
        overrides.setdefault("email_otp_poll_interval", int(data.get("email_otp_poll_interval") or 3))
        # Prefer explicit request, then config/db default, then python.
        backend_raw = str(
            data.get("email_protocol_backend")
            or data.get("protocol_backend")
            or ""
        ).strip().lower()
        if not backend_raw:
            # leave empty so task config inherits file/db default (now go)
            pass
        elif backend_raw in {"go", "golang", "go_worker", "go_daemon"}:
            overrides["email_protocol_backend"] = "go"
        else:
            overrides["email_protocol_backend"] = "python"
        if data.get("go_email_protocol_url") not in {None, ""}:
            overrides["go_email_protocol_url"] = str(data.get("go_email_protocol_url") or "").strip()
        if data.get("go_email_protocol_timeout_seconds") not in {None, ""}:
            overrides["go_email_protocol_timeout_seconds"] = int(data.get("go_email_protocol_timeout_seconds"))
        overrides["mailat_protocol_use_local_bridge"] = bool(data.get("mailat_protocol_use_local_bridge", data.get("codex_protocol_use_local_bridge", False)))
        for key in ("mailat_protocol_timeout_seconds", "mailat_protocol_proxy_attempts", "mailat_protocol_proxy_preflight_timeout_seconds", "mailat_protocol_proxy_precheck_enabled"):
            if data.get(key) not in {None, ""}:
                overrides[key] = data.get(key)
    for key in ("browser_engine", "browser_channel", "browser_profile_mode", "email_register_flow", "locale", "timezone_id", "accept_language"):
        if data.get(key) not in {None, ""}:
            overrides[key] = data.get(key)
    if data.get("browser_no_viewport") is not None:
        overrides["browser_no_viewport"] = bool(data.get("browser_no_viewport"))
    for key in ("email_otp_timeout", "email_otp_poll_interval"):
        if data.get(key) not in {None, ""}:
            overrides[key] = int(data.get(key))

    sms_provider = str(data.get("sms_provider") or data.get("sms_mode") or "herosms_api").strip().lower()
    if sms_provider in {"herosms", "herosms_api"}:
        overrides["sms_provider"] = "herosms_api"
        overrides["sms_phone_url"] = ""
    elif sms_provider in {"smsbower", "smsbower_api"}:
        overrides["sms_provider"] = "smsbower_api"
        overrides["sms_phone_url"] = ""
    elif sms_provider == "user_phone_url":
        overrides["sms_provider"] = "user_phone_url"
        if data.get("sms_phone_url"):
            overrides["sms_phone_url"] = str(data.get("sms_phone_url") or "")
    if data.get("auto_bind_billing_email"):
        overrides["auto_bind_billing_email_after_register"] = True
        overrides["billing_email_provider"] = str(data.get("billing_email_provider") or "icloud_api").strip() or "icloud_api"
    if data.get("icloud_api_order_file"):
        overrides["icloud_api_order_file"] = str(data.get("icloud_api_order_file") or "")
    if data.get("icloud_api_order_text"):
        overrides["icloud_api_order_text"] = str(data.get("icloud_api_order_text") or "")
    phone_country = str(data.get("phone_country") or data.get("sms_country") or "").upper()
    if phone_country == "BR":
        overrides.update({"country_code": "55", "country_name": "Brazil", "sms_country": "73"})
        if sms_provider in {"herosms", "herosms_api"}:
            overrides.update({"herosms_fixed_price": False, "herosms_max_price": 0.0999})
    elif phone_country == "US":
        overrides.update({"country_code": "1", "country_name": "United States", "sms_country": "1"})
    elif phone_country and phone_country.isdigit():
        overrides["sms_country"] = phone_country

    proxy_mode = str(data.get("proxy_mode") or "api").lower()
    if proxy_mode in {"credentials", "credential"}:
        overrides["lajiao_proxy_mode"] = "credentials"
        if data.get("lajiao_proxy_credential_protocol"):
            overrides["lajiao_proxy_credential_protocol"] = str(data.get("lajiao_proxy_credential_protocol") or "").strip().lower()
        else:
            overrides.setdefault("lajiao_proxy_credential_protocol", "http")
        if data.get("lajiao_credentials"):
            overrides["lajiao_proxy_credentials"] = str(data.get("lajiao_credentials") or "")
        overrides["lajiao_proxy_api_url"] = ""
        if data.get("lajiao_proxy_skip_check"):
            overrides["lajiao_proxy_skip_check"] = True
    else:
        overrides["lajiao_proxy_mode"] = "api"
    proxy_country = str(data.get("proxy_country") or data.get("proxy_region") or "").strip().upper()
    if proxy_country in {"AUTO", "ZONE", "ZONE_AUTO"}:
        # AUTO: no forced country; seed session will still get a default region at mint time if needed.
        overrides.update({"proxy_region": "", "lajiao_proxy_region": "", "lajiao_proxy_regions": "", "lajiao_proxy_expected_country": ""})
    elif proxy_country:
        overrides["proxy_region"] = proxy_country
        overrides["lajiao_proxy_regions"] = proxy_country
        overrides["lajiao_proxy_expected_country"] = proxy_country
        overrides["lajiao_proxy_region"] = proxy_country
    for key in ("codex_protocol_idle_timeout_seconds", "codex_fetch_timeout_ms", "codex_max_phone_tries", "codex_protocol_skip_proxy_preflight", "codex_protocol_proxy_attempts"): 
        if data.get(key) not in {None, ""}:
            overrides[key] = data.get(key)
    overrides["force_signup_from_login_password"] = bool(data.get("force_signup"))
    overrides["precheck_phone_before_sms"] = not bool(data.get("skip_precheck"))
    return overrides

def _email_protocol_backend_is_go(payload: dict, overrides: dict | None = None) -> bool:
    """True unless the request/config explicitly selects python/mailat/node."""
    ov = overrides or {}
    raw = str(
        ov.get("email_protocol_backend")
        or payload.get("email_protocol_backend")
        or payload.get("protocol_backend")
        or ""
    ).strip().lower()
    if raw in {"python", "mailat", "node", "py"}:
        return False
    if raw in {"go", "golang", "go_worker", "go_daemon"}:
        return True
    # Empty inherits dashboard default (go). Treat as Go for the product hot path.
    return True


def _start_registration_task(svc: TasksService, method_name: str, payload: dict, overrides: dict) -> dict:
    method = getattr(svc, method_name)
    try:
        return method(payload, overrides, defer_start=True)
    except TypeError as exc:
        if "defer_start" not in str(exc):
            raise
        return method(payload, overrides)


def _registration_method_name(payload: dict) -> str:
    mode = str(payload.get("mode") or "").strip().lower()
    engine = str(payload.get("registration_engine") or "").strip().lower()
    if mode in {"email_protocol", "email-protocol-register-token"}:
        return "start_email_protocol_register"
    if mode in {"email", "email_phone", "email-register-token"}:
        return "start_email_register"
    if mode in {"phone_protocol", "protocol-register-token"} or engine in {"protocol", "post", "http"}:
        return "start_protocol_register"
    return "start_register"


def _create_remaining_tasks(
    svc: TasksService,
    method_name: str,
    payload: dict,
    overrides: dict,
    remaining: int,
) -> None:
    """Create bulk tasks off the request path so the UI is not blocked."""
    created = 0
    try:
        bulk_create = getattr(svc, "start_email_protocol_register_many", None)
        if method_name == "start_email_protocol_register" and callable(bulk_create):
            created = int(bulk_create(payload, overrides, remaining) or 0)
            return
        for index in range(remaining):
            try:
                _start_registration_task(svc, method_name, payload, overrides)
                created += 1
            except Exception:
                logger.exception("bulk register create failed at remaining index %s", index)
            # Start work while the rest are still being enqueued.
            if created == 1 or created % 10 == 0:
                if hasattr(svc, "drain_queue_async"):
                    svc.drain_queue_async()
                elif hasattr(svc, "drain_queue"):
                    svc.drain_queue()
    finally:
        if hasattr(svc, "drain_queue_async"):
            svc.drain_queue_async()
        elif hasattr(svc, "drain_queue"):
            svc.drain_queue()
        logger.info(
            "bulk register background create finished method=%s created=%s remaining=%s batch_id=%s",
            method_name,
            created,
            remaining,
            overrides.get("batch_id"),
        )


# ── FastAPI routes ──

@router.post("/register", status_code=201)
def start_register(req: RegisterRequest, svc: TasksService = Depends(get_tasks_service)):
    payload = req.model_dump()
    overrides = config_overrides(payload)
    # No practical product cap — user controls volume/concurrency.
    count = max(1, int(req.register_count or 1))
    threads = max(1, int(req.register_threads or 1))
    batch_id = f"batch_{uuid.uuid4().hex[:12]}"
    overrides["batch_id"] = batch_id
    if hasattr(svc, "set_max_parallel"):
        svc.set_max_parallel(max(getattr(svc, "max_parallel", 1), threads))
    elif hasattr(svc, "max_parallel"):
        svc.max_parallel = max(int(getattr(svc, "max_parallel", 1) or 1), threads)
    if hasattr(svc, "bucket_limits"):
        svc.bucket_limits["register"] = max(int(svc.bucket_limits.get("register", 0) or 0), threads)
        # Keep global ceiling >= register bucket so queued jobs actually start.
        if hasattr(svc, "set_max_parallel"):
            svc.set_max_parallel(max(getattr(svc, "max_parallel", threads), threads))

    # The request's thread count is the operator's intended batch concurrency.
    # Persist it so later settings reloads / background drain threads do not fall
    # back to stale DB values such as max_register_tasks=2 mid-batch.
    config_svc = getattr(svc, "config_service", None)
    if config_svc is not None and hasattr(config_svc, "save_overrides"):
        try:
            config_svc.save_overrides({
                "max_parallel_tasks": max(int(getattr(svc, "max_parallel", threads) or threads), threads),
                "max_register_tasks": threads,
            })
            if hasattr(svc, "reload_limits"):
                svc.reload_limits()
                if hasattr(svc, "set_max_parallel"):
                    svc.set_max_parallel(max(getattr(svc, "max_parallel", threads), threads))
                if hasattr(svc, "bucket_limits"):
                    svc.bucket_limits["register"] = max(int(svc.bucket_limits.get("register", 0) or 0), threads)
        except Exception:
            logger.exception("failed to persist requested register concurrency")
    # Stamp concurrency into this request so Go batch max_concurrent does not
    # depend on a racing DB reload of max_register_tasks.
    overrides["max_register_tasks"] = threads
    overrides["max_parallel_tasks"] = max(int(overrides.get("max_parallel_tasks") or 0), threads)
    overrides["register_threads"] = threads
    method_name = _registration_method_name(payload)
    # Email-protocol + Go: whole batch goes to Go worker in one shot.
    # Never create a Python first-task shell (that path used defer_start and skipped Go batch).
    if method_name == "start_email_protocol_register" and _email_protocol_backend_is_go(payload, overrides):
        bulk_create = getattr(svc, "start_email_protocol_register_many", None)
        if not callable(bulk_create):
            raise RuntimeError("email protocol Go batch requires start_email_protocol_register_many")
        created = int(bulk_create(payload, overrides, count) or 0)
        if created <= 0:
            raise RuntimeError(
                "Go batch registration returned 0 tasks "
                "(is email-protocol-worker pure-go with email-register-batches?)"
            )
        # Prefer a real dashboard task id from the Go-created batch for UI polling.
        run_id = ""
        try:
            from services.go_registration_batch import get_go_registration_batch

            cfg = {}
            config_svc = getattr(svc, "config_service", None)
            if config_svc is not None and hasattr(config_svc, "merged_config"):
                try:
                    cfg = dict(config_svc.merged_config() or {})
                except Exception:
                    cfg = {}
            cfg.update(overrides or {})
            view = get_go_registration_batch(batch_id, cfg)
            ids = view.get("task_ids") if isinstance(view, dict) else None
            if isinstance(ids, list) and ids:
                run_id = str(ids[0] or "").strip()
        except Exception:
            logger.exception("failed to resolve go batch run_id batch_id=%s", batch_id)
        if not run_id:
            run_id = batch_id
        return {
            "ok": True,
            "run_id": run_id,
            "batch_id": batch_id,
            "task": {"id": run_id, "status": "running", "type": "email-protocol-register-token", "go_managed": True},
            "count": count,
            "accepted": count,
            "created": created,
            "creating": 0,
            "async_create": False,
            "go_managed": True,
            "threads": threads,
            "message": f"已提交 Go 批量注册 {created}/{count}（批次 {batch_id}，并发 {threads}）",
        }

    # Create the first task on the request path so the UI gets a real run_id immediately.
    first = _start_registration_task(svc, method_name, payload, overrides)
    remaining = count - 1
    if remaining > 0:
        # Serialize bulk creators lightly so overlapping 500-batches don't thrash the DB.
        def _runner() -> None:
            with _BULK_CREATE_LOCK:
                _create_remaining_tasks(svc, method_name, payload, overrides, remaining)

        threading.Thread(target=_runner, name="bulk-register-create", daemon=True).start()
    else:
        if hasattr(svc, "drain_queue_async"):
            svc.drain_queue_async()
        elif hasattr(svc, "drain_queue"):
            svc.drain_queue()

    return {
        "ok": True,
        "run_id": first["id"],
        "batch_id": batch_id,
        "task": first,
        "count": count,
        "accepted": count,
        "created": 1,
        "creating": remaining,
        "async_create": remaining > 0,
        "threads": threads,
        "message": (
            f"已接收 {count} 个注册任务（批次 {batch_id}），后台创建中"
            if remaining > 0
            else f"注册任务已创建（批次 {batch_id}）"
        ),
    }




@router.get("/register/{run_id}/status")
def register_status(run_id: str, since_id: int = 0, svc: TasksService = Depends(get_tasks_service)):
    task = svc.get_task(run_id)
    if not task.get("id"):
        return {"status": "not_found"}
    status_map = {"succeeded": "complete", "failed": "failed", "cancelled": "cancelled", "queued": "queued", "pending": "queued", "running": "running", "interrupted": "interrupted"}
    events = svc.task_events(run_id, since_id)
    return {"run_id": run_id, "status": status_map.get(str(task.get("status") or ""), str(task.get("status") or "unknown")), "stage": task.get("status"), "message": task.get("error") or "", "steps_completed": [event.get("message", "") for event in events if event.get("event_type") in {"started", "succeeded", "failed", "cancelled"}], "task": task}


@router.post("/register/{run_id}/cancel")
def cancel_register(run_id: str, svc: TasksService = Depends(get_tasks_service)):
    return {"ok": svc.stop(run_id)}


