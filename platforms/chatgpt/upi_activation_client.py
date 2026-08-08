"""UPI Activation customer API client (actk_ keys).

Docs: https://upi.akkkkk.top/api/v1/customer/docs

- Auth: X-Activation-Client-Key: actk_...
- v1 response envelope: {ok, requestId, data, error?, meta?}
- Stable task state: queued/processing/waiting_payment/succeeded/failed/cancelled/expired
- Channels: upi | pix | ideal | kakao
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

import requests

DEFAULT_BASE_URL = "https://upi.akkkkk.top"
# v1 docs list upi/pix/ideal/kakao; UPI self-serve usually uses "upi".
DEFAULT_CHANNEL = "upi"
SUPPORTED_CHANNELS = frozenset({"upi", "pix", "ideal", "kakao"})
IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{7,127}$")


class UpiActivationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 0,
        retry_after: float = 0,
        payload: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.status_code = int(status_code or 0)
        self.retry_after = float(retry_after or 0)
        self.payload = dict(payload or {})


@dataclass
class UpiTask:
    id: str = ""
    status: str = ""
    channel: str = ""
    can_release: bool = False
    cdk_consumed: int = 0
    display_action: str = ""
    display_description: str = ""
    reason: str = ""
    idempotent: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def done(self) -> bool:
        status = str(self.status or "").strip().lower()
        if status in {"success", "succeeded"}:
            return self.success
        return status in {
            "failed",
            "cancelled",
            "canceled",
            "expired",
            "released",
            "replace_account",
            "verified",
        }

    @property
    def success(self) -> bool:
        # v1 exposes stable ``state=succeeded``; old compatibility responses used
        # ``status=success``.  Treat both as authoritative terminal success.
        return str(self.status or "").strip().lower() in {"success", "succeeded"}
    @property
    def replace_account(self) -> bool:
        action = str(self.display_action or "").lower()
        if action == "replace_account":
            return True
        return "replace_account" in str(self.reason or "").lower()


def normalize_channel(channel: str | None) -> str:
    value = str(channel or DEFAULT_CHANNEL).strip().lower()
    if value not in SUPPORTED_CHANNELS:
        return DEFAULT_CHANNEL
    return value


def normalize_idempotency_key(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        raise ValueError("Idempotency-Key 不能为空")
    # Map unsafe chars to '-' while keeping URL-safe charset.
    cleaned = re.sub(r"[^A-Za-z0-9._~-]+", "-", text)
    cleaned = cleaned.strip("-._~")
    if not cleaned:
        cleaned = "order-default"
    if not cleaned[0].isalnum():
        cleaned = f"k{cleaned}"
    if len(cleaned) < 8:
        cleaned = (cleaned + "xxxxxxxx")[:8]
    if len(cleaned) > 128:
        cleaned = cleaned[:128]
    if not IDEMPOTENCY_RE.match(cleaned):
        raise ValueError(f"非法 Idempotency-Key: {cleaned}")
    return cleaned


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "y", "是"}


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return int(default)


def _detail_message(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            msg = error.get("message") or error.get("code") or error.get("type")
            if msg:
                return str(msg)
        detail = payload.get("detail")
        if isinstance(detail, dict):
            msg = detail.get("message") or detail.get("error") or detail.get("msg") or detail.get("code")
            if msg:
                return str(msg)
        if payload.get("message"):
            return str(payload.get("message"))
        if payload.get("error"):
            return str(payload.get("error"))
    return fallback


def _network_error_message(exc: BaseException) -> str:
    """Collapse requests/urllib3 connection noise into a short Chinese hint."""
    text = " ".join(str(part) for part in (exc, getattr(exc, "args", ())) if part)
    lower = text.lower()
    if "10054" in text or "connection reset" in lower or "强迫关闭" in text or "强制关闭" in text:
        return "网络连接被远端重置，稍后将用原幂等键自动重试"
    if "timed out" in lower or "timeout" in lower:
        return "网络请求超时，稍后将用原幂等键自动重试"
    if "name or service not known" in lower or "getaddrinfo" in lower or "nodename nor servname" in lower:
        return "DNS/域名解析失败，请检查 upi_base_url 与网络"
    if "connection aborted" in lower or "connection refused" in lower or "failed to establish" in lower:
        return "网络连接中断，稍后将用原幂等键自动重试"
    if "proxy" in lower:
        return "代理连接失败，请检查本机代理设置"
    cleaned = re.sub(r"\s+", " ", text).strip()
    if len(cleaned) > 160:
        cleaned = cleaned[:157] + "…"
    return f"UPI 网络异常: {cleaned}" if cleaned else "UPI 网络异常"


def _parse_retry_after(resp: requests.Response, payload: dict[str, Any] | None = None) -> float:
    raw = resp.headers.get("Retry-After") or resp.headers.get("retry-after") or ""
    text = str(raw or "").strip()
    if text:
        try:
            return max(0.0, float(text))
        except Exception:
            pass
    data = payload if isinstance(payload, dict) else {}
    if not data:
        try:
            body = resp.json()
            if isinstance(body, dict):
                data = body
        except Exception:
            data = {}
    detail = data.get("error") if isinstance(data.get("error"), dict) else data.get("detail") if isinstance(data.get("detail"), dict) else data
    if isinstance(detail, dict):
        for key in ("retryAfterSeconds", "retry_after_seconds", "retry_after", "retryAfter"):
            if key in detail:
                try:
                    return max(0.0, float(detail.get(key) or 0))
                except Exception:
                    continue
    return 0.0


class UpiActivationClient:
    """Customer-side UPI activation client."""

    def __init__(
        self,
        api_key: str = "",
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30,
        device_id: str = "gpt-register",
    ):
        key = str(api_key or "").strip()
        if key and not key.startswith("actk_"):
            raise ValueError("UPI client key 必须以 actk_ 开头（CDK 请先签发 key）")
        self.api_key = key
        self.base_url = str(base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = float(timeout or 30)
        self.device_id = str(device_id or "gpt-register").strip() or "gpt-register"
        self.session = requests.Session()
        self.session.trust_env = False
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["X-Activation-Client-Key"] = self.api_key
        self.session.headers.update(headers)

    def _require_key(self) -> None:
        if not self.api_key:
            raise ValueError("UPI client key 不能为空")

    def _url(self, path: str) -> str:
        return urljoin(self.base_url + "/", path.lstrip("/"))

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        expected: set[int] | None = None,
    ) -> dict[str, Any]:
        expected = expected or {200, 201}
        try:
            resp = self.session.request(
                method.upper(),
                self._url(path),
                json=json_body,
                headers=headers or None,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise UpiActivationError(_network_error_message(exc), status_code=0) from exc

        payload: dict[str, Any] = {}
        try:
            data = resp.json()
            if isinstance(data, dict):
                payload = data
        except Exception:
            payload = {}

        if resp.status_code == 429:
            raise UpiActivationError(
                _detail_message(payload, "UPI 限流"),
                status_code=429,
                retry_after=_parse_retry_after(resp, payload) or 12.0,
                payload=payload,
            )
        if resp.status_code not in expected:
            raise UpiActivationError(
                _detail_message(payload, f"UPI HTTP {resp.status_code}"),
                status_code=resp.status_code,
                retry_after=_parse_retry_after(resp, payload),
                payload=payload,
            )
        if payload.get("ok") is False:
            raise UpiActivationError(
                _detail_message(payload, "UPI 业务失败"),
                status_code=resp.status_code,
                payload=payload,
            )
        return payload

    @staticmethod
    def _task_from_payload(payload: dict[str, Any], *, fallback_id: str = "") -> UpiTask:
        data: dict[str, Any] = payload
        for key in ("data", "task"):
            nested = data.get(key) if isinstance(data, dict) else None
            if isinstance(nested, dict):
                data = nested
        nested_task = data.get("task") if isinstance(data.get("task"), dict) else None
        if nested_task is not None:
            data = nested_task
        display = data.get("display") if isinstance(data.get("display"), dict) else {}
        return UpiTask(
            id=str(data.get("id") or data.get("task_id") or data.get("taskId") or fallback_id or ""),
            status=str(data.get("state") or data.get("status") or ""),
            channel=str(data.get("channel") or ""),
            can_release=_as_bool(data.get("canRelease") if "canRelease" in data else data.get("can_release")),
            cdk_consumed=_as_int(data.get("cdkConsumed") if "cdkConsumed" in data else data.get("cdk_consumed")) or (1 if _as_bool(data.get("charged")) else 0),
            display_action=str(display.get("action") or data.get("display_action") or ""),
            display_description=str(
                display.get("description")
                or data.get("display_description")
                or data.get("reason")
                or ""
            ),
            reason=str(data.get("reason") or display.get("description") or ""),
            idempotent=_as_bool(payload.get("idempotent") if isinstance(payload, dict) else False) or _as_bool(data.get("idempotent")),
            raw=dict(data),
        )

    def create_key(self, cdk: str, *, note: str = "gpt-register", rotate: bool = False) -> dict[str, Any]:
        """Issue actk_ from CDK. Does not use client key auth."""
        body = {
            "cdk": str(cdk or "").strip(),
            "note": str(note or "gpt-register"),
            "rotate": bool(rotate),
        }
        # One-shot key creation uses no actk header.
        session = requests.Session()
        session.trust_env = False
        try:
            resp = session.post(
                self._url("/api/v1/customer/activation/keys"),
                json=body,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise UpiActivationError(f"签发 UPI key 失败: {exc}") from exc
        try:
            payload = resp.json() if resp.content else {}
        except Exception:
            payload = {}
        if resp.status_code not in {200, 201}:
            raise UpiActivationError(
                _detail_message(payload, f"签发 key HTTP {resp.status_code}"),
                status_code=resp.status_code,
                payload=payload if isinstance(payload, dict) else {},
            )
        if not isinstance(payload, dict):
            raise UpiActivationError("签发 key 响应无效")
        return payload

    def submit_task(
        self,
        access_token: str,
        *,
        channel: str = DEFAULT_CHANNEL,
        idempotency_key: str,
        device_id: str | None = None,
    ) -> UpiTask:
        self._require_key()
        token = str(access_token or "").strip()
        if not token:
            raise ValueError("accessToken 不能为空")
        body = {
            "accessToken": token,
            "channel": normalize_channel(channel),
            "deviceId": str(device_id or self.device_id),
        }
        headers = {"Idempotency-Key": normalize_idempotency_key(idempotency_key)}
        payload = self._request(
            "POST",
            "/api/v1/customer/activation/tasks",
            json_body=body,
            headers=headers,
            expected={200, 201},
        )
        task = self._task_from_payload(payload)
        if not task.id:
            raise UpiActivationError(
                "UPI 提交响应缺少 task id（协议错误）",
                status_code=502,
                payload={"protocol_error": "missing_task_id"},
            )
        return task

    def get_task(self, task_id: str) -> UpiTask:
        self._require_key()
        tid = str(task_id or "").strip()
        if not tid:
            raise ValueError("task_id 不能为空")
        payload = self._request("GET", f"/api/v1/customer/activation/tasks/{tid}", expected={200})
        return self._task_from_payload(payload, fallback_id=tid)

    def get_task_by_idempotency(self, idempotency_key: str) -> UpiTask:
        """Recover a previously submitted task by the original Idempotency-Key.

        Used when POST timed out / returned 429/503 and the client cannot tell
        whether the remote task was created. Official docs:
        GET /api/v1/customer/activation/tasks/idempotency/{idempotency_key}
        """
        self._require_key()
        key = normalize_idempotency_key(idempotency_key)
        payload = self._request(
            "GET",
            f"/api/v1/customer/activation/tasks/idempotency/{key}",
            expected={200},
        )
        task = self._task_from_payload(payload)
        if not task.id:
            raise UpiActivationError(
                "幂等查询响应缺少 task id",
                status_code=404,
                payload=payload if isinstance(payload, dict) else {},
            )
        return task

    def get_tasks(self, task_ids: list[str]) -> dict[str, UpiTask]:
        self._require_key()
        ids: list[str] = []
        seen: set[str] = set()
        for raw in task_ids:
            tid = str(raw or "").strip()
            if tid and tid not in seen:
                seen.add(tid)
                ids.append(tid)
        if not ids:
            return {}
        payload = self._request(
            "POST",
            "/api/v1/customer/activation/tasks/batch-get",
            json_body={"ids": ids},
            expected={200},
        )
        data = payload.get("data") if isinstance(payload.get("data"), (list, dict)) else payload.get("tasks")
        if isinstance(data, dict):
            rows = data.get("items") if isinstance(data.get("items"), list) else data.get("tasks") if isinstance(data.get("tasks"), list) else list(data.values())
        else:
            rows = data if isinstance(data, list) else []
        tasks: dict[str, UpiTask] = {}
        for item in rows:
            if not isinstance(item, dict):
                continue
            task = self._task_from_payload(item)
            if task.id:
                tasks[task.id] = task
        return tasks

    def get_tasks_by_idempotency(self, idempotency_keys: list[str]) -> dict[str, UpiTask]:
        self._require_key()
        keys: list[str] = []
        seen: set[str] = set()
        for raw in idempotency_keys:
            key = normalize_idempotency_key(raw)
            if key and key not in seen:
                seen.add(key)
                keys.append(key)
        if not keys:
            return {}
        payload = self._request(
            "POST",
            "/api/v1/customer/activation/tasks/idempotency/batch-get",
            json_body={"keys": keys},
            expected={200},
        )
        data = payload.get("data") if isinstance(payload.get("data"), (list, dict)) else payload.get("tasks")
        if isinstance(data, dict):
            rows = data.get("items") if isinstance(data.get("items"), list) else data.get("tasks") if isinstance(data.get("tasks"), list) else list(data.values())
        else:
            rows = data if isinstance(data, list) else []
        tasks: dict[str, UpiTask] = {}
        for item in rows:
            if not isinstance(item, dict):
                continue
            idem = str(item.get("idempotencyKey") or item.get("idempotency_key") or item.get("key") or "").strip()
            task = self._task_from_payload(item)
            if idem and task.id:
                tasks[idem] = task
        return tasks

    def release_task(self, task_id: str) -> dict[str, Any]:
        self._require_key()
        tid = str(task_id or "").strip()
        if not tid:
            raise ValueError("task_id 不能为空")
        return self._request(
            "POST",
            f"/api/v1/customer/activation/tasks/{tid}/release",
            json_body={},
            expected={200, 201},
        )


class TokenBucket:
    """Simple per-process token bucket for UPI submit rate limits."""

    def __init__(self, rate_per_minute: float = 5.0, capacity: float | None = None):
        self.rate_per_second = max(0.01, float(rate_per_minute) / 60.0)
        self.capacity = float(capacity if capacity is not None else max(1.0, rate_per_minute))
        self.tokens = self.capacity
        self.updated_at = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = max(0.0, now - self.updated_at)
        self.updated_at = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate_per_second)

    def try_acquire(self, amount: float = 1.0) -> float:
        """Return 0 if acquired, else seconds to wait."""
        self._refill()
        need = float(amount or 1.0)
        if self.tokens >= need:
            self.tokens -= need
            return 0.0
        missing = need - self.tokens
        return missing / self.rate_per_second

    def refund(self, amount: float = 1.0) -> None:
        """Return tokens that were reserved but never used for a real POST."""
        self._refill()
        self.tokens = min(self.capacity, self.tokens + max(0.0, float(amount or 0.0)))
