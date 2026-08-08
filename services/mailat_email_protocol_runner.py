from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import time
import yaml
from shutil import copyfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from core.proxy.credential_runtime import CredentialProxyRuntime
from infrastructure.repositories.resource_pool_repository import ResourcePoolRepository
from services.task_runtime import terminate_process_tree
from services.mailat_protocol_runtime import PROJECT_ROOT, validate_mailat_protocol_runtime

TASK_TMP_ROOT = PROJECT_ROOT / "tmp" / "mailat_email_protocol_tasks"


def _first(value: Any) -> str:
    if value is None:
        return ""
    for line in str(value).replace("\r", "\n").split("\n"):
        item = line.strip()
        if item:
            return item
    return ""


def _int_value(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _float_value(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _bool_value(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _mailat_sms_enabled(config: dict[str, Any]) -> bool:
    if _bool_value(config.get("mailat_protocol_enable_sms_fallback"), False):
        return True
    provider = str(config.get("sms_provider") or config.get("sms_provider_key") or "").strip().lower()
    return provider in {"herosms", "hero_sms", "herosms_api", "smsbower", "smsbower_api"}


def _mask_secret(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 8:
        return "***"
    return f"{text[:4]}***{text[-4:]}"


def _diagnose_failure(text: str) -> list[str]:
    diagnostics: list[str] = []
    if "[注册成功]" in text or "[✅️注册成功]" in text:
        diagnostics.append("OpenAI account creation reached success marker")
    if "ChatGPT session 中缺少 accessToken" in text:
        diagnostics.append("/api/auth/session returned no accessToken")
    if "WARNING_BANNER" in text:
        diagnostics.append("/api/auth/session payload only exposed WARNING_BANNER or similarly incomplete session data")
    if "add-phone" in text or "进入短信验证流程" in text:
        diagnostics.append("OpenAI redirected the authorization flow to add-phone verification")
    if "skip-phone" in text:
        diagnostics.append("mailat attempted the email-OTP skip-phone path before phone fallback")
    if "BAD_KEY" in text:
        diagnostics.append("SMS activation provider rejected the supplied API key")
    if "未配置 SMS provider" in text:
        diagnostics.append("phone fallback was disabled because GPT Register did not select an SMS provider")
    if "unsupported_country" in text:
        diagnostics.append("OpenAI rejected CreateAccount with unsupported_country; the current proxy exit jurisdiction is unavailable for this operation")
    return diagnostics


def _proxy_url(config: dict[str, Any]) -> str:
    explicit = _first(config.get("mailat_protocol_proxy") or config.get("codex_protocol_proxy") or config.get("defaultProxyUrl") or config.get("proxy"))
    if explicit:
        return explicit
    credential = _first(config.get("lajiao_proxy_credentials"))
    if not credential:
        return ""
    if "://" in credential:
        return credential
    protocol = str(config.get("lajiao_proxy_credential_protocol") or "http").strip().lower() or "http"
    if protocol == "socks5h":
        protocol = "socks5"
    return f"{protocol}://{credential}"


def _bridge_proxy_url(proxy_url: str, config: dict[str, Any], log: Callable[[str], None]) -> tuple[str, CredentialProxyRuntime | None]:
    # Default OFF: pure-Go / software path dial remote SOCKS/HTTP directly.
    if not bool(config.get("mailat_protocol_use_local_bridge", config.get("codex_protocol_use_local_bridge", False))):
        return proxy_url, None
    if not proxy_url:
        return "", None
    runtime = CredentialProxyRuntime(config, log_fn=lambda message: log(f"[mailat-protocol]{message}"))
    bridge_url = runtime.start_browser_bridge(proxy_url)
    if bridge_url != proxy_url:
        log(f"[mailat-protocol] proxy_bridge={bridge_url}")
    return bridge_url, runtime


def _proxy_resource_key(value: str) -> str:
    value = str(value or "").strip()
    if "://" in value:
        parsed = urlsplit(value)
        if parsed.netloc:
            return parsed.netloc.strip()
    return value


def _same_proxy_resource(left: str, right: str) -> bool:
    return _proxy_resource_key(left) == _proxy_resource_key(right)


def _cooldown_until(seconds: int) -> str:
    return (datetime.now() + timedelta(seconds=max(0, seconds))).isoformat(timespec="seconds")




def _persist_resource_leases(config: dict[str, Any]) -> None:
    path = Path(str(config.get("_task_config_path") or ""))
    if not path.exists():
        return
    try:
        current = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        current["resource_leases"] = config.get("resource_leases") or []
        path.write_text(yaml.safe_dump(current, allow_unicode=True, sort_keys=False), encoding="utf-8")
    except Exception:
        return

def _append_proxy_lease(config: dict[str, Any], provider: str, key: str) -> None:
    leases = config.get("resource_leases") if isinstance(config.get("resource_leases"), list) else []
    if not any(isinstance(item, dict) and item.get("type") == "proxy" and _same_proxy_resource(str(item.get("key") or ""), key) for item in leases):
        leases.append({"type": "proxy", "provider": provider, "key": key})
    config["resource_leases"] = leases
    _persist_resource_leases(config)


def _cooldown_proxy_resource(config: dict[str, Any], proxy_url: str, reason: str) -> None:
    key = _proxy_resource_key(proxy_url)
    if not key:
        return
    repo = ResourcePoolRepository(str(config.get("resource_pool_db_path") or config.get("_resource_pool_db_path") or "") or None)
    current = repo.get("proxy", "lajiao_credentials", key)
    if int(current.get("id") or 0) > 0:
        repo.set_status(int(current["id"]), status="cooldown", cooldown_until=_cooldown_until(1800), error=reason)


def _lease_next_proxy(config: dict[str, Any], task_id: str) -> str:
    repo = ResourcePoolRepository(str(config.get("resource_pool_db_path") or config.get("_resource_pool_db_path") or "") or None)
    region = str(config.get("lajiao_proxy_regions") or config.get("lajiao_proxy_expected_country") or "")
    region_value = region.split(",")[0].strip() if region else ""
    lease = repo.lease("proxy", "lajiao_credentials", task_id, region=region_value)
    if not lease.resource_key:
        return ""
    _append_proxy_lease(config, lease.provider, lease.resource_key)
    proxy_value = str(lease.payload.get("url") or lease.resource_key)
    if "://" in proxy_value:
        return proxy_value
    protocol = str(lease.payload.get("protocol") or config.get("lajiao_proxy_credential_protocol") or "socks5").strip().lower() or "socks5"
    if protocol == "auto":
        protocol = "socks5"
    return f"{protocol}://{proxy_value}"


def _proxy_exit_country(proxy_url: str, timeout_seconds: int, log: Callable[[str], None]) -> tuple[str, str]:
    import requests

    proxies = {"http": proxy_url, "https": proxy_url}
    for url in ("https://ipinfo.io/json", "https://ipapi.co/json/", "https://api.ipify.org?format=json"):
        try:
            response = requests.get(url, proxies=proxies, timeout=timeout_seconds)
            if response.status_code != 200:
                log(f"[mailat-protocol] proxy_country_check_status url={url} status={response.status_code}")
                continue
            payload = response.json()
            if "api.ipify" in url:
                return "", str(payload.get("ip") or "")
            country = str(payload.get("country") or payload.get("country_code") or "").upper()
            ip = str(payload.get("ip") or payload.get("query") or "")
            if country or ip:
                return country, ip
        except Exception as exc:
            log(f"[mailat-protocol] proxy_country_check_error url={url} error={str(exc).splitlines()[0][:160]}")
    return "", ""


def _proxy_precheck_enabled(config: dict[str, Any]) -> bool:
    if config.get("mailat_protocol_proxy_precheck_enabled") is not None:
        return _bool_value(config.get("mailat_protocol_proxy_precheck_enabled"), True)
    return _bool_value(config.get("openai_proxy_precheck_enabled"), True)


def _select_proxy_url(proxy_url: str, config: dict[str, Any], log: Callable[[str], None], *, task_id: str = "") -> tuple[str, str, str, CredentialProxyRuntime | None]:
    if not proxy_url:
        return "", "", "", None
    attempts = max(1, _int_value(config.get("mailat_protocol_proxy_attempts") or config.get("lajiao_proxy_max_candidates"), 6))
    timeout_seconds = max(3, _int_value(config.get("mailat_protocol_proxy_preflight_timeout_seconds") or config.get("lajiao_proxy_timeout"), 8))
    candidate = proxy_url
    last_error = ""
    for index in range(1, attempts + 1):
        runtime = CredentialProxyRuntime(config, log_fn=lambda message: log(f"[mailat-protocol]{message}"))
        expected_country = str(config.get("lajiao_proxy_expected_country") or config.get("proxy_region") or config.get("lajiao_proxy_regions") or "").split(",")[0].strip().upper() or runtime.country_from_proxy_zone(candidate)
        try:
            runtime_proxy_url = candidate
            if bool(config.get("mailat_protocol_use_local_bridge", config.get("codex_protocol_use_local_bridge", False))):
                runtime_proxy_url = runtime.start_browser_bridge(candidate)
                if runtime_proxy_url != candidate:
                    log(f"[mailat-protocol] proxy_bridge={runtime_proxy_url}")
            if not _proxy_precheck_enabled(config):
                log("[mailat-protocol] proxy_precheck=skipped")
                return candidate, runtime_proxy_url, "skip_check", runtime
            country, exit_ip = _proxy_exit_country(runtime_proxy_url, timeout_seconds, log)
            bridge_error = runtime.browser_bridge_error(candidate)
            country_ok = (not expected_country) or country == expected_country
            log(f"[mailat-protocol] proxy_country_check {index}/{attempts} country={country or 'unknown'} ip={exit_ip or 'unknown'} expected={expected_country or '-'} ok={country_ok}")
            if bridge_error:
                last_error = f"proxy bridge failed: {bridge_error}"
                log(f"[mailat-protocol] proxy_bridge_error error={bridge_error}")
            elif country_ok and exit_ip:
                return candidate, runtime_proxy_url, exit_ip, runtime
            else:
                last_error = f"proxy country mismatch expected={expected_country or '-'} actual={country or 'unknown'} ip={exit_ip or 'unknown'}"
            _cooldown_proxy_resource(config, candidate, last_error)
            runtime.cleanup()
            candidate = _lease_next_proxy(config, task_id)
            if not candidate:
                break
        except Exception as exc:
            last_error = str(exc).splitlines()[0][:240]
            _cooldown_proxy_resource(config, candidate, f"proxy country check failed: {last_error}")
            runtime.cleanup()
            candidate = _lease_next_proxy(config, task_id)
            if not candidate:
                break
    raise RuntimeError(f"邮箱协议注册代理出口国家校验失败：{last_error or 'no proxy'}")


def _mailat_config(config: dict[str, Any], *, email: str, password: str, proxy_url: str) -> dict[str, Any]:
    mailbox_provider = str(config.get("mailbox_provider") or "").strip().lower()
    provider = "cloudflare" if mailbox_provider in {"cfworker_admin_api", "cfworker", "cloudflare"} else "hotmail"
    sms_provider = str(config.get("sms_provider") or config.get("sms_provider_key") or "").strip().lower()
    hero_sms_key = str(config.get("sms_api_key") or config.get("heroSMSApiKey") or "").strip() if sms_provider in {"herosms", "hero_sms", "herosms_api"} else ""
    smsbower_key = str(config.get("smsbower_api_key") or "").strip()
    sms_country = _int_value(config.get("sms_country") or config.get("heroSMSCountry"), 187)
    return {
        "provider": provider,
        "defaultProxyUrl": proxy_url,
        "defaultPassword": password,
        "loopDelayMs": _int_value(config.get("loopDelayMs"), 30_000),
        "cloudflareEmailDomain": str(config.get("cfworker_domain") or config.get("mailbox_domain") or "").strip().lstrip("@"),
        "cloudflareApiBaseUrl": str(config.get("cfworker_api_url") or "").strip(),
        "cloudflareApiKey": str(config.get("cfworker_admin_token") or "").strip(),
        "heroSMSApiKey": hero_sms_key,
        "heroSMSCountry": sms_country,
        "heroSMSMaxPrice": _float_value(config.get("herosms_max_price") or config.get("heroSMSMaxPrice"), 0.0999),
        "heroSMSPollAttempts": _int_value(config.get("heroSMSPollAttempts") or config.get("sms_poll_attempts"), 24),
        "heroSMSPollIntervalMs": _int_value(config.get("heroSMSPollIntervalMs") or config.get("sms_poll_interval_ms"), 5_000),
        "smsbowerApiKey": smsbower_key,
        "smsbowerService": str(config.get("sms_service") or config.get("smsbower_service") or "dr").strip() or "dr",
        "smsbowerCountry": sms_country,
        "smsbowerMinPrice": _float_value(config.get("smsbower_min_price"), -1),
        "smsbowerMaxPrice": _float_value(config.get("smsbower_max_price"), -1),
        "smsbowerProviderIds": str(config.get("smsbower_provider_ids") or "").strip(),
        "gptRegisterSmsProvider": sms_provider,
        "gptRegisterSmsService": str(config.get("sms_service") or "dr").strip() or "dr",
        "gptRegisterBindSmsPhoneUrl": "",
        "gptRegisterCountryCode": str(config.get("country_code") or "").strip(),
        "gmailAccessToken": "",
        "gmailEmailAddress": "",
        "gptMailApiKey": "",
        "gptMailDomain": "",
        "2925EmailAddress": "",
        "2925Password": "",
        "cliproxyApiAutoUploadAuth": False,
        "cliproxyApiBaseUrl": str(config.get("cpa_base_url") or "").strip(),
        "cliproxyApiManagementKey": str(config.get("cpa_management_key") or "").strip(),
        "gptRegisterExternalEmail": email,
    }


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    try:
        part = token.split(".")[1]
        padded = part + "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception:
        return {}


def _safe_email_file(email: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(email or "").strip().lower())


def _parse_stdout(text: str, *, task_dir: Path, email: str) -> dict[str, str]:
    result: dict[str, str] = {"email": email}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[access_token]"):
            result["access_token"] = stripped.split("]", 1)[1].strip()
        elif stripped.startswith("[access_token_file]"):
            result["access_token_file"] = stripped.split("]", 1)[1].strip()
        elif stripped.startswith("[gp_token_out]"):
            result["token_out_detail"] = stripped.split("]", 1)[1].strip()
        elif stripped.startswith("[✅️注册成功]") and "邮箱：" in stripped:
            result["email"] = stripped.split("邮箱：", 1)[1].split()[0].strip()
    session_file = task_dir / "email_sessions" / f"{_safe_email_file(result.get('email') or email)}.json"
    if session_file.exists():
        result["protocol_session_state_path"] = str(session_file)
    return result


def run_mailat_email_protocol(
    config: dict[str, Any],
    *,
    email: str,
    password: str,
    otp_callback: Callable[[], str],
    task_id: str = "",
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    runtime = validate_mailat_protocol_runtime()
    tsx = runtime.tsx
    entry = runtime.entry
    if not email:
        raise RuntimeError("mailat 邮箱协议注册缺少 email")
    if not password:
        raise RuntimeError("mailat 邮箱协议注册缺少 password")

    task_name = task_id or f"mailat_email_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    task_dir = TASK_TMP_ROOT / task_name
    task_dir.mkdir(parents=True, exist_ok=True)
    proxy_runtime = None
    selected_proxy_url = _proxy_url(config)
    runtime_proxy_url = selected_proxy_url
    proxy_exit_ip = ""
    try:
        if selected_proxy_url:
            selected_proxy_url, runtime_proxy_url, proxy_exit_ip, proxy_runtime = _select_proxy_url(selected_proxy_url, config, log, task_id=task_id)
            log(f"使用新代理: {selected_proxy_url} exit_ip={proxy_exit_ip or '-'}")
            log(f"[mailat-protocol] proxy={runtime_proxy_url}")
        mailat_config = _mailat_config(config, email=email, password=password, proxy_url=runtime_proxy_url)
        (task_dir / "config.json").write_text(json.dumps(mailat_config, ensure_ascii=False, indent=2), encoding="utf-8")
        active_sms_key = mailat_config.get("smsbowerApiKey") if str(mailat_config.get("gptRegisterSmsProvider") or "").lower() in {"smsbower", "smsbower_api"} else mailat_config.get("heroSMSApiKey")
        log(
            "[mailat-protocol] config_summary "
            f"provider={mailat_config.get('provider')} "
            f"mailbox_provider={config.get('mailbox_provider') or ''} "
            f"sms_provider={mailat_config.get('gptRegisterSmsProvider') or ''} "
            f"sms_key={'present:' + _mask_secret(active_sms_key) if active_sms_key else 'disabled'} "
            f"sms_country={mailat_config.get('smsbowerCountry') if str(mailat_config.get('gptRegisterSmsProvider') or '').lower() in {'smsbower', 'smsbower_api'} else mailat_config.get('heroSMSCountry')}"
        )
        copyfile(runtime.sdk, task_dir / "sdk.js")
        token_out = task_dir / "pool_tokens.txt"
        command = [str(tsx), str(entry), "--at", "--email", email, "--otp", "--gp-token-out", str(token_out)]
        if _bool_value(config.get("mailat_protocol_skip_phone"), True):
            command.append("--skip-phone")
        log(f"[mailat-protocol] command_flags={' '.join(arg for arg in command[2:] if arg != email and arg != str(token_out))}")
        log(f"[mailat-protocol] 项目内置源码入口: {entry}")
        log(f"[mailat-protocol] 工作目录: {task_dir}")
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
            env={**os.environ, "CODEX_AT_OUT_DIR": str(task_dir), "CODEX_AUTH_DEBUG": "1"},
        )
        assert proc.stdout is not None
        assert proc.stdin is not None
        output: list[str] = []
        otp_sent = False
        started = time.monotonic()
        timeout_seconds = max(120, _int_value(config.get("mailat_protocol_timeout_seconds"), 900))
        try:
            for line in proc.stdout:
                output.append(line)
                log(line.rstrip("\n"))
                if not otp_sent and ("提交邮箱验证码" in line or "manualEmailOtp" in line or "请输入邮箱验证码" in line):
                    code = str(otp_callback() or "").strip()
                    if not code:
                        raise RuntimeError("mailat 邮箱协议注册未获取到邮箱验证码")
                    proc.stdin.write(code + "\n")
                    proc.stdin.flush()
                    otp_sent = True
                if time.monotonic() - started > timeout_seconds:
                    raise TimeoutError(f"mailat 邮箱协议注册超过 {timeout_seconds}s")
        except Exception:
            if proc.poll() is None:
                terminate_process_tree(proc.pid)
            raise
        code = proc.wait()
        text = "".join(output)
        if code != 0:
            diagnostics = _diagnose_failure(text)
            for diagnostic in diagnostics:
                log(f"[mailat-protocol][diag] {diagnostic}")
            if "unsupported_country" in text:
                raise RuntimeError(
                    "OpenAI 拒绝当前注册出口地区（unsupported_country）；"
                    "请仅使用您获授权且服务可用的出口地区后创建新的注册任务。"
                )
            raise subprocess.CalledProcessError(code, command, output=text)
        parsed = _parse_stdout(text, task_dir=task_dir, email=email)
        access_token = parsed.get("access_token", "")
        if not access_token:
            raise RuntimeError("mailat 邮箱协议注册完成但未解析到 access_token")
        claims = _decode_jwt_payload(access_token)
        auth_claims = claims.get("https://api.openai.com/auth") if isinstance(claims.get("https://api.openai.com/auth"), dict) else {}
        profile_claims = claims.get("https://api.openai.com/profile") if isinstance(claims.get("https://api.openai.com/profile"), dict) else {}
        return {
            "access_token": access_token,
            "email": str(profile_claims.get("email") or claims.get("email") or parsed.get("email") or email),
            "account_id": str(auth_claims.get("chatgpt_account_id") or auth_claims.get("user_id") or claims.get("sub") or ""),
            "plan_type": str(auth_claims.get("chatgpt_plan_type") or "free"),
            "access_token_file": parsed.get("access_token_file", ""),
            "protocol_session_state_path": parsed.get("protocol_session_state_path", ""),
            "protocol_runner": "vendor/mailat-codex-register/src/index.ts",
            "protocol_work_dir": str(task_dir),
            "registration_proxy": selected_proxy_url,
            "protocol_runtime_proxy": runtime_proxy_url,
            "registration_proxy_exit_ip": proxy_exit_ip,
        }
    finally:
        if proxy_runtime:
            proxy_runtime.cleanup()
