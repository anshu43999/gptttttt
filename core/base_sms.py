"""接码服务基类 + SMS-Activate / HeroSMS 实现。"""
from __future__ import annotations


import hashlib
import json
import logging
import threading
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import requests

logger = logging.getLogger(__name__)


@dataclass
class SmsActivation:
    """Represents an active phone number rental."""
    activation_id: str
    phone_number: str
    country: str = ""
    metadata: dict = field(default_factory=dict)


class BaseSmsProvider(ABC):
    """Base class for SMS verification code providers."""

    auto_report_success_on_code = True

    @abstractmethod
    def get_number(self, *, service: str, country: str = "") -> SmsActivation:
        """Rent a phone number for the given service."""
        ...

    @abstractmethod
    def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
        """Wait for and return the SMS verification code."""
        ...

    @abstractmethod
    def cancel(self, activation_id: str) -> bool:
        """Cancel/release an activation. Returns True on success."""
        ...

    def report_success(self, activation_id: str) -> bool:
        """Report that the code was used successfully (optional)."""
        return True

    def set_resend_callback(self, callback: Callable[[], None] | None) -> None:
        """Optional hook used by providers that can request upstream resend."""
        return None

    def mark_code_failed(self, activation_id: str, reason: str = "") -> None:
        """Optional hook used when the target service rejects a received code."""
        return None

    def mark_send_failed(self, activation_id: str, reason: str = "") -> None:
        """Optional hook used when the target service rejects the rented phone."""
        return None

    def mark_send_succeeded(self, activation_id: str) -> None:
        """Optional hook used when the target service accepts the rented phone."""
        return None

    def mark_attempt_failed(self, activation_id: str, *, outcome: str = "", failure_code: str = "", reason: str = "") -> None:
        """Optional hook used to finalize a failed add-phone/bind attempt."""
        return None

    def get_reuse_info(self) -> dict:
        """Return provider-specific reuse state for task scheduling."""
        return {}


# ---------------------------------------------------------------------------
# SMS-Activate implementation (https://sms-activate.guru)
# ---------------------------------------------------------------------------

SMS_ACTIVATE_SERVICES = {
    "cursor": "ot",
    "chatgpt": "dr",
    "openai": "dr",
    "google": "go",
    "microsoft": "mg",
    "default": "ot",
}

SMS_ACTIVATE_COUNTRIES = {
    "ru": "0",
    "us": "187",
    "uk": "16",
    "in": "22",
    "id": "6",
    "ph": "4",
    "th": "52",
    "br": "73",
    "default": "0",
}


def _resolve_sms_activate_country_id(country: str, default_country: str) -> str:
    raw = str(country or default_country or "").strip().lower()
    if not raw:
        raw = "default"
    if raw.isdigit():
        return raw
    return SMS_ACTIVATE_COUNTRIES.get(raw, SMS_ACTIVATE_COUNTRIES["default"])

def _sms_proxy_from_config(config: dict) -> str | None:
    proxy = str((config or {}).get("sms_proxy") or "").strip()
    if proxy.lower() in {"", "direct", "none", "singbox://direct"}:
        return None
    return proxy



class SmsActivateProvider(BaseSmsProvider):
    """SMS-Activate (sms-activate.guru) provider."""

    BASE_URL = "https://api.sms-activate.guru/stubs/handler_api.php"

    def __init__(self, api_key: str, *, default_country: str = "", proxy: str = None):
        self.api_key = api_key
        self.default_country = default_country or "ru"
        self._proxy = {"http": proxy, "https": proxy} if proxy else None

    def _request(self, action: str, **params) -> str:
        params["api_key"] = self.api_key
        params["action"] = action
        resp = requests.get(
            self.BASE_URL,
            params=params,
            timeout=20,
            proxies=self._proxy,
        )
        resp.raise_for_status()
        return resp.text.strip()

    def get_balance(self) -> float:
        result = self._request("getBalance")
        if result.startswith("ACCESS_BALANCE:"):
            return float(result.split(":")[1])
        raise RuntimeError(f"SMS-Activate getBalance failed: {result}")

    def get_number(self, *, service: str, country: str = "") -> SmsActivation:
        service_code = SMS_ACTIVATE_SERVICES.get(service, SMS_ACTIVATE_SERVICES["default"])
        country_id = _resolve_sms_activate_country_id(country, self.default_country)

        result = self._request("getNumber", service=service_code, country=country_id)
        if result.startswith("ACCESS_NUMBER:"):
            parts = result.split(":")
            return SmsActivation(
                activation_id=parts[1],
                phone_number=parts[2],
                country=country or self.default_country,
            )

        if "NO_NUMBERS" in result:
            raise RuntimeError(f"SMS-Activate: 当前无可用号码 (service={service_code}, country={country_id})")
        if "NO_BALANCE" in result:
            raise RuntimeError("SMS-Activate: 余额不足")
        raise RuntimeError(f"SMS-Activate getNumber failed: {result}")

    def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            result = self._request("getStatus", id=activation_id)
            if result.startswith("STATUS_OK:"):
                return result.split(":")[1]
            if result == "STATUS_WAIT_CODE":
                time.sleep(3)
                continue
            if result == "STATUS_WAIT_RETRY":
                self._request("setStatus", id=activation_id, status="6")
                time.sleep(3)
                continue
            if result == "STATUS_CANCEL":
                return ""
            time.sleep(3)

        self.cancel(activation_id)
        return ""

    def cancel(self, activation_id: str) -> bool:
        result = self._request("setStatus", id=activation_id, status="8")
        return "ACCESS" in result

    def report_success(self, activation_id: str) -> bool:
        result = self._request("setStatus", id=activation_id, status="6")
        return "ACCESS" in result


# ---------------------------------------------------------------------------
# HeroSMS implementation (https://hero-sms.com/stubs/handler_api.php)
# ---------------------------------------------------------------------------

HERO_SMS_DEFAULT_SERVICE = "dr"
HERO_SMS_DEFAULT_COUNTRY = "187"
HERO_SMS_PHONE_LIFETIME = 20 * 60
_HERO_SMS_CACHE_LOCK = threading.Lock()
_HERO_SMS_VERIFY_LOCK = threading.RLock()
_HERO_SMS_CACHE: dict | None = None


def _project_data_dir() -> Path:
    root = Path(__file__).resolve().parent.parent
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def hero_sms_cache_file() -> Path:
    return _project_data_dir() / ".herosms_phone_cache.json"


def _hash_secret(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _safe_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_bool(value, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", "否"}


def extract_verification_code(text: str, *, ignored_numbers: set[str] | None = None, expected_lengths: tuple[int, ...] = (6,)) -> str:
    """Extract a likely OpenAI/ChatGPT verification code from noisy SMS or email text."""
    raw = str(text or "")
    if not raw:
        return ""
    ignored = {"".join(ch for ch in str(item or "") if ch.isdigit()) for item in (ignored_numbers or set())}
    normalized = re.sub(r"\s+", " ", raw)
    normalized = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", " ", normalized)
    normalized = re.sub(r"https?://\S+", " ", normalized)
    semantic = (
        r"(?:openai|chatgpt|codex|verification code|verify code|security code|login code|one[- ]time code|temporary code|"
        r"c[oó]digo|codigo|验证码|驗證碼|校验码|認証コード|確認コード|コード|인증 코드)"
        r"[^0-9]{0,120}(\d{4,8})"
    )
    for pattern in (semantic, r"(\d{4,8})[^0-9]{0,120}(?:openai|chatgpt|verification|verify|c[oó]digo|验证码|驗證碼|認証|確認)"):
        for match in re.finditer(pattern, normalized, flags=re.IGNORECASE):
            value = match.group(1)
            if _verification_code_allowed(value, ignored, expected_lengths):
                return value
    candidates: list[tuple[int, str]] = []
    for match in re.finditer(r"(?<![A-Za-z0-9])(\d{4,8})(?![A-Za-z0-9])", normalized):
        value = match.group(1)
        if not _verification_code_allowed(value, ignored, expected_lengths):
            continue
        window = normalized[max(0, match.start() - 80): match.end() + 80]
        score = 0
        if len(value) == 6:
            score += 8
        if re.search(r"openai|chatgpt|codex", window, flags=re.IGNORECASE):
            score += 5
        if re.search(r"code|verify|verification|c[oó]digo|验证码|驗證碼|認証|確認|コード", window, flags=re.IGNORECASE):
            score += 4
        candidates.append((score, value))
    if not candidates:
        return ""
    candidates.sort(key=lambda item: (-item[0], -len(item[1])))
    return candidates[0][1]


def _verification_code_allowed(value: str, ignored: set[str], expected_lengths: tuple[int, ...]) -> bool:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if not digits or digits in ignored:
        return False
    if expected_lengths and len(digits) not in expected_lengths:
        return False
    if digits in {"0000", "000000", "111111", "123456"}:
        return False
    if len(digits) == 4 and digits.startswith(("19", "20")):
        return False
    return True


class UserProvidedSmsProvider(BaseSmsProvider):
    """Use operator-supplied phone numbers with per-number SMS polling URLs."""

    auto_report_success_on_code = False

    def __init__(self, entries: list[tuple[str, str]], *, proxy: str | None = None, country_code: str = "", resource_provider: str = "", task_id: str = ""):
        self._entries = list(entries)
        self._index = 0
        self.country_code = "".join(ch for ch in str(country_code or "") if ch.isdigit())
        self.proxies = {"http": proxy, "https": proxy} if proxy else None
        self._session = requests.Session()
        self._session.trust_env = False
        self._ignored_codes_by_activation: dict[str, set[str]] = {}
        self.resource_provider = str(resource_provider or "")
        self.task_id = str(task_id or "")
        self.current_resource_key = ""
        self.current_send_failed = False
        self.current_activation_id = ""
        self.current_lifecycle_reported = False

    @staticmethod
    def parse_entries(value: str | list[str] | tuple[str, ...]) -> list[tuple[str, str]]:
        if isinstance(value, (list, tuple)):
            rows = [str(item or "") for item in value]
        else:
            rows = str(value or "").replace("\r", "\n").split("\n")
        entries: list[tuple[str, str]] = []
        for row in rows:
            row = row.strip()
            if not row or row.startswith("#"):
                continue
            normalized = row.replace("----", "|")
            if "|" not in normalized:
                raise RuntimeError(f"手机号接码行格式错误，期望 phone|sms_url 或 phone----sms_url: {row[:80]}")
            parts = [part.strip() for part in normalized.split("|") if part.strip()]
            phone = parts[0] if parts else ""
            sms_url = ""
            for part in parts[1:]:
                if part.startswith(("http://", "https://")):
                    sms_url = part
                    break
            if not sms_url and len(parts) >= 2:
                sms_url = parts[1]
            if not phone or not sms_url:
                raise RuntimeError(f"手机号接码行缺少 phone 或 sms_url: {row[:80]}")
            entries.append((phone, sms_url))
        return entries

    @classmethod
    def from_config(cls, config: dict):
        entries = cls.parse_entries(config.get("sms_phone_url") or config.get("sms_phone_urls") or "")
        file_path = str(config.get("sms_phone_url_file") or "").strip()
        if file_path:
            path = Path(file_path)
            if not path.exists():
                raise RuntimeError(f"手机号接码文件不存在: {file_path}")
            entries.extend(cls.parse_entries(path.read_text(encoding="utf-8")))
        resource_provider = str(config.get("_resource_provider") or "")
        task_id = str(config.get("dashboard_task_id") or "")
        if not entries and not (resource_provider and task_id):
            raise RuntimeError("未配置 sms_phone_url / sms_phone_url_file，格式: 手机号|取码URL")
        proxy = _sms_proxy_from_config(config)
        return cls(entries, proxy=proxy, country_code=str(config.get("country_code") or ""), resource_provider=resource_provider, task_id=task_id)

    def get_number(self, *, service: str, country: str = "") -> SmsActivation:
        if self._index < len(self._entries):
            phone, sms_url = self._entries[self._index]
            self._index += 1
            resource_key = phone
        elif self.resource_provider and self.task_id:
            from infrastructure.repositories.resource_pool_repository import ResourcePoolRepository
            lease = ResourcePoolRepository().lease("phone", self.resource_provider, self.task_id)
            if not lease.resource_key:
                raise RuntimeError("用户提供的手机号已用完")
            phone = str(lease.payload.get("phone") or lease.resource_key)
            sms_url = str(lease.payload.get("sms_url") or "")
            resource_key = lease.resource_key
        else:
            raise RuntimeError("用户提供的手机号已用完")
        phone = self._normalize_phone(phone)
        self.current_resource_key = str(resource_key or phone)
        self.current_send_failed = False
        self.current_activation_id = sms_url
        self.current_lifecycle_reported = False
        existing_code = self._read_current_code(sms_url)
        if existing_code:
            self._ignored_codes_by_activation.setdefault(sms_url, set()).add(existing_code)
        return SmsActivation(activation_id=sms_url, phone_number=phone, country=country, metadata={"sms_url": sms_url, "resource_key": self.current_resource_key})

    def _normalize_phone(self, phone: str) -> str:
        value = str(phone or "").strip()
        if value.startswith("+"):
            return value
        digits = "".join(ch for ch in value if ch.isdigit())
        if self.country_code and digits.startswith(self.country_code):
            return "+" + digits
        if self.country_code and digits:
            return "+" + self.country_code + digits
        return value

    def _current_resource(self):
        if not (self.resource_provider and self.task_id and self.current_resource_key):
            return None, {}, 0
        from infrastructure.repositories.resource_pool_repository import ResourcePoolRepository
        repo = ResourcePoolRepository()
        resource = repo.get("phone", self.resource_provider, self.current_resource_key)
        return repo, resource, int(resource.get("id") or 0)

    @staticmethod
    def _resource_error(failure_code: str = "", reason: str = "") -> str:
        failure_code = str(failure_code or "").strip()
        reason = str(reason or "").strip()
        if failure_code and reason:
            return f"{failure_code}: {reason}"
        return failure_code or reason

    def _report_failed_attempt(self, *, outcome: str, failure_code: str = "", reason: str = "") -> None:
        repo, _resource, resource_id = self._current_resource()
        if repo is None or resource_id <= 0:
            return
        from application.resource_pool_service import BIND_PHONE_OUTCOME_RELEASED, ResourcePoolService, bind_phone_failure_resource_status
        error = self._resource_error(failure_code, reason) or outcome or "bind phone failed"
        self.current_send_failed = True
        self.current_lifecycle_reported = True
        resource_status, cooldown_seconds = bind_phone_failure_resource_status(outcome, failure_code=failure_code)
        if resource_status == "available" or outcome == BIND_PHONE_OUTCOME_RELEASED:
            repo.set_status(resource_id, status="available", error=error)
            return
        if resource_status == "cooldown":
            repo.report(
                self.task_id,
                self.current_resource_key,
                success=False,
                cooldown_until=ResourcePoolService().cooldown_until(cooldown_seconds),
                error=error,
            )
            return
        repo.report(self.task_id, self.current_resource_key, success=False, error=error)
        repo.set_status(resource_id, status=resource_status, error=error)

    def mark_send_failed(self, activation_id: str, reason: str = "") -> None:
        self.current_send_failed = True
        if not (self.resource_provider and self.task_id and self.current_resource_key):
            return
        from application.resource_pool_service import classify_bind_phone_failure
        outcome, failure_code = classify_bind_phone_failure(reason, phase="send")
        self._report_failed_attempt(outcome=outcome, failure_code=failure_code, reason=reason)

    def report_success(self, activation_id: str) -> bool:
        self.current_lifecycle_reported = True
        if self.resource_provider and self.task_id and self.current_resource_key:
            from infrastructure.repositories.resource_pool_repository import ResourcePoolRepository
            ResourcePoolRepository().report(self.task_id, self.current_resource_key, success=True)
        return True

    def cancel(self, activation_id: str) -> bool:
        repo, _resource, resource_id = self._current_resource()
        if repo is not None and resource_id > 0 and not self.current_send_failed and not self.current_lifecycle_reported:
            repo.set_status(resource_id, status="available", error=self._resource_error("bind_phone_released", "released unused phone"))
            self.current_lifecycle_reported = True
        return True

    def mark_attempt_failed(self, activation_id: str, *, outcome: str = "", failure_code: str = "", reason: str = "") -> None:
        if self.current_activation_id and str(activation_id or "") and str(activation_id) != self.current_activation_id:
            return
        self._report_failed_attempt(outcome=outcome, failure_code=failure_code, reason=reason)

    def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
        deadline = time.time() + timeout
        last_text = ""
        while time.time() < deadline:
            try:
                code, last_text = self._read_current_code_with_raw(activation_id)
                if code and code not in self._ignored_codes_by_activation.get(activation_id, set()):
                    return code
            except Exception as exc:
                last_text = str(exc)
            time.sleep(3)
        raise RuntimeError(f"用户接码 API 超时，未读取到验证码: {last_text[:160]}")

    def wait_for_code(self, activation_id: str, *, timeout: int = 120, poll_interval: int = 3) -> dict:
        code = self.get_code(activation_id, timeout=timeout)
        return {"status": "ok", "code": code}

    def get_status(self, activation_id: str) -> dict:
        try:
            code, text = self._read_current_code_with_raw(activation_id)
            return {"status": "ok" if code else "wait_code", "code": code, "raw": text[:500]}
        except Exception as exc:
            return {"status": "wait_code", "code": "", "raw": str(exc)[:500]}

    def mark_code_failed(self, activation_id: str, reason: str = "") -> None:
        code = self._read_current_code(activation_id)
        if code:
            self._ignored_codes_by_activation.setdefault(activation_id, set()).add(code)

    def _read_current_code(self, activation_id: str) -> str:
        code, _ = self._read_current_code_with_raw(activation_id)
        return code

    def _read_current_code_with_raw(self, activation_id: str) -> tuple[str, str]:
        response = self._session.get(activation_id, timeout=20, proxies=self.proxies)
        text = (response.text or "").strip()
        if response.status_code >= 400:
            return "", text
        return self._extract_code(text), text

    @staticmethod
    def _extract_code(text: str) -> str:
        if not text:
            return ""
        try:
            payload = json.loads(text)
            for key in ("text", "sms", "message", "msg", "content"):
                value = payload.get(key) if isinstance(payload, dict) else None
                if isinstance(value, str):
                    code = UserProvidedSmsProvider._extract_code_from_message(value)
                    if code:
                        return code
            data = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(data, dict):
                for key in ("text", "sms", "message", "msg", "content"):
                    value = data.get(key)
                    if isinstance(value, str):
                        code = UserProvidedSmsProvider._extract_code_from_message(value)
                        if code:
                            return code
                phone = str(data.get("phoneNumber") or data.get("phone") or "")
                generic = UserProvidedSmsProvider._extract_code_from_message(json.dumps(data, ensure_ascii=False), ignored_numbers={phone})
                if generic:
                    return generic
        except Exception:
            pass
        return UserProvidedSmsProvider._extract_code_from_message(text)

    @staticmethod
    def _extract_code_from_message(text: str, *, ignored_numbers: set[str] | None = None) -> str:
        return extract_verification_code(text, ignored_numbers=ignored_numbers, expected_lengths=(4, 5, 6, 7, 8))


def _normalize_hero_proxy(proxy: str | None) -> str | None:
    proxy = str(proxy or "").strip()
    if not proxy or proxy.startswith("singbox://"):
        return None
    return proxy


def _parse_hero_status_text(text: str) -> dict:
    text = str(text or "").strip()
    if text == "STATUS_WAIT_CODE":
        return {"status": "wait_code"}
    if text.startswith("STATUS_WAIT_RETRY"):
        return {"status": "wait_retry", "raw": text}
    if text == "STATUS_WAIT_RESEND":
        return {"status": "wait_resend"}
    if text.startswith("STATUS_OK:"):
        return {"status": "ok", "code": text.split(":", 1)[1]}
    if text == "STATUS_CANCEL":
        return {"status": "cancel"}
    return {"status": "unknown", "raw": text}


def _canonical_sms_event_fields(event_fields: dict | None) -> dict:
    event_fields = event_fields or {}
    canonical: dict[str, str] = {}
    channel = str(event_fields.get("channel") or "").strip()
    if channel:
        canonical["channel"] = channel
    sms_time = (
        event_fields.get("dateTime")
        or event_fields.get("date")
        or event_fields.get("smsDate")
        or event_fields.get("smsTime")
        or ""
    )
    if sms_time:
        canonical["time"] = str(sms_time)
    text = event_fields.get("text") or event_fields.get("smsText")
    if text:
        canonical["text"] = str(text)
    if channel == "call":
        for key in ("from", "url"):
            if event_fields.get(key):
                canonical[key] = str(event_fields[key])
    if not sms_time:
        for key in ("repeated", "activationStatus", "verificationType"):
            if event_fields.get(key) is not None:
                canonical[key] = str(event_fields[key])
    return canonical


def _has_real_sms_time(event_fields: dict | None) -> bool:
    raw_time = (
        (event_fields or {}).get("dateTime")
        or (event_fields or {}).get("date")
        or (event_fields or {}).get("smsDate")
        or (event_fields or {}).get("smsTime")
        or ""
    )
    raw_time = str(raw_time).strip()
    return bool(raw_time and raw_time not in {"0", "0000-00-00 00:00:00", "0000-00-00T00:00:00"})


def _sms_event_key(activation_id: str, code: str, event_fields: dict | None) -> str:
    identity = {"activation_id": str(activation_id), "code": str(code)}
    identity.update(_canonical_sms_event_fields(event_fields))
    raw = json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _make_sms_candidate(activation_id: str, source: str, code, event_fields: dict | None = None) -> dict | None:
    code = str(code or "").strip()
    if not code or code in {"null", "None"}:
        return None
    canonical = _canonical_sms_event_fields(event_fields)
    sms_key = _sms_event_key(activation_id, code, event_fields) if event_fields else ""
    return {
        "status": "ok",
        "code": code,
        "source": source,
        "sms_key": sms_key,
        "sms_time": canonical.get("time", ""),
        "sms_text": canonical.get("text", ""),
        "allow_same_code": _has_real_sms_time(event_fields),
    }


def _candidate_is_attempted(candidate: dict, used_codes: set, attempted_sms_keys: set) -> bool:
    sms_key = str(candidate.get("sms_key") or "")
    code = str(candidate.get("code") or "")
    if sms_key and sms_key in attempted_sms_keys:
        return True
    return bool(code in used_codes and not candidate.get("allow_same_code"))


class HeroSmsProvider(BaseSmsProvider):
    """HeroSMS provider with resend, SMS event dedupe, and short-lived phone reuse."""

    BASE_URL = "https://hero-sms.com/stubs/handler_api.php"
    auto_report_success_on_code = False

    def __init__(
        self,
        api_key: str,
        *,
        default_service: str = HERO_SMS_DEFAULT_SERVICE,
        default_country: str = HERO_SMS_DEFAULT_COUNTRY,
        max_price: float = -1,
        fixed_price: bool = False,
        phone_exceptions: list[str] | tuple[str, ...] | str | None = None,
        proxy: str | None = None,
        reuse_phone_to_max: bool = True,
        phone_success_max: int = 3,
    ):
        self.api_key = str(api_key or "").strip()
        self.default_service = str(default_service or HERO_SMS_DEFAULT_SERVICE).strip()
        self.default_country = str(default_country or HERO_SMS_DEFAULT_COUNTRY).strip()
        self.max_price = float(max_price or -1)
        self.fixed_price = bool(fixed_price)
        self.phone_exceptions = self._normalize_phone_exceptions(phone_exceptions)
        self.proxy = _normalize_hero_proxy(proxy)
        self.proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
        self.session = requests.Session()
        self.session.trust_env = False
        self.reuse_phone_to_max = bool(reuse_phone_to_max)
        self.phone_success_max = max(0, int(phone_success_max or 0))
        self.openai_resend_callback: Callable[[], None] | None = None
        self.last_code_result: dict | None = None
        self.current_activation: SmsActivation | None = None

    @staticmethod
    def _normalize_phone_exceptions(phone_exceptions: list[str] | tuple[str, ...] | str | None) -> list[str]:
        if not phone_exceptions:
            return []
        if isinstance(phone_exceptions, str):
            raw_items = phone_exceptions.replace("\n", ",").split(",")
        else:
            raw_items = list(phone_exceptions)
        result = []
        seen = set()
        for item in raw_items:
            digits = "".join(ch for ch in str(item or "") if ch.isdigit())
            if len(digits) > 7:
                digits = digits[:7]
            if len(digits) < 4 or digits in seen:
                continue
            seen.add(digits)
            result.append(digits)
            if len(result) >= 20:
                break
        return result

    @classmethod
    def from_config(cls, config: dict):
        config = dict(config or {})
        api_key = str(config.get("herosms_api_key") or config.get("sms_api_key") or "").strip()
        if not api_key:
            raise RuntimeError("HeroSMS 未配置 API Key")
        fixed_price = _safe_bool(config.get("herosms_fixed_price"), False)
        max_price = _safe_float(config.get("herosms_max_price"), 0.0999)
        if max_price <= 0 or max_price >= 0.1:
            max_price = 0.0999
        phone_exceptions = config.get("herosms_phone_exceptions") or config.get("sms_phone_exceptions")
        return cls(
            api_key=api_key,
            default_service=str(config.get("sms_service") or config.get("herosms_service") or config.get("herosms_default_service") or HERO_SMS_DEFAULT_SERVICE),
            default_country=str(config.get("sms_country") or config.get("herosms_country") or config.get("herosms_default_country") or HERO_SMS_DEFAULT_COUNTRY),
            max_price=max_price,
            fixed_price=fixed_price,
            phone_exceptions=phone_exceptions,
            proxy=_sms_proxy_from_config(config),
            reuse_phone_to_max=_safe_bool(config.get("register_reuse_phone_to_max"), True),
            phone_success_max=max(0, _safe_int(config.get("register_phone_extra_max") or config.get("register_phone_success_max"), 3)),
        )

    def _request(self, params: dict, *, needs_key: bool = True, timeout: int = 30) -> requests.Response:
        payload = dict(params)
        if needs_key:
            payload["api_key"] = self.api_key
        resp = self.session.get(self.BASE_URL, params=payload, timeout=timeout, proxies=self.proxies)
        resp.raise_for_status()
        return resp

    def get_balance(self) -> float:
        text = self._request({"action": "getBalance"}).text.strip()
        if text.startswith("ACCESS_BALANCE:"):
            return float(text.split(":", 1)[1])
        raise RuntimeError(f"HeroSMS getBalance failed: {text}")

    def get_services(self, country: str | int | None = None, lang: str = "cn") -> list:
        params = {"action": "getServicesList", "lang": lang}
        if country not in (None, ""):
            params["country"] = country
        data = self._request(params, needs_key=False).json()
        if isinstance(data, dict) and data.get("status") == "success":
            return list(data.get("services") or [])
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # 可能是 {"dr": {"name": "OpenAI", ...}, ...} 格式
            result = []
            for key, value in data.items():
                if key in ("status", "message", "error"):
                    continue
                if isinstance(value, dict):
                    if "code" not in value:
                        value["code"] = key
                    result.append(value)
                elif isinstance(value, str):
                    result.append({"code": key, "name": value})
            if result:
                return result
        raise RuntimeError("HeroSMS getServicesList returned unexpected response")

    def get_countries(self) -> list:
        data = self._request({"action": "getCountries"}, needs_key=False).json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # 检查是否是错误响应 {"status":0,"message":"No access","data":[]}
            if data.get("status") == 0 or data.get("message") == "No access":
                raise RuntimeError(f"SMS API access denied: {data.get('message', 'unknown')}")
            # HeroSMS 可能返回 {"0": {"id": 0, "eng": "Russia"}, ...} 格式
            result = []
            for key, value in data.items():
                if key in ("status", "message", "data", "error"):
                    continue
                if isinstance(value, dict):
                    if "id" not in value:
                        value["id"] = key
                    result.append(value)
                elif isinstance(value, str):
                    result.append({"id": key, "eng": value, "name": value})
            if result:
                return result
        raise RuntimeError("SMS getCountries returned unexpected response")

    def get_prices(self, service: str | None = None, country: str | int | None = None) -> dict:
        params = {"action": "getPrices"}
        if service:
            params["service"] = service
        if country not in (None, ""):
            params["country"] = country
        data = self._request(params).json()
        if isinstance(data, dict):
            return data
        raise RuntimeError("HeroSMS getPrices returned unexpected response")

    def get_top_countries(self, service: str | None = None) -> list[dict]:
        """获取指定服务按价格排序的国家列表（含价格和库存）。

        优先使用 getTopCountriesByServiceRank API，降级到 getPrices 全量解析。
        返回格式: [{"country": "66", "name": "Thailand", "price": 0.12, "count": 150}, ...]
        """
        service_code = str(service or self.default_service or HERO_SMS_DEFAULT_SERVICE).strip()

        # 策略1: 使用 getTopCountriesByServiceRank（HeroSMS 专用排名接口）
        for action in ("getTopCountriesByServiceRank", "getTopCountriesByService"):
            try:
                data = self._request({"action": action, "service": service_code}).json()
                rows = self._parse_top_countries_response(data)
                if rows:
                    rows.sort(key=lambda r: (r.get("price") or 999, -(r.get("count") or 0)))
                    return rows
            except Exception:
                continue

        # 策略2: 从 getPrices 全量数据中解析
        try:
            prices = self.get_prices(service=service_code)
            rows = []
            for country_id, services in prices.items():
                if not isinstance(services, dict):
                    continue
                svc_data = services.get(service_code)
                if not isinstance(svc_data, dict):
                    continue
                price = svc_data.get("cost") or svc_data.get("price")
                count = svc_data.get("count") or svc_data.get("qty") or svc_data.get("available")
                try:
                    price = float(price) if price is not None else None
                except (TypeError, ValueError):
                    price = None
                try:
                    count = int(count) if count is not None else 0
                except (TypeError, ValueError):
                    count = 0
                if price is not None and count > 0:
                    rows.append({"country": str(country_id), "price": price, "count": count})
            rows.sort(key=lambda r: (r.get("price") or 999, -(r.get("count") or 0)))
            return rows
        except Exception:
            return []

    def _parse_top_countries_response(self, data) -> list[dict]:
        """解析 getTopCountriesByServiceRank 响应。"""
        rows = []
        items = data
        # 可能嵌套在 data/result 键下
        if isinstance(data, dict):
            items = data.get("data") or data.get("result") or data.get("response") or data
        if isinstance(items, dict):
            # {country_id: {price, count, ...}} 格式
            for key, value in items.items():
                if not isinstance(value, dict):
                    continue
                try:
                    country_id = str(int(key))
                except (TypeError, ValueError):
                    continue
                price = value.get("price") or value.get("cost") or value.get("retail_price")
                count = value.get("count") or value.get("qty") or value.get("available") or value.get("stock")
                name = value.get("name") or value.get("countryName") or value.get("country_name") or ""
                try:
                    price = float(price) if price is not None else None
                except (TypeError, ValueError):
                    price = None
                try:
                    count = int(count) if count is not None else 0
                except (TypeError, ValueError):
                    count = 0
                if price is not None:
                    rows.append({"country": country_id, "name": str(name), "price": price, "count": count})
        elif isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                country_id = item.get("country") or item.get("countryId") or item.get("country_id") or item.get("id")
                if country_id is None:
                    continue
                price = item.get("price") or item.get("cost") or item.get("retail_price") or item.get("retailPrice")
                count = item.get("count") or item.get("qty") or item.get("available") or item.get("stock") or item.get("total")
                name = item.get("name") or item.get("countryName") or item.get("country_name") or item.get("title") or ""
                try:
                    price = float(price) if price is not None else None
                except (TypeError, ValueError):
                    price = None
                try:
                    count = int(count) if count is not None else 0
                except (TypeError, ValueError):
                    count = 0
                if price is not None:
                    rows.append({"country": str(country_id), "name": str(name), "price": price, "count": count})
        return rows

    def get_best_country(self, service: str | None = None, *, min_stock: int = 20, max_price: float = 0) -> str | None:
        """自动选择最优国家：价格最低且库存充足。

        Args:
            service: 服务代码（默认使用 self.default_service）
            min_stock: 最低库存要求（默认 20）
            max_price: 最高价格限制（0 表示不限）

        Returns:
            最优国家 ID 字符串，或 None（无可用国家）
        """
        # HeroSMS/SMSBower 中已验证对 OpenAI 走 SMS（非 WhatsApp）的国家白名单
        # OpenAI 2025年起对绝大多数国家改用 WhatsApp 验证
        # 目前只有泰国确认走 SMS
        ALLOWED_COUNTRIES = {
            "52",   # Thailand (已验证走SMS)
        }

        try:
            rows = self.get_top_countries(service=service)
        except Exception as exc:
            logger.warning("get_best_country 查询失败: %s", exc)
            return None

        if not rows:
            return None

        for row in rows:
            country_id = str(row.get("country") or "")
            if country_id not in ALLOWED_COUNTRIES:
                continue
            price = row.get("price") or 0
            count = row.get("count") or 0
            if count < min_stock:
                continue
            if max_price > 0 and price > max_price:
                continue
            return country_id

        # 如果没有满足 min_stock 的，放宽到 count > 0
        for row in rows:
            country_id = str(row.get("country") or "")
            if country_id not in ALLOWED_COUNTRIES:
                continue
            price = row.get("price") or 0
            count = row.get("count") or 0
            if count <= 0:
                continue
            if max_price > 0 and price > max_price:
                continue
            return country_id

        return None

    def _cache_identity(self, service: str, country: str) -> dict:
        return {
            "api_key_hash": _hash_secret(self.api_key),
            "service": str(service),
            "country": str(country),
        }

    def _load_cache(self, service: str, country: str) -> dict | None:
        global _HERO_SMS_CACHE
        if _HERO_SMS_CACHE is not None:
            cache = _HERO_SMS_CACHE
        else:
            path = hero_sms_cache_file()
            if not path.exists():
                return None
            try:
                cache = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return None
        identity = self._cache_identity(service, country)
        if any(str(cache.get(key) or "") != str(value) for key, value in identity.items()):
            return None
        elapsed = time.time() - float(cache.get("acquired_at") or 0)
        if elapsed >= HERO_SMS_PHONE_LIFETIME or cache.get("reuse_stopped"):
            self._clear_cache()
            return None
        if self.phone_success_max > 0 and int(cache.get("use_count") or 0) >= self.phone_success_max:
            cache["reuse_stopped"] = True
            cache["stop_reason"] = f"success max reached ({self.phone_success_max})"
            self._save_cache(cache)
            return None
        cache["used_codes"] = set(cache.get("used_codes") or [])
        cache["attempted_sms_keys"] = set(cache.get("attempted_sms_keys") or [])
        _HERO_SMS_CACHE = cache
        return cache

    def _save_cache(self, cache: dict | None) -> None:
        global _HERO_SMS_CACHE
        _HERO_SMS_CACHE = cache
        path = hero_sms_cache_file()
        if cache is None:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
            return
        serializable = dict(cache)
        serializable["used_codes"] = sorted(serializable.get("used_codes") or [])
        serializable["attempted_sms_keys"] = sorted(serializable.get("attempted_sms_keys") or [])
        serializable.pop("client", None)
        path.write_text(json.dumps(serializable, ensure_ascii=False), encoding="utf-8")

    def _clear_cache(self) -> None:
        self._save_cache(None)

    def _stop_reuse(self, reason: str) -> None:
        with _HERO_SMS_CACHE_LOCK:
            cache = _HERO_SMS_CACHE
            if not cache:
                return
            cache["reuse_stopped"] = True
            cache["stop_reason"] = reason
            self._save_cache(cache)

    def _number_request_params(self, service: str, country: str) -> dict:
        common = {"service": service, "country": country}
        if self.max_price > 0:
            common["maxPrice"] = self.max_price
            if self.fixed_price:
                common["fixedPrice"] = "true"
        else:
            try:
                prices = self.get_prices(service=service, country=country)
                country_prices = prices.get(str(country)) or prices.get(country) or {}
                service_prices = country_prices.get(service) or {}
                actual_cost = service_prices.get("cost") or service_prices.get("price")
                if actual_cost is not None:
                    common["maxPrice"] = float(actual_cost)
                    if self.fixed_price:
                        common["fixedPrice"] = "true"
            except Exception:
                common["maxPrice"] = 1
        if self.phone_exceptions:
            common["phoneException"] = ",".join(self.phone_exceptions[:20])
        return common

    def _request_number_raw(self, service: str, country: str) -> dict:
        common = self._number_request_params(service, country)
        v2_error = ""
        try:
            resp = self._request({"action": "getNumberV2", **common})
            try:
                data = resp.json()
            except ValueError:
                data = None
            if isinstance(data, dict) and data.get("activationId"):
                return data
            v2_error = resp.text.strip()[:200]
        except Exception as exc:
            v2_error = str(exc)

        try:
            text = self._request({"action": "getNumber", **common}).text.strip()
            if text.startswith("ACCESS_NUMBER:"):
                parts = text.split(":", 2)
                if len(parts) == 3:
                    return {
                        "activationId": parts[1],
                        "phoneNumber": parts[2],
                        "countryPhoneCode": "",
                        "activationCost": None,
                    }
            raise RuntimeError(text[:200])
        except Exception as exc:
            raise RuntimeError(f"HeroSMS 获取号码失败: V2={v2_error}; V1={exc}") from exc

    @staticmethod
    def _format_phone(number_info: dict) -> str:
        raw = str(number_info.get("phoneNumber") or "").strip()
        country_phone_code = str(number_info.get("countryPhoneCode") or "").strip()
        if raw.startswith("+"):
            return raw
        if country_phone_code and raw.startswith(country_phone_code):
            return f"+{raw}"
        if country_phone_code:
            return f"+{country_phone_code}{raw}"
        return f"+{raw}"

    def get_number(self, *, service: str, country: str = "") -> SmsActivation:
        service_code = str(service or self.default_service or HERO_SMS_DEFAULT_SERVICE).strip()
        country_id = str(country or self.default_country or HERO_SMS_DEFAULT_COUNTRY).strip()
        with _HERO_SMS_VERIFY_LOCK:
            with _HERO_SMS_CACHE_LOCK:
                cache = self._load_cache(service_code, country_id) if self.reuse_phone_to_max else None
                if cache:
                    activation = SmsActivation(
                        activation_id=str(cache["activation_id"]),
                        phone_number=str(cache["phone_number"]),
                        country=country_id,
                        metadata={"reused": True, "use_count": int(cache.get("use_count") or 0)},
                    )
                    self.current_activation = activation
                    return activation

                number_info = self._request_number_raw(service_code, country_id)
                activation_id = str(number_info.get("activationId") or "")
                phone = self._format_phone(number_info)
                if not activation_id or not phone.strip("+"):
                    raise RuntimeError("HeroSMS 返回的号码信息不完整")
                cache = {
                    **self._cache_identity(service_code, country_id),
                    "activation_id": activation_id,
                    "phone_number": phone,
                    "acquired_at": time.time(),
                    "use_count": 0,
                    "used_codes": set(),
                    "attempted_sms_keys": set(),
                    "reuse_stopped": False,
                    "stop_reason": "",
                }
                self._save_cache(cache)
                activation = SmsActivation(
                    activation_id=activation_id,
                    phone_number=phone,
                    country=country_id,
                    metadata={"reused": False, "number_info": number_info},
                )
                self.current_activation = activation
                return activation

    def get_status(self, activation_id: str) -> dict:
        return _parse_hero_status_text(self._request({"action": "getStatus", "id": activation_id}).text)

    def get_status_v2(self, activation_id: str) -> dict:
        resp = self._request({"action": "getStatusV2", "id": activation_id})
        text = resp.text.strip()
        try:
            data = resp.json()
        except ValueError:
            return _parse_hero_status_text(text)
        if isinstance(data, str):
            return _parse_hero_status_text(data)
        if not isinstance(data, dict):
            return {"status": "unknown", "raw": data}
        raw_status = data.get("status")
        if isinstance(raw_status, str):
            parsed = _parse_hero_status_text(raw_status)
            if parsed.get("status") != "unknown":
                return parsed
        for channel in ("sms", "call"):
            item = data.get(channel)
            if isinstance(item, dict):
                candidate = _make_sms_candidate(
                    activation_id,
                    f"getStatusV2.{channel}",
                    item.get("code"),
                    {
                        "channel": channel,
                        "dateTime": item.get("dateTime"),
                        "text": item.get("text"),
                        "from": item.get("from"),
                        "url": item.get("url"),
                        "verificationType": data.get("verificationType"),
                    },
                )
                if candidate:
                    return candidate
        return {"status": "wait_code", "raw": data}

    def get_active_activations(self, start: int = 0, limit: int = 20) -> list:
        data = self._request({"action": "getActiveActivations", "start": start, "limit": limit}).json()
        if isinstance(data, dict) and "data" in data:
            return list(data.get("data") or [])
        return []

    def set_status(self, activation_id: str, status: int) -> str:
        return self._request({"action": "setStatus", "id": activation_id, "status": status}).text.strip()

    def cancel_activation(self, activation_id: str) -> bool:
        try:
            resp = self._request({"action": "cancelActivation", "id": activation_id})
            if resp.status_code == 204 or "ACCESS_CANCEL" in resp.text:
                return True
        except Exception:
            pass
        try:
            return "ACCESS_CANCEL" in self.set_status(activation_id, 8)
        except Exception:
            return False

    def finish_activation(self, activation_id: str) -> bool:
        try:
            resp = self._request({"action": "finishActivation", "id": activation_id})
            text = resp.text.strip()
            return resp.status_code in (200, 204) or "ACCESS" in text
        except Exception:
            try:
                return "ACCESS" in self.set_status(activation_id, 6)
            except Exception:
                return False

    def request_resend_sms(self, activation_id: str) -> bool:
        try:
            self.set_status(activation_id, 3)
            return True
        except Exception:
            return False

    def wait_for_code(self, activation_id: str, *, timeout: int = 180, poll_interval: int = 3) -> dict | None:
        deadline = time.time() + timeout
        start = time.time()
        last_hero_resend = start
        openai_resent = False
        warned_v2 = False
        while time.time() < deadline:
            with _HERO_SMS_CACHE_LOCK:
                cache = _HERO_SMS_CACHE or {}
                used_codes = set(cache.get("used_codes") or [])
                attempted_sms_keys = set(cache.get("attempted_sms_keys") or [])

            for source in ("v2", "v1", "active"):
                try:
                    candidate = None
                    if source == "v2":
                        result = self.get_status_v2(activation_id)
                        if result.get("status") == "cancel":
                            return None
                        if result.get("status") == "ok":
                            candidate = result
                    elif source == "v1":
                        result = self.get_status(activation_id)
                        if result.get("status") == "cancel":
                            return None
                        if result.get("status") == "ok":
                            candidate = _make_sms_candidate(activation_id, "getStatus", result.get("code"))
                    else:
                        for item in self.get_active_activations():
                            if str(item.get("activationId")) == str(activation_id):
                                candidate = _make_sms_candidate(
                                    activation_id,
                                    "getActiveActivations",
                                    item.get("smsCode"),
                                    {
                                        "channel": "sms",
                                        "smsText": item.get("smsText"),
                                        "activationStatus": item.get("activationStatus"),
                                        "repeated": item.get("repeated"),
                                        "dateTime": item.get("dateTime"),
                                        "date": item.get("date") or item.get("smsDate") or item.get("smsTime"),
                                    },
                                )
                                break
                    if candidate and not _candidate_is_attempted(candidate, used_codes, attempted_sms_keys):
                        return candidate
                except Exception as exc:
                    if source == "v2" and not warned_v2:
                        logger.warning("HeroSMS getStatusV2 failed: %s", exc)
                        warned_v2 = True
                    else:
                        logger.debug("HeroSMS status check failed via %s: %s", source, exc)

            elapsed = time.time() - start
            if not openai_resent and elapsed >= 90 and self.openai_resend_callback:
                try:
                    self.openai_resend_callback()
                except Exception as exc:
                    logger.warning("OpenAI phone resend callback failed: %s", exc)
                self.request_resend_sms(activation_id)
                last_hero_resend = time.time()
                openai_resent = True
            elif time.time() - last_hero_resend >= 30:
                self.request_resend_sms(activation_id)
                last_hero_resend = time.time()

            time.sleep(poll_interval)
        return None

    def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
        wait_timeout = timeout
        with _HERO_SMS_CACHE_LOCK:
            cache = _HERO_SMS_CACHE or {}
            if cache and str(cache.get("activation_id")) == str(activation_id):
                remaining = max(0, int(HERO_SMS_PHONE_LIFETIME - (time.time() - float(cache.get("acquired_at") or 0))))
                if remaining:
                    wait_timeout = min(timeout, remaining)
        candidate = self.wait_for_code(activation_id, timeout=wait_timeout)
        self.last_code_result = candidate
        return str((candidate or {}).get("code") or "")

    def cancel(self, activation_id: str) -> bool:
        try:
            return self.cancel_activation(activation_id)
        finally:
            with _HERO_SMS_CACHE_LOCK:
                cache = _HERO_SMS_CACHE
                if cache and str(cache.get("activation_id")) == str(activation_id):
                    self._clear_cache()

    def report_success(self, activation_id: str) -> bool:
        should_finish = False
        should_clear_cache = False
        handled_cached_activation = False
        with _HERO_SMS_CACHE_LOCK:
            cache = _HERO_SMS_CACHE
            if cache and str(cache.get("activation_id")) == str(activation_id):
                handled_cached_activation = True
                cache["use_count"] = int(cache.get("use_count") or 0) + 1
                self._record_last_attempt(cache, failed=False)
                remaining = HERO_SMS_PHONE_LIFETIME - (time.time() - float(cache.get("acquired_at") or 0))
                if not self.reuse_phone_to_max:
                    cache["reuse_stopped"] = True
                    cache["stop_reason"] = "reuse disabled"
                    should_finish = True
                    should_clear_cache = True
                elif self.phone_success_max > 0 and int(cache["use_count"]) >= self.phone_success_max:
                    cache["reuse_stopped"] = True
                    cache["stop_reason"] = f"success max reached ({self.phone_success_max})"
                    should_finish = True
                elif remaining <= 30:
                    cache["reuse_stopped"] = True
                    cache["stop_reason"] = "phone lifetime nearly expired"
                    should_finish = True
                    should_clear_cache = True
                self._save_cache(cache)
                if should_clear_cache:
                    self._clear_cache()
        if handled_cached_activation:
            if should_finish:
                self.finish_activation(activation_id)
            return True
        return self.finish_activation(activation_id)

    def _record_last_attempt(self, cache: dict, *, failed: bool) -> None:
        candidate = self.last_code_result or {}
        code = str(candidate.get("code") or "")
        sms_key = str(candidate.get("sms_key") or "")
        used_codes = set(cache.get("used_codes") or [])
        attempted_sms_keys = set(cache.get("attempted_sms_keys") or [])
        if code:
            used_codes.add(code)
        if sms_key:
            attempted_sms_keys.add(sms_key)
        cache["used_codes"] = used_codes
        cache["attempted_sms_keys"] = attempted_sms_keys
        if failed:
            cache["last_failed_reason"] = "invalid otp"

    def mark_code_failed(self, activation_id: str, reason: str = "") -> None:
        with _HERO_SMS_CACHE_LOCK:
            cache = _HERO_SMS_CACHE
            if cache and str(cache.get("activation_id")) == str(activation_id):
                self._record_last_attempt(cache, failed=True)
                self._save_cache(cache)
        if self.openai_resend_callback:
            try:
                self.openai_resend_callback()
            except Exception:
                pass
        self.request_resend_sms(activation_id)

    def mark_send_succeeded(self, activation_id: str) -> None:
        try:
            self.set_status(activation_id, 1)
        except Exception:
            pass

    def mark_send_failed(self, activation_id: str, reason: str = "") -> None:
        reason_text = str(reason or "").lower()
        if any(keyword in reason_text for keyword in ("limit", "already", "too many", "exceeded", "maximum", "上限", "已达")):
            self._stop_reuse("phone limit reached")
        else:
            self._stop_reuse(reason or "phone rejected")

    def set_resend_callback(self, callback: Callable[[], None] | None) -> None:
        self.openai_resend_callback = callback

    def get_reuse_info(self) -> dict:
        with _HERO_SMS_CACHE_LOCK:
            cache = _HERO_SMS_CACHE or self._load_cache(self.default_service, self.default_country) or {}
            if not cache:
                return {"alive": False}
            remaining = max(0, int(HERO_SMS_PHONE_LIFETIME - (time.time() - float(cache.get("acquired_at") or 0))))
            return {
                "alive": remaining > 0 and not bool(cache.get("reuse_stopped")),
                "phone_number": cache.get("phone_number", ""),
                "use_count": int(cache.get("use_count") or 0),
                "remaining_seconds": remaining,
                "reuse_stopped": bool(cache.get("reuse_stopped")),
                "stop_reason": cache.get("stop_reason", ""),
            }


class SmsBowerProvider(HeroSmsProvider):
    """SMSBower's documented SMS-Activate-compatible activation API."""

    BASE_URL = "https://smsbower.page/stubs/handler_api.php"

    def __init__(
        self,
        api_key: str,
        *,
        min_price: float = -1,
        provider_ids: str | list[str] | tuple[str, ...] | None = None,
        **kwargs: Any,
    ):
        super().__init__(api_key, **kwargs)
        self.min_price = float(min_price or -1)
        self.provider_ids = self._normalize_provider_ids(provider_ids)

    @staticmethod
    def _normalize_provider_ids(value: str | list[str] | tuple[str, ...] | None) -> str:
        raw_items = value.replace("\n", ",").split(",") if isinstance(value, str) else list(value or [])
        items = [str(item).strip() for item in raw_items if str(item).strip()]
        return ",".join(dict.fromkeys(items))

    @classmethod
    def from_config(cls, config: dict):
        config = dict(config or {})
        api_key = str(config.get("smsbower_api_key") or "").strip()
        if not api_key:
            raise RuntimeError("SMSBower 未配置 API Key")
        return cls(
            api_key=api_key,
            default_service=str(config.get("sms_service") or config.get("smsbower_service") or config.get("smsbower_default_service") or HERO_SMS_DEFAULT_SERVICE),
            default_country=str(config.get("sms_country") or config.get("smsbower_country") or config.get("smsbower_default_country") or HERO_SMS_DEFAULT_COUNTRY),
            max_price=_safe_float(config.get("smsbower_max_price"), -1),
            min_price=_safe_float(config.get("smsbower_min_price"), -1),
            provider_ids=config.get("smsbower_provider_ids"),
            proxy=str(config.get("smsbower_proxy") or "").strip() or None,
            reuse_phone_to_max=_safe_bool(config.get("smsbower_reuse_phone_to_max"), False),
            phone_success_max=max(1, _safe_int(config.get("smsbower_phone_success_max"), 1)),
        )

    def _cache_identity(self, service: str, country: str) -> dict:
        return {**super()._cache_identity(service, country), "provider": "smsbower"}

    def _number_request_params(self, service: str, country: str) -> dict:
        if self.max_price <= 0:
            raise RuntimeError("SMSBower 必须配置 smsbower_max_price")
        params = {
            "service": service,
            "country": country,
            "maxPrice": self.max_price,
        }
        if self.min_price > 0:
            params["minPrice"] = self.min_price
        if self.provider_ids:
            params["providerIds"] = self.provider_ids
        if self.phone_exceptions:
            params["phoneException"] = ",".join(self.phone_exceptions[:20])
        return params

    def get_active_activations(self, start: int = 0, limit: int = 20) -> list:
        # SMSBower documents getStatus, not HeroSMS's active-activation endpoint.
        return []

    def get_status_v2(self, activation_id: str) -> dict:
        # SMSBower documents getStatus, not HeroSMS's getStatusV2 endpoint.
        return self.get_status(activation_id)

    def cancel_activation(self, activation_id: str) -> bool:
        try:
            return self.set_status(activation_id, 8).startswith("ACCESS_CANCEL")
        except Exception:
            return False

    def finish_activation(self, activation_id: str) -> bool:
        try:
            return self.set_status(activation_id, 6).startswith("ACCESS_ACTIVATION")
        except Exception:
            return False

    def _request(self, params: dict, *, needs_key: bool = True, timeout: int = 30) -> requests.Response:
        payload = dict(params)
        if needs_key or self.api_key:
            payload["api_key"] = self.api_key
        response = self.session.get(self.BASE_URL, params=payload, timeout=timeout, proxies=self.proxies)
        response.raise_for_status()
        return response

def is_herosms_phone_cache_alive(config: dict | None = None) -> tuple[bool, dict]:
    """Return whether the current HeroSMS cache is reusable for scheduling."""
    config = dict(config or {})
    api_key = str(config.get("herosms_api_key") or config.get("sms_api_key") or "").strip()
    if not api_key:
        return False, {"alive": False}
    provider = HeroSmsProvider(
        api_key,
        default_service=str(config.get("sms_service") or HERO_SMS_DEFAULT_SERVICE),
        default_country=str(config.get("sms_country") or config.get("herosms_country") or HERO_SMS_DEFAULT_COUNTRY),
        phone_success_max=max(0, _safe_int(config.get("register_phone_success_max"), 3)),
    )
    info = provider.get_reuse_info()
    return bool(info.get("alive")), info


# ---------------------------------------------------------------------------
# Factory and browser callback adapter
# ---------------------------------------------------------------------------

def create_sms_provider(provider_key: str, config: dict) -> BaseSmsProvider:
    """Create an SMS provider instance from config."""
    if provider_key in ("user_phone_url", "bind_user_phone_url", "phone_url", "manual_phone_url"):
        return UserProvidedSmsProvider.from_config(config)
    if provider_key in ("sms_activate", "sms_activate_api"):
        api_key = config.get("sms_activate_api_key", "")
        if not api_key:
            raise RuntimeError("SMS-Activate 未配置 API Key")
        return SmsActivateProvider(
            api_key=api_key,
            default_country=config.get("sms_activate_country", config.get("sms_activate_default_country", "")),
            proxy=_sms_proxy_from_config(config),
        )
    if provider_key in ("herosms", "herosms_api"):
        api_key = str(config.get("herosms_api_key") or config.get("sms_api_key") or "").strip()
        if not api_key:
            raise RuntimeError("HeroSMS 未配置 API Key")
        return HeroSmsProvider(
            api_key=api_key,
            default_service=str(config.get("sms_service") or config.get("herosms_service") or config.get("herosms_default_service") or HERO_SMS_DEFAULT_SERVICE),
            default_country=str(config.get("sms_country") or config.get("herosms_country") or config.get("herosms_default_country") or HERO_SMS_DEFAULT_COUNTRY),
            max_price=_safe_float(config.get("herosms_max_price"), -1),
            proxy=_sms_proxy_from_config(config),
            reuse_phone_to_max=_safe_bool(config.get("register_reuse_phone_to_max"), True),
            phone_success_max=max(0, _safe_int(config.get("register_phone_extra_max") or config.get("register_phone_success_max"), 3)),
        )
    if provider_key in ("smsbower", "smsbower_api"):
        return SmsBowerProvider.from_config(config)
    raise RuntimeError(f"未知的接码服务: {provider_key}")


class PhoneCallbackController:
    """Callable phone callback with optional lifecycle hooks for advanced providers."""

    def __init__(self, provider_key: str, config: dict, *, service: str, country: str = "", log_fn=None):
        self.provider_key = provider_key
        self.config = dict(config or {})
        self.service = service
        self.country = country
        self.log = log_fn or logger.info
        self.provider: Optional[BaseSmsProvider] = None
        self.activation: Optional[SmsActivation] = None
        self.phase = "need_number"
        self.completed = False
        self._verify_lock_acquired = False
        self.awaiting_external_success = False

    def _provider(self) -> BaseSmsProvider:
        if self.provider is None:
            self.provider = create_sms_provider(self.provider_key, self.config)
        return self.provider

    def __call__(self) -> str:
        provider = self._provider()
        if self.phase == "need_number":
            if self.provider_key == "herosms" and not self._verify_lock_acquired:
                _HERO_SMS_VERIFY_LOCK.acquire()
                self._verify_lock_acquired = True

            # 智能国家选择：如果启用了 auto_select_country，自动查询最优国家
            effective_country = self.country
            auto_select = _safe_bool(self.config.get("herosms_auto_country") or self.config.get("smsbower_auto_country"), False)
            if auto_select and isinstance(provider, HeroSmsProvider):
                self.log("正在查询最优国家（价格最低 + 库存充足）...")
                try:
                    min_stock = _safe_int(self.config.get("herosms_auto_country_min_stock") or self.config.get("smsbower_auto_country_min_stock"), 20)
                    max_price_limit = _safe_float(self.config.get("herosms_auto_country_max_price") or self.config.get("smsbower_auto_country_max_price"), 0)
                    best = provider.get_best_country(
                        service=self.service,
                        min_stock=min_stock,
                        max_price=max_price_limit,
                    )
                    if best:
                        self.log(f"自动选择最优国家: {best}")
                        effective_country = best
                    else:
                        self.log("未找到满足条件的国家，使用默认配置")
                except Exception as exc:
                    self.log(f"智能国家选择失败({exc})，使用默认配置")

            country_label = effective_country or self.config.get("sms_country") or self.config.get("sms_activate_country") or "default"
            self.log(f"已进入 add_phone，准备租用手机号: provider={self.provider_key} service={self.service} country={country_label}")
            self.log(f"正在从 {self.provider_key} 获取手机号...")
            try:
                self.activation = provider.get_number(service=self.service, country=effective_country)
            except Exception as first_exc:
                # 如果是自动选择的国家失败了，回退到默认国家重试
                fallback_country = self.country or self.config.get("sms_country") or self.config.get("herosms_country") or ""
                if auto_select and effective_country != fallback_country and fallback_country:
                    self.log(f"自动选择的国家({effective_country})获取号码失败，回退到默认国家({fallback_country})...")
                    try:
                        self.activation = provider.get_number(service=self.service, country=fallback_country)
                    except Exception:
                        if self._verify_lock_acquired:
                            _HERO_SMS_VERIFY_LOCK.release()
                            self._verify_lock_acquired = False
                        raise
                else:
                    if self._verify_lock_acquired:
                        _HERO_SMS_VERIFY_LOCK.release()
                        self._verify_lock_acquired = False
                    raise
            self.phase = "need_code"
            reused = bool((self.activation.metadata or {}).get("reused"))
            reuse_label = "复用号码" if reused else "新号码"
            self.log(f"已成功租到号码({reuse_label}): {self.activation.phone_number} (activation_id={self.activation.activation_id})")
            return self.activation.phone_number

        if self.phase == "need_code" and self.activation:
            self.log(f"等待短信验证码... (activation_id={self.activation.activation_id})")
            code = provider.get_code(self.activation.activation_id, timeout=180)
            if code:
                self.log(f"收到验证码: {code}")
                if getattr(provider, "auto_report_success_on_code", True):
                    self.report_success()
                else:
                    self.awaiting_external_success = True
            else:
                self.log(f"⚠️ 未收到验证码: activation_id={self.activation.activation_id}")
            return code
        return ""

    def set_resend_callback(self, callback: Callable[[], None] | None) -> None:
        if self.provider is not None:
            self.provider.set_resend_callback(callback)
        else:
            original_provider = self._provider()
            original_provider.set_resend_callback(callback)

    def mark_code_failed(self, reason: str = "") -> None:
        if self.activation and self.provider:
            hook = getattr(self.provider, "mark_code_failed", None)
            if callable(hook):
                hook(self.activation.activation_id, reason=reason)
            self.phase = "need_code"
            self.awaiting_external_success = False

    def mark_send_failed(self, reason: str = "") -> None:
        if self.activation and self.provider:
            hook = getattr(self.provider, "mark_send_failed", None)
            if callable(hook):
                hook(self.activation.activation_id, reason=reason)
            self.awaiting_external_success = False

    def mark_send_succeeded(self) -> None:
        if self.activation and self.provider:
            hook = getattr(self.provider, "mark_send_succeeded", None)
            if callable(hook):
                hook(self.activation.activation_id)

    def report_success(self) -> None:
        if self.activation and self.provider and not self.completed:
            self.provider.report_success(self.activation.activation_id)
            self.completed = True
            self.phase = "done"
            self.awaiting_external_success = False
            self.log(f"短信验证成功，已标记号码完成使用: activation_id={self.activation.activation_id}")
        if self._verify_lock_acquired:
            _HERO_SMS_VERIFY_LOCK.release()
            self._verify_lock_acquired = False

    def cleanup(self) -> None:
        if self.activation and not self.completed:
            try:
                provider = self._provider()
                provider.cancel(self.activation.activation_id)
                self.log(f"已释放未使用号码: activation_id={self.activation.activation_id}")
            except Exception:
                pass
        if self._verify_lock_acquired:
            _HERO_SMS_VERIFY_LOCK.release()
            self._verify_lock_acquired = False


def create_phone_callbacks(
    provider_key: str,
    config: dict,
    *,
    service: str,
    country: str = "",
    log_fn=None,
) -> tuple:
    """Create (phone_callback, cleanup) tuple for browser registration."""
    controller = PhoneCallbackController(
        provider_key,
        config,
        service=service,
        country=country,
        log_fn=log_fn,
    )
    return controller, controller.cleanup
