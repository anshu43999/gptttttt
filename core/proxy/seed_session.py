"""Proxy seed → per-registration session URL (region + fresh SID).

Supported seed shapes (password may be empty):

1. Kookeey / proxy001 style (underscore):
   account:pass@host:port
   → account_custom_zone_{REGION}_sid_{SID}_time_{TTL}:pass@host:port

2. Lajiao / region-sid style (hyphen):
   account:pass@host:port
   → account-region-{REGION}-sid-{SID}-t-{TTL}:pass@host:port

3. Bestgo / rrp style (zone-custom-region):
   account:pass@host:port
   → account-zone-custom-region-{REGION}:pass@host:port
   optional sticky: account-zone-custom-region-{REGION}-session-{SID}[:sessTime-{TTL}]

A seed may also already contain zone/sid tokens; those are stripped back to the
base account before rebuilding a session for the requested region.
"""
from __future__ import annotations

import re
import secrets
import string
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlunsplit, urlsplit


_KOOKEey_SESSION_RE = re.compile(
    r"^(?P<pre>.+?)_custom_zone_(?P<reg>[A-Za-z]{2})_sid_(?P<sid>[^_]+)_time_(?P<ttl>\d+)$",
    re.IGNORECASE,
)
_LAJIAO_SESSION_RE = re.compile(
    r"^(?P<pre>.+?)-region-(?P<reg>[A-Za-z]{2})-sid-(?P<sid>[^-]+)-t-(?P<ttl>\d+)$",
    re.IGNORECASE,
)
_BESTGO_SESSION_RE = re.compile(
    r"^(?P<pre>.+?)-zone-custom-region-(?P<reg>[A-Za-z]{2})"
    r"(?:-session-(?P<sid>[A-Za-z0-9]+))?"
    r"(?:-sessTime-(?P<ttl>\d+))?$",
    re.IGNORECASE,
)
_SCHEME_RE = re.compile(r"^(?P<scheme>https?|socks5h?|socks4)://", re.IGNORECASE)


@dataclass(frozen=True)
class ProxySeed:
    """Reusable proxy identity — not a sticky session."""

    account: str
    password: str
    host: str
    port: int
    style: str  # kookeey | lajiao | plain
    protocol: str  # socks5 | http | ...
    raw: str = ""

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def resource_key(self) -> str:
        if self.password:
            return f"{self.account}:{self.password}@{self.endpoint}"
        return f"{self.account}@{self.endpoint}"

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": "proxy_seed",
            "account": self.account,
            "password": self.password,
            "host": self.host,
            "port": self.port,
            "style": self.style,
            "protocol": self.protocol,
            "url": self.resource_key,
        }


@dataclass(frozen=True)
class ProxySession:
    seed: ProxySeed
    region: str
    sid: str
    ttl: int
    username: str
    url: str  # scheme://user:pass@host:port

    @property
    def credential(self) -> str:
        """user:pass@host:port without scheme."""
        userinfo = self.username
        if self.seed.password:
            userinfo = f"{self.username}:{self.seed.password}"
        return f"{userinfo}@{self.seed.endpoint}"


def _normalize_protocol(value: str | None, *, default: str = "socks5") -> str:
    text = str(value or default).strip().lower() or default
    if text in {"auto"}:
        return default
    if text == "socks5h":
        return "socks5"
    if text in {"socks5", "socks4", "http", "https"}:
        return text
    return default


def _normalize_region(value: str | None, *, fallback: str = "") -> str:
    text = str(value or "").split(",")[0].strip().upper()
    if text in {"", "AUTO", "ZONE", "ZONE_AUTO", "ANY", "*"}:
        return str(fallback or "").strip().upper()
    if re.fullmatch(r"[A-Z]{2}", text):
        return text
    return str(fallback or "").strip().upper()


def detect_style(account_or_username: str, host: str = "") -> str:
    text = str(account_or_username or "")
    host_l = str(host or "").lower()
    if _BESTGO_SESSION_RE.match(text) or "bestgo" in host_l or "rrp.best" in host_l:
        return "bestgo"
    if _KOOKEey_SESSION_RE.match(text) or "proxy001.com" in host_l or "kookeey" in host_l:
        return "kookeey"
    if _LAJIAO_SESSION_RE.match(text) or "lajiao" in host_l:
        return "lajiao"
    # Prefer kookeey when username already uses underscore zone tokens.
    if re.search(r"(?:^|_)(?:custom_)?zone_[A-Za-z]{2}(?:_|$)", text, flags=re.I):
        return "kookeey"
    if re.search(r"(?:^|-)zone-custom-region-[A-Za-z]{2}(?:-|$)", text, flags=re.I):
        return "bestgo"
    if re.search(r"(?:^|-)region-[A-Za-z]{2}(?:-|$)", text, flags=re.I):
        return "lajiao"
    return "plain"


def strip_session_username(username: str) -> tuple[str, str, str, int | None]:
    """Return (base_account, style_hint, region_hint, ttl_hint)."""
    text = unquote(str(username or "").strip())
    m = _BESTGO_SESSION_RE.match(text)
    if m:
        ttl = int(m.group("ttl")) if m.group("ttl") else None
        return m.group("pre"), "bestgo", m.group("reg").upper(), ttl
    m = _KOOKEey_SESSION_RE.match(text)
    if m:
        return m.group("pre"), "kookeey", m.group("reg").upper(), int(m.group("ttl"))
    m = _LAJIAO_SESSION_RE.match(text)
    if m:
        return m.group("pre"), "lajiao", m.group("reg").upper(), int(m.group("ttl"))
    return text, "", "", None


def new_sid(*, style: str, length: int | None = None) -> str:
    style = (style or "kookeey").lower()
    if style == "lajiao":
        # alnum mixed case, similar to tilianupi samples
        n = length or 8
        alphabet = string.ascii_letters + string.digits
        return "".join(secrets.choice(alphabet) for _ in range(n))
    if style == "bestgo":
        # bestgo/rrp session tokens are usually short alnum
        n = length or 10
        alphabet = string.ascii_letters + string.digits
        return "".join(secrets.choice(alphabet) for _ in range(n))
    # kookeey samples are 8-digit numeric sids
    n = length or 8
    return "".join(secrets.choice(string.digits) for _ in range(n))


def build_session_username(account: str, *, style: str, region: str, sid: str, ttl: int) -> str:
    region = _normalize_region(region) or "JP"
    ttl = max(1, int(ttl or 10))
    style = (style or "kookeey").lower()
    if style == "lajiao":
        return f"{account}-region-{region}-sid-{sid}-t-{ttl}"
    if style == "bestgo":
        # Match vendor sample: USER411934-zone-custom-region-JP
        # Attach session token so SID refresh rotates sticky exit.
        if sid:
            return f"{account}-zone-custom-region-{region}-session-{sid}-sessTime-{ttl}"
        return f"{account}-zone-custom-region-{region}"
    if style == "plain":
        return account
    return f"{account}_custom_zone_{region}_sid_{sid}_time_{ttl}"


def parse_seed(
    value: str,
    *,
    protocol: str = "socks5",
    style: str = "",
    default_ttl: int = 10,
) -> ProxySeed:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("empty proxy seed")
    scheme = ""
    body = raw
    m = _SCHEME_RE.match(raw)
    if m:
        scheme = m.group("scheme").lower()
        body = raw[m.end() :]
    if "@" not in body:
        raise ValueError(f"proxy seed missing user@host: {raw[:80]}")
    userinfo, hostport = body.rsplit("@", 1)
    if ":" not in hostport:
        raise ValueError(f"proxy seed missing host:port: {raw[:80]}")
    host, port_s = hostport.rsplit(":", 1)
    try:
        port = int(port_s)
    except Exception as exc:
        raise ValueError(f"proxy seed invalid port: {raw[:80]}") from exc
    if not host or port <= 0 or port > 65535:
        raise ValueError(f"proxy seed invalid endpoint: {raw[:80]}")

    if ":" in userinfo:
        username, password = userinfo.split(":", 1)
    else:
        username, password = userinfo, ""
    username = unquote(username)
    password = unquote(password)
    base, style_hint, _region_hint, _ttl_hint = strip_session_username(username)
    resolved_style = (style or style_hint or detect_style(username, host)).lower()
    if resolved_style not in {"kookeey", "lajiao", "bestgo", "plain"}:
        resolved_style = detect_style(base, host)
    proto = _normalize_protocol(scheme or protocol)
    return ProxySeed(
        account=base,
        password=password,
        host=host,
        port=port,
        style=resolved_style,
        protocol=proto,
        raw=raw,
    )


def seed_from_payload(payload: dict[str, Any] | None, *, resource_key: str = "") -> ProxySeed:
    data = payload if isinstance(payload, dict) else {}
    if str(data.get("kind") or "") == "proxy_seed" or data.get("account"):
        account = str(data.get("account") or "").strip()
        password = str(data.get("password") or "")
        host = str(data.get("host") or "").strip()
        port = int(data.get("port") or 0)
        if account and host and port:
            return ProxySeed(
                account=account,
                password=password,
                host=host,
                port=port,
                style=str(data.get("style") or detect_style(account, host)).lower(),
                protocol=_normalize_protocol(str(data.get("protocol") or "socks5")),
                raw=str(data.get("url") or resource_key or ""),
            )
    raw = str(data.get("url") or resource_key or data.get("seed") or "").strip()
    return parse_seed(raw, protocol=str(data.get("protocol") or "socks5"), style=str(data.get("style") or ""))


def build_session(
    seed: ProxySeed,
    *,
    region: str,
    sid: str | None = None,
    ttl: int | None = None,
    protocol: str | None = None,
) -> ProxySession:
    reg = _normalize_region(region) or "JP"
    session_ttl = max(1, int(ttl if ttl is not None else 10))
    session_sid = str(sid or "").strip() or new_sid(style=seed.style)
    username = build_session_username(
        seed.account,
        style=seed.style,
        region=reg,
        sid=session_sid,
        ttl=session_ttl,
    )
    proto = _normalize_protocol(protocol or seed.protocol)
    if seed.password:
        netloc = f"{username}:{seed.password}@{seed.endpoint}"
    else:
        netloc = f"{username}@{seed.endpoint}"
    url = urlunsplit((proto, netloc, "", "", ""))
    return ProxySession(seed=seed, region=reg, sid=session_sid, ttl=session_ttl, username=username, url=url)


def refresh_session(session_or_url: str | ProxySession, *, region: str = "", ttl: int | None = None) -> ProxySession:
    """Build a new SID session from an existing session URL or ProxySession."""
    if isinstance(session_or_url, ProxySession):
        seed = session_or_url.seed
        reg = _normalize_region(region) or session_or_url.region
        return build_session(seed, region=reg, ttl=ttl if ttl is not None else session_or_url.ttl)
    seed = parse_seed(str(session_or_url))
    # Prefer region embedded in the current session username when caller omits it.
    username = ""
    body = str(session_or_url)
    if "://" in body:
        username = unquote(urlsplit(body).username or "")
    elif "@" in body:
        username = body.split("@", 1)[0].split(":", 1)[0]
    _base, _style, region_hint, ttl_hint = strip_session_username(username)
    reg = _normalize_region(region) or region_hint or "JP"
    return build_session(seed, region=reg, ttl=ttl if ttl is not None else (ttl_hint or 10))


def is_proxy_network_error(message: str) -> bool:
    text = str(message or "").lower()
    # Global admission full is backpressure, not a bad proxy — do not rotate SID.
    if "admission rejected: global" in text or '"reason": "global"' in text or '"reason":"global"' in text:
        return False
    markers = (
        "socks connect",
        "general socks server failure",
        "proxyconnect",
        "proxy connection",
        "connection refused",
        "i/o timeout",
        "deadline exceeded",
        "unexpected eof",
        "eof",
        "tls handshake timeout",
        "network is unreachable",
        "no route to host",
        "dial tcp",
        "proxy_or_network",
        "err_proxy",
        "tunnel connection failed",
        "407 proxy",
        "cf_challenge",
        "session_invalid",
        "just a moment",
        "create_account_server_error",
        # Go admission MaxPerProxy=1: rotate sticky SID and retry.
        "admission rejected: proxy",
        "admission_rejected",
        "admission rejected",
        "http 429",
        "too many requests",
    )
    return any(marker in text for marker in markers)
