from __future__ import annotations

import json
import os
import secrets
import re
from pathlib import Path
from urllib.parse import urlsplit

from datetime import datetime, timedelta
from typing import Any

from core.base_sms import UserProvidedSmsProvider
from core.mailbox_providers import LinkApiMailbox
from infrastructure.repositories.resource_pool_repository import ResourcePoolRepository, ResourceLease


PROXY_PATTERN = re.compile(r"^(?:(?P<scheme>https?|socks5h?|socks4)://)?(?P<userinfo>[^@\s:]+:[^@\s]+@)?(?P<host>[^@\s:]+):(?P<port>\d{1,5})$")

SMS_TIMEOUT_MARKERS = (
    "sms timeout",
    "otp timeout",
    "awaiting_sms_code",
    "no sms",
    "未收到短信",
    "短信超时",
    "短信验证码超时",
    "验证码超时",
    "接码超时",
)
PHONE_USED_MARKERS = (
    "old phone",
    "existing account",
    "phone number is already",
    "phone already",
    "already registered with this phone",
    "手机号已注册",
    "号码已注册",
    "旧号",
    "老号",
    "already used",
    "この電話番号は既に",
    "電話番号は既に",
    "既に使用",
    "すでに使用",
    "最近使用された",
    "しばらくしてから",
    "recently used",
    "try again later",
    "上限数",
    "関連付けられています",
    "already associated",
    "associated with the maximum",
    "maximum number of accounts",
    "limit reached",
    "too many accounts",
)
PHONE_INVALID_MARKERS = (
    "invalid phone",
    "invalid number",
    "phone number is invalid",
    "入力した電話番号は無効",
    "電話番号は無効",
    "無効です",
    "手机号无效",
    "号码无效",
)
PROXY_FAILURE_MARKERS = (
    "err_proxy",
    "proxy authentication",
    "proxy connection",
    "proxy verification failed",
    "cannot connect to proxy",
    "failed to connect to proxy",
    "407 proxy",
    "socks connect",
    "tunnel connection failed",
    "代理连接失败",
    "代理认证失败",
    "代理不可用",
    "cloudflare",
    "cf_chl_",
    "turnstile",
    "captcha",
    "verify you are human",
    "security check",
    "status=403",
    "server responded with a status of 403",
    "openai 预检风控",
)
OPENAI_RATE_RISK_MARKERS = (
    "rate limit",
    "too many requests",
    "http 429",
    "status=429",
    "risk",
    "suspicious",
    "unusual activity",
    "access denied",
    "try again later",
    "temporarily unable",
    "风控",
    "风险",
    "频率限制",
    "请求过多",
)
EMAIL_USED_MARKERS = (
    "email is already associated",
    "email already in use",
    "email already used",
    "email already exists",
    "email already linked",
    "already linked to another account",
    "已关联至其他帐户",
    "已关联至其他账户",
    "already associated with another account",
    "邮箱已被使用",
    "邮箱已注册",
    "邮箱已经存在",
    # OpenAI S10 403 on already-registered / deleted / half-dead mailboxes
    "you do not have an account because it has been deleted or deactivated",
    "do not have an account",
    "deleted or deactivated",
)
OAUTH_NETWORK_MARKERS = (
    "oauth network",
    "token exchange failed",
    "network error",
    "connection timeout",
    "read timed out",
    "connection reset",
    "connection closed",
    "err_empty_response",
    "err_connection_closed",
    "err_connection_reset",
    "err_connection_timed_out",
    "err_failed",
    "page.goto:",
    "name resolution",
    "temporarily unavailable",
    "oauth 超时",
    "oauth 网络",
    "网络错误",
    "网络超时",
)

PROXY_SUCCESS_COOLDOWN_SECONDS = 1800
PROXY_FAILURE_COOLDOWN_SECONDS = 1800
BIND_PHONE_FAILURE_COOLDOWN_SECONDS = 3600

BIND_PHONE_OUTCOME_SUCCESS = "success"
BIND_PHONE_OUTCOME_RELEASED = "released"
BIND_PHONE_OUTCOME_RECENTLY_USED = "recently_used"
BIND_PHONE_OUTCOME_INVALID = "invalid"
BIND_PHONE_OUTCOME_TIMEOUT = "timeout"
BIND_PHONE_OUTCOME_OTP_SUBMIT_FAILED = "otp_submit_failed"
BIND_PHONE_OUTCOME_TRANSPORT_FAILED = "transport_failed"

BIND_PHONE_FAILURE_RELEASED = "bind_phone_released"
BIND_PHONE_FAILURE_RECENTLY_USED = "phone_recently_used"
BIND_PHONE_FAILURE_INVALID_NUMBER = "phone_invalid_number"
BIND_PHONE_FAILURE_INVALID_OTP = "phone_invalid_otp"
BIND_PHONE_FAILURE_TIMEOUT = "phone_timeout"
BIND_PHONE_FAILURE_OTP_SUBMIT_FAILED = "phone_otp_submit_failed"
BIND_PHONE_FAILURE_OTP_SUBMIT_STATUS_0 = "phone_otp_submit_status_0"
BIND_PHONE_FAILURE_OTP_SUBMIT_TRANSPORT = "phone_otp_submit_transport_failed"
BIND_PHONE_FAILURE_TRANSPORT = "phone_transport_failed"

BIND_PHONE_STATUS_ZERO_MARKERS = (
    "status 0",
    "status=0",
    "status: 0",
    BIND_PHONE_FAILURE_OTP_SUBMIT_STATUS_0,
)

BIND_PHONE_OTP_INVALID_MARKERS = (
    "invalid otp",
    "invalid code",
    "incorrect",
    "wrong",
    "expired",
    "验证码校验失败",
    BIND_PHONE_FAILURE_INVALID_OTP,
)

BIND_PHONE_OTP_SUBMIT_FAILURE_MARKERS = (
    "otp submit",
    "phone-otp",
    "验证码校验失败",
    BIND_PHONE_FAILURE_OTP_SUBMIT_FAILED,
    BIND_PHONE_FAILURE_OTP_SUBMIT_STATUS_0,
    BIND_PHONE_FAILURE_OTP_SUBMIT_TRANSPORT,
)


def _contains_marker(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def classify_bind_phone_failure(reason: str, *, phase: str = "") -> tuple[str, str]:
    text = str(reason or "").strip()
    lowered = text.lower()
    phase = str(phase or "").strip().lower()
    if not lowered:
        if phase == "cleanup":
            return (BIND_PHONE_OUTCOME_RELEASED, BIND_PHONE_FAILURE_RELEASED)
        return (BIND_PHONE_OUTCOME_TRANSPORT_FAILED, BIND_PHONE_FAILURE_TRANSPORT)
    if BIND_PHONE_FAILURE_RECENTLY_USED in lowered or _contains_marker(lowered, PHONE_USED_MARKERS):
        return (BIND_PHONE_OUTCOME_RECENTLY_USED, BIND_PHONE_FAILURE_RECENTLY_USED)
    if BIND_PHONE_FAILURE_INVALID_NUMBER in lowered or _contains_marker(lowered, PHONE_INVALID_MARKERS):
        return (BIND_PHONE_OUTCOME_INVALID, BIND_PHONE_FAILURE_INVALID_NUMBER)
    if BIND_PHONE_FAILURE_TIMEOUT in lowered or "未获取到短信验证码" in text or _contains_marker(lowered, SMS_TIMEOUT_MARKERS):
        return (BIND_PHONE_OUTCOME_TIMEOUT, BIND_PHONE_FAILURE_TIMEOUT)
    if phase == "otp" and (BIND_PHONE_FAILURE_INVALID_OTP in lowered or _contains_marker(lowered, BIND_PHONE_OTP_INVALID_MARKERS)):
        return (BIND_PHONE_OUTCOME_INVALID, BIND_PHONE_FAILURE_INVALID_OTP)
    if phase == "otp" and _contains_marker(lowered, BIND_PHONE_STATUS_ZERO_MARKERS):
        return (BIND_PHONE_OUTCOME_OTP_SUBMIT_FAILED, BIND_PHONE_FAILURE_OTP_SUBMIT_STATUS_0)
    if phase == "otp" and (
        BIND_PHONE_FAILURE_OTP_SUBMIT_TRANSPORT in lowered
        or _contains_marker(lowered, OAUTH_NETWORK_MARKERS)
        or "transport" in lowered
        or "network" in lowered
    ):
        return (BIND_PHONE_OUTCOME_OTP_SUBMIT_FAILED, BIND_PHONE_FAILURE_OTP_SUBMIT_TRANSPORT)
    if phase == "otp" and _contains_marker(lowered, BIND_PHONE_OTP_SUBMIT_FAILURE_MARKERS):
        return (BIND_PHONE_OUTCOME_OTP_SUBMIT_FAILED, BIND_PHONE_FAILURE_OTP_SUBMIT_FAILED)
    if BIND_PHONE_FAILURE_TRANSPORT in lowered or _contains_marker(lowered, OAUTH_NETWORK_MARKERS) or "transport" in lowered or "network" in lowered:
        return (BIND_PHONE_OUTCOME_TRANSPORT_FAILED, BIND_PHONE_FAILURE_TRANSPORT)
    if phase == "send":
        return (BIND_PHONE_OUTCOME_TRANSPORT_FAILED, BIND_PHONE_FAILURE_TRANSPORT)
    if phase == "otp":
        return (BIND_PHONE_OUTCOME_OTP_SUBMIT_FAILED, BIND_PHONE_FAILURE_OTP_SUBMIT_FAILED)
    return (BIND_PHONE_OUTCOME_TRANSPORT_FAILED, BIND_PHONE_FAILURE_TRANSPORT)


def bind_phone_failure_resource_status(outcome: str, *, failure_code: str = "") -> tuple[str, int]:
    outcome = str(outcome or "").strip().lower()
    failure_code = str(failure_code or "").strip().lower()
    if outcome in {"", BIND_PHONE_OUTCOME_RELEASED}:
        return ("available", 0)
    if outcome == BIND_PHONE_OUTCOME_RECENTLY_USED:
        return ("used", 0)
    if outcome == BIND_PHONE_OUTCOME_INVALID and failure_code == BIND_PHONE_FAILURE_INVALID_NUMBER:
        return ("disabled", 0)
    if outcome in {
        BIND_PHONE_OUTCOME_INVALID,
        BIND_PHONE_OUTCOME_TIMEOUT,
        BIND_PHONE_OUTCOME_OTP_SUBMIT_FAILED,
        BIND_PHONE_OUTCOME_TRANSPORT_FAILED,
    }:
        return ("cooldown", BIND_PHONE_FAILURE_COOLDOWN_SECONDS)
    return ("available", 0)


RESOURCE_CATEGORY_OPTIONS = [
    {"key": "phone/user_phone_url", "resource_type": "phone", "provider": "user_phone_url", "label": "注册手机号池", "group": "phone", "importable": True},
    {"key": "phone/bind_user_phone_url", "resource_type": "phone", "provider": "bind_user_phone_url", "label": "绑定手机号池", "group": "phone", "importable": True},
    {"key": "proxy/proxy_seed", "resource_type": "proxy", "provider": "proxy_seed", "label": "代理 Seed 池", "group": "proxy", "importable": True},
    {"key": "proxy/lajiao_credentials", "resource_type": "proxy", "provider": "lajiao_credentials", "label": "旧版代理会话池(兼容)", "group": "proxy", "importable": True},
    {"key": "email/outlook_token", "resource_type": "email", "provider": "outlook_token", "label": "Outlook Token 邮箱池", "group": "email", "importable": True},
    {"key": "email/icloud_api", "resource_type": "email", "provider": "icloud_api", "label": "iCloud API 邮箱池", "group": "email", "importable": True},
    {"key": "email/icloud_privacy", "resource_type": "email", "provider": "icloud_privacy", "label": "iCloud 隐私邮箱池", "group": "email", "importable": True},
    {"key": "email/forwarded_domain", "resource_type": "email", "provider": "forwarded_domain", "label": "转发域名绑定邮箱池", "group": "email", "importable": False},
    {"key": "email/cfworker_admin_api", "resource_type": "email", "provider": "cfworker_admin_api", "label": "CFWorker 绑定邮箱池", "group": "email", "importable": False},
    {"key": "sms_activation/herosms_api", "resource_type": "sms_activation", "provider": "herosms_api", "label": "HeroSMS 激活池", "group": "sms_activation", "importable": False},
]


class ResourcePoolService:
    def __init__(self, repo: ResourcePoolRepository | None = None):
        self.repo = repo or ResourcePoolRepository()

    def import_phone_urls(self, text: str, provider: str = "user_phone_url") -> int:
        entries = UserProvidedSmsProvider.parse_entries(text)
        rows = [(phone, {"phone": phone, "sms_url": url}) for phone, url in entries]
        return self.repo.import_many("phone", provider, rows)

    def import_lajiao_credentials(self, text: str, *, provider: str = "lajiao_credentials", region: str = "JP", protocol: str = "auto") -> int:
        # Backward-compatible entry: sticky session lines are collapsed into seeds.
        if provider in {"proxy_seed", "seed", "lajiao_seed"}:
            return self.import_proxy_seeds(text, protocol=protocol)
        return self.import_proxy_seeds(text, protocol=protocol, also_legacy_provider=provider, legacy_region=region)

    def import_proxy_seeds(
        self,
        text: str,
        *,
        protocol: str = "socks5",
        style: str = "",
        also_legacy_provider: str = "",
        legacy_region: str = "JP",
    ) -> int:
        """Import reusable proxy seeds (base account@host). Session SIDs are generated per task."""
        from core.proxy.seed_session import parse_seed

        seeds: list[tuple[str, dict[str, Any]]] = []
        seen: set[str] = set()
        for line in str(text or "").replace("\r", "\n").split("\n"):
            raw = line.strip()
            if not raw or raw.startswith("#"):
                continue
            value = self.normalize_proxy_value(raw)
            self.validate_proxy_format(value)
            seed = parse_seed(value, protocol=protocol or "socks5", style=style)
            key = seed.resource_key
            if key in seen:
                continue
            seen.add(key)
            seeds.append((key, seed.to_payload()))
        if not seeds:
            raise ValueError("请导入至少一条代理 seed（account:pass@host:port）")
        count = self.repo.import_many("proxy", "proxy_seed", seeds)
        # Optional: keep one legacy sticky row for old readers (disabled by default path).
        if also_legacy_provider and also_legacy_provider != "proxy_seed":
            # Do not flood legacy pool with thousands of sticky SIDs anymore.
            pass
        return count

    def migrate_sticky_proxies_to_seeds(self, *, disable_legacy: bool = True) -> dict[str, int]:
        """Collapse sticky session proxy rows into unique proxy_seed rows."""
        from core.proxy.seed_session import parse_seed
        from infrastructure import db as _db

        legacy = self.repo.list("proxy", "lajiao_credentials", "")
        created = 0
        skipped = 0
        seen: set[str] = set()
        for item in legacy:
            key = str(item.get("resource_key") or "")
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            raw = str(payload.get("url") or key or "").strip()
            if not raw:
                skipped += 1
                continue
            try:
                seed = parse_seed(raw, protocol=str(payload.get("protocol") or "socks5"))
            except Exception:
                skipped += 1
                continue
            seed_key = seed.resource_key
            if seed_key not in seen and not self.repo.get("proxy", "proxy_seed", seed_key).get("id"):
                self.repo.upsert("proxy", "proxy_seed", seed_key, seed.to_payload(), status="available")
                created += 1
            seen.add(seed_key)
        disabled = 0
        if disable_legacy and seen:
            # One-shot disable is much faster than per-row set_status over thousands of sticky SIDs.
            try:
                with _db.connect(getattr(self.repo, "db_path", None)) as conn:
                    cur = conn.execute(
                        """
                        UPDATE resource_pool
                        SET status='disabled', last_error='migrated_to_proxy_seed', updated_at=?
                        WHERE resource_type='proxy' AND provider='lajiao_credentials' AND status <> 'disabled'
                        """,
                        (_db.now_iso(),),
                    )
                    disabled = int(getattr(cur, "rowcount", 0) or 0)
            except Exception:
                disabled = 0
        return {"seeds_created": created, "unique_seeds": len(seen), "legacy_disabled": disabled, "skipped": skipped}

    def import_outlook_tokens(self, text: str, provider: str = "outlook_token") -> int:
        rows = []
        for line in str(text or "").replace("\r", "\n").split("\n"):
            value = line.strip()
            if not value:
                continue
            parts = [part.strip() for part in value.split("----")]
            if len(parts) != 4 or "@" not in parts[0] or not parts[2] or not parts[3]:
                raise ValueError(f"Outlook token 行格式错误: {value[:80]}")
            email, password, client_id, refresh_token = parts
            rows.append((email.lower(), {"email": email, "password": password, "client_id": client_id, "refresh_token": refresh_token}))
        if not rows:
            raise ValueError("请导入至少一条邮箱")
        return self.repo.import_many("email", provider, rows)

    def import_link_api_mailboxes(self, text: str, provider: str = "icloud_api") -> int:
        rows = []
        for line in str(text or "").replace("\r", "\n").split("\n"):
            value = line.strip().lstrip("\ufeff")
            if not value:
                continue
            parts = [part.strip() for part in value.split("----")]
            if len(parts) < 2 or "@" not in parts[0]:
                raise ValueError(f"iCloud API 邮箱行格式错误: {value[:80]}")
            email = parts[0]
            payload = {"email": email, "inbox_url": "", "code_url": "", "mail_url": ""}
            for part in parts[1:]:
                label = ""
                link = part
                if ":" in part:
                    prefix, suffix = part.split(":", 1)
                    if prefix.strip().lower() in {"show", "inbox", "mail", "code"}:
                        label = prefix.strip().lower()
                        link = suffix.strip()
                kind = LinkApiMailbox._classify_api_link(link, label) if link.startswith(("http://", "https://")) else ""
                if kind == "code_url":
                    payload["code_url"] = link
                elif kind == "mail_url":
                    payload["mail_url"] = link
                elif kind == "inbox_url" and not payload["inbox_url"]:
                    payload["inbox_url"] = link
            if not (payload["inbox_url"] or payload["code_url"] or payload["mail_url"]):
                raise ValueError(f"iCloud API 邮箱行格式错误: {value[:80]}")
            rows.append((email.lower(), payload))
        if not rows:
            raise ValueError("请导入至少一条 iCloud API 邮箱")
        return self.repo.import_many("email", provider, rows)

    def import_icloud_privacy_mailboxes(self, text: str, provider: str = "icloud_privacy") -> int:
        rows = []
        for line in str(text or "").replace("\r", "\n").split("\n"):
            value = line.strip().lstrip("\ufeff")
            if not value:
                continue
            email = value.split("----", 1)[0].strip().lower()
            if "@" not in email:
                raise ValueError(f"iCloud 隐私邮箱行格式错误: {value[:80]}")
            rows.append((email, {"email": email}))
        if not rows:
            raise ValueError("请导入至少一条 iCloud 隐私邮箱")
        return self.repo.import_many("email", provider, rows)

    def _restore_bind_phone_quota(self) -> None:
        for item in self.repo.list("phone", "bind_user_phone_url", "used"):
            success_count = int(item.get("success_count") or 0)
            last_error = str(item.get("last_error") or "").lower()
            if 0 < success_count < 3 and BIND_PHONE_FAILURE_RECENTLY_USED not in last_error and not _contains_marker(last_error, PHONE_USED_MARKERS):
                self.repo.set_status(int(item["id"]), status="available", error="binding phone quota remaining")
        for item in self.repo.list("phone", "bind_user_phone_url", "disabled"):
            success_count = int(item.get("success_count") or 0)
            last_error = str(item.get("last_error") or "")
            if success_count < 3 and BIND_PHONE_FAILURE_INVALID_NUMBER not in last_error.lower() and "phone_invalid" not in last_error.lower() and not _contains_marker(last_error.lower(), PHONE_INVALID_MARKERS):
                self.repo.set_status(int(item["id"]), status="available", error="binding phone disabled state restored")

    def _restore_expired_cooldowns(self) -> None:
        for item in self.repo.list(status="cooldown"):
            cooldown_until = str(item.get("cooldown_until") or "").strip()
            if not cooldown_until:
                continue
            try:
                expires_at = datetime.fromisoformat(cooldown_until.replace("Z", "+00:00"))
            except ValueError:
                continue
            now = datetime.now(expires_at.tzinfo) if expires_at.tzinfo is not None else datetime.now()
            if expires_at <= now:
                self.repo.set_status(int(item["id"]), status="available", error="cooldown expired")

    def category_options(self) -> list[dict[str, Any]]:
        self._restore_expired_cooldowns()
        self._restore_bind_phone_quota()
        options: list[dict[str, Any]] = []
        for option in RESOURCE_CATEGORY_OPTIONS:
            item = dict(option)
            item["total"] = len(self.repo.list(item["resource_type"], item["provider"]))
            item["available"] = len(self.repo.list(item["resource_type"], item["provider"], "available"))
            options.append(item)
        return options

    def list_resources(self, resource_type: str = "", provider: str = "", status: str = "") -> list[dict[str, Any]]:
        self._restore_expired_cooldowns()
        if (not resource_type or resource_type == "phone") and (not provider or provider == "bind_user_phone_url"):
            self._restore_bind_phone_quota()
        return self.repo.list(resource_type, provider, status)

    def _bind_phone_available_quota(self) -> int:
        self._restore_bind_phone_quota()
        total = 0
        for item in self.repo.list("phone", "bind_user_phone_url", "available"):
            total += max(0, 3 - int(item.get("success_count") or 0))
        return total


    def capacity_summary(self, *, need_phone: int = 0, need_bind_phone: int = 0, need_proxy: int = 0, need_email: int = 0) -> dict[str, Any]:
        self._restore_expired_cooldowns()
        checks = [
            ("phone", "user_phone_url", int(need_phone or 0)),
            ("phone", "bind_user_phone_url", int(need_bind_phone or 0)),
            ("proxy", "lajiao_credentials", int(need_proxy or 0)),
            ("email", "outlook_token", int(need_email or 0)),
            ("email", "icloud_api", int(need_email or 0)),
            ("email", "icloud_privacy", int(need_email or 0)),
        ]
        resources: list[dict[str, Any]] = []
        ok = True
        for resource_type, provider, required in checks:
            if resource_type == "phone" and provider == "bind_user_phone_url":
                available = self._bind_phone_available_quota()
            else:
                available = len(self.repo.list(resource_type, provider, "available"))
            leased = len(self.repo.list(resource_type, provider, "leased"))
            used = len(self.repo.list(resource_type, provider, "used"))
            cooldown = len(self.repo.list(resource_type, provider, "cooldown"))
            disabled = len(self.repo.list(resource_type, provider, "disabled"))
            enough = available >= required
            ok = ok and enough
            resources.append({
                "resource_type": resource_type,
                "provider": provider,
                "required": required,
                "available": available,
                "leased": leased,
                "used": used,
                "cooldown": cooldown,
                "disabled": disabled,
                "enough": enough,
            })
        return {"ok": ok, "resources": resources}

    def recover_stale(self, *, lease_ttl_seconds: int = 1800) -> int:
        return self.repo.recover_stale(lease_ttl_seconds=lease_ttl_seconds)

    def list_sms_activations(self, provider: str = "herosms_api", status: str = "") -> list[dict[str, Any]]:
        return self.repo.list("sms_activation", provider, status)

    def upsert_sms_activation(self, activation_id: str, payload: dict[str, Any], *, provider: str = "herosms_api", status: str = "reserved", error: str = "") -> None:
        activation_id = str(activation_id or "").strip()
        if not activation_id:
            raise ValueError("activation_id 不能为空")
        self.repo.upsert("sms_activation", provider or "herosms_api", activation_id, dict(payload or {}), status=status, error=error)

    def set_status(self, resource_id: int, status: str, *, cooldown_seconds: int = 0, error: str = "") -> None:
        self._validate_status(status)
        cooldown_until = self.cooldown_until(cooldown_seconds) if status == "cooldown" and cooldown_seconds else ""
        self.repo.set_status(resource_id, status=status, cooldown_until=cooldown_until, error=error)

    def set_status_bulk(self, *, resource_ids: list[int] | None = None, resource_type: str = "", provider: str = "", current_status: str = "", status: str, cooldown_seconds: int = 0, error: str = "") -> int:
        self._validate_status(status)
        ids = [int(resource_id) for resource_id in (resource_ids or []) if int(resource_id) > 0]
        if not ids:
            ids = self.repo.list_ids(resource_type, provider, current_status)
        if not ids:
            return 0
        cooldown_until = self.cooldown_until(cooldown_seconds) if status == "cooldown" and cooldown_seconds else ""
        return self.repo.set_status_many(ids, status=status, cooldown_until=cooldown_until, error=error)

    def delete_bulk(self, *, resource_ids: list[int] | None = None, resource_type: str = "", provider: str = "", current_status: str = "") -> int:
        ids = [int(resource_id) for resource_id in (resource_ids or []) if int(resource_id) > 0]
        if not ids:
            ids = self.repo.list_ids(resource_type, provider, current_status)
        if not ids:
            return 0
        return self.repo.delete_many(ids)

    def normalize_proxy_value(self, value: str) -> str:
        candidate = str(value or "").strip()
        if PROXY_PATTERN.match(candidate):
            return candidate
        scheme = ""
        rest = candidate
        if "://" in rest:
            scheme, rest = rest.split("://", 1)
            scheme = f"{scheme}://"
        userinfo = ""
        host_port = rest
        if "@" in rest:
            userinfo, host_port = rest.rsplit("@", 1)
            userinfo = f"{userinfo}@"
        if ":" not in host_port:
            for port_len in range(5, 1, -1):
                port = host_port[-port_len:]
                host = host_port[:-port_len]
                if port.isdigit() and host and "." in host and 0 < int(port) <= 65535:
                    fixed = f"{scheme}{userinfo}{host}:{port}"
                    if PROXY_PATTERN.match(fixed):
                        return fixed
        return candidate

    def _lajiao_credential_protocol(self, proxy_value: str, configured_protocol: str = "") -> str:
        value = str(proxy_value or "").strip().lower()
        parsed = urlsplit(value if "://" in value else f"//{value}")
        host = str(parsed.hostname or "").lower()
        if host.endswith("kookeey.info") or host.endswith("kookeey.com"):
            return "socks5"
        protocol = str(configured_protocol or "auto").strip().lower()
        if protocol and protocol != "auto":
            return protocol
        return "socks5"

    def validate_proxy_format(self, value: str) -> None:
        candidate = self.normalize_proxy_value(value)
        match = PROXY_PATTERN.match(candidate)
        if not match:
            raise ValueError(f"代理格式错误: {candidate[:80]}")
        port = int(match.group("port"))
        if port < 1 or port > 65535:
            raise ValueError(f"代理端口错误: {candidate[:80]}")
        parsed = urlsplit(candidate if "://" in candidate else f"//{candidate}")
        if not parsed.hostname:
            raise ValueError(f"代理主机错误: {candidate[:80]}")

    def check_proxy_health(self, text: str, *, external: bool = False) -> dict[str, Any]:
        rows = [line.strip() for line in str(text or "").replace("\r", "\n").split("\n") if line.strip()]
        if not rows:
            raise ValueError("请提供至少一条代理")
        results: list[dict[str, Any]] = []
        for row in rows:
            try:
                self.validate_proxy_format(row)
            except ValueError as exc:
                results.append({"proxy": row, "ok": False, "message": str(exc)})
            else:
                results.append({"proxy": row, "ok": True, "message": "格式有效，未连接外部代理"})
        if external and os.getenv("GPT_REGISTER_PROXY_HEALTHCHECK_EXTERNAL") != "1":
            raise ValueError("外部代理检测未启用，请设置 GPT_REGISTER_PROXY_HEALTHCHECK_EXTERNAL=1")
        return {"checked": len(results), "valid": sum(1 for item in results if item["ok"]), "external": False, "items": results}

    def _validate_status(self, status: str) -> None:
        allowed = {"available", "leased", "used", "cooldown", "disabled", "reserved", "post_send_pending", "blocked", "release_pending", "released", "completed", "expired"}
        if status not in allowed:
            raise ValueError("不支持的资源状态")

    def _load_legacy_blocked_outlook_emails(self) -> set[str]:
        blocked: set[str] = set()
        path = Path("data/used_outlook_emails.txt")
        if path.exists():
            for row in path.read_text(encoding="utf-8").splitlines():
                email = row.strip().split("#", 1)[0].strip().lower()
                if email:
                    blocked.add(email)
        # Successful registration artifacts — never re-lease these mailboxes.
        for products_dir in (Path("output/products"), Path("output/registered_accounts")):
            if not products_dir.exists():
                continue
            for product_file in products_dir.glob("*.json"):
                try:
                    data = json.loads(product_file.read_text(encoding="utf-8")) or {}
                except Exception:
                    # Filename often encodes email_YYYYMMDD.json
                    stem = product_file.stem
                    maybe = stem.split("_20")[0].strip().lower()
                    if "@" in maybe:
                        blocked.add(maybe)
                    continue
                for key in ("email", "outlook_email"):
                    email = str(data.get(key) or "").strip().lower()
                    if email:
                        blocked.add(email)
        pure_dir = Path("output/pure_go_register_batch")
        if pure_dir.exists():
            for product_file in pure_dir.glob("*.json"):
                if product_file.name.startswith("FAIL"):
                    continue
                try:
                    data = json.loads(product_file.read_text(encoding="utf-8")) or {}
                except Exception:
                    continue
                email = str(data.get("email") or "").strip().lower()
                tok = str(data.get("access_token") or "")
                if email and tok:
                    blocked.add(email)
        state_path = Path("data/outlook_pool_state.jsonl")
        if state_path.exists():
            retryable_failures: dict[str, int] = {}
            for row in state_path.read_text(encoding="utf-8").splitlines():
                try:
                    event = json.loads(row)
                except Exception:
                    continue
                email = str(event.get("email") or "").strip().lower()
                if not email:
                    continue
                status = str(event.get("status") or "")
                if status in {"consumed", "dirty_email_already_used", "cooldown", "registered"}:
                    blocked.add(email)
                elif status == "failed_retryable":
                    retryable_failures[email] = retryable_failures.get(email, 0) + 1
            for email, count in retryable_failures.items():
                if count >= 2:
                    blocked.add(email)
        return blocked

    def _load_icloud_privacy_pool_state(self) -> dict[str, str]:
        state_path = Path("data/icloud_privacy_pool_state.jsonl")
        latest: dict[str, str] = {}
        if not state_path.exists():
            return latest
        for row in state_path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(row)
            except Exception:
                continue
            email = str(event.get("email") or "").strip().lower()
            status = str(event.get("status") or "").strip()
            if email and status:
                latest[email] = status
        return latest

    def _load_icloud_api_pool_state(self) -> dict[str, str]:
        # LinkApiMailbox historically wrote into the shared outlook_pool_state.jsonl.
        state_path = Path("data/outlook_pool_state.jsonl")
        latest: dict[str, str] = {}
        if not state_path.exists():
            return latest
        for row in state_path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(row)
            except Exception:
                continue
            email = str(event.get("email") or "").strip().lower()
            status = str(event.get("status") or "").strip()
            if email and status:
                latest[email] = status
        return latest

    def _blocked_emails_from_accounts(self) -> set[str]:
        blocked: set[str] = set()
        try:
            from core import account_store
            for account in account_store.list_accounts(refresh_legacy=False):
                for key in ("email", "outlook_email", "billing_email", "codex_email"):
                    email = str(account.get(key) or "").strip().lower()
                    if email:
                        blocked.add(email)
        except Exception:
            return blocked
        return blocked

    def _random_mailbox_local_part(self, length: int = 12) -> str:
        alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
        return "".join(alphabet[secrets.randbelow(len(alphabet))] for _ in range(max(6, length)))

    def _lease_generated_binding_email(self, provider: str, task_id: str, domain: str, blocked: set[str]) -> ResourceLease:
        domain = str(domain or "").strip().lower().lstrip("@")
        if not domain:
            return ResourceLease()
        for _ in range(50):
            email = f"{self._random_mailbox_local_part()}@{domain}".lower()
            if email in blocked:
                continue
            existing = self.repo.get("email", provider, email)
            if existing and str(existing.get("status") or "") != "available":
                blocked.add(email)
                continue
            if not existing:
                self.repo.upsert("email", provider, email, {"email": email, "domain": domain, "usage": "billing_and_codex"})
            lease = self.repo.lease("email", provider, task_id)
            if lease.resource_key:
                return lease
        return ResourceLease()

    _proxy_seed_cache: list[dict[str, Any]] | None = None
    _proxy_seed_cache_at: float = 0.0
    _proxy_seed_cache_lock = __import__("threading").Lock()

    def _cached_proxy_seed_candidates(self, *, styles: set[str] | None = None) -> list[dict[str, Any]]:
        """Avoid scanning the whole proxy table on every task start (was serial bottleneck).

        When styles is provided (e.g. {'bestgo'}), only matching seeds are returned.
        """
        import time

        style_key = ",".join(sorted(styles)) if styles else "*"
        now = time.monotonic()
        cache_attr = f"_proxy_seed_cache::{style_key}"
        with self._proxy_seed_cache_lock:
            # Per-style cache stored on class-level dict via instance attributes map.
            cache_map = getattr(self, "_proxy_seed_cache_by_style", None)
            if not isinstance(cache_map, dict):
                cache_map = {}
                self._proxy_seed_cache_by_style = cache_map
            entry = cache_map.get(style_key)
            if entry and (now - float(entry.get("at") or 0.0)) < 30.0:
                return list(entry.get("items") or [])

            candidates = self.repo.list("proxy", "proxy_seed", "available") or self.repo.list("proxy", "proxy_seed", "") or []
            cleaned: list[dict[str, Any]] = []
            for item in candidates:
                payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
                host = str(payload.get("host") or item.get("resource_key") or "").lower()
                style = str(payload.get("style") or "").lower()
                vendor = str(payload.get("vendor") or "").lower()
                tags = payload.get("style_tags") if isinstance(payload.get("style_tags"), list) else []
                tag_blob = " ".join(str(t).lower() for t in tags)
                key_l = str(item.get("resource_key") or "").lower()
                if "proxy.local" in host or host.endswith(".invalid") or "auth-0.local" in host or "auth-1.local" in host:
                    continue
                if styles:
                    # bestgo/1024: match style, vendor, tags, host, or resource_key
                    if not any(
                        s == style
                        or s == vendor
                        or s in host
                        or s in key_l
                        or s in tag_blob
                        # 1024 seed mints lajiao-shaped SIDs
                        or (s == "1024" and ("1024" in host or style == "lajiao" and "1024" in key_l))
                        for s in styles
                    ):
                        continue
                cleaned.append(item)
            cache_map[style_key] = {"at": now, "items": cleaned}
            self._proxy_seed_cache = cleaned
            self._proxy_seed_cache_at = now
            return list(cleaned)


    def lease_for_task(self, task_id: str, config: dict[str, Any]) -> tuple[dict[str, Any], list[ResourceLease]]:
        overrides: dict[str, Any] = {}
        leases: list[ResourceLease] = []
        skip_phone_leases = bool(config.get("_skip_phone_leases"))
        resume_oauth_task = bool(config.get("_resume_oauth_task"))
        existing_account_binding_task = resume_oauth_task or bool(config.get("_protocol_cpa_bind_task"))

        if not skip_phone_leases and not existing_account_binding_task and str(config.get("sms_provider") or "") == "user_phone_url":
            lease = self.repo.lease("phone", "user_phone_url", task_id)
            if lease.resource_key:
                leases.append(lease)
                overrides["sms_phone_url"] = f"{lease.payload.get('phone')}|{lease.payload.get('sms_url')}"
                overrides["sms_phone_urls"] = overrides["sms_phone_url"]


        if not skip_phone_leases and not resume_oauth_task and str(config.get("bind_sms_provider") or "") == "bind_user_phone_url":
            self._restore_bind_phone_quota()
            lease = self.repo.lease("phone", "bind_user_phone_url", task_id)
            if lease.resource_key:
                leases.append(lease)
                overrides["bind_sms_phone_url"] = f"{lease.payload.get('phone')}|{lease.payload.get('sms_url')}"
                overrides["bind_sms_phone_urls"] = overrides["bind_sms_phone_url"]
                overrides["_resource_provider"] = "bind_user_phone_url"

        proxy_mode = str(config.get("lajiao_proxy_mode") or config.get("proxy_mode") or "").strip().lower()
        use_seed_pool = proxy_mode in {"credentials", "credential", "seed", "proxy_seed", "account", "auth", ""}
        if use_seed_pool:
            region = str(
                config.get("lajiao_proxy_expected_country")
                or config.get("proxy_region")
                or config.get("lajiao_proxy_regions")
                or config.get("lajiao_proxy_region")
                or ""
            )
            region_value = region.split(",")[0].strip().upper()
            if region_value in {"AUTO", "ZONE", "ZONE_AUTO", "ANY", "*"}:
                region_value = ""
            # pure-Go / software path dials SOCKS; never mint http:// for pool sessions.
            mint_protocol = str(config.get("lajiao_proxy_credential_protocol") or "socks5").strip().lower()
            if mint_protocol in {"", "auto", "http", "https"}:
                mint_protocol = "socks5"
            session_url, seed_meta = self._lease_proxy_session(
                task_id,
                region=region_value,
                protocol=mint_protocol,
                ttl=int(config.get("proxy_seed_ttl") or config.get("lajiao_proxy_session_ttl") or 10),
                config=config,
            )
            if session_url:
                # Strip scheme for lajiao_proxy_credentials consumers that re-prefix.
                credential = session_url.split("://", 1)[-1]
                overrides["lajiao_proxy_mode"] = "credentials"
                overrides["lajiao_proxy_credentials"] = credential
                overrides["lajiao_proxy_credential_protocol"] = str(seed_meta.get("protocol") or "socks5")
                overrides["proxy_seed_key"] = str(seed_meta.get("seed_key") or "")
                overrides["proxy_session_sid"] = str(seed_meta.get("sid") or "")
                overrides["proxy_session_region"] = str(seed_meta.get("region") or region_value)
                overrides["mailat_protocol_proxy"] = session_url
                if region_value:
                    overrides.setdefault("lajiao_proxy_expected_country", region_value)
                    overrides.setdefault("lajiao_proxy_regions", region_value)
                if seed_meta.get("lease"):
                    leases.append(seed_meta["lease"])

        if not existing_account_binding_task and str(config.get("mailbox_provider") or "") == "outlook_token":
            legacy_used = self._load_legacy_blocked_outlook_emails()
            leased_outlook = False
            for _ in range(20):
                lease = self.repo.lease("email", "outlook_token", task_id)
                if not lease.resource_key:
                    break
                if lease.resource_key.lower() in legacy_used:
                    self.repo.report(task_id, lease.resource_key, success=True, error="legacy outlook email already used")
                    continue
                leases.append(lease)
                overrides["outlook_email"] = str(lease.payload.get("email") or lease.resource_key)
                overrides["outlook_password"] = str(lease.payload.get("password") or "")
                overrides["outlook_client_id"] = str(lease.payload.get("client_id") or "")
                overrides["outlook_refresh_token"] = str(lease.payload.get("refresh_token") or "")
                leased_outlook = True
                break
            if not leased_outlook:
                # Never fall through to shared order-file selection: concurrent tasks would
                # all grab the same first unused mailbox and Go admission MaxPerMailbox=1
                # would 429 the rest with "admission rejected: mailbox: ...".
                for lease in list(leases):
                    try:
                        self.repo.report(task_id, lease.resource_key, success=False, error="outlook token pool exhausted")
                    except Exception:
                        pass
                raise RuntimeError(
                    "Outlook token 邮箱池已耗尽，无法为任务租用独占邮箱。"
                    "请导入新的 outlook_token 资源，或把可用邮箱从 used 重置为 available。"
                )

        if not existing_account_binding_task and str(config.get("mailbox_provider") or "") == "icloud_api":
            pool_state = self._load_icloud_api_pool_state()
            leased_icloud = False
            for _ in range(50):
                lease = self.repo.lease("email", "icloud_api", task_id)
                if not lease.resource_key:
                    break
                email = str(lease.payload.get("email") or lease.resource_key).strip()
                email_key = email.lower()
                state = pool_state.get(email_key, "")
                if state in {"consumed", "dirty_email_already_used"}:
                    current = self.repo.get("email", "icloud_api", lease.resource_key)
                    if int(current.get("id") or 0) > 0:
                        self.repo.set_status(int(current["id"]), status="used", error=f"icloud api state {state}")
                    continue
                if state == "cooldown":
                    current = self.repo.get("email", "icloud_api", lease.resource_key)
                    if int(current.get("id") or 0) > 0:
                        self.repo.set_status(int(current["id"]), status="cooldown", cooldown_until=self.cooldown_until(3600), error="icloud api state cooldown")
                    continue
                # Stale jsonl "reserved" is ignored: resource pool is the exclusive lease authority.
                leases.append(lease)
                inbox_url = str(lease.payload.get("inbox_url") or "")
                code_url = str(lease.payload.get("code_url") or "")
                mail_url = str(lease.payload.get("mail_url") or "")
                parts = [email]
                if inbox_url:
                    parts.append(inbox_url)
                if code_url:
                    parts.append(f"code:{code_url}")
                if mail_url:
                    parts.append(f"mail:{mail_url}")
                overrides["icloud_api_order_text"] = "----".join(parts)
                overrides["icloud_api_order_file"] = ""
                overrides["icloud_api_email"] = email
                overrides["email"] = email
                leased_icloud = True
                break
            if not leased_icloud and str(config.get("mailbox_provider") or "") == "icloud_api":
                # Fail closed when pool cannot supply an exclusive mailbox, same as outlook_token.
                for lease in list(leases):
                    try:
                        self.repo.report(task_id, lease.resource_key, success=False, error="icloud api pool exhausted")
                    except Exception:
                        pass
                raise RuntimeError(
                    "iCloud API 邮箱池没有可租用邮箱。"
                    "资源页显示 available 但租不到时，通常是 jsonl 状态为 consumed/cooldown，或池子已被租空。"
                )

        if not existing_account_binding_task and str(config.get("mailbox_provider") or "") == "icloud_privacy":
            privacy_state = self._load_icloud_privacy_pool_state()
            for _ in range(50):
                lease = self.repo.lease("email", "icloud_privacy", task_id)
                if not lease.resource_key:
                    break
                email = str(lease.payload.get("email") or lease.resource_key).strip().lower()
                state = privacy_state.get(email, "")
                if state in {"consumed", "dirty_email_already_used"}:
                    current = self.repo.get("email", "icloud_privacy", lease.resource_key)
                    if int(current.get("id") or 0) > 0:
                        self.repo.set_status(int(current["id"]), status="used", error=f"icloud privacy state {state}")
                    continue
                if state == "cooldown":
                    current = self.repo.get("email", "icloud_privacy", lease.resource_key)
                    if int(current.get("id") or 0) > 0:
                        self.repo.set_status(int(current["id"]), status="cooldown", cooldown_until=self.cooldown_until(3600), error="icloud privacy state cooldown")
                    continue
                leases.append(lease)
                overrides["icloud_privacy_order_text"] = email
                overrides["icloud_privacy_order_file"] = ""
                break

        if not existing_account_binding_task and str(config.get("registration_engine") or "").strip().lower() == "protocol":
            mailbox_provider = str(config.get("mailbox_provider") or "").strip()
            blocked_emails = self._blocked_emails_from_accounts() | self._load_legacy_blocked_outlook_emails()
            lease = ResourceLease()
            if mailbox_provider == "forwarded_domain":
                lease = self._lease_generated_binding_email("forwarded_domain", task_id, str(config.get("mailbox_domain") or config.get("forwardedEmailDomain") or ""), blocked_emails)
            elif mailbox_provider == "cfworker_admin_api":
                lease = self._lease_generated_binding_email("cfworker_admin_api", task_id, str(config.get("cfworker_domain") or config.get("cloudflareEmailDomain") or ""), blocked_emails)
            if lease.resource_key:
                leases.append(lease)
                email = str(lease.payload.get("email") or lease.resource_key).lower()
                overrides["codex_bind_email"] = email
                overrides["billing_email"] = email
                overrides["codex_email"] = email
                overrides["mailbox_provider"] = mailbox_provider

        if leases:
            overrides["resource_leases"] = [{"type": item.resource_type, "provider": item.provider, "key": item.resource_key} for item in leases]
        return overrides, leases

    @staticmethod
    def _proxy_style_filter(config: dict[str, Any] | None = None) -> set[str] | None:
        """Return allowed proxy seed styles. Default: bestgo only (user preference)."""
        cfg = config if isinstance(config, dict) else {}
        raw = str(
            cfg.get("proxy_seed_styles")
            or cfg.get("proxy_seed_style")
            or cfg.get("preferred_proxy_style")
            or "bestgo"
        ).strip().lower()
        if raw in {"", "*", "any", "all"}:
            return None
        styles = {part.strip() for part in raw.replace(";", ",").split(",") if part.strip()}
        return styles or {"bestgo"}

    def _lease_proxy_session(
        self,
        task_id: str,
        *,
        region: str = "",
        protocol: str = "socks5",
        ttl: int = 10,
        config: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Lease one proxy seed and mint a fresh region/SID session URL."""
        from core.proxy.seed_session import build_session, seed_from_payload

        region_value = str(region or "").split(",")[0].strip().upper()
        if region_value in {"AUTO", "ZONE", "ZONE_AUTO", "ANY", "*"}:
            region_value = ""

        allowed_styles = self._proxy_style_filter(config)
        lease = None
        # Prefer region-capable seeds. Use a short process cache instead of
        # listing the entire proxy table on every registration start.
        candidates = self._cached_proxy_seed_candidates(styles=allowed_styles)
        ranked: list[tuple[int, dict[str, Any]]] = []
        for item in candidates:
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            host = str(payload.get("host") or item.get("resource_key") or "").lower()
            style = str(payload.get("style") or "").lower()
            vendor = str(payload.get("vendor") or "").lower()
            score = 0
            # User-allowed pool (bestgo + 1024) must share one score band so
            # task_id round-robin can diversify exits. Do not let bestgo always win.
            if style == "bestgo" or "bestgo" in host or vendor == "bestgo":
                score += 20
            if vendor == "1024" or "1024" in host:
                score += 20
            if style in {"kookeey", "lajiao", "bestgo"}:
                score += 10
            if region_value and style in {"kookeey", "lajiao", "bestgo"}:
                score += 5
            if str(item.get("status") or "") == "available":
                score += 1
            ranked.append((score, item))
        ranked.sort(key=lambda pair: pair[0], reverse=True)
        # Same base account can mint many sticky SIDs; still spread across
        # providers for exit-IP diversity under concurrent software-path smoke.
        # Prefer high-score seeds, then pick by task_id hash (stable round-robin).
        top_score = ranked[0][0] if ranked else -1
        top_tier = [item for score, item in ranked if score >= max(0, top_score - 5)] or [item for _, item in ranked]
        chosen = None
        if top_tier:
            tid = str(task_id or "")
            idx = sum(ord(c) for c in tid) % len(top_tier) if tid else 0
            chosen = top_tier[idx]
        if chosen:
            try:
                seed = seed_from_payload(chosen.get("payload") if isinstance(chosen.get("payload"), dict) else {}, resource_key=str(chosen.get("resource_key") or ""))
            except Exception:
                seed = None
            if seed is not None:
                session = build_session(
                    seed,
                    region=region_value or "JP",
                    ttl=max(1, int(ttl or 10)),
                    protocol=protocol or seed.protocol or "socks5",
                )
                return session.url, {
                    "seed_key": seed.resource_key,
                    "sid": session.sid,
                    "region": session.region,
                    "protocol": seed.protocol,
                    "style": seed.style,
                    "lease": ResourceLease(
                        id=int(chosen.get("id") or 0),
                        resource_type="proxy",
                        provider="proxy_seed",
                        resource_key=seed.resource_key,
                        payload=seed.to_payload(),
                        status="available",
                        lease_id=task_id,
                    ),
                }

        # Fallback: exclusive lease path — still respect style filter (bestgo only).
        # Never fall back to legacy lajiao sticky rows when styles are restricted.
        lease = self.repo.lease("proxy", "proxy_seed", task_id, region="")
        if lease.resource_key:
            payload = lease.payload if isinstance(lease.payload, dict) else {}
            host = str(payload.get("host") or lease.resource_key or "").lower()
            style = str(payload.get("style") or "").lower()
            if allowed_styles and not any(
                s == style or s in host or s in str(lease.resource_key or "").lower()
                for s in allowed_styles
            ):
                try:
                    self.repo.report(task_id, lease.resource_key, success=False, error="style filter reject")
                except Exception:
                    pass
                lease = ResourceLease()
        if not lease.resource_key:
            if allowed_styles:
                # Restricted styles (bestgo): do not use lajiao sticky fallback.
                return "", {}
            lease = self.repo.lease("proxy", "lajiao_credentials", task_id, region=region_value or "")
            if not lease.resource_key:
                return "", {}
        try:
            seed = seed_from_payload(lease.payload, resource_key=lease.resource_key)
        except Exception:
            return "", {}
        session = build_session(
            seed,
            region=region_value or "JP",
            ttl=max(1, int(ttl or 10)),
            protocol=protocol or seed.protocol or "socks5",
        )
        if lease.provider == "proxy_seed" and int(lease.id or 0) > 0:
            try:
                self.repo.set_status(int(lease.id), status="available", error="")
            except Exception:
                pass
        return session.url, {
            "seed_key": seed.resource_key,
            "sid": session.sid,
            "region": session.region,
            "protocol": seed.protocol,
            "style": seed.style,
            "lease": lease if lease.provider != "proxy_seed" else ResourceLease(
                id=lease.id,
                resource_type="proxy",
                provider="proxy_seed",
                resource_key=seed.resource_key,
                payload=seed.to_payload(),
                status="available",
                lease_id=task_id,
            ),
        }

    def mint_proxy_session_from_config(self, config: dict[str, Any], *, region: str = "", refresh: bool = False) -> str:
        """Build/refresh a session URL for runners (network-error SID rotate)."""
        from core.proxy.seed_session import build_session, parse_seed, refresh_session, seed_from_payload

        region_value = str(
            region
            or config.get("proxy_session_region")
            or config.get("lajiao_proxy_expected_country")
            or config.get("proxy_region")
            or config.get("lajiao_proxy_regions")
            or "JP"
        ).split(",")[0].strip().upper() or "JP"
        ttl = int(config.get("proxy_seed_ttl") or config.get("lajiao_proxy_session_ttl") or 10)
        current = str(
            config.get("mailat_protocol_proxy")
            or config.get("lajiao_proxy_credentials")
            or config.get("proxy")
            or ""
        ).strip()
        if refresh and current:
            try:
                base = current if "://" in current else f"socks5://{current}"
                if base.startswith("http://") or base.startswith("https://"):
                    base = "socks5://" + base.split("://", 1)[-1]
                session = refresh_session(base, region=region_value, ttl=ttl)
                url = session.url
                if url.startswith("http://") or url.startswith("https://"):
                    url = "socks5://" + url.split("://", 1)[-1]
                return url
            except Exception:
                pass
        seed_key = str(config.get("proxy_seed_key") or "").strip()
        if seed_key:
            row = self.repo.get("proxy", "proxy_seed", seed_key)
            if row.get("id"):
                payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
                host = str(payload.get("host") or seed_key or "").lower()
                style = str(payload.get("style") or "").lower()
                allowed = self._proxy_style_filter(config)
                if (not allowed) or any(s == style or s in host or s in seed_key.lower() for s in allowed):
                    seed = seed_from_payload(payload, resource_key=seed_key)
                    return build_session(seed, region=region_value, ttl=ttl, protocol="socks5").url
        # Round-robin pick without exclusive lease when only minting.
        allowed = self._proxy_style_filter(config)
        seeds = self.repo.list("proxy", "proxy_seed", "available") or self.repo.list("proxy", "proxy_seed", "")
        if allowed and seeds:
            filtered = []
            for item in seeds:
                payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
                host = str(payload.get("host") or item.get("resource_key") or "").lower()
                style = str(payload.get("style") or "").lower()
                key = str(item.get("resource_key") or "").lower()
                if any(s == style or s in host or s in key for s in allowed):
                    filtered.append(item)
            seeds = filtered
        if seeds:
            # simple rotate by task/time hash
            idx = abs(hash(str(config.get("dashboard_task_id") or secrets.token_hex(4)))) % len(seeds)
            item = seeds[idx]
            seed = seed_from_payload(item.get("payload") if isinstance(item.get("payload"), dict) else {}, resource_key=str(item.get("resource_key") or ""))
            return build_session(seed, region=region_value, ttl=ttl, protocol="socks5").url
        if current and not allowed:
            seed = parse_seed(current if "://" in current else f"socks5://{current}")
            return build_session(seed, region=region_value, ttl=ttl, protocol="socks5").url
        return ""

    def cooldown_until(self, seconds: int) -> str:
        return (datetime.now() + timedelta(seconds=max(0, seconds))).isoformat(timespec="seconds")

    def _report_failure_status(self, task_id: str, lease: dict[str, Any], resource_status: str, *, error: str, cooldown_seconds: int = 3600) -> str:
        resource_type = str(lease.get("type") or "")
        provider = str(lease.get("provider") or "")
        resource_key = str(lease.get("key") or "")
        # proxy_seed is a reusable mint template (one row → many sticky SIDs).
        # Never exclusive-lease / cooldown / disable it on task outcome.
        if resource_type == "proxy" and provider in {"proxy_seed", "seed", "lajiao_seed"}:
            current = self.repo.get(resource_type, provider, resource_key) if provider else {}
            if int(current.get("id") or 0) > 0:
                self.repo.set_status(int(current["id"]), status="available", error="")
            return "available"
        if resource_status == "used":
            current = self.repo.get(resource_type, provider, resource_key) if provider else {}
            if resource_type == "phone" and provider == "bind_user_phone_url" and int(current.get("id") or 0) > 0:
                self.repo.set_status(int(current["id"]), status="used", error=error)
            else:
                self.repo.report(task_id, resource_key, success=True, error=error)
                if resource_type == "email" and provider == "icloud_privacy":
                    refreshed = self.repo.get(resource_type, provider, resource_key)
                    if int(refreshed.get("id") or 0) > 0:
                        self.repo.set_status(int(refreshed["id"]), status="disabled", error=error or "icloud privacy consumed")
                    return "disabled"
            return "used"
        if resource_status == "cooldown" and resource_type == "phone" and provider == "bind_user_phone_url":
            self.repo.report(task_id, resource_key, success=False, cooldown_until=self.cooldown_until(cooldown_seconds), error=error)
            return "cooldown"
        if resource_status == "cooldown":
            current = self.repo.get(resource_type, provider, resource_key) if provider else {}
            if int(current.get("fail_count") or 0) >= 2:
                self.repo.report(task_id, resource_key, success=False, error=error)
                refreshed = self.repo.get(resource_type, provider, resource_key) if provider else {}
                if int(refreshed.get("id") or 0) > 0:
                    self.repo.set_status(int(refreshed["id"]), status="disabled", error=error)
                return "disabled"
            self.repo.report(task_id, resource_key, success=False, cooldown_until=self.cooldown_until(cooldown_seconds), error=error)
            return "cooldown"
        if resource_status == "disabled":
            self.repo.report(task_id, resource_key, success=False, error=error)
            current = self.repo.get(resource_type, provider, resource_key) if provider else {}
            if int(current.get("id") or 0) > 0:
                self.repo.set_status(int(current["id"]), status="disabled", error=error)
            return "disabled"
        current = self.repo.get(resource_type, provider, resource_key) if provider else {}
        if int(current.get("id") or 0) > 0:
            self.repo.set_status(int(current["id"]), status="available", error=error)
        return "available"

    def _report_success_status(self, task_id: str, lease: dict[str, Any], *, selected_exit_ip: str = "") -> str:
        resource_type = str(lease.get("type") or "")
        provider = str(lease.get("provider") or "")
        resource_key = str(lease.get("key") or "")
        if resource_type == "proxy" and provider in {"proxy_seed", "seed", "lajiao_seed"}:
            current = self.repo.get(resource_type, provider, resource_key) if provider else {}
            if int(current.get("id") or 0) > 0:
                self.repo.set_status(int(current["id"]), status="available", error="")
            return "available"
        if resource_type == "proxy":
            if selected_exit_ip:
                self._remember_proxy_exit_ip(provider, resource_key, selected_exit_ip)
            self.repo.report(task_id, resource_key, success=True, cooldown_until=self.cooldown_until(PROXY_SUCCESS_COOLDOWN_SECONDS), error="proxy success cooldown")
            return "cooldown"
        if resource_type == "email" and provider == "icloud_privacy":
            self.repo.report(task_id, resource_key, success=True, error="icloud privacy success auto disabled")
            current = self.repo.get(resource_type, provider, resource_key) if provider else {}
            if int(current.get("id") or 0) > 0:
                self.repo.set_status(int(current["id"]), status="disabled", error="icloud privacy success auto disabled")
            return "disabled"
        self.repo.report(task_id, resource_key, success=True)
        current = self.repo.get(resource_type, provider, resource_key) if provider else {}
        return str(current.get("status") or "used")

    def _classify_resource_status(self, resource_type: str, task_status: str, evidence: str) -> tuple[str, str, int]:
        if task_status == "succeeded":
            return ("success", "available" if resource_type == "proxy" else "used", 0)
        if task_status == "interrupted" and resource_type == "proxy":
            return ("interrupted", "cooldown", PROXY_FAILURE_COOLDOWN_SECONDS)
        if task_status == "cancelled":
            return ("cancelled", "available" if resource_type == "proxy" else "cooldown", 600)
        if task_status != "succeeded" and resource_type == "email" and ("[注册成功]" in evidence or "[✅️注册成功]" in evidence):
            return ("registered_without_token", "used", 0)
        if _contains_marker(evidence, EMAIL_USED_MARKERS):
            if resource_type == "email":
                return ("email_already_used", "used", 0)
            return ("email_already_used", "available", 0)
        if BIND_PHONE_FAILURE_RECENTLY_USED in evidence:
            if resource_type == "phone":
                return (BIND_PHONE_FAILURE_RECENTLY_USED, "used", 0)
            return (BIND_PHONE_FAILURE_RECENTLY_USED, "available", 0)
        if BIND_PHONE_FAILURE_INVALID_NUMBER in evidence:
            if resource_type == "phone":
                return (BIND_PHONE_FAILURE_INVALID_NUMBER, "disabled", 0)
            return (BIND_PHONE_FAILURE_INVALID_NUMBER, "available", 0)
        if BIND_PHONE_FAILURE_INVALID_OTP in evidence:
            if resource_type == "phone":
                return (BIND_PHONE_FAILURE_INVALID_OTP, "cooldown", BIND_PHONE_FAILURE_COOLDOWN_SECONDS)
            return (BIND_PHONE_FAILURE_INVALID_OTP, "available", 0)
        if BIND_PHONE_FAILURE_TIMEOUT in evidence:
            if resource_type == "phone":
                return (BIND_PHONE_FAILURE_TIMEOUT, "cooldown", BIND_PHONE_FAILURE_COOLDOWN_SECONDS)
            return (BIND_PHONE_FAILURE_TIMEOUT, "available", 0)
        if (
            BIND_PHONE_FAILURE_OTP_SUBMIT_FAILED in evidence
            or BIND_PHONE_FAILURE_OTP_SUBMIT_STATUS_0 in evidence
            or BIND_PHONE_FAILURE_OTP_SUBMIT_TRANSPORT in evidence
            or BIND_PHONE_FAILURE_TRANSPORT in evidence
        ):
            if resource_type == "phone":
                return ("bind_phone_transport_or_submit_failure", "cooldown", BIND_PHONE_FAILURE_COOLDOWN_SECONDS)
            return ("bind_phone_transport_or_submit_failure", "available", 0)
        if _contains_marker(evidence, PHONE_USED_MARKERS):
            if resource_type == "phone":
                return ("phone_already_used", "used", 0)
            return ("phone_already_used", "available", 0)
        if _contains_marker(evidence, PHONE_INVALID_MARKERS):
            if resource_type == "phone":
                return ("phone_invalid", "disabled", 0)
            return ("phone_invalid", "available", 0)
        if _contains_marker(evidence, SMS_TIMEOUT_MARKERS):
            if resource_type == "phone":
                return ("sms_timeout", "cooldown", 3600)
            return ("sms_timeout", "available", 0)
        if _contains_marker(evidence, PROXY_FAILURE_MARKERS):
            if resource_type == "proxy":
                return ("proxy_failure", "cooldown", PROXY_FAILURE_COOLDOWN_SECONDS)
            return ("proxy_failure", "available", 0)
        if _contains_marker(evidence, OPENAI_RATE_RISK_MARKERS):
            if resource_type == "proxy":
                return ("openai_rate_or_risk", "cooldown", PROXY_FAILURE_COOLDOWN_SECONDS)
            return ("openai_rate_or_risk", "available", 0)
        if _contains_marker(evidence, OAUTH_NETWORK_MARKERS):
            if resource_type in {"proxy", "email"}:
                return ("oauth_or_network_error", "cooldown", PROXY_FAILURE_COOLDOWN_SECONDS)
            return ("oauth_or_network_error", "available", 0)
        if resource_type in {"phone", "email"}:
            return ("unknown_failure", "cooldown", 3600)
        return ("unknown_failure", "available", 0)

    def _selected_registration_proxy_info_from_log(self, log_text: str) -> tuple[str, str]:
        for line in str(log_text or "").splitlines():
            marker = "使用新代理:"
            if marker not in line:
                continue
            rest = line.split(marker, 1)[1].strip()
            proxy = rest.split(" exit_ip=", 1)[0].strip()
            exit_ip = rest.split(" exit_ip=", 1)[1].strip().split()[0] if " exit_ip=" in rest else ""
            if proxy:
                return proxy, exit_ip
        return "", ""

    def _selected_registration_proxy_from_log(self, log_text: str) -> str:
        return self._selected_registration_proxy_info_from_log(log_text)[0]

    def _remember_proxy_exit_ip(self, provider: str, resource_key: str, exit_ip: str) -> None:
        exit_ip = str(exit_ip or "").strip()
        if not provider or not resource_key or not exit_ip or exit_ip == "skip_check":
            return
        current = self.repo.get("proxy", provider, resource_key)
        if int(current.get("id") or 0) <= 0:
            return
        payload = dict(current.get("payload") or {})
        payload["exit_ip"] = exit_ip
        self.repo.upsert("proxy", provider, resource_key, payload, status=str(current.get("status") or "available"), error=str(current.get("last_error") or ""))

    def _cooldown_proxy_exit_ip_siblings(self, provider: str, selected_key: str, exit_ip: str, *, error: str) -> None:
        exit_ip = str(exit_ip or "").strip()
        if not provider or not selected_key or not exit_ip or exit_ip == "skip_check":
            return
        cooldown_until = self.cooldown_until(PROXY_FAILURE_COOLDOWN_SECONDS)
        for item in self.repo.list("proxy", provider):
            if str(item.get("resource_key") or "") == selected_key:
                continue
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            if str(payload.get("exit_ip") or "").strip() != exit_ip:
                continue
            status = str(item.get("status") or "")
            if status in {"available", "leased", "cooldown"}:
                self.repo.set_status(int(item.get("id") or 0), status="cooldown", cooldown_until=cooldown_until, error=error)

    def _normalized_proxy_resource_key(self, value: str) -> str:
        value = str(value or "").strip()
        if "://" in value:
            parsed = urlsplit(value)
            if parsed.netloc:
                return parsed.netloc.strip()
        return value

    def _same_proxy_resource(self, leased_key: str, selected_proxy: str) -> bool:
        return self._normalized_proxy_resource_key(leased_key) == self._normalized_proxy_resource_key(selected_proxy)

    def report_for_task(self, task_id: str, status: str, config: dict[str, Any], *, error: str = "", log_text: str = "") -> list[dict[str, str]]:
        leases = config.get("resource_leases") if isinstance(config.get("resource_leases"), list) else []
        if not leases:
            return []
        evidence = f"{error}\n{log_text}".lower()
        selected_proxy, selected_exit_ip = self._selected_registration_proxy_info_from_log(log_text)
        if selected_proxy and not any(
            isinstance(item, dict)
            and str(item.get("type") or "") == "proxy"
            and self._same_proxy_resource(str(item.get("key") or ""), selected_proxy)
            for item in leases
        ):
            selected_key = self._normalized_proxy_resource_key(selected_proxy)
            selected_row = self.repo.get("proxy", "lajiao_credentials", selected_key)
            if int(selected_row.get("id") or 0) > 0:
                leases = list(leases) + [{"type": "proxy", "provider": "lajiao_credentials", "key": selected_key}]
        icloud_privacy_state = self._load_icloud_privacy_pool_state()
        reported: list[dict[str, str]] = []
        for lease in leases:
            if not isinstance(lease, dict):
                continue
            resource_type = str(lease.get("type") or "")
            provider = str(lease.get("provider") or "")
            resource_key = str(lease.get("key") or "")
            if status == "succeeded" and bool(config.get("_resource_provider")) and resource_type == "phone" and provider == str(config.get("_resource_provider")):
                continue
            if not resource_key:
                continue
            if status == "succeeded" and resource_type == "proxy" and selected_proxy and not self._same_proxy_resource(resource_key, selected_proxy):
                current = self.repo.get(resource_type, provider, resource_key) if provider else {}
                if str(current.get("status") or "") == "cooldown":
                    resource_status = "cooldown"
                    classification = "proxy_not_selected_cooldown_preserved"
                else:
                    resource_status = self._report_failure_status(task_id, lease, "available", error="proxy leased but not selected")
                    classification = "proxy_not_selected"
                reported.append({"type": resource_type, "provider": provider, "key": resource_key, "status": resource_status, "classification": classification})
                continue
            if status == "succeeded" and resource_type == "proxy" and not selected_proxy:
                current = self.repo.get(resource_type, provider, resource_key) if provider else {}
                if str(current.get("status") or "") == "cooldown":
                    resource_status = "cooldown"
                    classification = "proxy_not_selected_cooldown_preserved"
                else:
                    resource_status = self._report_failure_status(task_id, lease, "available", error="proxy leased but no selected proxy in log")
                    classification = "proxy_not_selected"
                reported.append({"type": resource_type, "provider": provider, "key": resource_key, "status": resource_status, "classification": classification})
                continue
            if status == "succeeded":
                resource_status = self._report_success_status(task_id, lease, selected_exit_ip=selected_exit_ip if self._same_proxy_resource(resource_key, selected_proxy) else "")
                classification = "success"
            else:
                if resource_type == "proxy" and selected_proxy and not self._same_proxy_resource(resource_key, selected_proxy):
                    resource_status = self._report_failure_status(task_id, lease, "available", error="proxy leased but not selected")
                    classification = "proxy_not_selected"
                    reported.append({"type": resource_type, "provider": provider, "key": resource_key, "status": resource_status, "classification": classification})
                    continue
                state = icloud_privacy_state.get(resource_key.strip().lower(), "") if provider == "icloud_privacy" else ""
                if resource_type == "email" and provider == "icloud_privacy" and state in {"consumed", "dirty_email_already_used"}:
                    classification, resource_status, cooldown_seconds = ("icloud_privacy_state_" + state, "used", 0)
                elif resource_type == "email" and provider == "icloud_privacy" and state == "cooldown":
                    classification, resource_status, cooldown_seconds = ("icloud_privacy_state_cooldown", "cooldown", 3600)
                else:
                    classification, resource_status, cooldown_seconds = self._classify_resource_status(resource_type, status, evidence)
                resource_status = self._report_failure_status(task_id, lease, resource_status, error=error or classification or status, cooldown_seconds=cooldown_seconds)
                if resource_type == "proxy" and self._same_proxy_resource(resource_key, selected_proxy) and selected_exit_ip and resource_status == "cooldown":
                    self._remember_proxy_exit_ip(provider, resource_key, selected_exit_ip)
                    self._cooldown_proxy_exit_ip_siblings(provider, resource_key, selected_exit_ip, error=error or classification or status)
            reported.append({"type": resource_type, "provider": provider, "key": resource_key, "status": resource_status, "classification": classification})
        return reported
