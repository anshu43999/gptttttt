from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.config_loader import load_config
from platforms.chatgpt.utils import generate_random_password
from registration.email_register import EmailRegistrationOrchestrator
from services.mailat_email_protocol_runner import run_mailat_email_protocol
from services.go_email_protocol_runner import normalize_email_protocol_backend, run_go_email_protocol


def _cookie_expires(value: Any) -> int:
    if value in {None, "", "Infinity"}:
        return -1
    if isinstance(value, (int, float)):
        return int(value / 1000) if value > 10_000_000_000 else int(value)
    text = str(value).strip()
    if not text or text.lower() == "infinity":
        return -1
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except Exception:
        return -1


def _playwright_same_site(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if text == "strict":
        return "Strict"
    if text == "lax":
        return "Lax"
    if text in {"none", "no_restriction"}:
        return "None"
    return None


def _mailat_session_to_storage_state(payload: dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload.get("cookies"), list):
        return payload
    jar = payload.get("cookieJar") if isinstance(payload.get("cookieJar"), dict) else {}
    jar_cookies = jar.get("cookies") if isinstance(jar.get("cookies"), list) else []
    cookies: list[dict[str, Any]] = []
    for raw in jar_cookies:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("key") or raw.get("name") or "").strip()
        domain = str(raw.get("domain") or "").strip()
        if not name or not domain:
            continue
        cookie = {
            "name": name,
            "value": str(raw.get("value") or ""),
            "domain": domain,
            "path": str(raw.get("path") or "/"),
            "expires": _cookie_expires(raw.get("expires")),
            "httpOnly": bool(raw.get("httpOnly")),
            "secure": bool(raw.get("secure")),
        }
        same_site = _playwright_same_site(raw.get("sameSite"))
        if same_site:
            cookie["sameSite"] = same_site
        cookies.append(cookie)
    return {"cookies": cookies, "origins": []}

class _ProtocolStorageSession:
    def __init__(self, storage_state_path: str):
        self.storage_state_path = storage_state_path

    def save_storage_state(self, path: str) -> str:
        if not self.storage_state_path:
            return ""
        source = Path(self.storage_state_path)
        target = Path(path)
        if source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                payload = json.loads(source.read_text(encoding="utf-8")) or {}
                if isinstance(payload, dict):
                    storage_state = _mailat_session_to_storage_state(payload)
                    target.write_text(json.dumps(storage_state, ensure_ascii=False, indent=2), encoding="utf-8")
                elif source.resolve() != target.resolve():
                    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            except Exception:
                if source.resolve() != target.resolve():
                    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            self.storage_state_path = str(target)
        return str(self.storage_state_path)


def run(config_path: str, *, task_id: str = "") -> dict[str, Any]:
    config = load_config(config_path)
    task_id = task_id or str(config.get("dashboard_task_id") or f"email_protocol_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    provider_key = str(config.get("mailbox_provider") or "outlook_token").strip().lower()
    orchestrator = EmailRegistrationOrchestrator(log_fn=print)
    orchestrator._result = {
        "success": False,
        "status": "running",
        "stage": "running",
        "steps": [],
        "task_id": task_id,
        "registration_task_id": task_id,
        "registration_mode": "email_protocol",
        "binding_status": "not_ready",
    }
    if provider_key == "outlook_token":
        leased_email = str(config.get("outlook_email") or "").strip()
        leases = config.get("resource_leases") if isinstance(config.get("resource_leases"), list) else []
        has_email_lease = any(
            isinstance(item, dict)
            and str(item.get("type") or "") == "email"
            and str(item.get("provider") or "") == "outlook_token"
            and str(item.get("key") or "").strip()
            for item in leases
        )
        if config.get("_resources_prepared") and (not leased_email or not has_email_lease):
            raise RuntimeError(
                "邮箱协议任务未租到独占 Outlook token 邮箱（资源池可能已耗尽），"
                "拒绝回退到 order 文件共享邮箱，避免 Go admission mailbox 冲突。"
            )
    mailbox, account = orchestrator._select_mailbox_account(provider_key, config)
    password = str(config.get("chatgpt_password") or "").strip() or generate_random_password(16)

    before_ids: set[str] = set()
    if provider_key != "outlook_token" and hasattr(mailbox, "get_current_ids"):
        try:
            # poualiis top=1 only exposes the latest mail. Capturing it as baseline makes the
            # subsequent otp_callback treat the real OpenAI code as already-seen and hang.
            extra = getattr(account, "extra", None) or {}
            mail_url = str(extra.get("mail_url") or "").lower()
            is_poualiis_slot = ("/api/mails" in mail_url or "/api/imap/mails" in mail_url) and "recipient=" in mail_url
            if is_poualiis_slot:
                before_ids = set()
                print("邮箱协议注册验证码等待基线: 0 封历史邮件 (poualiis latest-mail API)")
            else:
                before_ids = set(mailbox.get_current_ids(account) or set())
                print(f"邮箱协议注册验证码等待基线: {len(before_ids)} 封历史邮件")
        except Exception as exc:
            print(f"邮箱协议注册验证码等待基线获取失败: {str(exc).splitlines()[0][:160]}")

    # OTP wait anchors: bind time window to first challenge request (≈ after S8),
    # and reject previously submitted codes when worker re-parks for recovery.
    otp_not_before: datetime | None = None
    otp_reject_codes: set[str] = set()

    def otp_callback() -> str:
        nonlocal otp_not_before
        timeout = int(config.get("email_otp_timeout", 200) or 200)
        if otp_not_before is None:
            # Slight skew before this callback — OpenAI mail may already be in flight.
            otp_not_before = datetime.now(timezone.utc) - timedelta(seconds=45)
        code = orchestrator._wait_email_code(
            mailbox,
            account,
            timeout=timeout,
            before_ids=before_ids,
            not_before=otp_not_before,
            reject_codes=set(otp_reject_codes),
        )
        if code:
            otp_reject_codes.add(str(code).strip())
            # Next recovery wait should only accept mail after this attempt.
            otp_not_before = datetime.now(timezone.utc) - timedelta(seconds=15)
        if code and hasattr(mailbox, "get_current_ids"):
            try:
                before_ids.update(set(mailbox.get_current_ids(account) or set()))
            except Exception:
                pass
        return code

    backend = normalize_email_protocol_backend(config.get("email_protocol_backend") or config.get("protocol_backend"))
    print("=" * 60)
    if backend == "go":
        print("Step: 邮箱协议注册（Go email-protocol-worker）")
    else:
        print("Step: 邮箱协议注册（项目内置 Mailat/Node 运行时）")
    print("=" * 60)
    print(f"协议后端: {backend}")
    print(f"邮箱: {account.email}")
    if backend == "go":
        result = run_go_email_protocol(
            config,
            email=account.email,
            password=password,
            otp_callback=otp_callback,
            task_id=task_id,
            log=print,
        )
        step_name = "go_email_protocol_register"
    else:
        result = run_mailat_email_protocol(
            config,
            email=account.email,
            password=password,
            otp_callback=otp_callback,
            task_id=task_id,
            log=print,
        )
        step_name = "mailat_email_protocol_register"
    orchestrator._result.update(
        {
            "success": True,
            "status": "email_protocol_registered",
            "stage": "manual_plus_required",
            "steps": [step_name],
            "registration_status": "registered",
            "email": result.get("email") or account.email,
            "password": password,
            "account_id": result.get("account_id", ""),
            "plan_type": result.get("plan_type", "free"),
            "access_token": result.get("access_token", ""),
            "chatgpt_access_token_initial": result.get("access_token", ""),
            "registration_proxy": result.get("registration_proxy", ""),
            "registration_proxy_exit_ip": result.get("registration_proxy_exit_ip", ""),
            "access_token_file": result.get("access_token_file", ""),
            "protocol_runner": result.get("protocol_runner", ""),
            "protocol_work_dir": result.get("protocol_work_dir", ""),
            "protocol_backend": result.get("protocol_backend") or backend,
        }
    )
    if result.get("access_token"):
        orchestrator._populate_claims(str(result.get("access_token") or ""), fallback_email=account.email)
    session = _ProtocolStorageSession(str(result.get("protocol_session_state_path") or ""))
    registered_file = orchestrator._save_registered_account_json(config, session)  # type: ignore[arg-type]
    resume_file = orchestrator._save_manual_plus_handoff_json(config, session)  # type: ignore[arg-type]
    orchestrator._result["registered_file"] = str(registered_file)
    orchestrator._result["resume_file"] = str(resume_file)
    record = dict(orchestrator._result)
    orchestrator._mark_mailbox_used(mailbox, account.email, reason="email_protocol_registered")
    print("=" * 60)
    print("执行完成")
    print("=" * 60)
    print("  状态: email_protocol_registered")
    print("  成功: True")
    print(f"  邮箱: {record.get('email', '')}")
    print(f"  账号ID: {record.get('account_id', '')}")
    print(f"  交接文件: {record.get('resume_file')}")
    print(f"  注册文件: {record.get('registered_file')}")
    print(f"  文本文件: {record.get('text_file')}")
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run project-local Mailat email protocol registration")
    parser.add_argument("--config", required=True)
    parser.add_argument("--task-id", default="")
    args = parser.parse_args(argv)
    try:
        run(args.config, task_id=args.task_id)
        return 0
    except Exception as exc:
        print(f"流水线异常: {exc}")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
