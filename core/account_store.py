from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
from infrastructure import db

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = PROJECT_ROOT / "data"
ACCOUNTS_ROOT = DATA_ROOT / "accounts"
OUTPUT_ROOT = PROJECT_ROOT / "output"


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def safe_key(value: str, fallback: str = "account") -> str:
    raw = str(value or "").strip() or fallback
    raw = raw.replace("auth0|", "auth0_")
    safe = re.sub(r"[^A-Za-z0-9_.@+-]+", "_", raw).strip("_")
    return safe or fallback


def account_key(record: dict[str, Any]) -> str:
    return safe_key(
        record.get("account_key")
        or record.get("account_id")
        or record.get("email")
        or record.get("phone_number")
        or record.get("resume_id")
        or record.get("created_at")
        or "account"
    )


def read_json(path: str | Path) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_json(path: str | Path, data: dict[str, Any]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def copy_if_exists(src: str | Path, dst: str | Path) -> str:
    source = Path(src)
    if not source.exists():
        return ""
    target = Path(dst)
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() != target.resolve():
        shutil.copy2(source, target)
    return str(target)


def _public_stage(stage: str, status: str = "") -> str:
    stage = str(stage or "").strip()
    status = str(status or "").strip()
    if stage == "complete" or status == "complete":
        return "complete"
    if stage == "failed" or status == "error":
        return "failed"
    if stage == "manual_plus_required":
        return "manual_plus_required"
    if stage == "registered":
        return "registered"
    return stage or status or "unknown"


def _looks_like_timestamp_key(value: str) -> bool:
    text = str(value or "").strip()
    return text.startswith("202") and ("T" in text or "_" in text) and "@" not in text and "+" not in text


def has_account_identity(source: dict[str, Any]) -> bool:
    account = str(source.get("account_key") or "").strip()
    return bool(
        (account and not _looks_like_timestamp_key(account))
        or str(source.get("account_id") or "").strip()
        or str(source.get("email") or "").strip()
        or str(source.get("outlook_email") or "").strip()
        or str(source.get("phone_number") or "").strip()
    )


def is_identityless_failed_run(source: dict[str, Any]) -> bool:
    stage = str(source.get("stage") or "").strip().lower()
    status = str(source.get("status") or "").strip().lower()
    return (stage == "failed" or status in {"failed", "error", "sms_code_pending"}) and not has_account_identity(source)



def normalize_record(source: dict[str, Any], *, source_file: str = "") -> dict[str, Any]:
    stage = _public_stage(str(source.get("stage") or ""), str(source.get("status") or ""))
    key = account_key(source)
    paths = source.get("paths") if isinstance(source.get("paths"), dict) else {}
    record = {
        "schema_version": 2,
        "account_key": key,
        "stage": stage,
        "status": str(source.get("status") or stage),
        "created_at": str(source.get("created_at") or source.get("completed_at") or now_iso()),
        "updated_at": now_iso(),
        "phone_number": str(source.get("phone_number") or ""),
        "email": str(source.get("email") or source.get("outlook_email") or ""),
        "billing_email": str(source.get("billing_email") or ""),
        "codex_email": str(source.get("codex_email") or ""),
        "password": str(source.get("password") or source.get("generated_chatgpt_password") or ""),
        "account_id": str(source.get("account_id") or source.get("chatgpt_account_id") or ""),
        "plan_type": str(source.get("plan_type") or source.get("plan_type_before_activation") or ""),
        "registration_mode": str(source.get("registration_mode") or source.get("mode") or ("email" if source.get("email") and not source.get("phone_number") else "phone" if source.get("phone_number") else "")),
        "login_identifier": str(source.get("login_identifier") or source.get("email") or source.get("outlook_email") or source.get("phone_number") or key),
        "registration_status": str(source.get("registration_status") or ("failed" if stage == "failed" else "archived" if stage == "archived" else "registered" if source.get("account_id") or source.get("email") or source.get("phone_number") else "unknown")),
        "registration_task_id": str(source.get("registration_task_id") or source.get("task_id") or ""),
        "registration_started_at": str(source.get("registration_started_at") or source.get("started_at") or ""),
        "registration_completed_at": str(source.get("registration_completed_at") or source.get("completed_at") or source.get("finished_at") or ""),
        "registration_error": str(source.get("registration_error") or (source.get("failure") or {}).get("reason") if isinstance(source.get("failure"), dict) else ""),
        "display_name": str(source.get("display_name") or source.get("nickname") or source.get("name") or ""),
        "plus_status": str(source.get("plus_status") or ("verified_plus" if str(source.get("plan_type") or "").lower() in {"plus", "pro", "team", "business", "enterprise", "paid"} else "needs_plus" if str(source.get("stage") or source.get("status") or "") in {"manual_plus_required", "email_registered", "registered"} else "")),
        "plus_verified_at": str(source.get("plus_verified_at") or ""),
        "plus_check_source": str(source.get("plus_check_source") or ""),
        "plus_check_error": str(source.get("plus_check_error") or ""),
        "binding_status": str(source.get("binding_status") or ("cpa_submitted" if str(source.get("stage") or source.get("status") or "") == "cpa_bound" else "bound" if str(source.get("stage") or source.get("status") or "") == "complete" else "not_ready")),
        "binding_task_id": str(source.get("binding_task_id") or ""),
        "binding_provider": str(source.get("binding_provider") or ""),
        "binding_phone_number": str(source.get("binding_phone_number") or ""),
        "binding_completed_at": str(source.get("binding_completed_at") or ""),
        "binding_started_at": str(source.get("binding_started_at") or ""),
        "oauth_callback_mode": str(source.get("oauth_callback_mode") or ""),
        "cpa_base_url": str(source.get("cpa_base_url") or ""),
        "cpa_submitted_at": str(source.get("cpa_submitted_at") or ""),
        "cpa_submit_status": str(source.get("cpa_submit_status") or ""),
        "cpa_submit_error": str(source.get("cpa_submit_error") or ""),
        "registration_phone_resource_id": int(source.get("registration_phone_resource_id") or 0),
        "binding_phone_resource_id": int(source.get("binding_phone_resource_id") or 0),
        "email_resource_id": int(source.get("email_resource_id") or 0),
        "proxy_resource_id": int(source.get("proxy_resource_id") or 0),
        "registration_proxy_exit_ip": str(source.get("registration_proxy_exit_ip") or ""),
        "registration_proxy_region": str(source.get("registration_proxy_region") or ""),
        "resume_file": str(source.get("resume_file") or ""),
        "storage_file": str(source.get("browser_storage_state_path") or source.get("storage_file") or ""),
        "account_file": str(source_file or source.get("account_file") or ""),
        "binding_error": str(source.get("binding_error") or ""),
        "activation_id": str(source.get("activation_id") or ""),
        "resume_id": str(source.get("resume_id") or ""),
        "paths": {
            "source": source_file,
            "registered": str(source.get("registered_file") or paths.get("registered") or ""),
            "resume": str(source.get("resume_file") or paths.get("resume") or ""),
            "storage_state": str(source.get("browser_storage_state_path") or paths.get("storage_state") or ""),
            "product": str(paths.get("product") or ""),
            "tokens": str(paths.get("tokens") or ""),
            "debug_log": str(source.get("debug_log_file") or paths.get("debug_log") or ""),
        },
        "proxy": {
            "registration_proxy": str(source.get("registration_proxy") or ((source.get("proxy") or {}).get("registration_proxy") if isinstance(source.get("proxy"), dict) else "")),
            "registration_exit_ip": str(source.get("registration_proxy_exit_ip") or ((source.get("proxy") or {}).get("registration_exit_ip") if isinstance(source.get("proxy"), dict) else "")),
            "subscription_check_proxy": str(source.get("subscription_check_proxy") or ((source.get("proxy") or {}).get("subscription_check_proxy") if isinstance(source.get("proxy"), dict) else "")),
        },
        "tokens": {
            "access_token": str(source.get("access_token") or ""),
            "refresh_token": str(source.get("refresh_token") or ""),
            "id_token": str(source.get("id_token") or ""),
            "chatgpt_access_token_initial": str(source.get("chatgpt_access_token_initial") or source.get("access_token") or ""),
            "has_access_token": bool(source.get("access_token")),
            "has_refresh_token": bool(source.get("refresh_token")),
            "has_id_token": bool(source.get("id_token")),
            "has_initial_access_token": bool(source.get("chatgpt_access_token_initial") or source.get("access_token")),
        },
        "manual_plus": {
            "required": stage in {"registered", "manual_plus_required"},
            "confirmed": stage == "complete" or str(source.get("manual_plus_status") or "") == "confirmed",
            "url": str(source.get("manual_plus_url") or "https://plus.iceaix.com/"),
        },
        "failure": {
            "reason": str(source.get("failure_reason") or ""),
            "failed_step": str(source.get("failed_step") or ""),
            "retryable": bool(source.get("retryable")),
        },
    }
    email = str(record.get("email") or "").strip().lower()
    codex_email = str(record.get("codex_email") or "").strip().lower()
    billing_email = str(record.get("billing_email") or "").strip().lower()
    if billing_email and (billing_email == email or billing_email == codex_email):
        record["billing_email"] = ""
    if codex_email and codex_email == email:
        record["codex_email"] = ""
    return record


def account_dir(key: str) -> Path:
    return ACCOUNTS_ROOT / safe_key(key)


def upsert_account(source: dict[str, Any], *, source_file: str = "", copy_artifacts: bool = True) -> dict[str, Any]:
    """Persist normalized account data to SQLite.

    JSON files under data/accounts are legacy read-only fallback now. Runtime
    writes go to the database; JSON is produced only by explicit export or by
    pipeline handoff artifacts such as resume files. Failed runs without an
    account identity stay as task/failed-run artifacts and are not account rows.
    """
    if is_identityless_failed_run(source):
        return {}
    record = normalize_record(source, source_file=source_file)
    existing = db.get_account(record["account_key"])
    if existing:
        merged = existing | record
        merged["paths"] = (existing.get("paths") or {}) | (record.get("paths") or {})
        merged["proxy"] = (existing.get("proxy") or {}) | (record.get("proxy") or {})
        merged["tokens"] = (existing.get("tokens") or {}) | (record.get("tokens") or {})
        record = merged
        record["updated_at"] = now_iso()
    if "sms" not in source:
        record.pop("sms", None)
    raw_tokens = {}
    for source_key, token_key in (("access_token", "access_token"), ("refresh_token", "refresh_token"), ("id_token", "id_token"), ("chatgpt_access_token_initial", "chatgpt_access_token_initial")):
        if source.get(source_key):
            raw_tokens[token_key] = source.get(source_key)
    if raw_tokens:
        record["raw_tokens"] = raw_tokens
    db.upsert_account(record)
    event_type = "registration_failed" if record.get("registration_status") == "failed" or record.get("stage") == "failed" else "registration_succeeded"
    db.add_account_event(
        record["account_key"],
        event_type,
        task_id=str(record.get("registration_task_id") or source.get("task_id") or ""),
        status=str(record.get("registration_status") or record.get("stage") or ""),
        message=str(record.get("registration_error") or record.get("last_error") or ""),
        payload={"source_file": source_file, "stage": record.get("stage"), "plan_type": record.get("plan_type")},
    )
    return db.get_account(record["account_key"]) or record


def append_event(key: str, event: dict[str, Any]) -> None:
    target = account_dir(key) / "events.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"ts": now_iso(), **event}
    with target.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def list_accounts(*, refresh_legacy: bool = True) -> list[dict[str, Any]]:
    if refresh_legacy:
        import_legacy_outputs(copy_artifacts=False)
    db_items = db.list_accounts()
    if db_items:
        return db_items
    items: list[dict[str, Any]] = []
    if not ACCOUNTS_ROOT.exists():
        return []
    for path in ACCOUNTS_ROOT.glob("*/account.json"):
        data = read_json(path)
        if data:
            items.append(data)
    return sorted(items, key=lambda x: str(x.get("updated_at") or x.get("created_at") or ""), reverse=True)


def get_account(key: str) -> dict[str, Any]:
    return db.get_account(key) or read_json(account_dir(key) / "account.json")


def import_legacy_outputs(*, copy_artifacts: bool = False) -> int:
    count = 0
    patterns = [
        (OUTPUT_ROOT / "registered_accounts", "*.json"),
        (OUTPUT_ROOT / "products", "*.json"),
        (OUTPUT_ROOT / "failed_runs", "*.json"),
        (OUTPUT_ROOT, "resume_*.json"),
    ]
    for directory, pattern in patterns:
        if not directory.exists():
            continue
        for path in directory.glob(pattern):
            data = read_json(path)
            if not data or is_identityless_failed_run(data):
                continue
            if directory.name == "products":
                data.setdefault("paths", {})["product"] = str(path)
            if path.name.startswith("resume_"):
                data.setdefault("resume_file", str(path))
            upsert_account(data, source_file=str(path), copy_artifacts=copy_artifacts)
            count += 1
    try:
        db.init_db()
    except Exception:
        pass
    return count


def summary() -> dict[str, Any]:
    accounts = list_accounts(refresh_legacy=True)
    stages: dict[str, int] = {}
    plans: dict[str, int] = {}
    for item in accounts:
        stages[str(item.get("stage") or "unknown")] = stages.get(str(item.get("stage") or "unknown"), 0) + 1
        plans[str(item.get("plan_type") or "unknown")] = plans.get(str(item.get("plan_type") or "unknown"), 0) + 1
    return {"total": len(accounts), "stages": stages, "plans": plans, "updated_at": now_iso()}


def product_export(account: dict[str, Any]) -> dict[str, Any]:
    tokens = account.get("tokens") if isinstance(account.get("tokens"), dict) else {}
    # list_accounts() may expose tokens as {has_access_token: bool, ...} only.
    # Never treat those boolean flags as credential values.
    def _token(name: str) -> str:
        raw = tokens.get(name)
        if isinstance(raw, str) and raw and not raw.lower() in {"true", "false"}:
            return raw
        top = account.get(name)
        if isinstance(top, str) and top:
            return top
        return ""

    proxy = account.get("proxy") if isinstance(account.get("proxy"), dict) else {}
    sms = account.get("sms") if isinstance(account.get("sms"), dict) else {}
    paths = account.get("paths") if isinstance(account.get("paths"), dict) else {}
    export = {
        "schema_version": 2,
        "account_key": account.get("account_key", ""),
        "account_id": account.get("account_id", ""),
        "platform": account.get("platform", "chatgpt"),
        "login_identifier": account.get("login_identifier") or account.get("email") or account.get("phone_number") or account.get("account_key", ""),
        "phone_number": account.get("phone_number", ""),
        "email": account.get("email", ""),
        "password": account.get("password", ""),
        "display_name": account.get("display_name", ""),
        "registration_mode": account.get("registration_mode", ""),
        "registration_status": account.get("registration_status", ""),
        "registration_task_id": account.get("registration_task_id", ""),
        "registration_started_at": account.get("registration_started_at", ""),
        "registration_completed_at": account.get("registration_completed_at", ""),
        "registration_error": account.get("registration_error", ""),
        "plan_type": account.get("plan_type", ""),
        "plus_status": account.get("plus_status", ""),
        "plus_verified_at": account.get("plus_verified_at", ""),
        "plus_check_source": account.get("plus_check_source", ""),
        "plus_check_error": account.get("plus_check_error", ""),
        "binding_status": account.get("binding_status", ""),
        "binding_task_id": account.get("binding_task_id", ""),
        "binding_provider": account.get("binding_provider", ""),
        "binding_phone_number": account.get("binding_phone_number", ""),
        "binding_started_at": account.get("binding_started_at", ""),
        "binding_completed_at": account.get("binding_completed_at", ""),
        "binding_error": account.get("binding_error", ""),
        "oauth_callback_mode": account.get("oauth_callback_mode", ""),
        "cpa_base_url": account.get("cpa_base_url", ""),
        "cpa_submitted_at": account.get("cpa_submitted_at", ""),
        "cpa_submit_status": account.get("cpa_submit_status", ""),
        "cpa_submit_error": account.get("cpa_submit_error", ""),
        "access_token": _token("access_token"),
        "refresh_token": _token("refresh_token"),
        "id_token": _token("id_token"),
        "chatgpt_access_token_initial": _token("chatgpt_access_token_initial"),
        "cpa_auth_file_name": account.get("cpa_auth_file_name", ""),
        "cpa_auth_file_json": account.get("cpa_auth_file_json", ""),
        "cpa_synced_at": account.get("cpa_synced_at", ""),
        "cpa_sync_error": account.get("cpa_sync_error", ""),
        "token_expires_at": _token("token_expires_at"),
        "oauth_result": account.get("oauth_result") if isinstance(account.get("oauth_result"), dict) else {
            "access_token": _token("access_token"),
            "refresh_token": _token("refresh_token"),
            "id_token": _token("id_token"),
        },
        "proxy": proxy,
        "sms": sms,
        "resume_file": account.get("resume_file") or paths.get("resume", ""),
        "storage_file": account.get("storage_file") or paths.get("storage_state", ""),
        "account_file": account.get("account_file") or paths.get("source", ""),
        "created_at": account.get("created_at", ""),
        "updated_at": account.get("updated_at", ""),
        "completed_at": account.get("binding_completed_at") or account.get("registration_completed_at") or account.get("updated_at") or now_iso(),
        "stage": account.get("stage", ""),
        "status": account.get("status", ""),
        "last_error": account.get("last_error", ""),
    }
    return export
