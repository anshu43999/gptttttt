"""Persistent UPI Plus activation state machine.

The queue is deliberately backed by the accounts table rather than in-memory
work items.  A process restart therefore resumes submit, poll, and verifying
work without duplicating remote tasks.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from application.accounts_service import AccountsService, AccountOperationBusy, account_operation_lock
from application.config_service import ConfigService
from infrastructure import db
from platforms.chatgpt.upi_activation_client import (
    DEFAULT_BASE_URL,
    DEFAULT_CHANNEL,
    TokenBucket,
    UpiActivationClient,
    UpiActivationError,
    UpiTask,
    normalize_channel,
    normalize_idempotency_key,
)

# ``submitting`` is durable: the POST may still be in flight after restart.
# ``verifying`` is active because the local Plus check may still be running.
# ``submit_unknown`` is active: POST may have created a remote task but the
# client never received a task id (timeout/429/503). Recover via idempotency.
ACTIVE_STATUSES = frozenset({"queued", "submitting", "submit_unknown", "submitted", "processing", "verifying"})
TERMINAL_STATUSES = frozenset({"success", "failed", "replace_account", "released", "verified", "cancelled"})
DANGEROUS_ENQUEUE_STATUSES = frozenset({"active", "success", "verified", "replace_account", "verified_plus"})
REQUEUEABLE_STATUSES = frozenset({"", "failed", "cancelled", "released", "submit_rejected"})
KEY_PROBE_STATUS_CODES = frozenset({401, 404})
# Transient submit outcomes that must NOT be counted as business failures.
# 409 is included so we can recover an existing task for the same Idempotency-Key.
RETRYABLE_STATUS_CODES = frozenset({0, 408, 409, 425, 429, 500, 502, 503, 504})
SUBMIT_UNKNOWN_MESSAGE = (
    "提交结果待确认：远端可能已创建任务。不会按业务失败统计；"
    "将用同一 Idempotency-Key 自动找回任务号。"
)
# In-flight POSTs per actk_ key. Old value 3 capped real submit at ~10/min when
# each POST took 15–20s (3 * 60/18 ≈ 10). v1 exposes rate-limit headers and may
# allow higher per-key ceilings; TokenBucket still honors the operator setting.
MAX_SUBMISSIONS_PER_KEY = 16
MAX_CONFIGURED_SUBMIT_RATE_PER_KEY = 120


_TRUE_VALUES = {"1", "true", "yes", "on", "y", "是", "启用", "enabled"}
_FALSE_VALUES = {"0", "false", "no", "off", "n", "否", "禁用", "disabled", ""}
_SECRET_PATTERN = re.compile(r"(?i)(actk_[A-Za-z0-9._~-]+|accessToken\s*[:=]\s*[^,;\s]+|cdk\s*[:=]\s*[^,;\s]+)")


def parse_bool(value: Any, *, default: bool = False) -> bool:
    """Parse config booleans explicitly; never rely on Python truthiness."""
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    return bool(default)


def _safe_message(value: Any, fallback: str = "", *, secrets: tuple[str, ...] = ()) -> str:
    text = str(value or fallback or "").strip()
    for secret in secrets:
        if secret:
            text = text.replace(str(secret), "[已隐藏]")
    if not text:
        return str(fallback or "")
    # Collapse common Windows/Python connection dumps before they hit the UI.
    lower = text.lower()
    if "10054" in text or "connection reset" in lower or "强迫关闭" in text or "强制关闭" in text:
        text = "网络连接被远端重置，稍后将用原幂等键自动重试"
    elif "connection aborted" in lower and ("reset" in lower or "10054" in text or "关闭" in text):
        text = "网络连接被远端重置，稍后将用原幂等键自动重试"
    elif "timed out" in lower or "timeout" in lower:
        text = "网络请求超时，稍后将用原幂等键自动重试"
    elif "connection aborted" in lower or "connection refused" in lower:
        text = "网络连接中断，稍后将用原幂等键自动重试"
    text = re.sub(r"\s+", " ", text).strip()
    return _SECRET_PATTERN.sub("[已隐藏]", text)[:220]


def _key_hash(key: str) -> str:
    return hashlib.sha256(str(key or "").encode("utf-8")).hexdigest()


def _int_value(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _float_value(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


class UpiActivationService:
    def __init__(self, accounts: AccountsService | None = None, config_service: ConfigService | None = None):
        self.accounts = accounts or AccountsService()
        self.config_service = config_service or ConfigService()
        self._lock = threading.RLock()
        self._submit_thread: threading.Thread | None = None
        self._poll_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._buckets: dict[str, TokenBucket] = {}
        self._key_rr = 0
        self._started = False
        self._worker_error = ""
        self._submit_slots: dict[str, threading.BoundedSemaphore] = {}
        self._local_submission_claims: set[str] = set()
        self._submission_workers: set[threading.Thread] = set()
        # Process-local 429 holds: one UPI service process owns dispatch.
        self._key_retry_after_until: dict[str, float] = {}

    # ------------------------------------------------------------------
    # config and lifecycle
    # ------------------------------------------------------------------

    def _cfg(self) -> dict[str, Any]:
        try:
            value = self.config_service.merged_config()
            return dict(value) if isinstance(value, dict) else {}
        except Exception as exc:
            self._worker_error = _safe_message(exc, "读取 UPI 配置失败")
            return {}

    def _client_keys(self, cfg: dict[str, Any] | None = None) -> list[str]:
        cfg = cfg if cfg is not None else self._cfg()
        raw_values: list[Any] = []
        raw_list = cfg.get("upi_client_keys")
        if isinstance(raw_list, (list, tuple, set)):
            raw_values.extend(raw_list)
        elif isinstance(raw_list, str):
            raw_values.extend(re.split(r"[\r\n,;]+", raw_list))
        raw_values.append(cfg.get("upi_client_key"))
        result: list[str] = []
        seen: set[str] = set()
        for raw in raw_values:
            key = str(raw or "").strip()
            if not key.startswith("actk_") or key in seen:
                continue
            seen.add(key)
            result.append(key)
        return result

    def _runtime_config(self) -> dict[str, Any]:
        cfg = self._cfg()
        status = self.config_status(cfg, include_secrets=True)
        status["base_url"] = str(cfg.get("upi_base_url") or DEFAULT_BASE_URL).rstrip("/")
        status["device_id"] = str(cfg.get("upi_device_id") or "gpt-register").strip() or "gpt-register"
        status["keys"] = self._client_keys(cfg)
        return status

    def config_status(self, cfg: dict[str, Any] | None = None, *, include_secrets: bool = False) -> dict[str, Any]:
        cfg = cfg if cfg is not None else self._cfg()
        keys = self._client_keys(cfg)
        submit_rate = max(1, min(MAX_CONFIGURED_SUBMIT_RATE_PER_KEY, _int_value(cfg.get("upi_submit_per_key_per_min", 50), 50)))
        poll_interval = max(1, min(300, _int_value(cfg.get("upi_poll_interval_sec", 5), 5)))
        poll_timeout = max(1, _int_value(cfg.get("upi_poll_timeout_sec", 1800), 1800))
        status = {
            "enabled": parse_bool(cfg.get("upi_activation_enabled"), default=True),
            "has_key": bool(keys),
            "key_count": len(keys),
            "key_prefixes": [f"{key[:8]}…" for key in keys],
            "submit_per_key_per_min": submit_rate,
            "max_submissions_per_key": MAX_SUBMISSIONS_PER_KEY,
            "poll_interval_sec": poll_interval,
            "poll_timeout_sec": poll_timeout,
            "auto_verify_plus": parse_bool(cfg.get("upi_auto_verify_plus"), default=True),
        }
        if include_secrets:
            status["client_keys"] = list(keys)
        return status

    def ensure_worker(self) -> None:
        with self._lock:
            if self._started and any(thread and thread.is_alive() for thread in (self._submit_thread, self._poll_thread)):
                return
            self._stop.clear()
            self._wake.clear()
            self._submit_thread = threading.Thread(target=self._submit_loop, name="upi-submit", daemon=True)
            self._poll_thread = threading.Thread(target=self._poll_loop, name="upi-poll", daemon=True)
            self._submit_thread.start()
            self._poll_thread.start()
            self._started = True

    def wake(self) -> None:
        self.ensure_worker()
        self._wake.set()

    def shutdown(self, timeout: float = 5.0) -> None:
        with self._lock:
            self._stop.set()
            self._wake.set()
            threads = [self._submit_thread, self._poll_thread, *self._submission_workers]
        deadline = time.monotonic() + max(0.0, float(timeout or 0))
        for thread in threads:
            if thread and thread.is_alive():
                remaining = max(0.0, deadline - time.monotonic())
                thread.join(remaining)
        with self._lock:
            self._submit_thread = None
            self._poll_thread = None
            self._started = False

    stop = shutdown

    # ------------------------------------------------------------------
    # persistence helpers
    # ------------------------------------------------------------------

    def _now(self) -> str:
        return self.accounts._now()

    @staticmethod
    def _public_account(account: dict[str, Any]) -> dict[str, Any]:
        # Activation responses must never expose credentials, raw tokens, or
        # the association hash used to select a client key.
        safe = dict(account or {})
        for field in (
            "tokens",
            "raw_tokens",
            "access_token",
            "refresh_token",
            "id_token",
            "chatgpt_access_token_initial",
            "token_expires_at",
            "password",
            "cpa_auth_file_json",
            "oauth_result",
            "activation_client_key_hash",
        ):
            safe.pop(field, None)
        return safe
    def _load_account(self, key: str) -> dict[str, Any]:
        account = self.accounts.get_account(str(key or "").strip())
        return dict(account) if isinstance(account, dict) else {}

    def _activation_snapshot(self, key: str) -> dict[str, Any]:
        return self.accounts.repo.activation_snapshot(str(key or "").strip())

    def _persist_claimed_submission(
        self,
        account: dict[str, Any],
        claim_id: str,
        *,
        event: str = "",
        message: str = "",
        clear_claim: bool = True,
    ) -> bool:
        return self.accounts.repo.persist_claimed_activation_submission(
            str(account.get("account_key") or ""),
            claim_id,
            account,
            event=event,
            message=_safe_message(message or event),
            clear_claim=clear_claim,
        )


    def _save_account(self, account: dict[str, Any], *, event: str = "", message: str = "") -> dict[str, Any]:
        saved = self.accounts.repo.upsert(account).to_dict()
        if event:
            try:
                db.add_account_event(
                    str(saved.get("account_key") or account.get("account_key") or ""),
                    event,
                    status=str(account.get("activation_status") or ""),
                    message=_safe_message(message or event),
                    payload={
                        "provider": account.get("activation_provider") or "upi",
                        "channel": account.get("activation_channel") or "",
                        "task_id": account.get("activation_task_id") or "",
                    },
                    path=getattr(self.accounts.repo, "db_path", None),
                )
            except Exception as exc:
                self._worker_error = _safe_message(exc, "记录 UPI 事件失败")
        return saved

    def _mark_retry(self, account: dict[str, Any], message: str, *, display: bool = True) -> None:
        safe = _safe_message(message, "UPI 请求暂时失败，将重试")
        account["activation_error"] = safe
        if display:
            account["activation_display"] = safe
        account["activation_updated_at"] = self._now()

    def _list_by_status(self, statuses: set[str], *, limit: int = 100) -> list[dict[str, Any]]:
        if not statuses:
            return []
        ordered = sorted(str(status) for status in statuses)
        placeholders = ",".join("?" for _ in ordered)
        try:
            with db.connect(getattr(self.accounts.repo, "db_path", None)) as conn:
                rows = conn.execute(
                    f"""
                    SELECT account_key FROM accounts
                    WHERE COALESCE(activation_provider,'') IN ('','upi')
                      AND COALESCE(activation_status,'') IN ({placeholders})
                    ORDER BY COALESCE(NULLIF(activation_updated_at,''), updated_at, created_at) ASC, id ASC
                    LIMIT ?
                    """,
                    (*ordered, int(limit)),
                ).fetchall()
        except Exception as exc:
            self._worker_error = _safe_message(exc, "读取 UPI 队列失败")
            return []
        result: list[dict[str, Any]] = []
        for row in rows:
            key = str(row["account_key"] or "")
            if not key:
                continue
            try:
                result.append(self._load_account(key))
            except Exception as exc:
                self._worker_error = _safe_message(exc, "读取账号失败")
        return result

    # ------------------------------------------------------------------
    # enqueue / stats
    # ------------------------------------------------------------------

    def enqueue_accounts(
        self,
        keys: list[str],
        *,
        channel: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        status = self._runtime_config()
        clean_keys = [str(raw or "").strip() for raw in (keys or []) if str(raw or "").strip()]
        if not status["enabled"]:
            return {
                "ok": False,
                "message": "UPI Plus 激活已禁用（upi_activation_enabled=false）",
                "queued": 0,
                "skipped": 0,
                "failed": len(clean_keys),
                "results": [],
                "config": self.config_status(),
            }
        if not status["has_key"]:
            return {
                "ok": False,
                "message": "未配置 UPI client key（设置页填写 upi_client_key / actk_…）",
                "queued": 0,
                "skipped": 0,
                "failed": len(clean_keys),
                "results": [],
                "config": self.config_status(),
            }

        channel_norm = normalize_channel(channel or self._cfg().get("upi_default_channel") or DEFAULT_CHANNEL)
        results: list[dict[str, Any]] = []
        queued = skipped = failed = 0
        now = self._now()
        for key in clean_keys:
            try:
                with account_operation_lock(key, blocking=False):
                    account = self._load_account(key)
                    if not account.get("account_key"):
                        failed += 1
                        results.append({"key": key, "ok": False, "message": "未找到账号"})
                        continue
                    activation_status = str(account.get("activation_status") or "").strip().lower()
                    plus_status = str(account.get("plus_status") or "").strip().lower()
                    # Force is intentionally ignored for all dangerous/remote states.
                    if activation_status in DANGEROUS_ENQUEUE_STATUSES or plus_status == "verified_plus":
                        failed += 1
                        results.append({
                            "key": key,
                            "ok": False,
                            "skipped": True,
                            "message": f"当前状态 {activation_status or plus_status} 禁止重复激活（force 不覆盖远端/终态）",
                            "activation_status": activation_status,
                        })
                        continue
                    if activation_status in ACTIVE_STATUSES:
                        skipped += 1
                        results.append({
                            "key": key,
                            "ok": True,
                            "skipped": True,
                            "message": f"已在激活中: {activation_status}",
                            "activation_status": activation_status,
                        })
                        continue
                    if activation_status not in REQUEUEABLE_STATUSES:
                        failed += 1
                        results.append({"key": key, "ok": False, "message": f"状态 {activation_status} 不允许重新排队"})
                        continue

                    token = self.accounts._account_access_token(account)
                    if not token:
                        failed += 1
                        results.append({"key": key, "ok": False, "message": "缺少 access_token"})
                        continue
                    attempt = _int_value(account.get("activation_attempt"), 0) + 1
                    idem = normalize_idempotency_key(f"upi-{account.get('account_key')}-a{attempt}")
                    account.update(
                        {
                            "activation_provider": "upi",
                            "activation_status": "queued",
                            "activation_channel": channel_norm,
                            "activation_task_id": "",
                            "activation_client_key_hash": "",
                            "activation_idempotency_key": idem,
                            "activation_attempt": attempt,
                            "activation_error": "",
                            "activation_display": "",
                            "activation_can_release": 0,
                            "activation_cdk_consumed": 0,
                            "activation_submitted_at": "",
                            "activation_finished_at": "",
                            "activation_updated_at": now,
                        }
                    )
                    saved = self._save_account(account, event="activation_queued", message=f"UPI 激活已排队 channel={channel_norm}")
                    queued += 1
                    results.append(
                        {
                            "key": key,
                            "ok": True,
                            "activation_status": "queued",
                            "activation_channel": channel_norm,
                            "account": {
                                "key": saved.get("account_key"),
                                "activation_status": saved.get("activation_status"),
                                "activation_channel": saved.get("activation_channel"),
                                "activation_task_id": saved.get("activation_task_id"),
                                "activation_error": saved.get("activation_error"),
                                "activation_updated_at": saved.get("activation_updated_at"),
                            },
                        }
                    )
            except AccountOperationBusy:
                failed += 1
                results.append({"key": key, "ok": False, "message": "账号正在执行其他操作，请稍后重试"})
            except Exception as exc:
                failed += 1
                results.append({"key": key, "ok": False, "message": _safe_message(exc, "排队失败")})
        if queued:
            self.wake()
        return {
            "ok": failed == 0,
            "queued": queued,
            "skipped": skipped,
            "failed": failed,
            "channel": channel_norm,
            "message": f"已排队 {queued}，跳过 {skipped}，失败 {failed}（submit ≤{status.get('submit_per_key_per_min')}/min/key）",
            "config": self.config_status(),
            "results": results,
        }

    def _enqueue_accounts_fast(
        self,
        keys: list[str],
        *,
        channel: str,
        force: bool = False,
    ) -> dict[str, Any]:
        """Durably mark a bulk activation batch queued before the HTTP response.

        The old async path returned immediately and then wrote account rows one by
        one in a daemon thread. Large batches looked accepted but were invisible
        for minutes if the process was busy. One SELECT + batched UPDATE makes the
        UI truth match the API response; remote submit still happens asynchronously
        through the worker and the documented Idempotency-Key state machine.
        """
        status = self._runtime_config()
        clean_keys = [str(raw or "").strip() for raw in (keys or []) if str(raw or "").strip()]
        channel_norm = normalize_channel(channel or self._cfg().get("upi_default_channel") or DEFAULT_CHANNEL)
        if not clean_keys:
            return {
                "ok": False,
                "async": False,
                "message": "请至少选择一个账号",
                "accepted": 0,
                "queued": 0,
                "skipped": 0,
                "failed": 0,
                "results": [],
                "config": self.config_status(),
            }

        placeholders = ",".join("?" for _ in clean_keys)
        now = self._now()
        rows_by_key: dict[str, dict[str, Any]] = {}
        try:
            with db.connect(getattr(self.accounts.repo, "db_path", None)) as conn:
                rows = conn.execute(
                    f"""
                    SELECT a.account_key, a.plus_status, a.activation_status,
                           a.activation_attempt, c.access_token, c.chatgpt_access_token_initial
                    FROM accounts a
                    LEFT JOIN account_credentials c ON c.account_id_ref=a.id
                    WHERE a.account_key IN ({placeholders})
                    """,
                    tuple(clean_keys),
                ).fetchall()
                rows_by_key = {str(row["account_key"] or ""): dict(row) for row in rows}

                results: list[dict[str, Any]] = []
                updates: list[tuple[Any, ...]] = []
                events: list[tuple[Any, ...]] = []
                queued = skipped = failed = 0
                for key in clean_keys:
                    row = rows_by_key.get(key)
                    if not row:
                        failed += 1
                        results.append({"key": key, "ok": False, "message": "未找到账号"})
                        continue
                    activation_status = str(row.get("activation_status") or "").strip().lower()
                    plus_status = str(row.get("plus_status") or "").strip().lower()
                    if activation_status in DANGEROUS_ENQUEUE_STATUSES or plus_status == "verified_plus":
                        failed += 1
                        results.append({
                            "key": key,
                            "ok": False,
                            "skipped": True,
                            "message": f"当前状态 {activation_status or plus_status} 禁止重复激活（force 不覆盖远端/终态）",
                            "activation_status": activation_status,
                        })
                        continue
                    if activation_status in ACTIVE_STATUSES:
                        skipped += 1
                        results.append({
                            "key": key,
                            "ok": True,
                            "skipped": True,
                            "message": f"已在激活中: {activation_status}",
                            "activation_status": activation_status,
                        })
                        continue
                    if activation_status not in REQUEUEABLE_STATUSES:
                        failed += 1
                        results.append({"key": key, "ok": False, "message": f"状态 {activation_status} 不允许重新排队"})
                        continue
                    token = str(row.get("access_token") or row.get("chatgpt_access_token_initial") or "").strip()
                    if not token:
                        failed += 1
                        results.append({"key": key, "ok": False, "message": "缺少 access_token"})
                        continue
                    attempt = _int_value(row.get("activation_attempt"), 0) + 1
                    idem = normalize_idempotency_key(f"upi-{key}-a{attempt}")
                    updates.append((
                        "upi",
                        "queued",
                        channel_norm,
                        "",
                        "",
                        idem,
                        attempt,
                        "",
                        "",
                        0,
                        0,
                        "",
                        "",
                        now,
                        now,
                        key,
                    ))
                    events.append((key, "activation_queued", "queued", f"UPI 激活已排队 channel={channel_norm}", now))
                    queued += 1
                    results.append({
                        "key": key,
                        "ok": True,
                        "activation_status": "queued",
                        "activation_channel": channel_norm,
                        "account": {
                            "key": key,
                            "activation_status": "queued",
                            "activation_channel": channel_norm,
                            "activation_task_id": "",
                            "activation_error": "",
                            "activation_updated_at": now,
                        },
                    })
                if updates:
                    conn.executemany(
                        """
                        UPDATE accounts
                        SET activation_provider=?,
                            activation_status=?,
                            activation_channel=?,
                            activation_task_id=?,
                            activation_client_key_hash=?,
                            activation_idempotency_key=?,
                            activation_attempt=?,
                            activation_error=?,
                            activation_display=?,
                            activation_can_release=?,
                            activation_cdk_consumed=?,
                            activation_submitted_at=?,
                            activation_finished_at=?,
                            activation_updated_at=?,
                            updated_at=?
                        WHERE account_key=?
                          AND COALESCE(activation_provider,'') IN ('','upi')
                          AND COALESCE(activation_status,'') NOT IN ('queued','submitting','submit_unknown','submitted','processing','verifying','active','success','verified','replace_account','verified_plus')
                        """,
                        updates,
                    )
                    conn.executemany(
                        """
                        INSERT INTO account_events(account_key, task_id, event_type, status, message, payload_json, created_at)
                        VALUES(?, '', ?, ?, ?, '{}', ?)
                        """,
                        events,
                    )
        except Exception as exc:
            return {
                "ok": False,
                "async": False,
                "message": _safe_message(exc, "UPI 批量入队失败"),
                "accepted": len(clean_keys),
                "queued": 0,
                "skipped": 0,
                "failed": len(clean_keys),
                "results": [],
                "config": self.config_status(),
            }

        if queued:
            self.wake()
        rate = int(status.get("submit_per_key_per_min") or 50)
        keys_n = max(1, int(status.get("key_count") or 1))
        est_min = max(1, (queued + rate * keys_n - 1) // (rate * keys_n)) if queued else 0
        return {
            "ok": failed == 0,
            "async": True,
            "accepted": len(clean_keys),
            "queued": queued,
            "skipped": skipped,
            "failed": failed,
            "channel": channel_norm,
            "message": (
                f"已入队 {queued}，跳过 {skipped}，失败 {failed}；"
                f"远端提交上限约 {rate * keys_n}/分，估 {est_min} 分钟交完，不含值守付款"
            ),
            "config": self.config_status(),
            "results": results,
        }

    def enqueue_accounts_async(
        self,
        keys: list[str],
        *,
        channel: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Accept a bulk activation request and make queued rows visible immediately.

        Remote UPI POSTs remain asynchronous, but the local queue write is
        synchronous and batched so a 300-account request shows up in the UI as
        queued/submitting right after the API returns.
        """
        status = self._runtime_config()
        clean_keys = [str(raw or "").strip() for raw in (keys or []) if str(raw or "").strip()]
        if not status["enabled"]:
            return {
                "ok": False,
                "async": False,
                "message": "UPI Plus 激活已禁用（upi_activation_enabled=false）",
                "accepted": 0,
                "queued": 0,
                "skipped": 0,
                "failed": len(clean_keys),
                "results": [],
                "config": self.config_status(),
            }
        if not status["has_key"]:
            return {
                "ok": False,
                "async": False,
                "message": "未配置 UPI client key（设置页填写 upi_client_key / actk_…）",
                "accepted": 0,
                "queued": 0,
                "skipped": 0,
                "failed": len(clean_keys),
                "results": [],
                "config": self.config_status(),
            }
        if not clean_keys:
            return {
                "ok": False,
                "async": False,
                "message": "请至少选择一个账号",
                "accepted": 0,
                "queued": 0,
                "skipped": 0,
                "failed": 0,
                "results": [],
                "config": self.config_status(),
            }

        channel_norm = normalize_channel(channel or self._cfg().get("upi_default_channel") or DEFAULT_CHANNEL)
        return self._enqueue_accounts_fast(clean_keys, channel=channel_norm, force=force)


    def queue_stats(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        try:
            with db.connect(getattr(self.accounts.repo, "db_path", None)) as conn:
                rows = conn.execute(
                    "SELECT COALESCE(activation_status,'') AS st, COUNT(*) AS n FROM accounts GROUP BY COALESCE(activation_status,'')"
                ).fetchall()
                counts = {str(row["st"] or ""): int(row["n"] or 0) for row in rows}
        except Exception as exc:
            self._worker_error = _safe_message(exc, "读取 UPI 统计失败")
        return {
            "ok": True,
            "counts": counts,
            "active": sum(counts.get(value, 0) for value in ACTIVE_STATUSES),
            "config": self.config_status(),
            "worker_started": self._started,
        }

    def list_activation_tasks(self, *, statuses: list[str] | None = None, limit: int = 100000) -> dict[str, Any]:
        """Return public UPI activation rows without per-account N+1 loading."""
        clean_statuses = {str(value or "").strip().lower() for value in (statuses or []) if str(value or "").strip()}
        params: list[Any] = []
        where = "COALESCE(activation_provider,'') IN ('','upi') AND COALESCE(activation_status,'') NOT IN ('','idle')"
        if clean_statuses:
            placeholders = ",".join("?" for _ in clean_statuses)
            where += f" AND LOWER(COALESCE(activation_status,'')) IN ({placeholders})"
            params.extend(sorted(clean_statuses))
        safe_limit = max(1, min(int(limit or 100000), 100000))
        try:
            with db.connect(getattr(self.accounts.repo, "db_path", None)) as conn:
                total = int(conn.execute(f"SELECT COUNT(*) AS n FROM accounts WHERE {where}", params).fetchone()["n"] or 0)
                rows = conn.execute(
                    f"""
                    SELECT
                      id,
                      account_key,
                      account_key AS key,
                      account_id,
                      email,
                      billing_email,
                      codex_email,
                      login_identifier,
                      plan_type,
                      plus_status,
                      plus_verified_at,
                      plus_check_source,
                      status,
                      stage,
                      registration_status,
                      activation_provider,
                      activation_status,
                      activation_channel,
                      activation_task_id,
                      activation_error,
                      activation_display,
                      activation_can_release,
                      activation_cdk_consumed,
                      activation_submitted_at,
                      activation_finished_at,
                      activation_updated_at,
                      created_at,
                      updated_at
                    FROM accounts
                    WHERE {where}
                    ORDER BY
                      CASE WHEN COALESCE(activation_status,'') IN ('queued','submitting','submit_unknown','submitted','processing','verifying') THEN 0 ELSE 1 END,
                      COALESCE(NULLIF(activation_updated_at,''), updated_at, created_at) DESC,
                      id DESC
                    LIMIT ?
                    """,
                    (*params, safe_limit),
                ).fetchall()
        except Exception as exc:
            self._worker_error = _safe_message(exc, "读取 UPI 进度失败")
            return {"ok": False, "message": self._worker_error, "items": [], "total": 0, "truncated": False}
        return {"ok": True, "items": [dict(row) for row in rows], "total": total, "truncated": total > len(rows), "stats": self.queue_stats()}

    def refresh_activation_tasks(self, keys: list[str] | None = None, *, statuses: list[str] | None = None) -> dict[str, Any]:
        """Poll remote status now for submitted/processing activation tasks."""
        allowed = {str(value or "").strip().lower() for value in (statuses or ["submitted", "processing"]) if str(value or "").strip()}
        pollable = allowed & {"submitted", "processing"}
        if not pollable:
            self.wake()
            return {"ok": True, "checked": 0, "updated": 0, "message": "无可直接轮询的状态，已唤醒后台任务", "items": []}
        ordered: list[str] = []
        seen: set[str] = set()
        for raw in keys or []:
            key = str(raw or "").strip()
            if key and key not in seen:
                seen.add(key)
                ordered.append(key)
        snapshots = [self._load_account(key) for key in ordered] if ordered else self._list_by_status(pollable, limit=200)
        cfg = self._runtime_config()
        if not cfg.get("enabled") or not cfg.get("has_key"):
            return {"ok": False, "checked": 0, "updated": 0, "message": "UPI 未启用或未配置 client key", "items": []}
        checked = 0
        updated = 0
        items: list[dict[str, Any]] = []
        for snapshot in snapshots:
            key = str(snapshot.get("account_key") or "")
            task_id = str(snapshot.get("activation_task_id") or "")
            if not key or not task_id or str(snapshot.get("activation_status") or "").lower() not in pollable:
                continue
            checked += 1
            try:
                task, used_key, error, _retryable = self._poll_remote(dict(snapshot), cfg)
                with account_operation_lock(key, blocking=False):
                    account = self._load_account(key)
                    if str(account.get("activation_task_id") or "") != task_id or str(account.get("activation_status") or "").lower() not in pollable:
                        items.append(self._public_account(account))
                        continue
                    before = (account.get("activation_status"), account.get("activation_cdk_consumed"), account.get("activation_display"), account.get("activation_error"))
                    if task is None:
                        self._mark_retry(account, error or "UPI 轮询失败，将重试")
                    else:
                        if used_key:
                            account["activation_client_key_hash"] = _key_hash(used_key)
                        self._apply_task_result(account, task, save=False, cfg=cfg)
                    after = (account.get("activation_status"), account.get("activation_cdk_consumed"), account.get("activation_display"), account.get("activation_error"))
                    saved = self._save_account(account) if after != before or used_key else account
                    if after != before or used_key:
                        updated += 1
                    items.append(self._public_account(saved))
            except AccountOperationBusy:
                continue
            except Exception as exc:
                self._worker_error = _safe_message(exc, "UPI 手动刷新失败")
        self.wake()
        return {"ok": True, "checked": checked, "updated": updated, "items": items, "stats": self.queue_stats()}

    def retry_activation_tasks(self, keys: list[str] | None = None, *, statuses: list[str] | None = None, channel: str = "upi") -> dict[str, Any]:
        retry_statuses = {str(value or "").strip().lower() for value in (statuses or ["failed", "cancelled", "released"]) if str(value or "").strip()}
        allowed = retry_statuses & REQUEUEABLE_STATUSES
        if not allowed:
            return {"ok": False, "accepted": 0, "queued": 0, "failed": 0, "message": "没有可重试的状态", "results": []}
        ordered: list[str] = []
        seen: set[str] = set()
        for raw in keys or []:
            key = str(raw or "").strip()
            if key and key not in seen:
                seen.add(key)
                ordered.append(key)
        if not ordered:
            ordered = [str(item.get("account_key") or "") for item in self._list_by_status(allowed, limit=100000)]
        return self.enqueue_accounts_async(ordered, channel=channel, force=True)

    # ------------------------------------------------------------------
    # key selection and remote calls
    # ------------------------------------------------------------------

    def _pick_key(self, keys: list[str]) -> str | None:
        if not keys:
            return None
        with self._lock:
            index = self._key_rr % len(keys)
            selected = keys[index]
            self._key_rr = (index + 1) % len(keys)
            return selected

    def _ordered_key_candidates(self, keys: list[str], preferred_hash: str = "") -> list[str]:
        if not keys:
            return []
        ordered: list[str] = []
        if preferred_hash:
            ordered.extend(key for key in keys if _key_hash(key) == preferred_hash)
        start = self._key_rr % len(keys)
        ordered.extend(keys[start:])
        ordered.extend(keys[:start])
        result: list[str] = []
        seen: set[str] = set()
        for key in ordered:
            if key not in seen:
                seen.add(key)
                result.append(key)
        return result

    def _bucket_for(self, key: str, rate: float) -> TokenBucket:
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None or abs(bucket.rate_per_second * 60 - rate) > 0.01:
                bucket = TokenBucket(rate_per_minute=rate)
                self._buckets[key] = bucket
            return bucket

    def _key_on_hold(self, key: str) -> bool:
        """True while a per-key Retry-After deadline is still active."""
        with self._lock:
            deadline = float(self._key_retry_after_until.get(key) or 0.0)
            if deadline <= 0:
                return False
            if time.monotonic() >= deadline:
                self._key_retry_after_until.pop(key, None)
                return False
            return True

    def _hold_key(self, key: str, retry_after: float) -> None:
        delay = max(0.0, float(retry_after or 0.0))
        if delay <= 0 or not key:
            return
        deadline = time.monotonic() + delay
        with self._lock:
            prev = float(self._key_retry_after_until.get(key) or 0.0)
            if deadline > prev:
                self._key_retry_after_until[key] = deadline

    def _reserve_submission_dispatch(self, key: str, rate: float) -> threading.BoundedSemaphore | None:
        """Reserve one configured-rate POST token and one per-key in-flight slot."""
        if self._key_on_hold(key):
            return None
        with self._lock:
            bucket = self._bucket_for(key, rate)
            if bucket.try_acquire(1.0) > 0:
                return None
            slot = self._submit_slots.get(key)
            if slot is None:
                slot = threading.BoundedSemaphore(MAX_SUBMISSIONS_PER_KEY)
                self._submit_slots[key] = slot
            if not slot.acquire(blocking=False):
                # Slot full: give the rate token back so claim races / peer
                # processes do not silently burn the 50/min budget.
                bucket.refund(1.0)
                return None
            return slot

    def _release_submission_dispatch(
        self,
        key: str,
        rate: float,
        slot: threading.BoundedSemaphore | None,
        *,
        refund_token: bool = False,
    ) -> None:
        """Release an in-flight slot; optionally refund an unused rate token."""
        if slot is not None:
            try:
                slot.release()
            except ValueError:
                pass
        if refund_token and key:
            with self._lock:
                self._bucket_for(key, rate).refund(1.0)

    @staticmethod
    def _idempotency_not_found(error: str) -> bool:
        value = str(error or "").lower()
        return "404" in value or "未找到" in value or "not found" in value

    def _client(self, key: str, cfg: dict[str, Any]) -> UpiActivationClient:
        return UpiActivationClient(key, base_url=str(cfg.get("base_url") or DEFAULT_BASE_URL), device_id=str(cfg.get("device_id") or "gpt-register"))

    @staticmethod
    def _is_retryable(exc: UpiActivationError) -> bool:
        payload = getattr(exc, "payload", {}) if isinstance(getattr(exc, "payload", {}), dict) else {}
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        if isinstance(error.get("retryable"), bool):
            return bool(error.get("retryable"))
        code_text = str(error.get("code") or "").strip().lower()
        if code_text in {"channel_disabled", "unsupported_field", "idempotency_key_invalid", "idempotency_key_conflict"}:
            return False
        message = str(exc or "").lower()
        if ("渠道" in message and "已关闭" in message) or "channel" in message and "closed" in message:
            return False
        return int(exc.status_code or 0) in RETRYABLE_STATUS_CODES or int(exc.status_code or 0) >= 500

    def _submit_remote(self, account: dict[str, Any], token: str, cfg: dict[str, Any], *, first_key: str | None = None) -> tuple[UpiTask | None, str | None, str | None, bool]:
        keys = list(cfg.get("keys") or [])
        first = first_key or self._pick_key(keys)
        candidates = [first] + [key for key in keys if key != first] if first else keys
        rate = float(cfg.get("submit_per_key_per_min") or 50)
        last_error = ""
        last_retryable = True
        last_used_key: str | None = None
        for client_key in candidates:
            if not client_key:
                continue
            fallback_slot: threading.BoundedSemaphore | None = None
            # The dispatcher reserved ``first_key`` before the durable claim.
            # A key-probe fallback needs its own rate token and in-flight slot.
            if first_key is None or client_key != first_key:
                fallback_slot = self._reserve_submission_dispatch(client_key, rate)
                if fallback_slot is None:
                    last_error = "UPI client key 当前提交额度或并发已满"
                    last_retryable = True
                    continue
            try:
                task = self._client(client_key, cfg).submit_task(
                    token,
                    channel=normalize_channel(account.get("activation_channel") or DEFAULT_CHANNEL),
                    idempotency_key=str(account.get("activation_idempotency_key") or ""),
                    device_id=str(cfg.get("device_id") or "gpt-register"),
                )
                if not task.id:
                    return None, None, "UPI 提交响应缺少 task id（协议错误）", False
                return task, client_key, None, False
            except UpiActivationError as exc:
                last_error = _safe_message(exc, "UPI 提交失败", secrets=(token,))
                last_retryable = self._is_retryable(exc)
                last_used_key = client_key
                if int(exc.status_code or 0) == 429:
                    # Honor server Retry-After before any requeue/POST on this key.
                    self._hold_key(client_key, float(exc.retry_after or 0.0) or 1.0)
                if int(exc.status_code or 0) in KEY_PROBE_STATUS_CODES:
                    continue
                # Transient: try to recover the task that may already exist.
                if last_retryable:
                    recovered, recovered_key, _recover_error, _recover_retryable = self._recover_by_idempotency(
                        account,
                        cfg,
                        preferred_key=client_key,
                    )
                    if recovered is not None and recovered.id:
                        return recovered, recovered_key or client_key, None, False
                return None, client_key, last_error, last_retryable
            except Exception as exc:
                last_error = _safe_message(exc, "UPI 提交失败", secrets=(token,))
                last_retryable = True
                last_used_key = client_key
                recovered, recovered_key, _recover_error, _recover_retryable = self._recover_by_idempotency(
                    account,
                    cfg,
                    preferred_key=client_key,
                )
                if recovered is not None and recovered.id:
                    return recovered, recovered_key or client_key, None, False
                return None, client_key, last_error, True
            finally:
                if fallback_slot is not None:
                    fallback_slot.release()
        if last_retryable and last_error:
            recovered, recovered_key, _recover_error, _recover_retryable = self._recover_by_idempotency(account, cfg)
            if recovered is not None and recovered.id:
                return recovered, recovered_key or last_used_key, None, False
        return None, last_used_key, last_error or "没有可用 UPI client key", last_retryable

    def _recover_by_idempotency(
        self,
        account: dict[str, Any],
        cfg: dict[str, Any],
        *,
        preferred_key: str | None = None,
    ) -> tuple[UpiTask | None, str | None, str, bool]:
        """Probe every configured client key for a durable Idempotency-Key.

        A single-key 404 is not proof the task is absent: a 401 fallback POST may
        have created the task under another key while the row still points at the
        original key hash. Only after every usable key returns a definitive 404 is
        the task declared missing. Transient/non-404 errors retain ambiguous state.
        """
        idem = str(account.get("activation_idempotency_key") or "").strip()
        if not idem:
            return None, None, "缺少 Idempotency-Key，无法找回任务", False
        keys = list(cfg.get("keys") or [])
        ordered: list[str] = []
        if preferred_key:
            ordered.append(preferred_key)
        ordered.extend(self._ordered_key_candidates(keys, str(account.get("activation_client_key_hash") or "")))
        seen: set[str] = set()
        last_error = ""
        saw_retryable = False
        saw_definitive_404 = False
        all_probed_404 = True
        probed = 0
        for client_key in ordered:
            if not client_key or client_key in seen:
                continue
            seen.add(client_key)
            probed += 1
            try:
                task = self._client(client_key, cfg).get_task_by_idempotency(idem)
                if task.id:
                    return task, client_key, "", False
                # Empty id without HTTP error: treat as transient, not absent.
                last_error = "幂等查询返回空 task id"
                saw_retryable = True
                all_probed_404 = False
            except UpiActivationError as exc:
                code = int(exc.status_code or 0)
                last_error = _safe_message(exc, "幂等查询失败")
                if code == 404:
                    saw_definitive_404 = True
                    continue
                all_probed_404 = False
                if code in KEY_PROBE_STATUS_CODES:
                    # 401/other key probe: try next key; do not declare absent.
                    continue
                retryable = self._is_retryable(exc)
                saw_retryable = saw_retryable or retryable
                if not retryable:
                    return None, client_key, last_error, False
            except Exception as exc:
                last_error = _safe_message(exc, "幂等查询失败")
                saw_retryable = True
                all_probed_404 = False
        if probed == 0:
            return None, None, last_error or "没有可用 UPI client key", True
        if saw_definitive_404 and all_probed_404:
            return None, None, "幂等查询未找到任务", True
        return None, None, last_error or "幂等查询失败", True if saw_retryable or not last_error else False

    def _poll_remote(self, account: dict[str, Any], cfg: dict[str, Any]) -> tuple[UpiTask | None, str | None, str, bool]:
        keys = list(cfg.get("keys") or [])
        candidates = self._ordered_key_candidates(keys, str(account.get("activation_client_key_hash") or ""))
        last_error = ""
        saw_key_error = False
        for client_key in candidates:
            try:
                task = self._client(client_key, cfg).get_task(str(account.get("activation_task_id") or ""))
                return task, client_key, "", False
            except UpiActivationError as exc:
                last_error = _safe_message(exc, "UPI 轮询失败")
                if int(exc.status_code or 0) in KEY_PROBE_STATUS_CODES:
                    saw_key_error = True
                    continue
                return None, client_key, last_error, self._is_retryable(exc)
            except Exception as exc:
                return None, client_key, _safe_message(exc, "UPI 轮询失败"), True
        if saw_key_error:
            return None, None, last_error or "当前配置 key 无法找到该任务，已等待轮换探测", True
        return None, None, last_error or "没有可用 UPI client key", True

    # ------------------------------------------------------------------
    # worker loops
    # ------------------------------------------------------------------

    def _submit_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._submit_once()
            except Exception as exc:
                self._worker_error = _safe_message(exc, "UPI submit worker 异常")
            self._wake.wait(timeout=1.0)
            self._wake.clear()

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            did = False
            try:
                did = self._poll_once() or did
                did = self._verify_once() or did
            except Exception as exc:
                self._worker_error = _safe_message(exc, "UPI poll worker 异常")
            interval = float(self.config_status().get("poll_interval_sec") or 5)
            self._stop.wait(timeout=interval)

    @staticmethod
    def _event_for_activation_state(state: str) -> str:
        return {
            "submitted": "activation_submitted",
            "processing": "activation_processing",
            "success": "activation_success",
            "verifying": "activation_verifying",
            "verified": "activation_verified",
            "failed": "activation_failed",
            "expired": "activation_expired",
            "cancelled": "activation_cancelled",
            "released": "activation_released",
            "replace_account": "activation_replace_account",
        }.get(state, "")

    def _recover_ambiguous_submissions(self, cfg: dict[str, Any]) -> None:
        """Resolve persisted submit intents before any new POST can be scheduled."""
        for candidate in self._list_by_status({"submitting", "submit_unknown"}, limit=50):
            key = str(candidate.get("account_key") or "").strip()
            if not key:
                continue
            try:
                snapshot = self._activation_snapshot(key)
                status = str(snapshot.get("activation_status") or "").lower()
                if status not in {"submitting", "submit_unknown"}:
                    continue
                claim_id = str(snapshot.get("activation_submission_claim") or "")
                # A worker in this process owns the live POST. A second lookup
                # could observe a transient 404 and incorrectly requeue it.
                with self._lock:
                    if status == "submitting" and claim_id and claim_id in self._local_submission_claims:
                        continue
                task, used_key, error, retryable = self._recover_by_idempotency(snapshot, cfg)
                current = self._activation_snapshot(key)
                current_status = str(current.get("activation_status") or "").lower()
                if current_status != status:
                    continue
                if status == "submitting" and str(current.get("activation_submission_claim") or "") != claim_id:
                    continue
                if task is not None and task.id:
                    if used_key:
                        current["activation_client_key_hash"] = _key_hash(used_key)
                    current["activation_status"] = "submitted"
                    current["activation_task_id"] = task.id
                    current["activation_channel"] = task.channel or current.get("activation_channel") or DEFAULT_CHANNEL
                    current["activation_error"] = ""
                    current["activation_display"] = task.display_description or ""
                    current["activation_can_release"] = 1 if task.can_release else 0
                    current["activation_cdk_consumed"] = int(task.cdk_consumed or 0)
                    current["activation_submitted_at"] = current.get("activation_submitted_at") or self._now()
                    current["activation_updated_at"] = self._now()
                    state = self._apply_task_result(current, task, save=False, cfg=cfg)
                    event = self._event_for_activation_state(state)
                    message = f"幂等找回 task={task.id} state={state}"
                    if status == "submitting":
                        self._persist_claimed_submission(current, claim_id, event=event, message=message)
                    else:
                        current["activation_submission_claim"] = ""
                        self._save_account(current, event=event, message=message)
                    continue
                if self._idempotency_not_found(error):
                    current["activation_status"] = "queued"
                    current["activation_error"] = error
                    current["activation_display"] = "幂等查询未找到任务，将用原幂等键重新提交"
                    current["activation_updated_at"] = self._now()
                    if status == "submitting":
                        self._persist_claimed_submission(
                            current,
                            claim_id,
                            event="activation_requeue_after_unknown",
                            message=current["activation_display"],
                        )
                    else:
                        current["activation_submission_claim"] = ""
                        self._save_account(
                            current,
                            event="activation_requeue_after_unknown",
                            message=current["activation_display"],
                        )
                    continue
                self._mark_retry(current, error or SUBMIT_UNKNOWN_MESSAGE)
                current["activation_display"] = SUBMIT_UNKNOWN_MESSAGE
                if status == "submitting":
                    # Keep the durable claim until a later idempotency lookup
                    # establishes a result or an explicit 404.
                    current["activation_status"] = "submitting"
                    self._persist_claimed_submission(current, claim_id, clear_claim=False)
                else:
                    current["activation_status"] = "submit_unknown"
                    if not retryable and error:
                        current["activation_error"] = error
                    self._save_account(current)
            except Exception as exc:
                self._worker_error = _safe_message(exc, "UPI ambiguous submission recovery failed")

    def _submit_claimed_worker(
        self,
        account: dict[str, Any],
        claim_id: str,
        selected_key: str,
        slot: threading.BoundedSemaphore,
        cfg: dict[str, Any],
    ) -> None:
        try:
            if self._stop.is_set():
                account["activation_status"] = "submit_unknown"
                account["activation_error"] = "服务停止前未确认提交结果，将用原幂等键找回"
                account["activation_display"] = SUBMIT_UNKNOWN_MESSAGE
                account["activation_updated_at"] = self._now()
                self._persist_claimed_submission(
                    account,
                    claim_id,
                    event="activation_submit_unknown",
                    message=account["activation_error"],
                )
                return
            token = self.accounts._account_access_token(account)
            if not token:
                account["activation_status"] = "failed"
                account["activation_error"] = "缺少 access_token"
                account["activation_finished_at"] = self._now()
                account["activation_updated_at"] = self._now()
                self._persist_claimed_submission(account, claim_id, event="activation_failed", message="缺少 access_token")
                return
            task, used_key, error, retryable = self._submit_remote(account, token, cfg, first_key=selected_key)
            if task is None:
                if error and "缺少 task id" in error:
                    account["activation_status"] = "failed"
                    account["activation_error"] = error
                    account["activation_finished_at"] = self._now()
                    event = "activation_failed"
                elif retryable:
                    # The POST may have succeeded despite the missing response.
                    account["activation_status"] = "submit_unknown"
                    account["activation_error"] = _safe_message(error or SUBMIT_UNKNOWN_MESSAGE)
                    account["activation_display"] = SUBMIT_UNKNOWN_MESSAGE
                    if used_key:
                        account["activation_client_key_hash"] = _key_hash(used_key)
                    event = "activation_submit_unknown"
                else:
                    account["activation_status"] = "failed"
                    account["activation_error"] = error or "UPI 提交被拒绝"
                    account["activation_display"] = account["activation_error"]
                    account["activation_finished_at"] = self._now()
                    event = "activation_failed"
                account["activation_updated_at"] = self._now()
                self._persist_claimed_submission(account, claim_id, event=event, message=str(account.get("activation_error") or ""))
                return
            account["activation_client_key_hash"] = _key_hash(used_key or selected_key)
            account["activation_status"] = "submitted"
            account["activation_task_id"] = task.id
            account["activation_channel"] = task.channel or account.get("activation_channel") or DEFAULT_CHANNEL
            account["activation_error"] = ""
            account["activation_display"] = task.display_description or ""
            account["activation_can_release"] = 1 if task.can_release else 0
            account["activation_cdk_consumed"] = int(task.cdk_consumed or 0)
            account["activation_submitted_at"] = self._now()
            account["activation_updated_at"] = self._now()
            state = self._apply_task_result(account, task, save=False, cfg=cfg)
            self._persist_claimed_submission(
                account,
                claim_id,
                event=self._event_for_activation_state(state),
                message=f"UPI task={task.id} state={state}",
            )
        except Exception as exc:
            account["activation_status"] = "submit_unknown"
            account["activation_error"] = _safe_message(exc, SUBMIT_UNKNOWN_MESSAGE)
            account["activation_display"] = SUBMIT_UNKNOWN_MESSAGE
            account["activation_updated_at"] = self._now()
            self._persist_claimed_submission(
                account,
                claim_id,
                event="activation_submit_unknown",
                message=account["activation_error"],
            )
        finally:
            slot.release()
            with self._lock:
                self._local_submission_claims.discard(claim_id)
                self._submission_workers.discard(threading.current_thread())
            self._wake.set()

    def _submit_once(self) -> None:
        cfg = self._runtime_config()
        if not cfg.get("enabled") or not cfg.get("has_key"):
            return
        # A durable ``submitting`` row is ambiguous after a process restart.
        # Resolve it with its persisted idempotency key before claiming queued work.
        self._recover_ambiguous_submissions(cfg)
        rate = float(cfg.get("submit_per_key_per_min") or 50)
        for candidate in self._list_by_status({"queued"}, limit=50):
            key = str(candidate.get("account_key") or "").strip()
            if not key:
                continue
            selected = self._pick_key(list(cfg.get("keys") or []))
            if not selected:
                return
            slot = self._reserve_submission_dispatch(selected, rate)
            if slot is None:
                continue
            claim_id = uuid.uuid4().hex
            account: dict[str, Any] = {}
            worker: threading.Thread | None = None
            try:
                account = self.accounts.repo.claim_queued_activation_submission(key, claim_id, _key_hash(selected))
                if not account:
                    # Peer process or concurrent loop already took the row.
                    # Do not burn the 50/min budget for a no-op claim.
                    self._release_submission_dispatch(selected, rate, slot, refund_token=True)
                    continue
                with self._lock:
                    self._local_submission_claims.add(claim_id)
                worker = threading.Thread(
                    target=self._submit_claimed_worker,
                    args=(account, claim_id, selected, slot, cfg),
                    name=f"upi-post-{_key_hash(selected)[:8]}",
                    daemon=True,
                )
                with self._lock:
                    self._submission_workers.add(worker)
                worker.start()
            except Exception as exc:
                # Dispatch did not complete locally; preserve the idempotency
                # key and let the next loop resolve this claim before any POST.
                try:
                    if account:
                        account["activation_status"] = "submit_unknown"
                        account["activation_error"] = _safe_message(exc, SUBMIT_UNKNOWN_MESSAGE)
                        account["activation_display"] = SUBMIT_UNKNOWN_MESSAGE
                        account["activation_updated_at"] = self._now()
                        self._persist_claimed_submission(
                            account,
                            claim_id,
                            event="activation_submit_unknown",
                            message=account["activation_error"],
                        )
                finally:
                    with self._lock:
                        self._local_submission_claims.discard(claim_id)
                        if worker is not None:
                            self._submission_workers.discard(worker)
                    slot.release()
                self._worker_error = _safe_message(exc, "UPI submit dispatch failed")

    # ------------------------------------------------------------------
    # task result and polling
    # ------------------------------------------------------------------

    def _timeout_message(self, account: dict[str, Any], timeout_sec: int) -> str:
        submitted = str(account.get("activation_submitted_at") or "").strip()
        if not submitted or timeout_sec <= 0:
            return ""
        try:
            parsed = datetime.fromisoformat(submitted.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - parsed).total_seconds()
            if age >= timeout_sec:
                return f"轮询超过 {timeout_sec} 秒仍未完成，已标记失败"
        except (TypeError, ValueError, OverflowError):
            return ""
        return ""


    def _apply_task_result(self, account: dict[str, Any], task: UpiTask, *, save: bool = True, cfg: dict[str, Any] | None = None) -> str:
        cfg = cfg or self._runtime_config()
        now = self._now()
        remote_status = str(task.status or "").strip().lower()
        account["activation_task_id"] = task.id or account.get("activation_task_id") or ""
        account["activation_channel"] = task.channel or account.get("activation_channel") or ""
        account["activation_can_release"] = 1 if task.can_release else 0
        account["activation_cdk_consumed"] = int(task.cdk_consumed or 0)
        account["activation_display"] = task.display_description or task.reason or account.get("activation_display") or ""
        account["activation_updated_at"] = now

        if task.replace_account or remote_status == "replace_account" or (remote_status == "failed" and str(task.display_action or "").lower() == "replace_account"):
            account["activation_status"] = "replace_account"
            account["activation_error"] = _safe_message(task.display_description or task.reason, "需要更换账号")
            account["activation_display"] = ""
            account["activation_finished_at"] = now
            account["activation_can_release"] = 0
            # Best-effort auto-release when remote still allows it. Never block the
            # local terminal state on release network failures.
            if task.can_release and str(account.get("activation_task_id") or "").strip():
                try:
                    released, used_key, _error, _retryable = self._release_remote(account, cfg)
                    if released is not None:
                        if used_key:
                            account["activation_client_key_hash"] = _key_hash(used_key)
                        account["activation_status"] = "released"
                        account["activation_error"] = _safe_message(
                            task.display_description or task.reason,
                            "需换号，远端任务已自动释放",
                        )
                        account["activation_display"] = ""
                        if save:
                            self._save_account(account, event="activation_released", message=account["activation_error"])
                        return "released"
                except Exception as exc:
                    self._worker_error = _safe_message(exc, "需换号自动释放失败")
            if save:
                self._save_account(account, event="activation_replace_account", message=account["activation_error"])
            return "replace_account"
        if remote_status in {"cancelled", "canceled"}:
            account["activation_status"] = "cancelled"
            account["activation_error"] = _safe_message(task.display_description or task.reason, "已取消")
            account["activation_finished_at"] = now
            if save:
                self._save_account(account, event="activation_cancelled", message=account["activation_error"])
            return "cancelled"
        if remote_status == "released":
            account["activation_status"] = "released"
            account["activation_error"] = _safe_message(task.display_description or task.reason, "已释放")
            account["activation_finished_at"] = now
            if save:
                self._save_account(account, event="activation_released", message=account["activation_error"])
            return "released"
        if remote_status in {"failed", "expired"}:
            account["activation_status"] = "expired" if remote_status == "expired" else "failed"
            fallback = "支付二维码过期" if remote_status == "expired" else "远端激活失败"
            account["activation_error"] = _safe_message(task.display_description or task.reason, fallback)
            account["activation_finished_at"] = now
            if save:
                self._save_account(account, event="activation_expired" if remote_status == "expired" else "activation_failed", message=account["activation_error"])
            return account["activation_status"]
        if task.success or remote_status in {"success", "succeeded"}:
            account["activation_finished_at"] = now
            account["activation_error"] = ""
            if int(task.cdk_consumed or 0) != 1:
                # Keep a soft note for diagnostics; remote success remains terminal locally.
                note = f"远端已成功（cdkConsumed={int(task.cdk_consumed or 0)}）"
                account["activation_display"] = task.display_description or note
            account["plan_type"] = "plus"
            account["plus_status"] = "verified_plus"
            account["plus_verified_at"] = account.get("plus_verified_at") or now
            account["plus_check_source"] = "upi_activation"
            account["plus_check_error"] = ""
            if account.get("stage") not in {"complete", "cpa_bound"}:
                account["stage"] = "plus_verified_needs_oauth"
                account["status"] = "plus_verified_needs_oauth"
                account["binding_status"] = "pending"
            account["activation_status"] = "success"
            if save:
                self._save_account(account, event="activation_success", message=f"UPI 成功 task={task.id}")
            return "success"
        account["activation_status"] = "processing" if remote_status not in {"submitted", "queued"} else "submitted"
        account["activation_error"] = ""
        if save:
            self._save_account(account)
        return str(account.get("activation_status") or "processing")

    def _poll_once(self) -> bool:
        cfg = self._runtime_config()
        if not cfg.get("enabled") or not cfg.get("has_key"):
            return False
        inflight = self._list_by_status({"submitted", "processing"}, limit=80)
        progressed = False
        for snapshot in inflight:
            key = str(snapshot.get("account_key") or "")
            task_id = str(snapshot.get("activation_task_id") or "")
            if not key or not task_id:
                continue
            try:
                # Only local bookkeeping is done under the account lock.  Network
                # I/O happens after releasing it so verification/release cannot
                # deadlock a concurrent account operation.
                with account_operation_lock(key, blocking=False):
                    account = self._load_account(key)
                    if str(account.get("activation_status") or "").lower() not in {"submitted", "processing"}:
                        continue
                    timeout_message = self._timeout_message(account, int(cfg.get("poll_timeout_sec") or 1800))
                    if timeout_message:
                        account["activation_status"] = "failed"
                        account["activation_error"] = timeout_message
                        account["activation_display"] = timeout_message
                        account["activation_finished_at"] = self._now()
                        account["activation_updated_at"] = self._now()
                        self._save_account(account, event="activation_failed", message=timeout_message)
                        progressed = True
                        continue
                    poll_snapshot = dict(account)
                task, used_key, error, retryable = self._poll_remote(poll_snapshot, cfg)
                with account_operation_lock(key, blocking=False):
                    account = self._load_account(key)
                    if str(account.get("activation_task_id") or "") != task_id or str(account.get("activation_status") or "").lower() not in {"submitted", "processing"}:
                        continue
                    if task is None:
                        self._mark_retry(account, error or "UPI 轮询失败，将重试")
                        self._save_account(account)
                        progressed = True
                        continue
                    if used_key:
                        account["activation_client_key_hash"] = _key_hash(used_key)
                    before = (account.get("activation_status"), account.get("activation_cdk_consumed"), account.get("activation_display"), account.get("activation_error"))
                    self._apply_task_result(account, task, save=False, cfg=cfg)
                    after = (account.get("activation_status"), account.get("activation_cdk_consumed"), account.get("activation_display"), account.get("activation_error"))
                    if after != before or used_key:
                        self._save_account(account)
                        progressed = True
            except AccountOperationBusy:
                continue
            except Exception as exc:
                self._worker_error = _safe_message(exc, "UPI poll worker 处理账号失败")
        return progressed

    # ------------------------------------------------------------------
    # restart-safe Plus verification
    # ------------------------------------------------------------------

    @staticmethod
    def _verification_succeeded(result: Any, latest: dict[str, Any]) -> bool:
        payload: dict[str, Any] = {}
        status_code = 0
        if isinstance(result, tuple) and len(result) >= 2:
            payload = result[0] if isinstance(result[0], dict) else {}
            status_code = _int_value(result[1], 0)
        elif isinstance(result, dict):
            payload = result
            status_code = _int_value(result.get("status_code"), 200 if result.get("ok") else 0)
        plus = payload.get("account") if isinstance(payload.get("account"), dict) else payload
        return status_code == 200 and (
            str((plus or {}).get("plus_status") or latest.get("plus_status") or "").lower() == "verified_plus"
            or bool(payload.get("paid"))
        )

    def _verify_once(self) -> bool:
        cfg = self._runtime_config()
        if not cfg.get("enabled") or not cfg.get("auto_verify_plus"):
            return False
        verifying = self._list_by_status({"verifying"}, limit=80)
        if not verifying:
            return False

        # Snapshot keys that are still verifying; skip ones locked by other ops.
        keys: list[str] = []
        for snapshot in verifying:
            key = str(snapshot.get("account_key") or "").strip()
            if not key:
                continue
            try:
                with account_operation_lock(key, blocking=False):
                    account = self._load_account(key)
                    if str(account.get("activation_status") or "").lower() != "verifying":
                        continue
                    keys.append(key)
            except AccountOperationBusy:
                continue
            except Exception as exc:
                self._worker_error = _safe_message(exc, "读取 verifying 队列失败")
        if not keys:
            return False

        # Prefer Go 32-worker batch (shared SOCKS bridge). Serial Python verify_plus
        # is only a fallback when Go is unavailable or returns nothing for a key.
        batch: dict[str, Any] | None = None
        try:
            batch = self.accounts.verify_plus_batch(keys, proxy_region="JP")
        except Exception as exc:
            self._worker_error = _safe_message(exc, "Plus 批量校验异常")
            batch = None

        results_by_key: dict[str, dict[str, Any]] = {}
        if isinstance(batch, dict):
            for item in list(batch.get("results") or []):
                if not isinstance(item, dict):
                    continue
                item_key = str(item.get("key") or "").strip()
                if item_key:
                    results_by_key[item_key] = item

        progressed = False
        for key in keys:
            try:
                item = results_by_key.get(key)
                if item is None:
                    # Go unavailable / key missing from batch → single-key fallback.
                    try:
                        try:
                            payload, status_code = self.accounts.verify_plus(key, wait_for_lock=False)
                        except TypeError as exc:
                            if "wait_for_lock" not in str(exc):
                                raise
                            payload, status_code = self.accounts.verify_plus(key)
                        item = {
                            "key": key,
                            "ok": bool(isinstance(payload, dict) and payload.get("ok") and status_code == 200),
                            "status_code": status_code,
                            "paid": bool(isinstance(payload, dict) and payload.get("paid")),
                            "message": str((payload or {}).get("message") or "") if isinstance(payload, dict) else "",
                            "account": (payload or {}).get("account") if isinstance(payload, dict) else None,
                        }
                    except Exception as exc:
                        item = {"key": key, "ok": False, "status_code": 500, "message": _safe_message(exc, "Plus 自动校验异常")}

                latest = self._load_account(key)
                ok = self._verification_succeeded(
                    ({"ok": bool(item.get("ok")), "paid": bool(item.get("paid")), "account": item.get("account") or latest}, int(item.get("status_code") or 0)),
                    latest,
                )
                verify_error = ""
                if not ok:
                    verify_error = _safe_message(item.get("message"), "Plus 自动校验未通过")

                with account_operation_lock(key, blocking=False):
                    current = self._load_account(key)
                    if str(current.get("activation_status") or "").lower() != "verifying":
                        continue
                    if ok:
                        current["activation_status"] = "verified"
                        current["activation_error"] = ""
                        current["activation_display"] = ""
                        current["activation_can_release"] = 0
                        current["activation_updated_at"] = self._now()
                        self._save_account(current, event="activation_verified", message="Plus 校验通过")
                    else:
                        # Keep success (activation done) but surface verify failure once.
                        current["activation_status"] = "success"
                        current["activation_error"] = f"UPI 已成功，但 Plus 自动校验失败：{verify_error or '未确认 Plus'}"
                        current["activation_display"] = ""
                        current["activation_can_release"] = 0
                        current["activation_updated_at"] = self._now()
                        self._save_account(current, event="activation_verify_failed", message=current["activation_error"])
                    progressed = True
            except AccountOperationBusy:
                continue
            except Exception as exc:
                self._worker_error = _safe_message(exc, "UPI verifying worker 处理账号失败")
        return progressed

    # ------------------------------------------------------------------
    # explicit release/cancel
    # ------------------------------------------------------------------

    def release_account(self, key: str, *, cancel: bool = False) -> tuple[dict[str, Any], int]:
        account_key = str(key or "").strip()
        if not account_key:
            return {"ok": False, "message": "账号 key 不能为空"}, 400
        cfg = self._runtime_config()
        try:
            ambiguous_snapshot: dict[str, Any] | None = None
            with account_operation_lock(account_key, blocking=False):
                account = self._load_account(account_key)
                if not account.get("account_key"):
                    return {"ok": False, "message": "未找到账号"}, 404
                task_id = str(account.get("activation_task_id") or "").strip()
                state = str(account.get("activation_status") or "").strip().lower()
                can_release = bool(_int_value(account.get("activation_can_release"), 0))
                if not task_id and state == "queued":
                    account["activation_status"] = "cancelled"
                    account["activation_finished_at"] = self._now()
                    account["activation_updated_at"] = self._now()
                    account["activation_can_release"] = 0
                    account["activation_error"] = "尚未提交远端任务，已取消"
                    account["activation_display"] = account["activation_error"]
                    saved = self._save_account(account, event="activation_cancelled", message=account["activation_error"])
                    return {"ok": True, "message": "已取消本地开通排队", "account": self._public_account(saved)}, 200
                if not task_id and state in {"submitting", "submit_unknown"}:
                    ambiguous_snapshot = self._activation_snapshot(account_key)
                    if state == "submitting":
                        # Never terminally cancel a durable submitting claim from a
                        # 404 alone: the claim owner may still be mid-POST, or a
                        # concurrent process may own the in-flight request.
                        # Local ownership is process-local; recovery must finish first.
                        with self._lock:
                            claim = str(ambiguous_snapshot.get("activation_submission_claim") or "")
                            if claim in self._local_submission_claims:
                                return {
                                    "ok": False,
                                    "message": "远端提交正在进行，请等待结果或稍后重试",
                                    "account": self._public_account(ambiguous_snapshot),
                                }, 409
                        return {
                            "ok": False,
                            "message": "提交中状态不可直接取消，请等待提交结果或重启恢复后再试",
                            "account": self._public_account(ambiguous_snapshot),
                        }, 409
                elif not task_id:
                    return {"ok": False, "message": "当前没有可释放的激活任务", "account": self._public_account(account)}, 409
                elif not can_release:
                    return {"ok": False, "message": "远端任务当前不可释放（canRelease=false）", "account": self._public_account(account)}, 409
                else:
                    remote_snapshot = dict(account)

            if ambiguous_snapshot is not None:
                # Only ``submit_unknown`` reaches here; ``submitting`` is blocked above.
                task, used_key, error, retryable = self._recover_by_idempotency(ambiguous_snapshot, cfg)
                if task is not None and task.id:
                    with account_operation_lock(account_key, blocking=False):
                        current = self._activation_snapshot(account_key)
                        current_state = str(current.get("activation_status") or "").lower()
                        if current_state != "submit_unknown" or str(current.get("activation_task_id") or ""):
                            return {"ok": False, "message": "激活状态已变化，请刷新后重试", "account": self._public_account(current)}, 409
                        if str(current.get("activation_idempotency_key") or "") != str(ambiguous_snapshot.get("activation_idempotency_key") or ""):
                            return {"ok": False, "message": "激活幂等键已变化，请刷新后重试", "account": self._public_account(current)}, 409
                        if used_key:
                            current["activation_client_key_hash"] = _key_hash(used_key)
                        current["activation_status"] = "submitted"
                        current["activation_task_id"] = task.id
                        current["activation_channel"] = task.channel or current.get("activation_channel") or DEFAULT_CHANNEL
                        current["activation_can_release"] = 1 if task.can_release else 0
                        current["activation_cdk_consumed"] = int(task.cdk_consumed or 0)
                        current["activation_error"] = ""
                        current["activation_display"] = task.display_description or ""
                        current["activation_submitted_at"] = current.get("activation_submitted_at") or self._now()
                        current["activation_updated_at"] = self._now()
                        current["activation_submission_claim"] = ""
                        self._save_account(current, event="activation_submitted", message=f"取消前幂等找回 task={task.id}")
                    # The recovered remote task now follows the normal release
                    # path, including its canRelease guard.
                    return self.release_account(account_key, cancel=cancel)
                if not self._idempotency_not_found(error):
                    return {
                        "ok": False,
                        "message": error or "幂等查询失败，不能取消待确认提交",
                        "account": self._public_account(ambiguous_snapshot),
                    }, 503 if retryable else 502
                with account_operation_lock(account_key, blocking=False):
                    current = self._activation_snapshot(account_key)
                    current_state = str(current.get("activation_status") or "").lower()
                    if current_state != "submit_unknown" or str(current.get("activation_task_id") or ""):
                        return {"ok": False, "message": "激活状态已变化，请刷新后重试", "account": self._public_account(current)}, 409
                    current["activation_status"] = "cancelled"
                    current["activation_finished_at"] = self._now()
                    current["activation_updated_at"] = self._now()
                    current["activation_can_release"] = 0
                    current["activation_error"] = "幂等查询确认未创建远端任务，已取消本地开通"
                    current["activation_display"] = current["activation_error"]
                    current["activation_submission_claim"] = ""
                    saved = self._save_account(current, event="activation_cancelled", message=current["activation_error"])
                    return {"ok": True, "message": "已确认无远端任务并取消本地开通", "account": self._public_account(saved)}, 200

            task, used_key, error, retryable = self._release_remote(remote_snapshot, cfg)
            if task is None:
                with account_operation_lock(account_key, blocking=False):
                    current = self._load_account(account_key)
                    self._mark_retry(current, error or "释放请求失败，将保留原状态")
                    self._save_account(current)
                    response = {"ok": False, "message": current.get("activation_error") or "释放请求失败", "account": self._public_account(current)}
                return response, 503 if retryable else 502
            with account_operation_lock(account_key, blocking=False):
                current = self._load_account(account_key)
                if used_key:
                    current["activation_client_key_hash"] = _key_hash(used_key)
                current["activation_status"] = "released"
                current["activation_can_release"] = 0
                current["activation_finished_at"] = self._now()
                current["activation_updated_at"] = self._now()
                current["activation_error"] = ""
                current["activation_display"] = "已释放远端激活任务"
                saved = self._save_account(current, event="activation_released", message="已释放远端激活任务")
                return {"ok": True, "message": "已释放激活任务", "account": self._public_account(saved)}, 200
        except AccountOperationBusy:
            return {"ok": False, "message": "账号正在执行其他操作，请稍后重试"}, 409
        except Exception as exc:
            return {"ok": False, "message": _safe_message(exc, "释放激活失败")}, 500

    def _release_remote(self, account: dict[str, Any], cfg: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str, bool]:
        keys = list(cfg.get("keys") or [])
        candidates = self._ordered_key_candidates(keys, str(account.get("activation_client_key_hash") or ""))
        last_error = ""
        for client_key in candidates:
            try:
                payload = self._client(client_key, cfg).release_task(str(account.get("activation_task_id") or ""))
                return payload, client_key, "", False
            except UpiActivationError as exc:
                last_error = _safe_message(exc, "UPI 释放失败")
                if int(exc.status_code or 0) in KEY_PROBE_STATUS_CODES:
                    continue
                return None, client_key, last_error, self._is_retryable(exc)
            except Exception as exc:
                return None, client_key, _safe_message(exc, "UPI 释放失败"), True
        return None, None, last_error or "所有 UPI client key 均无法释放该任务", True

    def cancel_account(self, key: str) -> tuple[dict[str, Any], int]:
        return self.release_account(key, cancel=True)

    def release_accounts(self, keys: list[str]) -> dict[str, Any]:
        """Batch cancel local queue / release remote UPI tasks (frees client key capacity).

        Reuses release_account per key so submit_unknown recovery and canRelease
        guards stay identical to the single-row path.
        """
        ordered: list[str] = []
        seen: set[str] = set()
        for raw in keys or []:
            key = str(raw or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            ordered.append(key)
        if not ordered:
            return {
                "ok": False,
                "released": 0,
                "failed": 0,
                "message": "请至少选择一个账号",
                "results": [],
            }

        results: list[dict[str, Any]] = []
        released = 0
        failed = 0
        for key in ordered:
            payload, status_code = self.release_account(key)
            account = payload.get("account") if isinstance(payload, dict) else None
            ok = bool(payload.get("ok")) and int(status_code or 0) < 400
            item: dict[str, Any] = {
                "key": key,
                "ok": ok,
                "status_code": int(status_code or 0),
                "message": str(payload.get("message") or ("已释放" if ok else "释放失败")),
                "activation_status": (
                    (account or {}).get("activation_status")
                    if isinstance(account, dict)
                    else None
                ),
            }
            if isinstance(account, dict):
                item["account"] = account
            results.append(item)
            if ok:
                released += 1
            else:
                failed += 1

        ok = released > 0 and failed == 0
        if released > 0 and failed > 0:
            message = f"已释放/取消 {released} 个，失败 {failed} 个"
        elif released > 0:
            message = f"已释放/取消 {released} 个开通任务（释放 API Key 占用）"
        else:
            message = f"全部失败（{failed}），未能释放 API Key"
        return {
            "ok": ok,
            "released": released,
            "failed": failed,
            "message": message,
            "results": results,
        }

    # ------------------------------------------------------------------
    # CDK -> client key issuance
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_api_key(payload: Any) -> str:
        if isinstance(payload, dict):
            for name in ("apiKey", "api_key", "clientKey", "client_key", "key"):
                value = payload.get(name)
                if isinstance(value, str) and value.startswith("actk_"):
                    return value.strip()
            for name in ("data", "result", "keyInfo"):
                nested = payload.get(name)
                found = UpiActivationService._extract_api_key(nested)
                if found:
                    return found
        return ""

    @staticmethod
    def _key_metadata(payload: Any, api_key: str) -> dict[str, Any]:
        metadata: dict[str, Any] = {"key_prefix": f"{api_key[:8]}…"}
        if isinstance(payload, dict):
            nested = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            for name in ("createdAt", "created_at", "expiresAt", "expires_at", "expiresIn", "expires_in", "id", "keyId", "key_id", "rotate"):
                value = nested.get(name) if isinstance(nested, dict) else None
                if isinstance(value, (str, int, float, bool)) and value not in ("", None):
                    metadata[name] = value
        return metadata

    def issue_client_key(self, cdk: str, *, note: str = "gpt-register", rotate: bool = False) -> tuple[dict[str, Any], int]:
        cdk_value = str(cdk or "").strip()
        if not cdk_value:
            return {"ok": False, "message": "CDK 不能为空"}, 400
        cfg = self._runtime_config()
        try:
            client = UpiActivationClient(
                "",
                base_url=str(cfg.get("base_url") or DEFAULT_BASE_URL),
                device_id=str(cfg.get("device_id") or "gpt-register"),
            )
            payload = client.create_key(cdk_value, note=str(note or "gpt-register"), rotate=parse_bool(rotate, default=False))
            api_key = self._extract_api_key(payload)
            if not api_key:
                return {"ok": False, "message": "官方签发响应缺少 apiKey（协议错误）"}, 502
            self.config_service.save_overrides({"upi_client_key": api_key})
            return {"ok": True, "client_key": api_key, **self._key_metadata(payload, api_key), "config": self.config_status()}, 200
        except UpiActivationError as exc:
            return {"ok": False, "message": _safe_message(exc, "签发 UPI client key 失败", secrets=(cdk_value,))}, 502 if self._is_retryable(exc) else 400
        except Exception as exc:
            return {"ok": False, "message": _safe_message(exc, "签发 UPI client key 失败", secrets=(cdk_value,))}, 500


_SERVICE: UpiActivationService | None = None
_SERVICE_LOCK = threading.Lock()


def get_upi_activation_service() -> UpiActivationService:
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            _SERVICE = UpiActivationService()
            _SERVICE.ensure_worker()
        return _SERVICE
