from __future__ import annotations

import json
import os
import re
import subprocess
import time
from shutil import copyfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from services.mailat_protocol_runtime import PROJECT_ROOT, validate_mailat_protocol_runtime
from services.mailat_email_protocol_runner import (
    _bool_value,
    _first,
    _int_value,
    _float_value,
    _mailat_config,
    _mask_secret,
    _proxy_url,
    _select_proxy_url,
)

from services.task_runtime import terminate_process_tree
from registration.email_register import EmailRegistrationOrchestrator
from core.mailbox.outlook_token import OutlookTokenMailbox

TASK_TMP_ROOT = PROJECT_ROOT / "tmp" / "mailat_protocol_bind_tasks"


def _country_code(config: dict[str, Any]) -> str:
    raw = str(config.get("bind_country_code") or config.get("country_code") or "").strip()
    return re.sub(r"\D+", "", raw)


def _binding_sms_country(config: dict[str, Any]) -> str:
    raw = str(config.get("bind_sms_country") or config.get("sms_country") or config.get("smsbower_country") or "").strip()
    normalized = raw.upper()
    if normalized == "BR":
        return "73"
    return re.sub(r"\D+", "", raw)

def _binding_sms_entry(config: dict[str, Any]) -> str:
    return _first(
        config.get("bind_sms_phone_url")
        or config.get("bind_sms_phone_urls")
        or config.get("sms_phone_url")
        or config.get("sms_phone_urls")
    )


def _binding_provider(config: dict[str, Any]) -> str:
    return str(config.get("bind_sms_provider") or config.get("sms_provider") or "").strip().lower()


def _apply_binding_config(mailat_config: dict[str, Any], config: dict[str, Any]) -> None:
    provider = _binding_provider(config)
    phone_entry = _binding_sms_entry(config)
    phone_url_provider = provider in {"user_phone_url", "bind_user_phone_url", "phone_url", "manual_phone_url"}
    if provider:
        mailat_config["gptRegisterSmsProvider"] = provider
    mailat_config["gptRegisterBindSmsPhoneUrl"] = phone_entry if phone_url_provider else ""
    country_code = _country_code(config)
    if country_code:
        mailat_config["gptRegisterCountryCode"] = country_code
    service = str(config.get("bind_sms_service") or config.get("sms_service") or "dr").strip() or "dr"
    mailat_config["gptRegisterSmsService"] = service

    if provider in {"herosms", "hero_sms", "herosms_api"}:
        hero_key = str(config.get("sms_api_key") or config.get("heroSMSApiKey") or "").strip()
        if hero_key:
            mailat_config["heroSMSApiKey"] = hero_key
        return

    if provider in {"smsbower", "smsbower_api"}:
        smsbower_key = str(config.get("bind_sms_api_key") or config.get("smsbower_api_key") or "").strip()
        if not smsbower_key:
            raise RuntimeError("SMSBower 绑定缺少 smsbower_api_key")
        country = _binding_sms_country(config)
        if not country:
            raise RuntimeError("SMSBower 绑定缺少巴西国家代码（BR/73）")
        mailat_config["smsbowerApiKey"] = smsbower_key
        mailat_config["smsbowerService"] = service
        mailat_config["smsbowerCountry"] = _int_value(country, 73)
        mailat_config["smsbowerMinPrice"] = _float_value(config.get("smsbower_min_price"), -1)
        mailat_config["smsbowerMaxPrice"] = _float_value(config.get("smsbower_max_price"), -1)
        mailat_config["smsbowerProviderIds"] = str(config.get("smsbower_provider_ids") or "").strip()


def _parse_stdout(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        if "[POOL-RESULT]" in line:
            result["pool_result"] = line.strip()
        if "CPA status=" in line:
            result["cpa_status"] = line.split("CPA status=", 1)[1].strip()
        phone_match = re.search(r"(?:phone=\+|phone=)(\d{6,20})", line)
        if phone_match:
            result["binding_phone_number"] = f"+{phone_match.group(1)}"
        if "callback:" in line:
            result["callback_seen"] = "1"
    return result


def normalize_oauth_callback_mode(value: Any) -> str:
    return "local" if str(value or "").strip().lower() == "local" else "cpa"


def _mailbox_provider_key(config: dict[str, Any]) -> str:
    provider = str(config.get("mailbox_provider") or "outlook_token").strip().lower()
    return {
        "icloud": "icloud_privacy",
        "icloud_hide_my_email": "icloud_privacy",
        "icloud_private": "icloud_privacy",
        "cloudflare_worker": "cfworker_admin_api",
        "cloud_mail": "cfworker_admin_api",
        "email_link_api": "icloud_api",
        "link_api": "icloud_api",
    }.get(provider, provider)


def _existing_email_otp_callback(
    config: dict[str, Any],
    *,
    email: str,
    log: Callable[[str], None],
) -> Callable[[], str]:
    provider = _mailbox_provider_key(config)
    if provider not in {"outlook_token", "icloud_privacy", "icloud_api"}:
        raise RuntimeError(f"协议绑定无法为已有账号读取邮箱验证码: mailbox_provider={provider}")

    mailbox_config = dict(config)
    mailbox_config["email"] = email
    mailbox_config["outlook_email"] = email
    mailbox_config["icloud_privacy_email"] = email
    mailbox_config["icloud_api_email"] = email
    orchestrator = EmailRegistrationOrchestrator(log_fn=log)
    if provider == "outlook_token":
        mailbox = OutlookTokenMailbox(mailbox_config, log_fn=log)
        account = mailbox.first(email, include_used=True)
    else:
        mailbox, account = orchestrator._select_mailbox_account(provider, mailbox_config)
    account_email = str(getattr(account, "email", "") or "").strip()
    if account_email.lower() != email.lower():
        raise RuntimeError("协议绑定邮箱资源与目标账号不匹配")

    before_ids: set[str] = set()
    if hasattr(mailbox, "get_current_ids"):
        try:
            before_ids = set(mailbox.get_current_ids(account) or set())
        except Exception:
            before_ids = set()
    timeout_seconds = max(30, _int_value(config.get("email_otp_timeout"), 300))

    def fetch_otp() -> str:
        nonlocal before_ids
        code = orchestrator._wait_email_code(
            mailbox,
            account,
            timeout=timeout_seconds,
            before_ids=before_ids,
        ).strip()
        if not code:
            raise RuntimeError("协议绑定未获取到账号邮箱验证码")
        if hasattr(mailbox, "get_current_ids"):
            try:
                before_ids = set(mailbox.get_current_ids(account) or set())
            except Exception:
                pass
        return code

    return fetch_otp


def _load_local_oauth_result(task_dir: Path) -> tuple[Path, dict[str, Any]]:
    auth_dir = task_dir / "auth"
    auth_files = sorted(path for path in auth_dir.glob("*.json") if path.is_file()) if auth_dir.is_dir() else []
    if len(auth_files) != 1:
        raise RuntimeError(f"协议本地 OAuth 需要唯一 auth JSON，实际找到 {len(auth_files)} 个: {auth_dir}")
    auth_file = auth_files[0]
    try:
        payload = json.loads(auth_file.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"协议本地 OAuth auth JSON 读取失败: {auth_file}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"协议本地 OAuth auth JSON 不是对象: {auth_file}")
    missing = [name for name in ("access_token", "refresh_token", "id_token") if not str(payload.get(name) or "").strip()]
    if missing:
        raise RuntimeError(f"协议本地 OAuth auth JSON 缺少 token: {', '.join(missing)}")
    return auth_file, payload


def run_mailat_protocol_cpa_bind(
    config: dict[str, Any],
    *,
    email: str,
    password: str,
    otp_callback: Callable[[], str] | None = None,
    task_id: str = "",
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    callback_mode = normalize_oauth_callback_mode(config.get("oauth_callback_mode"))
    runtime = validate_mailat_protocol_runtime()
    tsx = runtime.tsx
    entry = runtime.entry
    if not email:
        raise RuntimeError("协议绑定缺少账号邮箱")
    if not password:
        raise RuntimeError("协议绑定缺少账号密码")
    cpa_base = str(config.get("cpa_base_url") or "").strip()
    cpa_key = str(config.get("cpa_management_key") or "").strip()
    if callback_mode == "cpa" and (not cpa_base or not cpa_key):
        raise RuntimeError("协议 CPA 绑定缺少 cpa_base_url / cpa_management_key")
    otp_callback = otp_callback or _existing_email_otp_callback(config, email=email, log=log)

    task_name = task_id or f"mailat_bind_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    task_dir = TASK_TMP_ROOT / task_name
    task_dir.mkdir(parents=True, exist_ok=True)
    proxy_runtime = None
    selected_proxy_url = _proxy_url(config)
    runtime_proxy_url = selected_proxy_url
    proxy_exit_ip = ""
    try:
        if selected_proxy_url:
            selected_proxy_url, runtime_proxy_url, proxy_exit_ip, proxy_runtime = _select_proxy_url(selected_proxy_url, config, log, task_id=task_id)
            log(f"[mailat-bind] proxy={runtime_proxy_url} exit_ip={proxy_exit_ip or '-'}")
        mailat_config = _mailat_config(config, email=email, password=password, proxy_url=runtime_proxy_url)
        _apply_binding_config(mailat_config, config)
        (task_dir / "config.json").write_text(json.dumps(mailat_config, ensure_ascii=False, indent=2), encoding="utf-8")
        copyfile(runtime.sdk, task_dir / "sdk.js")
        log(
            "[mailat-bind] config_summary "
            f"mode={callback_mode} "
            f"email={email} "
            f"sms_provider={mailat_config.get('gptRegisterSmsProvider') or ''} "
            f"bind_phone_url={'present' if mailat_config.get('gptRegisterBindSmsPhoneUrl') else 'missing'} "
            f"country_code={mailat_config.get('gptRegisterCountryCode') or ''} "
            f"cpa_base={cpa_base if callback_mode == 'cpa' else ''} "
            f"cpa_key={'present:' + _mask_secret(cpa_key) if callback_mode == 'cpa' and cpa_key else 'missing'}"
        )
        if callback_mode == "local":
            command = [
                str(tsx),
                str(entry),
                "--auth",
                "--email",
                email,
                "--otp",
            ]
            command_flags = "--auth --email *** --otp"
        else:
            command = [
                str(tsx),
                str(entry),
                "--codex-cpa",
                "--email",
                email,
                "--password",
                password,
                "--cpa-base",
                cpa_base,
                "--cpa-key",
                cpa_key,
            ]
            command.append("--otp")
            command.append("--cpa-bind-only")
            command_flags = "--codex-cpa --email *** --password *** --cpa-base *** --cpa-key *** --otp --cpa-bind-only"
        log(f"[mailat-bind] command_flags={command_flags}")
        log(f"[mailat-bind] 源码入口: {entry}")
        log(f"[mailat-bind] 工作目录: {task_dir}")
        proc = subprocess.Popen(
            command,
            cwd=str(task_dir),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env={**os.environ, "CODEX_AUTH_DEBUG": "1"},
        )
        assert proc.stdout is not None
        assert proc.stdin is not None
        output: list[str] = []
        otp_sent = False
        started = time.monotonic()
        timeout_seconds = max(120, _int_value(config.get("mailat_protocol_bind_timeout_seconds") or config.get("mailat_protocol_timeout_seconds"), 900))
        try:
            for line in proc.stdout:
                output.append(line)
                log(line.rstrip("\n"))
                if not otp_sent and ("提交邮箱验证码" in line or "manualEmailOtp" in line or "请输入邮箱验证码" in line):
                    code = str(otp_callback() or "").strip()
                    if not code:
                        raise RuntimeError("协议绑定未获取到账号邮箱验证码")
                    proc.stdin.write(code + "\n")
                    proc.stdin.flush()
                    otp_sent = True
                if time.monotonic() - started > timeout_seconds:
                    raise TimeoutError(f"mailat 协议绑定超过 {timeout_seconds}s")
        except Exception:
            if proc.poll() is None:
                terminate_process_tree(proc.pid)
            raise
        code = proc.wait()
        text = "".join(output)
        if code != 0:
            raise subprocess.CalledProcessError(code, command, output=text)
        parsed = _parse_stdout(text)
        result: dict[str, Any] = {
            "ok": True,
            "email": email,
            "oauth_callback_mode": callback_mode,
            "binding_phone_number": parsed.get("binding_phone_number", ""),
            "cpa_status": parsed.get("cpa_status", ""),
            "pool_result": parsed.get("pool_result", ""),
            "protocol_runner": f"vendor/mailat-codex-register/src/index.ts {'--auth' if callback_mode == 'local' else '--codex-cpa'} --email",
            "protocol_work_dir": str(task_dir),
            "registration_proxy": selected_proxy_url,
            "protocol_runtime_proxy": runtime_proxy_url,
            "registration_proxy_exit_ip": proxy_exit_ip,
        }
        if callback_mode == "local":
            auth_file, oauth_result = _load_local_oauth_result(task_dir)
            result["oauth_result"] = oauth_result
            result["oauth_auth_file"] = str(auth_file)
        return result
    finally:
        if proxy_runtime:
            proxy_runtime.cleanup()
