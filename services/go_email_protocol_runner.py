"""Go email-protocol daemon client (V2).

HTTP contract: docs/EMAIL_PROTOCOL_GO_PLAN.md §5
Routes: /health, POST/GET/OTP/DELETE /v2/email-register...
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from services.mailat_email_protocol_runner import (
    TASK_TMP_ROOT,
    _bool_value,
    _decode_jwt_payload,
    _int_value,
    _proxy_url,
    _safe_email_file,
    _select_proxy_url,
)

DEFAULT_GO_EMAIL_PROTOCOL_URL = "http://127.0.0.1:18765"


def normalize_email_protocol_backend(value: Any) -> str:
    text = str(value or "python").strip().lower().replace("-", "_")
    if text in {"go", "golang", "go_worker", "go_daemon"}:
        return "go"
    if text in {"python", "mailat", "node", "tsx", "codex", ""}:
        return "python"
    return "python"


def go_protocol_use_direct_socks(config: dict[str, Any]) -> bool:
    """True when pure-Go path should dial proxy itself (no local HTTP bridge).

    Enabled when any of:
      - go_email_protocol_transport in {direct, socks, socks5, tls}
      - go_email_protocol_mode / email_protocol_go_mode in {pure, pure_go, live, direct}
      - env GO_EMAIL_PROTOCOL_PURE_GO=1
    """
    import os
    env = str(os.environ.get("GO_EMAIL_PROTOCOL_PURE_GO") or "").strip().lower()
    if env in {"1", "true", "yes", "on"}:
        return True
    transport = str(
        config.get("go_email_protocol_transport")
        or config.get("go_protocol_transport")
        or ""
    ).strip().lower()
    if transport in {"direct", "socks", "socks5", "socks5h", "tls"}:
        return True
    mode = str(
        config.get("go_email_protocol_mode")
        or config.get("email_protocol_go_mode")
        or ""
    ).strip().lower().replace("-", "_")
    if mode in {"pure", "pure_go", "live", "direct"}:
        return True
    return False



def _first(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        for item in value:
            text = str(item or "").strip()
            if text:
                return text
        return ""
    return str(value).strip()


def _go_base_url(config: dict[str, Any]) -> str:
    return _first(
        config.get("go_email_protocol_url")
        or config.get("email_protocol_go_url")
        or DEFAULT_GO_EMAIL_PROTOCOL_URL
    ).rstrip("/")


def _http_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    body = None
    req_headers = {"Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=req_headers, method=method.upper())
    # Loopback daemon must not go through system proxy (Fiddler 502 on 127.0.0.1).
    opener = urlopen if not urlparse(url).hostname in {"127.0.0.1", "localhost", "::1"} else None
    try:
        if opener is None:
            from urllib.request import build_opener, ProxyHandler

            with build_opener(ProxyHandler({})).open(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        else:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        raise RuntimeError(f"Go 协议 worker HTTP {exc.code}: {detail[:500]}") from exc
    except URLError as exc:
        raise RuntimeError(
            f"无法连接 Go 邮箱协议 worker（{url}）：{exc.reason}。"
            f"请先启动 email-protocol-worker，或在 UI 将协议后端切回 Python（mailat/Node）。"
            f" 参考 docs/EMAIL_PROTOCOL_GO_PLAN.md"
        ) from exc
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Go 协议 worker 返回非 JSON: {raw[:300]}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"Go 协议 worker 返回类型错误: {type(data).__name__}")
    return data

def check_go_email_protocol_health(config: dict[str, Any], *, timeout: float = 5.0) -> dict[str, Any]:
    base = _go_base_url(config)
    data = _http_json("GET", f"{base}/health", timeout=timeout)
    status = str(data.get("status") or "").lower()
    if status and status not in {"ok", "healthy", "up"} and not data.get("ok", True):
        raise RuntimeError(f"Go 协议 worker 健康检查失败: {data}")
    # Fail closed when software expects pure-Go but worker is mailat/legacy.
    if go_protocol_use_direct_socks(config):
        runner = str(data.get("runner") or "").strip().lower()
        mode = str(data.get("protocol_mode") or "").strip().lower()
        transport = str(data.get("transport") or "").strip().lower()
        if not runner:
            raise RuntimeError(
                "Go worker /health 缺少 runner 字段（旧二进制）。"
                "请重建并重启 email-protocol-worker（start.py 会带 -pure-go）。"
                f" health={data}"
            )
        if runner != "protocol":
            raise RuntimeError(
                f"Go worker runner={runner!r}，配置要求 pure-Go（protocol/live）。"
                f" 请重启 worker 或检查 GO_EMAIL_PROTOCOL_PURE_GO。health={data}"
            )
        if mode and mode not in {"live", "engine"}:
            raise RuntimeError(
                f"Go worker protocol_mode={mode!r}，期望 live。health={data}"
            )
        if transport in {"fake", ""}:
            raise RuntimeError(
                f"Go worker transport={transport!r}，pure-Go 需要 tls 或 direct。health={data}"
            )
    return data


def _parse_admission_reason(message: str) -> str:
    """Extract admission reason: global|proxy|mailbox|domain|queue|unknown."""
    text = str(message or "").lower()
    if "admission" not in text and "429" not in text:
        return ""
    for key in ("mailbox", "proxy", "global", "domain", "queue"):
        if f"admission rejected: {key}" in text or f'"reason": "{key}"' in text or f'"reason":"{key}"' in text:
            return key
    if "mailbox" in text and "admission" in text:
        return "mailbox"
    if "proxy" in text and "admission" in text:
        return "proxy"
    if "max_active" in text or ("global" in text and "admission" in text):
        return "global"
    if "admission" in text or "http 429" in text or "too many requests" in text:
        return "unknown"
    return ""


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _proxy_admission_key(config: dict[str, Any], *, runtime_proxy_url: str, fallback: str) -> str:
    """Return a non-secret admission bucket key for the concrete proxy session.

    Direct SOCKS uses dynamic sticky credentials; the resource-pool key can be the
    shared provider gateway, so using it would serialize unrelated sessions.
    """
    if go_protocol_use_direct_socks(config):
        proxy_url = str(runtime_proxy_url or "").strip()
        if proxy_url and "://" not in proxy_url:
            proxy_url = f"socks5://{proxy_url}"
        if proxy_url:
            return f"direct-socks:{_sha256_hex(proxy_url)}"
    key = str(fallback or runtime_proxy_url or "proxy").strip()
    return key or "proxy"


def _normalize_direct_socks_url(proxy_url: str) -> str:
    """Pure-Go dials SOCKS itself; force remote hops to socks5://user@host.

    Resource pool historically mints ``http://user:pass@host:port``. Go
    validateCreate rejects non-loopback http. Keep loopback http for local bridge.
    """
    raw = str(proxy_url or "").strip()
    if not raw:
        return ""
    for line in raw.replace("\r", "\n").split("\n"):
        line = line.strip()
        if line and not line.startswith("#"):
            raw = line
            break
    # Fast path: scheme rewrite without urlparse edge cases.
    lower = raw.lower()
    if lower.startswith("http://") or lower.startswith("https://"):
        rest = raw.split("://", 1)[1]
        # preserve user:pass@host:port
        if rest.startswith("127.0.0.1") or rest.startswith("localhost") or rest.startswith("[::1]"):
            return "http://" + rest  # local CONNECT bridge
        return "socks5://" + rest
    if lower.startswith("socks5h://"):
        return "socks5://" + raw.split("://", 1)[1]
    if lower.startswith("socks5://"):
        return raw
    if "://" not in raw:
        return f"socks5://{raw}"
    # Unknown scheme: still force socks5 for remote hops.
    return "socks5://" + raw.split("://", 1)[-1]



def _bridge_parts(runtime_proxy_url: str, *, generation: int = 1) -> dict[str, Any]:
    """Build V2 bridge grant from local CONNECT bridge URL (http://127.0.0.1:port)."""
    url = str(runtime_proxy_url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost"}:
        # Go validateCreate requires loopback bridge; surface a clear error early.
        raise RuntimeError(
            f"Go V2 需要本地 bridge（http://127.0.0.1:<port>），当前 proxy/bridge={url or '(empty)'}。"
            f"请开启 mailat_protocol_use_local_bridge=true。"
        )
    if not parsed.port:
        raise RuntimeError(f"Go V2 bridge URL 缺少端口: {url}")
    cap = secrets.token_hex(16)
    return {
        "bridge_id": f"br_{parsed.port}",
        "url": f"http://127.0.0.1:{parsed.port}",
        "capability": cap,
        "generation": int(generation or 1),
        "protocol": "http-connect",
    }


def _resource_grant(config: dict[str, Any], *, email: str, runtime_proxy_url: str, exit_ip: str) -> dict[str, Any]:
    leases = config.get("resource_leases") if isinstance(config.get("resource_leases"), list) else []
    email_key = str(config.get("outlook_email") or email or "").strip()
    proxy_key = str(config.get("proxy") or runtime_proxy_url or "proxy").strip()
    lease_fence = int(time.time())
    for item in leases:
        if not isinstance(item, dict):
            continue
        rtype = str(item.get("type") or item.get("resource_type") or "").lower()
        key = str(item.get("key") or item.get("resource_key") or "").strip()
        if rtype in {"email", "mailbox", "outlook"} and key:
            email_key = key
        if rtype in {"proxy", "lajiao", "lajiao_credentials", "proxy_seed"} and key:
            proxy_key = key
        if item.get("lease_fence") not in (None, ""):
            try:
                lease_fence = int(item.get("lease_fence"))
            except Exception:
                pass
    expected_country = str(
        config.get("lajiao_proxy_expected_country")
        or config.get("proxy_region")
        or "JP"
    ).split(",")[0].strip().upper()
    if go_protocol_use_direct_socks(config):
        bridge_url = _normalize_direct_socks_url(runtime_proxy_url)
        if not bridge_url:
            bridge_url = _normalize_direct_socks_url(
                str(config.get("mailat_protocol_proxy") or config.get("lajiao_proxy_credentials") or "")
            )
        if not bridge_url:
            raise RuntimeError("pure-go bridge.url empty after socks5 normalize")
        bridge = {
            "id": "direct-socks",
            "url": bridge_url,
            "generation": 1,
            "capability": "direct",
        }
    else:
        bridge = _bridge_parts(runtime_proxy_url, generation=1)
    return {
        "email_key": email_key or email,
        "proxy_key": _proxy_admission_key(config, runtime_proxy_url=runtime_proxy_url, fallback=proxy_key),
        "lease_fence": lease_fence,
        "exit_ip": str(exit_ip or config.get("registration_proxy_exit_ip") or "").strip(),
        "expected_country": expected_country,
        "bridge": bridge,
    }


def _payload_field(payload: dict[str, Any], *names: str) -> str:
    for name in names:
        value = payload.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _session_document_from_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    session_json = payload.get("session_state_json")
    if isinstance(session_json, dict):
        return session_json
    raw_b64 = str(payload.get("session_state_b64") or "").strip()
    if raw_b64:
        try:
            decoded = json.loads(base64.b64decode(raw_b64).decode("utf-8"))
            if isinstance(decoded, dict):
                return decoded
        except Exception:
            return None
    return None


def _access_token_from_mapping(payload: dict[str, Any]) -> str:
    token = _payload_field(payload, "access_token", "accessToken")
    if token:
        return token
    session = payload.get("session")
    if isinstance(session, dict):
        token = _payload_field(session, "access_token", "accessToken")
        if token:
            return token
    session_doc = _session_document_from_payload(payload)
    if isinstance(session_doc, dict):
        token = _payload_field(session_doc, "access_token", "accessToken")
        if token:
            return token
    return ""


def _persist_session_state(task_dir: Path, email: str, payload: dict[str, Any]) -> str:
    session = payload.get("session") if isinstance(payload.get("session"), dict) else None
    session_json = _session_document_from_payload(payload)
    if session and not isinstance(session_json, dict):
        # Map V2 SessionDocument → storage-ish JSON for handoff.
        session_json = {
            "cookies": session.get("cookies") or [],
            "origins": session.get("origins") or [],
            "access_token": _access_token_from_mapping(payload),
            "email": session.get("email") or email,
            "account_id": session.get("account_id") or "",
            "plan_type": session.get("plan_type") or "free",
        }
    path_hint = str(payload.get("session_state_path") or payload.get("protocol_session_state_path") or "").strip()
    if path_hint and Path(path_hint).is_file():
        return path_hint
    if not isinstance(session_json, dict):
        return ""
    if not _payload_field(session_json, "access_token", "accessToken"):
        session_json["access_token"] = _access_token_from_mapping(payload)
    session_dir = task_dir / "email_sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    target = session_dir / f"{_safe_email_file(email)}.json"
    target.write_text(json.dumps(session_json, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(target)


def _access_token_from_payload(payload: dict[str, Any]) -> str:
    return _access_token_from_mapping(payload)


def _result_from_payload(
    payload: dict[str, Any],
    *,
    email: str,
    task_dir: Path,
    selected_proxy_url: str,
    runtime_proxy_url: str,
    proxy_exit_ip: str,
) -> dict[str, Any]:
    access_token = _access_token_from_payload(payload)
    if not access_token:
        raise RuntimeError(
            f"Go 邮箱协议注册完成但未返回 access_token: {payload.get('message') or payload.get('failure_code') or payload.get('error') or payload}"
        )
    claims = _decode_jwt_payload(access_token)
    auth_claims = claims.get("https://api.openai.com/auth") if isinstance(claims.get("https://api.openai.com/auth"), dict) else {}
    profile_claims = claims.get("https://api.openai.com/profile") if isinstance(claims.get("https://api.openai.com/profile"), dict) else {}
    session = payload.get("session") if isinstance(payload.get("session"), dict) else {}
    resolved_email = str(
        session.get("email")
        or payload.get("email")
        or profile_claims.get("email")
        or claims.get("email")
        or email
    )
    session_path = _persist_session_state(task_dir, resolved_email, payload)
    return {
        "access_token": access_token,
        "email": resolved_email,
        "account_id": str(
            session.get("account_id")
            or payload.get("account_id")
            or auth_claims.get("chatgpt_account_id")
            or auth_claims.get("user_id")
            or claims.get("sub")
            or ""
        ),
        "plan_type": str(session.get("plan_type") or payload.get("plan_type") or auth_claims.get("chatgpt_plan_type") or "free"),
        "access_token_file": str(payload.get("access_token_file") or ""),
        "protocol_session_state_path": session_path,
        "protocol_runner": "go/email-protocol-worker",
        "protocol_work_dir": str(task_dir),
        "protocol_backend": "go",
        "registration_proxy": selected_proxy_url,
        "protocol_runtime_proxy": runtime_proxy_url,
        "registration_proxy_exit_ip": proxy_exit_ip,
        "go_job_id": str(payload.get("job_id") or ""),
    }


def _cap_headers(capability: str) -> dict[str, str]:
    cap = str(capability or "").strip()
    if not cap:
        return {}
    return {"X-Job-Capability": cap, "Authorization": f"Bearer {cap}"}


def run_go_email_protocol(
    config: dict[str, Any],
    *,
    email: str,
    password: str,
    otp_callback: Callable[[], str],
    task_id: str = "",
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    if not email:
        raise RuntimeError("Go 邮箱协议注册缺少 email")
    if not password:
        raise RuntimeError("Go 邮箱协议注册缺少 password")

    base = _go_base_url(config)
    task_name = task_id or f"go_email_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    task_dir = TASK_TMP_ROOT / task_name
    task_dir.mkdir(parents=True, exist_ok=True)

    proxy_runtime = None
    selected_proxy_url = _proxy_url(config)
    runtime_proxy_url = selected_proxy_url
    proxy_exit_ip = ""
    max_proxy_rotations = max(1, _int_value(config.get("proxy_seed_network_retries") or config.get("mailat_protocol_proxy_attempts"), 4))
    try:
        log(f"[go-protocol] backend=go base={base} api=v2")
        try:
            health = check_go_email_protocol_health(config)
            log(
                f"[go-protocol] health ok phase={health.get('phase') or '-'} "
                f"version={health.get('version') or health.get('service') or 'email-protocol-worker'}"
            )
        except Exception as exc:
            log(f"[go-protocol] health check failed: {exc}")
            raise

        last_error = ""
        for proxy_attempt in range(1, max_proxy_rotations + 1):
            if proxy_attempt > 1:
                # Network/proxy/admission failure: rotate sticky SID and/or mailbox.
                try:
                    from core.proxy.seed_session import is_proxy_network_error

                    err_l = str(last_error or "").lower()
                    adm_reason = _parse_admission_reason(last_error)
                    mailbox_collision = adm_reason == "mailbox" or (
                        "mailbox" in err_l and ("admission" in err_l or "429" in err_l)
                    )
                    proxy_seat = adm_reason == "proxy" or (
                        "proxy" in err_l and "admission" in err_l
                    )
                    global_full = adm_reason == "global"

                    # global seat full: back off then retry same resources (not network rotate).
                    if global_full and not mailbox_collision and not proxy_seat:
                        # Only retry a few times; is_proxy_network_error includes bare 429.
                        if not is_proxy_network_error(last_error) and "admission" not in err_l:
                            raise RuntimeError(last_error)
                        import time as _time

                        delay = min(8.0, 0.5 * proxy_attempt)
                        log(
                            f"[go-protocol] admission_global_full "
                            f"attempt={proxy_attempt}/{max_proxy_rotations} sleep={delay:.1f}s"
                        )
                        _time.sleep(delay)
                        # fall through to retry create with same email/proxy
                    elif last_error and not (
                        is_proxy_network_error(last_error) or mailbox_collision or proxy_seat or global_full
                    ):
                        raise RuntimeError(last_error)

                    if mailbox_collision:
                        # MaxPerMailbox=1: this email already holds a Go seat (or stale).
                        # Release current exclusive lease and take a fresh outlook_token.
                        try:
                            from application.resource_pool_service import ResourcePoolService

                            pool = ResourcePoolService()
                            old_key = str(config.get("outlook_email") or email or "").strip()
                            if old_key:
                                try:
                                    pool.repo.report(task_id, old_key, success=False, error="mailbox admission collision")
                                except Exception:
                                    pass
                            fresh = pool.repo.lease("email", "outlook_token", task_id)
                            if not fresh.resource_key:
                                raise RuntimeError(last_error or "mailbox pool exhausted after admission 429")
                            new_email = str(
                                (fresh.payload or {}).get("email") if isinstance(fresh.payload, dict) else ""
                                or fresh.resource_key
                            ).strip()
                            if not new_email:
                                raise RuntimeError(last_error or "mailbox re-lease empty")
                            email = new_email
                            config["outlook_email"] = new_email
                            # Keep other leases (proxy) and replace email lease entry.
                            leases = list(config.get("resource_leases") or []) if isinstance(config.get("resource_leases"), list) else []
                            leases = [
                                item for item in leases
                                if not (
                                    isinstance(item, dict)
                                    and str(item.get("type") or item.get("resource_type") or "").lower() in {"email", "mailbox", "outlook"}
                                )
                            ]
                            leases.append({
                                "type": "email",
                                "provider": "outlook_token",
                                "key": fresh.resource_key,
                            })
                            config["resource_leases"] = leases
                            # OTP path must follow the newly leased mailbox.
                            if isinstance(fresh.payload, dict):
                                for k in ("password", "client_id", "refresh_token", "client_secret"):
                                    if fresh.payload.get(k):
                                        config[f"outlook_{k}" if k != "password" else "outlook_password"] = fresh.payload.get(k)
                                if fresh.payload.get("password"):
                                    config["outlook_password"] = fresh.payload.get("password")
                                if fresh.payload.get("client_id"):
                                    config["outlook_client_id"] = fresh.payload.get("client_id")
                                if fresh.payload.get("refresh_token"):
                                    config["outlook_refresh_token"] = fresh.payload.get("refresh_token")
                            log(f"[go-protocol] mailbox_release attempt={proxy_attempt}/{max_proxy_rotations} email={new_email}")
                        except Exception as exc:
                            # Fall through to proxy SID rotate when re-lease fails.
                            log(f"[go-protocol] mailbox_release_failed: {exc}")

                    # Rotate sticky SID on proxy seat collision or network errors (not global-only).
                    if not global_full or mailbox_collision or proxy_seat:
                        try:
                            from application.resource_pool_service import ResourcePoolService

                            refreshed = ResourcePoolService().mint_proxy_session_from_config(config, refresh=True)
                            if refreshed:
                                selected_proxy_url = refreshed
                                config["mailat_protocol_proxy"] = refreshed
                                config["lajiao_proxy_credentials"] = refreshed.split("://", 1)[-1]
                                log(f"[go-protocol] proxy_sid_refresh attempt={proxy_attempt}/{max_proxy_rotations} url={selected_proxy_url}")
                            elif not mailbox_collision and not global_full:
                                raise RuntimeError(last_error or "proxy seed refresh failed")
                        except Exception as exc:
                            if not mailbox_collision and not global_full:
                                raise RuntimeError(last_error or str(exc)) from exc
                except Exception as exc:
                    raise RuntimeError(last_error or str(exc)) from exc

            direct = go_protocol_use_direct_socks(config)
            if selected_proxy_url:
                if direct:
                    # pure-Go worker dials SOCKS itself — do not open local HTTP bridge.
                    runtime_proxy_url = _normalize_direct_socks_url(selected_proxy_url)
                    if not runtime_proxy_url:
                        raise RuntimeError("pure-go direct proxy URL empty after normalize")
                    # Keep selected_proxy_url as original mint string for session refresh; wire uses socks5.
                    proxy_exit_ip = str(config.get("registration_proxy_exit_ip") or "").strip()
                    proxy_runtime = None
                    # Lightweight exit preflight (no HTTP CONNECT bridge).
                    try:
                        from services.mailat_email_protocol_runner import (
                            _proxy_exit_country,
                            _proxy_precheck_enabled,
                        )

                        if _proxy_precheck_enabled(config):
                            timeout_seconds = max(
                                3,
                                _int_value(
                                    config.get("mailat_protocol_proxy_preflight_timeout_seconds")
                                    or config.get("lajiao_proxy_timeout"),
                                    8,
                                ),
                            )
                            country, exit_ip = _proxy_exit_country(
                                runtime_proxy_url, timeout_seconds, log
                            )
                            if exit_ip:
                                proxy_exit_ip = exit_ip
                                config["registration_proxy_exit_ip"] = exit_ip
                            # Clash fake-ip / broken tunnel ranges
                            if exit_ip.startswith("198.18.") or exit_ip.startswith("198.19."):
                                raise RuntimeError(
                                    f"proxy_or_network: fake-ip exit {exit_ip} (reject for pure-go)"
                                )
                            expected = str(
                                config.get("lajiao_proxy_expected_country")
                                or config.get("proxy_region")
                                or ""
                            ).split(",")[0].strip().upper()
                            if expected and country and country != expected:
                                raise RuntimeError(
                                    f"proxy_or_network: proxy country mismatch expected={expected} "
                                    f"actual={country} ip={exit_ip or '-'}"
                                )
                            log(
                                f"[go-protocol] proxy_direct_precheck country={country or 'unknown'} "
                                f"ip={exit_ip or 'unknown'} expected={expected or '-'}"
                            )
                    except Exception as pre_exc:
                        last_error = str(pre_exc)
                        log(f"[go-protocol] proxy_direct_precheck_failed: {last_error[:200]}")
                        if proxy_attempt >= max_proxy_rotations:
                            raise
                        continue
                    log(f"使用新代理(direct): {selected_proxy_url} exit_ip={proxy_exit_ip or '-'}")
                    log(f"[go-protocol] proxy_direct={runtime_proxy_url}")
                else:
                    selected_proxy_url, runtime_proxy_url, proxy_exit_ip, proxy_runtime = _select_proxy_url(
                        selected_proxy_url, config, log, task_id=task_id
                    )
                    log(f"使用新代理: {selected_proxy_url} exit_ip={proxy_exit_ip or '-'}")
                    log(f"[go-protocol] proxy_bridge={runtime_proxy_url}")

            try:
                return _run_go_email_protocol_once(
                    config,
                    email=email,
                    password=password,
                    otp_callback=otp_callback,
                    task_id=task_id,
                    task_name=task_name,
                    task_dir=task_dir,
                    base=base,
                    selected_proxy_url=selected_proxy_url,
                    runtime_proxy_url=runtime_proxy_url,
                    proxy_exit_ip=proxy_exit_ip,
                    log=log,
                )
            except Exception as exc:
                from core.proxy.seed_session import is_proxy_network_error

                last_error = str(exc)
                if proxy_runtime:
                    try:
                        proxy_runtime.cleanup()
                    except Exception:
                        pass
                    proxy_runtime = None
                adm_reason = _parse_admission_reason(last_error)
                retryable = (
                    is_proxy_network_error(last_error)
                    or adm_reason in {"global", "proxy", "mailbox", "unknown"}
                )
                if proxy_attempt >= max_proxy_rotations or not retryable:
                    raise
                log(
                    f"[go-protocol] retryable_error reason={adm_reason or 'network'} "
                    f"retry={proxy_attempt}/{max_proxy_rotations}: {last_error[:200]}"
                )
                continue
        raise RuntimeError(last_error or "Go 邮箱协议注册失败")
    finally:
        if proxy_runtime:
            proxy_runtime.cleanup()


def _run_go_email_protocol_once(
    config: dict[str, Any],
    *,
    email: str,
    password: str,
    otp_callback: Callable[[], str],
    task_id: str,
    task_name: str,
    task_dir: Path,
    base: str,
    selected_proxy_url: str,
    runtime_proxy_url: str,
    proxy_exit_ip: str,
    log: Callable[[str], None],
) -> dict[str, Any]:
    timeout_seconds = max(
        120,
        _int_value(
            config.get("go_email_protocol_timeout_seconds") or config.get("mailat_protocol_timeout_seconds"),
            900,
        ),
    )
    skip_phone = _bool_value(config.get("mailat_protocol_skip_phone"), True)
    if go_protocol_use_direct_socks(config):
        runtime_proxy_url = _normalize_direct_socks_url(runtime_proxy_url) or _normalize_direct_socks_url(
            str(selected_proxy_url or config.get("mailat_protocol_proxy") or "")
        )
        if not runtime_proxy_url or runtime_proxy_url.startswith("http://") or runtime_proxy_url.startswith("https://"):
            # Absolute last line of defense: never POST non-loopback http to pure-Go.
            forced = _normalize_direct_socks_url(str(selected_proxy_url or runtime_proxy_url or ""))
            if forced.startswith("socks5://"):
                runtime_proxy_url = forced
            else:
                raise RuntimeError(
                    f"pure-go bridge.url not socks5 after normalize: {runtime_proxy_url!r}"
                )
    grant = _resource_grant(config, email=email, runtime_proxy_url=runtime_proxy_url, exit_ip=proxy_exit_ip)
    fingerprint_src = json.dumps(
        {
            "email": email,
            "proxy_key": grant["proxy_key"],
            "bridge_url": grant["bridge"]["url"],
            "bridge_generation": grant["bridge"]["generation"],
            "skip_phone": skip_phone,
            # include sid so refreshed proxy sessions get a new job fingerprint
            "proxy_session": selected_proxy_url,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    # Prefer in-worker Go Graph OTP when outlook/hotmail token is available.
    mailbox_client_id = str(
        config.get("outlook_client_id")
        or config.get("oauth_client_id")
        or config.get("client_id")
        or ""
    ).strip()
    mailbox_refresh_token = str(
        config.get("outlook_refresh_token")
        or config.get("refresh_token")
        or ""
    ).strip()
    if (not mailbox_client_id or not mailbox_refresh_token) and isinstance(config.get("resource_leases"), list):
        for item in config.get("resource_leases") or []:
            if not isinstance(item, dict):
                continue
            rtype = str(item.get("type") or item.get("resource_type") or "").lower()
            if rtype not in {"email", "mailbox", "outlook"}:
                continue
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            if not mailbox_client_id:
                mailbox_client_id = str(payload.get("client_id") or "").strip()
            if not mailbox_refresh_token:
                mailbox_refresh_token = str(payload.get("refresh_token") or "").strip()
            if mailbox_client_id and mailbox_refresh_token:
                break
    otp_timeout_seconds = 0
    try:
        otp_timeout_seconds = int(config.get("email_otp_timeout") or 0)
    except Exception:
        otp_timeout_seconds = 0

    create_body = {
        "task_id": f"{task_name}_{secrets.token_hex(3)}",
        "attempt_id": 1,
        "idempotency_key": f"idem_{task_name}_{secrets.token_hex(4)}",
        "request_fingerprint": f"sha256:{_sha256_hex(fingerprint_src)}",
        "email": email,
        "password": password,
        "resource_grant": grant,
        "profile": {"id": f"profile_{task_name}"},
        "skip_phone": skip_phone,
        "deadline_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    if mailbox_client_id and mailbox_refresh_token:
        create_body["mailbox_client_id"] = mailbox_client_id
        create_body["mailbox_refresh_token"] = mailbox_refresh_token
        if otp_timeout_seconds > 0:
            create_body["otp_timeout_seconds"] = otp_timeout_seconds
        log(
            f"[go-protocol] in-worker Graph OTP enabled client_id={mailbox_client_id[:8]}… "
            f"otp_timeout={otp_timeout_seconds or 'default'}"
        )

    from datetime import timedelta

    create_body["deadline_at"] = (
        datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    redacted = {**create_body, "password": "***"}
    if "mailbox_refresh_token" in redacted:
        redacted["mailbox_refresh_token"] = "***"
    redacted["resource_grant"] = {
        **grant,
        "bridge": {**grant["bridge"], "capability": "***"},
    }
    (task_dir / "go_request.json").write_text(json.dumps(redacted, ensure_ascii=False, indent=2), encoding="utf-8")
    log(
        f"[go-protocol] POST /v2/email-register email={email} skip_phone={skip_phone} "
        f"timeout={timeout_seconds}s bridge={grant['bridge']['url']}"
    )

    payload = _http_json(
        "POST",
        f"{base}/v2/email-register",
        create_body,
        timeout=min(60.0, float(timeout_seconds)),
    )
    job_id = str(payload.get("job_id") or "").strip()
    capability = str(payload.get("job_capability") or "").strip()
    if not job_id:
        if _access_token_from_payload(payload):
            return _result_from_payload(
                payload,
                email=email,
                task_dir=task_dir,
                selected_proxy_url=selected_proxy_url,
                runtime_proxy_url=runtime_proxy_url,
                proxy_exit_ip=proxy_exit_ip,
            )
        raise RuntimeError(f"Go 协议 worker 未返回 job_id: {payload}")
    if not capability:
        log("[go-protocol] warning: create response missing job_capability; subsequent calls may 401")

    started = time.monotonic()
    otp_submitted = False
    last_challenge_id = ""
    poll_interval = max(
        0.5,
        float(_int_value(config.get("go_email_protocol_poll_interval_ms"), 1000)) / 1000.0,
    )
    headers = _cap_headers(capability)

    while True:
        status = str(payload.get("status") or "").strip().lower()
        if status in {"succeeded", "completed", "success", "ok"} or _access_token_from_payload(payload):
            result = _result_from_payload(
                payload,
                email=email,
                task_dir=task_dir,
                selected_proxy_url=selected_proxy_url,
                runtime_proxy_url=runtime_proxy_url,
                proxy_exit_ip=proxy_exit_ip,
            )
            log(f"[go-protocol] completed account_id={result.get('account_id') or '-'}")
            return result
        if status in {"failed", "error", "cancelled", "reconcile_required"}:
            message = str(
                payload.get("message")
                or payload.get("failure_code")
                or payload.get("error")
                or status
            )
            raise RuntimeError(f"Go 邮箱协议注册失败: {message}")
        if status in {"waiting_for_otp", "need_otp", "awaiting_otp", "otp_required"}:
            challenge = payload.get("challenge") if isinstance(payload.get("challenge"), dict) else {}
            challenge_id = str(challenge.get("challenge_id") or payload.get("challenge_id") or "").strip()
            state_version = challenge.get("state_version")
            if state_version is None:
                state_version = payload.get("state_version")
            try:
                state_version_i = int(state_version or 0)
            except Exception:
                state_version_i = 0
            go_handles_otp = bool(
                create_body.get("mailbox_client_id") and create_body.get("mailbox_refresh_token")
            )
            # Worker may re-park waiting_for_otp after wrong-code recovery; accept a new challenge_id.
            if otp_submitted and challenge_id and challenge_id == last_challenge_id:
                time.sleep(poll_interval)
            elif go_handles_otp:
                # In-worker Graph OTP: do not block on Python callback; poll status only.
                if not otp_submitted:
                    log(
                        f"[go-protocol] waiting for in-worker Graph OTP challenge={challenge_id or '-'} "
                        f"v={state_version_i}"
                    )
                    otp_submitted = True
                    last_challenge_id = challenge_id
                time.sleep(poll_interval)
            else:
                log(f"[go-protocol] worker 请求邮箱验证码 challenge={challenge_id or '-'} v={state_version_i}")
                code = str(otp_callback() or "").strip()
                if not code:
                    raise RuntimeError("Go 邮箱协议注册未获取到邮箱验证码")
                otp_body = {
                    "challenge_id": challenge_id,
                    "state_version": state_version_i,
                    "code": code,
                }
                payload = _http_json(
                    "POST",
                    f"{base}/v2/email-register/{job_id}/otp",
                    otp_body,
                    headers=headers,
                    timeout=min(60.0, float(timeout_seconds)),
                )
                otp_submitted = True
                last_challenge_id = challenge_id
                if payload.get("job_capability"):
                    capability = str(payload.get("job_capability") or capability)
                    headers = _cap_headers(capability)
                continue
        if time.monotonic() - started > timeout_seconds:
            raise TimeoutError(f"Go 邮箱协议注册超过 {timeout_seconds}s")
        time.sleep(poll_interval)
        wait_ms = min(5000, int(poll_interval * 1000))
        payload = _http_json(
            "GET",
            f"{base}/v2/email-register/{job_id}?wait_ms={wait_ms}",
            headers=headers,
            timeout=min(30.0, float(timeout_seconds)),
        )
