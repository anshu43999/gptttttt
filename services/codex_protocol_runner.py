from __future__ import annotations

import base64
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from core import account_store
from core.proxy.credential_runtime import CredentialProxyRuntime

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CODEX_DIR = Path("E:/project/codex-phone-at-bundle/codex_register")
TASK_TMP_ROOT = PROJECT_ROOT / "tmp" / "codex_protocol_tasks"
OUTPUT_ROOT = PROJECT_ROOT / "output"


def _first(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r", "\n").strip()
    for line in text.split("\n"):
        item = line.strip()
        if item:
            return item
    return ""


def _proxy_url(config: dict[str, Any]) -> str:
    explicit = _first(config.get("codex_protocol_proxy") or config.get("defaultProxyUrl") or config.get("proxy"))
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


def _candidate_proxy_urls(config: dict[str, Any]) -> list[str]:
    runtime = CredentialProxyRuntime(config)
    candidates = runtime.credential_candidates()
    if str(config.get("lajiao_proxy_credential_protocol") or "").strip().lower() in {"http", "https"}:
        for item in list(candidates):
            if item.startswith("http://"):
                candidates.append("socks5://" + item[len("http://"):])
            elif item.startswith("https://"):
                candidates.append("socks5://" + item[len("https://"):])
    unique: list[str] = []
    for item in candidates:
        if item not in unique:
            unique.append(item)
    return unique


def _preflight_proxy(proxy_url: str, timeout_seconds: int) -> tuple[bool, str]:
    from curl_cffi import requests as curl_requests
    session = curl_requests.Session(impersonate="chrome", verify=False)
    session.proxies = {"http": proxy_url, "https": proxy_url}
    try:
        authorize_url = "https://auth.openai.com/api/accounts/authorize?client_id=app_X8zY6vW2pQ9tR3dE7nK1jL5gH&scope=openid+email+profile+offline_access+model.request+model.read+organization.read+organization.write&response_type=code&redirect_uri=https%3A%2F%2Fchatgpt.com%2Fapi%2Fauth%2Fcallback%2Fopenai&audience=https%3A%2F%2Fapi.openai.com%2Fv1&code_challenge=preflight&code_challenge_method=S256&state=preflight&login_hint=%2B5500000000000&screen_hint=login_or_signup"
        response = session.get(authorize_url, timeout=timeout_seconds, allow_redirects=True)
        return True, f"status={response.status_code} final_url={response.url}"
    except Exception as exc:
        return False, str(exc)[:240]


def _proxy_exit_country(proxy_url: str, timeout_seconds: int) -> tuple[str, str]:
    from curl_cffi import requests as curl_requests
    session = curl_requests.Session(impersonate="chrome", verify=False)
    session.proxies = {"http": proxy_url, "https": proxy_url}
    for url in ("https://ipapi.co/json/", "https://ipinfo.io/json"):
        try:
            response = session.get(url, timeout=timeout_seconds)
            payload = response.json()
            country = str(payload.get("country_code") or payload.get("country") or "").upper()
            ip = str(payload.get("ip") or payload.get("query") or "")
            if country:
                return country, ip
        except Exception:
            continue
    return "", ""


def _select_preflighted_proxy(config: dict[str, Any], default_proxy_url: str) -> tuple[str, CredentialProxyRuntime | None]:
    if not bool(config.get("codex_protocol_use_local_bridge", False)):
        return default_proxy_url, None
    attempts = max(1, _int_value(config.get("codex_protocol_proxy_attempts"), 6))
    timeout_seconds = max(3, _int_value(config.get("codex_protocol_proxy_preflight_timeout_seconds"), 12))
    candidates = _candidate_proxy_urls(config)[:attempts]
    if bool(config.get("codex_protocol_skip_proxy_preflight", False)):
        candidate = (candidates[0] if candidates else default_proxy_url)
        runtime = CredentialProxyRuntime({**config, "lajiao_proxy_credential_protocol": urlsplit(candidate).scheme or config.get("lajiao_proxy_credential_protocol")}, log_fn=lambda message: print(f"[protocol]{message}"))
        bridge_url = runtime.start_browser_bridge(candidate)
        print(f"[resource] proxy_preflight skipped candidate={urlsplit(candidate).scheme} bridge={bridge_url}")
        return bridge_url, runtime
    for index, candidate in enumerate(candidates, 1):
        runtime = CredentialProxyRuntime({**config, "lajiao_proxy_credential_protocol": urlsplit(candidate).scheme or config.get("lajiao_proxy_credential_protocol")}, log_fn=lambda message: print(f"[protocol]{message}"))
        required_country = str(config.get("lajiao_proxy_expected_country") or config.get("proxy_region") or config.get("lajiao_proxy_region") or config.get("lajiao_proxy_regions") or "").split(",")[0].strip().upper() or runtime.country_from_proxy_zone(candidate)
        bridge_url = runtime.start_browser_bridge(candidate)
        ok, detail = _preflight_proxy(bridge_url, timeout_seconds)
        country, ip = _proxy_exit_country(bridge_url, timeout_seconds) if ok and required_country else ("", "")
        country_ok = (not required_country) or country == required_country
        print(f"[resource] proxy_preflight {index}/{len(candidates)} candidate={urlsplit(candidate).scheme} bridge={bridge_url} ok={ok} country={country or 'unknown'} ip={ip or 'unknown'} detail={detail}")
        if ok and country_ok:
            return bridge_url, runtime
        last_error = detail if ok else detail
        if ok and not country_ok:
            last_error = f"proxy country mismatch: expected={required_country} actual={country or 'unknown'} ip={ip or 'unknown'}"
        runtime.cleanup()
    raise RuntimeError(f"协议注册代理预检全部失败：{last_error}")

def _codex_config(config: dict[str, Any]) -> dict[str, Any]:
    sms_country = config.get("heroSMSCountry") or config.get("herosms_country") or config.get("sms_country") or 73
    mailbox_provider = str(config.get("mailbox_provider") or "forwarded_domain").strip().lower()
    provider = "cloudflare" if mailbox_provider in {"cfworker_admin_api", "cloudflare"} else "forwarded"
    mailbox_domain = str(config.get("forwardedEmailDomain") or config.get("mailbox_domain") or config.get("cfworker_domain") or "").strip().lstrip("@")
    return {
        "provider": provider,
        "defaultProxyUrl": _proxy_url(config),
        "defaultPassword": str(config.get("chatgpt_password") or config.get("defaultPassword") or "kuaileshifu88"),
        "loopDelayMs": _int_value(config.get("loopDelayMs"), 30_000),
        "heroSMSApiKey": str(config.get("heroSMSApiKey") or config.get("herosms_api_key") or config.get("sms_api_key") or "").strip(),
        "heroSMSCountry": _int_value(sms_country, 73),
        "heroSMSMaxPrice": _float_value(config.get("heroSMSMaxPrice") or config.get("herosms_max_price"), 0.0999),
        "heroSMSPollAttempts": _int_value(config.get("heroSMSPollAttempts") or config.get("sms_poll_attempts"), 24),
        "heroSMSPollIntervalMs": _int_value(config.get("heroSMSPollIntervalMs") or config.get("sms_poll_interval_ms"), 5_000),
        "forwardedEmailDomain": mailbox_domain,
        "forwardedImapUser": str(config.get("forwardedImapUser") or config.get("mailbox_imap_user") or "").strip(),
        "forwardedImapPass": str(config.get("forwardedImapPass") or config.get("mailbox_imap_pass") or ""),
        "forwardedImapHost": str(config.get("forwardedImapHost") or config.get("mailbox_imap_host") or "imap.163.com").strip(),
        "forwardedImapPort": _int_value(config.get("forwardedImapPort") or config.get("mailbox_imap_port"), 993),
        "cloudflareEmailDomain": mailbox_domain,
        "cloudflareApiBaseUrl": str(config.get("cloudflareApiBaseUrl") or config.get("cfworker_api_url") or "").strip(),
        "cloudflareApiKey": str(config.get("cloudflareApiKey") or config.get("cfworker_admin_token") or "").strip(),
        "codexBindEmail": str(config.get("codex_bind_email") or config.get("billing_email") or config.get("codex_email") or "").strip().lower(),
        "cliproxyApiAutoUploadAuth": False,
        "cliproxyApiBaseUrl": str(config.get("cpa_base_url") or "").strip(),
        "cliproxyApiManagementKey": str(config.get("cpa_management_key") or "").strip(),
    }


def _redact(text: str) -> str:
    text = re.sub(r"(access_token\]\s+)[A-Za-z0-9._\-]+", r"\1***", text)
    text = re.sub(r"(Bearer\s+)[A-Za-z0-9._\-]+", r"\1***", text)
    return text


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    try:
        part = token.split(".")[1]
        padded = part + "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception:
        return {}


def _parse_stdout(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[access_token]"):
            result["access_token"] = stripped.split("]", 1)[1].strip()
        elif stripped.startswith("[phone]"):
            result["phone_number"] = stripped.split("]", 1)[1].strip()
        elif stripped.startswith("[access_token_file]"):
            result["access_token_file"] = stripped.split("]", 1)[1].strip()
        elif stripped.startswith("[storage_state_file]"):
            result["storage_state_file"] = stripped.split("]", 1)[1].strip()
        elif stripped.startswith("[pool_phones]"):
            result["pool_phones"] = stripped
        elif "add-email 候选邮箱:" in stripped:
            value = stripped.split("add-email 候选邮箱:", 1)[1].strip().split()[0]
            result["email"] = value
        elif stripped.startswith("[checkout]"):
            if "session_id=" in stripped:
                for key in ("session_id", "status", "payment_status", "promo"):
                    marker = f"{key}="
                    if marker in stripped:
                        tail = stripped.split(marker, 1)[1].split()
                        if tail:
                            result[f"checkout_{key}"] = tail[0].strip()
    return result


def _finish_callback_with_curl(callback_file: Path, config: dict[str, Any], codex_config: dict[str, Any], proxy_url: str) -> dict[str, str]:
    from curl_cffi import requests as curl_requests
    from core.mailbox.forwarded_domain import ForwardedDomainMailbox, MailboxAccount

    payload = json.loads(callback_file.read_text(encoding="utf-8"))
    callback_url = str(payload.get("callback_url") or "")
    phone = str(payload.get("phone") or "")
    if not callback_url:
        raise RuntimeError("callback_out 缺少 callback_url")
    session = curl_requests.Session(impersonate="chrome", verify=False)
    if proxy_url:
        session.proxies = {"http": proxy_url, "https": proxy_url}
    headers = {"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36", "accept-language": "ja-JP,ja;q=0.9,en-US;q=0.6,en;q=0.5"}
    print("[callback] 初始化 ChatGPT curl_cffi session")
    session.get("https://chatgpt.com/auth/login", headers={**headers, "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}, timeout=30)
    csrf_resp = session.get("https://chatgpt.com/api/auth/csrf", headers={**headers, "accept": "application/json", "referer": "https://chatgpt.com/auth/login"}, timeout=30)
    print(f"[callback] csrf status={csrf_resp.status_code}")
    callback_resp = session.get(callback_url, headers={**headers, "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "referer": "https://auth.openai.com/"}, allow_redirects=True, timeout=45)
    print(f"[callback] oauth callback status={callback_resp.status_code} final_url={callback_resp.url}")
    sess = session.get("https://chatgpt.com/api/auth/session", headers={**headers, "accept": "application/json", "referer": "https://chatgpt.com/"}, timeout=30)
    print(f"[callback] session status={sess.status_code}")
    session_payload = sess.json() if sess.text else {}
    access_token = str(session_payload.get("accessToken") or session_payload.get("access_token") or "")
    if not access_token:
        raise RuntimeError(f"callback session 中缺少 accessToken: {str(session_payload)[:300]}")
    email = str(codex_config.get("codexBindEmail") or "").strip().lower()
    if email:
        mailbox = ForwardedDomainMailbox.from_config(config)
        account = MailboxAccount(email=email, account_id=email)
        before_ids = mailbox.get_current_ids(account)
        print(f"[add-email] begin email={email}")
        begin = session.post("https://chatgpt.com/backend-api/accounts/add_email/begin", headers={**headers, "accept": "application/json", "content-type": "application/json", "origin": "https://chatgpt.com", "referer": "https://chatgpt.com/", "authorization": f"Bearer {access_token}"}, json={"email": email}, timeout=30)
        print(f"[add-email] begin status={begin.status_code} body={begin.text[:300]}")
        if begin.status_code >= 300:
            raise RuntimeError(f"add-email begin failed: {begin.status_code} {begin.text[:300]}")
        code = mailbox.wait_for_code(account, timeout=_int_value(config.get("email_otp_timeout"), 180), before_ids=before_ids)
        print(f"[add-email] otp received length={len(code)}")
        verify = session.post("https://chatgpt.com/backend-api/accounts/add_email/verify", headers={**headers, "accept": "application/json", "content-type": "application/json", "origin": "https://chatgpt.com", "referer": "https://chatgpt.com/", "authorization": f"Bearer {access_token}"}, json={"email": email, "code": code}, timeout=30)
        print(f"[add-email] verify status={verify.status_code} body={verify.text[:300]}")
        if verify.status_code >= 300:
            raise RuntimeError(f"add-email verify failed: {verify.status_code} {verify.text[:300]}")
        refreshed = session.get("https://chatgpt.com/api/auth/session?refresh=true&reason=verify_otp", headers={**headers, "accept": "application/json", "referer": "https://chatgpt.com/", "authorization": f"Bearer {access_token}"}, timeout=30)
        print(f"[add-email] refresh session status={refreshed.status_code}")
        try:
            refreshed_payload = refreshed.json()
            access_token = str(refreshed_payload.get("accessToken") or refreshed_payload.get("access_token") or access_token)
        except Exception:
            pass
    return {"access_token": access_token, "phone_number": phone, "email": email, "access_token_file": ""}


def _terminate_process_tree(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    proc.kill()


FATAL_OUTPUT_MARKERS = (
    "[❌️授权失败]",
    "rate_limit_exceeded",
    "Too many requests. Please try again later.",
)


def _fatal_output_reason(lines: list[str]) -> str:
    text = "".join(lines)
    for marker in FATAL_OUTPUT_MARKERS:
        index = text.find(marker)
        if index >= 0:
            line_start = text.rfind("\n", 0, index) + 1
            line_end = text.find("\n", index)
            if line_end < 0:
                line_end = min(len(text), index + 300)
            return text[line_start:line_end].strip()[:500]
    return ""


def _stream_process(proc: subprocess.Popen[str], command: list[str], *, idle_timeout_seconds: int, stop_when_file: Path | None = None) -> str:
    assert proc.stdout is not None
    lines: list[str] = []
    output_queue: queue.Queue[str | None] = queue.Queue()

    def reader() -> None:
        try:
            for item in proc.stdout:
                output_queue.put(item)
        finally:
            output_queue.put(None)

    threading.Thread(target=reader, daemon=True).start()
    last_output = time.monotonic()
    while True:
        if stop_when_file and stop_when_file.exists():
            _terminate_process_tree(proc)
            return "".join(lines)
        try:
            item = output_queue.get(timeout=1)
        except queue.Empty:
            if proc.poll() is not None:
                break
            if stop_when_file and stop_when_file.exists():
                _terminate_process_tree(proc)
                return "".join(lines)
            if idle_timeout_seconds > 0 and time.monotonic() - last_output > idle_timeout_seconds:
                _terminate_process_tree(proc)
                raise TimeoutError(f"协议注册 {idle_timeout_seconds}s 无日志输出，已终止子进程")
            continue
        if item is None:
            break
        last_output = time.monotonic()
        lines.append(item)
        sys.stdout.write(_redact(item))
        sys.stdout.flush()
        fatal_reason = _fatal_output_reason(lines)
        if fatal_reason:
            _terminate_process_tree(proc)
            raise RuntimeError(fatal_reason)
    code = proc.wait()
    if code != 0:
        raise subprocess.CalledProcessError(code, command)
    return "".join(lines)


def _save_account(parsed: dict[str, str], *, task_id: str, config: dict[str, Any], task_dir: Path) -> dict[str, Any]:
    access_token = parsed.get("access_token", "")
    claims = _decode_jwt_payload(access_token)
    auth_claim = claims.get("https://api.openai.com/auth") if isinstance(claims.get("https://api.openai.com/auth"), dict) else {}
    profile_claim = claims.get("https://api.openai.com/profile") if isinstance(claims.get("https://api.openai.com/profile"), dict) else {}
    account_id = str(auth_claim.get("user_id") or claims.get("sub") or "")
    email = str(parsed.get("email") or profile_claim.get("email") or "")
    phone = str(parsed.get("phone_number") or profile_claim.get("phone_number") or "")
    key = account_store.safe_key(account_id or phone or email or task_id)
    now = datetime.now().isoformat(timespec="seconds")
    registered_dir = OUTPUT_ROOT / "registered_accounts"
    registered_dir.mkdir(parents=True, exist_ok=True)
    resume_file = OUTPUT_ROOT / f"resume_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{task_id[-6:]}.json"
    account_file = registered_dir / f"{key}_{datetime.now().strftime('%Y%m%d')}.json"
    record = {
        "schema_version": 2,
        "task_id": task_id,
        "account_key": key,
        "account_id": account_id,
        "phone_number": phone,
        "email": email,
        "billing_email": email,
        "codex_email": email,
        "password": str(config.get("chatgpt_password") or config.get("defaultPassword") or "kuaileshifu88"),
        "login_identifier": phone or email,
        "registration_mode": "phone_protocol",
        "registration_status": "registered",
        "stage": "registered",
        "status": "registered",
        "plus_status": "needs_plus",
        "binding_status": "email_bound" if email else "not_ready",
        "access_token": access_token,
        "created_at": now,
        "registration_completed_at": now,
        "resume_file": str(resume_file),
        "registered_file": str(account_file),
        "protocol_runner": "codex_register",
        "protocol_work_dir": str(task_dir),
        "paths": {"registered": str(account_file), "resume": str(resume_file), "tokens": str(parsed.get("access_token_file") or ""), "storage_state": str(parsed.get("storage_state_file") or "")},
        "storage_file": str(parsed.get("storage_state_file") or ""),
        "browser_storage_state_path": str(parsed.get("storage_state_file") or ""),
        "checkout_session_id": parsed.get("checkout_session_id", ""),
        "checkout_status": parsed.get("checkout_status", ""),
        "checkout_payment_status": parsed.get("checkout_payment_status", ""),
        "checkout_promo_campaign": parsed.get("checkout_promo", ""),
    }
    account_file.write_text(json.dumps({k: v for k, v in record.items() if k != "access_token"}, ensure_ascii=False, indent=2), encoding="utf-8")
    resume_file.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    account_store.upsert_account(record, source_file=str(account_file))
    return record


def run(config_path: str, *, task_id: str = "") -> int:
    config = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    task_id = task_id or str(config.get("dashboard_task_id") or f"protocol_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    codex_dir = Path(str(config.get("codex_protocol_dir") or DEFAULT_CODEX_DIR))
    tsx = codex_dir / "node_modules" / ".bin" / ("tsx.cmd" if os.name == "nt" else "tsx")
    entry = codex_dir / "src" / "index.ts"
    if not tsx.exists():
        raise RuntimeError(f"缺少 codex_register tsx: {tsx}")
    if not entry.exists():
        raise RuntimeError(f"缺少 codex_register 入口: {entry}")

    task_dir = TASK_TMP_ROOT / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    codex_config = _codex_config(config)
    if not codex_config.get("heroSMSApiKey"):
        raise RuntimeError("协议注册缺少 HeroSMS API Key")
    if not codex_config.get("codexBindEmail"):
        raise RuntimeError("协议注册缺少已租用绑定邮箱；请配置 forwarded_domain 或 cfworker_admin_api 邮箱池")
    if not codex_config.get("defaultProxyUrl"):
        raise RuntimeError("协议注册缺少代理")
    if not (codex_config.get("forwardedEmailDomain") or codex_config.get("cloudflareEmailDomain")):
        print("[protocol] 未配置邮箱域；如果 ChatGPT 要求 add-email，codex_register 会失败。")
    proxy_runtime = None
    try:
        if bool(config.get("codex_protocol_use_local_bridge", False)) and codex_config.get("defaultProxyUrl"):
            codex_config["defaultProxyUrl"], proxy_runtime = _select_preflighted_proxy(config, str(codex_config["defaultProxyUrl"]))
            print(f"[resource] proxy=local_bridge url={codex_config['defaultProxyUrl']}")
        print(f"[resource] binding_email={codex_config.get('codexBindEmail')} role=billing_and_codex provider={config.get('mailbox_provider') or codex_config.get('provider')}")
        print(f"[resource] herosms_country={codex_config.get('heroSMSCountry')} max_price={codex_config.get('heroSMSMaxPrice')}")
        (task_dir / "config.json").write_text(json.dumps(codex_config, ensure_ascii=False, indent=2), encoding="utf-8")
        token_out = task_dir / "pool_tokens.txt"
        callback_out = task_dir / "chatgpt_callback.json"
        use_callback_out = bool(config.get("codex_protocol_use_callback_out", False))
        command = [str(tsx), str(entry), "--phone", "--st", "--gp-token-out", str(token_out)]
        if use_callback_out:
            command[3:3] = ["--callback-out", str(callback_out)]
        print(f"[protocol] 使用 codex_register: {codex_dir}")
        print(f"[protocol] 工作目录: {task_dir}")
        print(f"[protocol] 运行命令: {' '.join(command)}")
        idle_timeout = _int_value(config.get("codex_protocol_idle_timeout_seconds"), 300)
        env = {**os.environ, "CODEX_FETCH_TIMEOUT_MS": str(_int_value(config.get("codex_fetch_timeout_ms"), 45_000)), "CODEX_MAX_PHONE_TRIES": str(_int_value(config.get("codex_max_phone_tries"), 8))}
        proc = subprocess.Popen(command, cwd=str(task_dir), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        text = _stream_process(proc, command, idle_timeout_seconds=idle_timeout, stop_when_file=callback_out if use_callback_out else token_out)
        parsed = _parse_stdout(text)
        if use_callback_out and callback_out.exists():
            parsed.update(_finish_callback_with_curl(callback_out, config, codex_config, str(codex_config.get("defaultProxyUrl") or "")))
    finally:
        if proxy_runtime:
            proxy_runtime.cleanup()
    if not parsed.get("access_token"):
        raise RuntimeError("协议注册完成但未解析到 access_token")
    record = _save_account(parsed, task_id=task_id, config=codex_config, task_dir=task_dir)
    print(f"[protocol] 账号已入库: key={record.get('account_key')} phone={record.get('phone_number')} email={record.get('email')}")
    print(f"[protocol] resume_file={record.get('resume_file')}")
    print(f"[protocol] registered_file={record.get('registered_file')}")
    return 0


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Run protocol phone registration through codex_register")
    parser.add_argument("--config", required=True)
    parser.add_argument("--task-id", default="")
    args = parser.parse_args()
    raise SystemExit(run(args.config, task_id=args.task_id))


if __name__ == "__main__":
    main()
