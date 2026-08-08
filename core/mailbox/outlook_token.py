from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, NamedTuple, Optional

from core.base_sms import extract_verification_code
from core.proxy_utils import build_requests_proxy_config


# Cap concurrent Graph token+inbox polls so 100-register does not stampede
# login.microsoftonline.com / graph.microsoft.com from one host.
_GRAPH_POLL_LIMIT_DEFAULT = 20
_graph_poll_sem_lock = threading.Lock()
_graph_poll_sem: threading.Semaphore | None = None
_graph_poll_sem_limit = 0

class OutlookTokenAccount(NamedTuple):
    email: str
    password: str
    client_id: str
    refresh_token: str


class OutlookTokenMailbox:
    def __init__(self, config: dict[str, Any], *, log_fn: Callable[[str], None] | None = None):
        self.config = config
        self.log_fn = log_fn or (lambda _msg: None)

    def log(self, message: str) -> None:
        self.log_fn(message)

    @staticmethod
    def _graph_poll_limit(config: dict[str, Any] | None) -> int:
        cfg = config if isinstance(config, dict) else {}
        raw = cfg.get("outlook_graph_max_concurrent") or cfg.get("email_otp_graph_max_concurrent")
        try:
            n = int(raw) if raw is not None and str(raw).strip() != "" else _GRAPH_POLL_LIMIT_DEFAULT
        except (TypeError, ValueError):
            n = _GRAPH_POLL_LIMIT_DEFAULT
        return max(1, min(100, n))

    @classmethod
    def _graph_poll_semaphore(cls, config: dict[str, Any] | None) -> threading.Semaphore:
        global _graph_poll_sem, _graph_poll_sem_limit
        limit = cls._graph_poll_limit(config)
        with _graph_poll_sem_lock:
            if _graph_poll_sem is None or _graph_poll_sem_limit != limit:
                _graph_poll_sem = threading.Semaphore(limit)
                _graph_poll_sem_limit = limit
            return _graph_poll_sem

    def _resolve_graph_proxy_url(self) -> str:
        """Task sticky SID for Graph — remote socks5h/http only, never local bridge."""
        cfg = self.config if isinstance(self.config, dict) else {}
        for key in (
            "mailat_protocol_proxy",
            "lajiao_proxy_credentials",
            "proxy",
            "outlook_graph_proxy",
            "email_otp_proxy",
        ):
            value = str(cfg.get(key) or "").strip()
            if not value:
                continue
            # Multi-line credential dumps: take first non-empty line only.
            line = next((part.strip() for part in value.replace("\r", "\n").split("\n") if part.strip()), "")
            if not line:
                continue
            if "://" not in line:
                line = f"socks5h://{line}"
            low = line.lower()
            # Never use local HTTP CONNECT bridge / loopback / fake seeds for Graph.
            if (
                "127.0.0.1" in low
                or "localhost" in low
                or "proxy.local" in low
                or "[::1]" in low
            ):
                continue
            return line
        return ""

    def _graph_proxy_candidates(self) -> list[str]:
        """Remote-only candidates for Microsoft Graph (no local bridge).

        Measured (bestgo/1024): requests needs socks5h or http; plain socks5 SSL-EOFs.
        """
        raw = self._resolve_graph_proxy_url()
        if not raw:
            return []
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(raw if "://" in raw else f"socks5h://{raw}")
        scheme = (parts.scheme or "socks5h").lower()
        if scheme == "socks5":
            scheme = "socks5h"
        hostport = parts.netloc or ""
        if not hostport and parts.path:
            hostport = parts.path
        if not hostport:
            return []
        # Rebuild without path/query noise.
        base = urlunsplit((scheme, hostport, "", "", ""))
        # Prefer remote DNS SOCKS, then HTTP CONNECT to same upstream (bestgo supports both).
        out: list[str] = []
        for s in ("socks5h", "http"):
            cand = urlunsplit((s, hostport, "", "", ""))
            if cand not in out:
                out.append(cand)
        if base not in out:
            out.insert(0, base)
        return out

    def _graph_proxies(self) -> dict[str, str] | None:
        cands = self._graph_proxy_candidates()
        if not cands:
            return None
        # Default first hop: socks5h (remote DNS). HTTP tried on transport failure.
        return build_requests_proxy_config(cands[0])

    def _poll_interval_seconds(self) -> float:
        raw = self.config.get("email_otp_poll_interval") if isinstance(self.config, dict) else None
        try:
            n = float(raw) if raw is not None and str(raw).strip() != "" else 3.0
        except (TypeError, ValueError):
            n = 3.0
        return max(1.0, min(30.0, n))

    @staticmethod
    def _is_dead_graph_token_error(message: str) -> bool:
        text = str(message or "").lower()
        markers = (
            "invalid_grant",
            "aadsts70000",
            "aadsts50173",
            "aadsts50076",
            "compromised",
            "user account is found as compromised",
            "account security interrupt",
            "refresh token has expired",
            "the refresh token has expired due to inactivity",
            "interaction_required",
        )
        return any(m in text for m in markers)

    def mark_disabled(self, email: str, reason: str = "graph_token_dead") -> None:
        """Permanent local mark: dead MSA/Graph credential — do not retry OTP on this mailbox."""
        self._append_pool_event(email, "disabled", reason)
        # Best-effort resource_pool disable when running under Dashboard.
        try:
            from application.resource_pool_service import ResourcePoolService

            key = str(email or "").strip()
            if not key:
                return
            rps = ResourcePoolService()
            row = rps.repo.get("email", "outlook_token", key)
            if not row.get("id"):
                # resource_key may be lowercased
                row = rps.repo.get("email", "outlook_token", key.lower())
            rid = int(row.get("id") or 0)
            if rid > 0:
                rps.repo.set_status(rid, status="disabled", error=str(reason or "graph_token_dead")[:300])
                self.log(f"  Outlook token 已禁用(resource_pool): {key} reason={reason[:120]}")
        except Exception as exc:
            self.log(f"  Outlook token 禁用 resource_pool 失败: {str(exc).splitlines()[0][:160]}")

    def _pool_state_path(self) -> Path:
        configured = str(self.config.get("outlook_pool_state_file") or "").strip()
        if configured:
            path = Path(configured)
            path.parent.mkdir(parents=True, exist_ok=True)
            return path
        data_dir = Path("data")
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir / "outlook_pool_state.jsonl"

    def _load_pool_events(self) -> list[dict[str, Any]]:
        path = self._pool_state_path()
        if not path.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except Exception:
                continue
            if isinstance(event, dict):
                events.append(event)
        return events

    def _append_pool_event(self, email: str, status: str, reason: str = "") -> None:
        normalized = str(email or "").strip().lower()
        if not normalized:
            return
        event = {
            "email": normalized,
            "status": status,
            "reason": reason,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        path = self._pool_state_path()
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _load_used_emails(self) -> set[str]:
        used: set[str] = set()
        for event in self._load_pool_events():
            status = str(event.get("status") or "").strip().lower()
            email = str(event.get("email") or "").strip().lower()
            if email and status in {"used", "completed", "registered", "disabled"}:
                used.add(email)
        return used

    def _retryable_failures(self, email_value: str) -> int:
        normalized = str(email_value or "").strip().lower()
        if not normalized:
            return 0
        count = 0
        for event in self._load_pool_events():
            if str(event.get("email") or "").strip().lower() != normalized:
                continue
            status = str(event.get("status") or "").strip().lower()
            if status in {"used", "completed", "registered"}:
                return 999999
            if status in {"failed_retryable", "cooldown", "otp_timeout", "wrong_otp"}:
                count += 1
        return count

    def _is_cooled_down(self, email_value: str) -> bool:
        failures = self._retryable_failures(email_value)
        limit = int(self.config.get("outlook_failed_retryable_limit", 2) or 2)
        if failures < limit:
            return False
        hours = float(self.config.get("outlook_cooldown_hours", 24) or 24)
        cutoff = datetime.now() - timedelta(hours=hours)
        latest: Optional[datetime] = None
        for event in self._load_pool_events():
            if str(event.get("email") or "").strip().lower() != str(email_value or "").strip().lower():
                continue
            raw_ts = str(event.get("updated_at") or "")
            try:
                ts = datetime.fromisoformat(raw_ts)
            except Exception:
                continue
            latest = ts if latest is None or ts > latest else latest
        return latest is None or latest > cutoff

    def _has_prepared_outlook_lease(self, email_value: str) -> bool:
        if not self.config.get("_resources_prepared"):
            return False
        target = str(email_value or "").strip().lower()
        if not target:
            return False
        leases = self.config.get("resource_leases") if isinstance(self.config.get("resource_leases"), list) else []
        return any(
            isinstance(item, dict)
            and str(item.get("type") or "").strip().lower() == "email"
            and str(item.get("provider") or "").strip().lower() == "outlook_token"
            and str(item.get("key") or "").strip().lower() == target
            for item in leases
        )


    def candidates(self, email: str = "", *, include_used: bool = False) -> list[OutlookTokenAccount]:
        target = str(email or self.config.get("outlook_email") or "").strip().lower()
        configured_email = str(self.config.get("outlook_email") or "").strip()
        configured_password = str(self.config.get("outlook_password") or "").strip()
        configured_client_id = str(self.config.get("outlook_client_id") or self.config.get("oauth_client_id") or "").strip()
        configured_refresh_token = str(self.config.get("outlook_refresh_token") or "").strip()
        used = set() if include_used else self._load_used_emails()
        candidates: list[OutlookTokenAccount] = []
        if configured_email and configured_client_id and configured_refresh_token:
            configured_key = configured_email.lower()
            lease_override = self._has_prepared_outlook_lease(configured_key)
            if (not target or configured_key == target) and (include_used or lease_override or (configured_key not in used and not self._is_cooled_down(configured_key))):
                candidates.append(OutlookTokenAccount(configured_email, configured_password, configured_client_id, configured_refresh_token))

        order_paths: list[Path] = []
        configured_order = str(self.config.get("outlook_token_order_file") or "").strip()
        if configured_order:
            order_paths.append(Path(configured_order))
        order_paths.append(Path("outlook_accounts_token.txt"))
        seen: set[str] = {item.email.lower() for item in candidates}
        for order_file in order_paths:
            if not order_file.exists():
                continue
            for raw_row in order_file.read_text(encoding="utf-8-sig").splitlines():
                row = raw_row.strip()
                if not row:
                    continue
                parts = [part.strip() for part in row.split("----")]
                if len(parts) != 4:
                    continue
                candidate_email, candidate_password, client_id, refresh_token = parts
                candidate_key = candidate_email.lower()
                if "@" not in candidate_email or not client_id or not refresh_token:
                    continue
                if candidate_key in seen or (not include_used and (candidate_key in used or self._is_cooled_down(candidate_key))):
                    continue
                if not target or candidate_key == target:
                    candidates.append(OutlookTokenAccount(candidate_email, candidate_password, client_id, refresh_token))
                    seen.add(candidate_key)
        return candidates

    def first(self, email: str = "", *, include_used: bool = False) -> OutlookTokenAccount:
        candidates = self.candidates(email, include_used=include_used)
        if not candidates:
            raise RuntimeError("邮箱注册缺少可用 Outlook token 邮箱；请在服务商页导入 Outlook token 池")
        return candidates[0]

    def refresh_graph_access_token(
        self,
        client_id: str,
        refresh_token: str,
        *,
        proxies: dict[str, str] | None | object = ...,
    ) -> str:
        import requests

        def _once(use_proxies: dict[str, str] | None) -> str:
            session = requests.Session()
            session.trust_env = False
            scopes = (
                "https://graph.microsoft.com/.default offline_access",
                "https://graph.microsoft.com/Mail.Read offline_access",
            )
            last_error = ""
            try:
                for scope in scopes:
                    try:
                        response = session.post(
                            "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
                            data={
                                "client_id": client_id,
                                "grant_type": "refresh_token",
                                "refresh_token": refresh_token,
                                "scope": scope,
                            },
                            timeout=30,
                            proxies=use_proxies,
                        )
                    except requests.RequestException as exc:
                        last_error = f"network {exc}"
                        continue
                    data = response.json() if response.content else {}
                    token = str(data.get("access_token") or "")
                    if token:
                        return token
                    last_error = f"{response.status_code} {data.get('error')} {str(data.get('error_description', ''))[:160]}"
                    err = str(data.get("error") or "").lower()
                    desc = str(data.get("error_description") or "").lower()
                    if self._is_dead_graph_token_error(f"{err} {desc} {last_error}"):
                        break
                    if "invalid_grant" not in err and "aadsts70000" not in desc and "scope" not in desc:
                        break
            finally:
                session.close()
            raise RuntimeError(f"Outlook Graph token 刷新失败: {last_error}")

        # Explicit proxies from caller (incl. None=direct).
        if proxies is not ...:
            return _once(proxies if isinstance(proxies, dict) or proxies is None else None)

        # Remote-only chain: socks5h → http(same upstream) → host direct. Never local bridge.
        chain: list[dict[str, str] | None] = []
        for cand in self._graph_proxy_candidates():
            cfg = build_requests_proxy_config(cand)
            if cfg and cfg not in chain:
                chain.append(cfg)
        chain.append(None)  # direct host egress last

        last_exc: Exception | None = None
        for i, use_px in enumerate(chain):
            try:
                return _once(use_px)
            except RuntimeError as exc:
                last_exc = exc
                msg = str(exc)
                if self._is_dead_graph_token_error(msg):
                    raise
                if not self._is_graph_proxy_transport_error(msg) and use_px is not None:
                    # Non-transport business error from Microsoft — don't thrash routes.
                    raise
                label = "direct" if use_px is None else str((use_px or {}).get("https") or "")[:60]
                if i + 1 < len(chain):
                    self.log(f"  Outlook Graph token 路由失败({label})，换下一跳: {msg[:140]}")
                continue
        raise RuntimeError(str(last_exc or "Outlook Graph token 刷新失败"))

    @staticmethod
    def _is_graph_proxy_transport_error(message: str) -> bool:
        text = str(message or "").lower()
        markers = (
            "network ",
            "ssl",
            "unexpected_eof",
            "max retries exceeded",
            "socks",
            "connection pool",
            "timed out",
            "timeout",
            "connection reset",
            "connection aborted",
            "proxyerror",
            "tunnel connection failed",
        )
        return any(m in text for m in markers)

    @staticmethod
    def _parse_graph_received_at(value: Any) -> Optional[datetime]:
        try:
            raw = str(value or "").strip()
            if raw.endswith("Z"):
                raw = raw[:-1] + "+00:00"
            received_at = datetime.fromisoformat(raw)
            if received_at.tzinfo is None:
                return received_at.replace(tzinfo=timezone.utc)
            return received_at.astimezone(timezone.utc)
        except (TypeError, ValueError):
            return None

    def _list_graph_messages(
        self,
        access_token: str,
        *,
        proxies: dict[str, str] | None | object = ...,
    ) -> list[dict[str, Any]]:
        import requests

        def _once(use_proxies: dict[str, str] | None) -> list[dict[str, Any]]:
            session = requests.Session()
            session.trust_env = False
            try:
                response = session.get(
                    "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Prefer": 'outlook.body-content-type="text"',
                    },
                    params={
                        "$top": "50",
                        "$select": "from,subject,body,receivedDateTime",
                        "$orderby": "receivedDateTime desc",
                    },
                    timeout=30,
                    proxies=use_proxies,
                )
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise RuntimeError(f"Outlook Graph 邮箱响应不是 JSON: HTTP {response.status_code}") from exc
            except requests.RequestException as exc:
                raise RuntimeError(f"Outlook Graph 邮箱读取网络失败: {exc}") from exc
            finally:
                session.close()

            if not isinstance(payload, dict):
                raise RuntimeError(f"Outlook Graph 邮箱响应格式错误: HTTP {response.status_code}")
            if response.status_code != 200:
                error = payload.get("error") or {}
                code = str(error.get("code") or "unknown_error") if isinstance(error, dict) else "unknown_error"
                message = str(error.get("message") or "")[:160] if isinstance(error, dict) else ""
                raise RuntimeError(f"Outlook Graph 邮箱读取失败: HTTP {response.status_code} {code} {message}".rstrip())
            messages = payload.get("value")
            if not isinstance(messages, list):
                raise RuntimeError("Outlook Graph 邮箱响应缺少消息列表")
            return [message for message in messages if isinstance(message, dict)]

        if proxies is not ...:
            return _once(proxies if isinstance(proxies, dict) or proxies is None else None)

        chain: list[dict[str, str] | None] = []
        for cand in self._graph_proxy_candidates():
            cfg = build_requests_proxy_config(cand)
            if cfg and cfg not in chain:
                chain.append(cfg)
        chain.append(None)

        last_exc: Exception | None = None
        for i, use_px in enumerate(chain):
            try:
                return _once(use_px)
            except RuntimeError as exc:
                last_exc = exc
                msg = str(exc)
                if not self._is_graph_proxy_transport_error(msg) and use_px is not None:
                    raise
                label = "direct" if use_px is None else str((use_px or {}).get("https") or "")[:60]
                if i + 1 < len(chain):
                    self.log(f"  Outlook Graph 读信路由失败({label})，换下一跳: {msg[:140]}")
                continue
        raise RuntimeError(str(last_exc or "Outlook Graph 邮箱读取失败"))

    def _extract_openai_code_from_text(self, text: str) -> str:
        return extract_verification_code(text, expected_lengths=(6,))

    def wait_for_openai_code(
        self,
        account: OutlookTokenAccount,
        *,
        timeout: int = 180,
        not_before: datetime | None = None,
        reject_codes: set[str] | None = None,
    ) -> str:
        deadline = time.time() + max(15, int(timeout or 180))
        # Prefer caller-supplied not_before (S8 / challenge time). Default 60s skew —
        # tighter than the old 3min stale-code window, looser than 20s under Graph lag.
        if not_before is not None:
            started_at = not_before.astimezone(timezone.utc) if not_before.tzinfo else not_before.replace(tzinfo=timezone.utc)
        else:
            started_at = datetime.now(timezone.utc) - timedelta(seconds=60)
        rejected = {str(c).strip() for c in (reject_codes or set()) if str(c).strip()}
        proxy_url = self._resolve_graph_proxy_url()
        proxy_hint = "direct"
        if proxy_url and "@" in proxy_url:
            proxy_hint = proxy_url.split("@", 1)[-1]
        elif proxy_url:
            proxy_hint = proxy_url[:48]
        poll_iv = self._poll_interval_seconds()
        sem = self._graph_poll_semaphore(self.config)
        self.log(
            f"  使用 Outlook Graph 自动读取验证码: {account.email} "
            f"since={started_at.isoformat(timespec='seconds')} reject={len(rejected)} "
            f"proxy={proxy_hint} conc={self._graph_poll_limit(self.config)}"
        )

        access_token = ""
        consecutive_errors = 0
        while time.time() < deadline:
            acquired = sem.acquire(timeout=max(1.0, min(15.0, deadline - time.time())))
            if not acquired:
                if time.time() >= deadline:
                    break
                time.sleep(0.2)
                continue
            try:
                if not access_token:
                    try:
                        access_token = self.refresh_graph_access_token(account.client_id, account.refresh_token)
                    except Exception as exc:
                        msg = str(exc)
                        if self._is_dead_graph_token_error(msg):
                            self.mark_disabled(account.email, reason=msg[:240])
                            self.log(f"  Outlook Graph token 永久失效，已禁用邮箱: {account.email}")
                            raise RuntimeError(f"Outlook Graph token 永久失效: {msg}") from exc
                        consecutive_errors += 1
                        self.log(f"  Outlook Graph token 刷新异常: {exc}")
                        # backoff under stampede / proxy blip
                        time.sleep(min(15.0, poll_iv * (1 + min(5, consecutive_errors))))
                        continue
                best_code = ""
                best_received: datetime | None = None
                try:
                    messages = self._list_graph_messages(access_token)
                    consecutive_errors = 0
                except Exception as exc:
                    consecutive_errors += 1
                    self.log(f"  Outlook Graph 读取异常: {exc}")
                    # Force token refresh after repeated auth-ish failures.
                    if consecutive_errors >= 3:
                        access_token = ""
                    time.sleep(min(15.0, poll_iv * (1 + min(5, consecutive_errors))))
                    continue
                for message in messages:
                    sender = str(((message.get("from") or {}).get("emailAddress") or {}).get("address") or "")
                    subject = str(message.get("subject") or "")
                    body = str((message.get("body") or {}).get("content") or "")
                    received_at = self._parse_graph_received_at(message.get("receivedDateTime"))
                    searchable = f"{sender} {subject} {body[:1000]}"
                    if not re.search(r"openai|chatgpt", searchable, flags=re.I):
                        continue
                    if received_at and received_at < started_at:
                        continue
                    code = self._extract_openai_code_from_text(body)
                    if not code or code in rejected:
                        continue
                    if best_received is None or (received_at and received_at > best_received):
                        best_code = code
                        best_received = received_at or datetime.now(timezone.utc)
                if best_code:
                    self.log(f"  Outlook Graph 获取验证码: {best_code}")
                    return best_code
            finally:
                sem.release()
            time.sleep(poll_iv)
        self.log("  Outlook Graph 未读取到验证码")
        return ""

    def mark_used(self, email: str, reason: str = "registered") -> None:
        self._append_pool_event(email, "registered", reason)

    def mark_cooldown(self, email: str, reason: str) -> None:
        self._append_pool_event(email, "failed_retryable", reason)
