from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from datetime import datetime, timezone, timedelta
from typing import Any

import base64
import hashlib
import json
import re
import subprocess
import urllib.parse
import urllib.request
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
from collections.abc import Iterable, Iterator
from pathlib import Path

from domain.accounts import AccountQuery
from infrastructure.repositories.accounts_repository import AccountsRepository
from core import account_store
from core.account_store import product_export
from application.config_service import ConfigService
from core.proxy.credential_runtime import CredentialProxyRuntime
from core.proxy.seed_session import build_session, seed_from_payload
from infrastructure import db
from infrastructure.repositories.resource_pool_repository import ResourcePoolRepository
from services.mailat_protocol_bind_runner import normalize_oauth_callback_mode


PROJECT_ROOT = Path(__file__).resolve().parents[1]


_ACCOUNT_LOCKS: dict[str, threading.Lock] = {}
_ACCOUNT_LOCKS_GUARD = threading.Lock()



class AccountOperationBusy(RuntimeError):
    """Raised when an account already has a long-running operation in progress."""

EXPORT_FIELD_DEFINITIONS: tuple[dict[str, str], ...] = tuple(
    {"key": key, "label": label, "description": desc}
    for key, label, desc in [
        ("schema_version", "导出格式版本", "用于识别账号导出 JSON 结构版本。"),
        ("account_key", "账号键", "数据库账号唯一键。"),
        ("account_id", "账号 ID", "平台返回的账号唯一标识。"),
        ("platform", "平台", "账号所属平台。"),
        ("login_identifier", "登录账号", "邮箱、手机号或账号键。"),
        ("phone_number", "手机号", "注册账号时使用的手机号。"),
        ("email", "邮箱", "账号登录邮箱。"),
        ("password", "密码", "账号登录密码。"),
        ("display_name", "昵称", "账号展示名。"),
        ("registration_mode", "注册来源", "手机号注册、邮箱注册或导入来源。"),
        ("registration_status", "注册状态", "注册生命周期状态。"),
        ("registration_task_id", "注册任务 ID", "创建账号的注册任务 ID。"),
        ("registration_started_at", "注册开始时间", "注册任务开始时间。"),
        ("registration_completed_at", "注册完成时间", "注册完成时间。"),
        ("registration_error", "注册错误", "注册失败原因。"),
        ("plan_type", "套餐类型", "账号当前套餐类型，例如 free 或 plus。"),
        ("plus_status", "Plus 状态", "Plus 自动校验或手动确认状态。"),
        ("plus_verified_at", "Plus 校验时间", "Plus 校验或确认时间。"),
        ("plus_check_source", "Plus 校验来源", "订阅接口来源。"),
        ("plus_check_error", "Plus 校验错误", "Plus 校验失败原因。"),
        ("binding_status", "绑定状态", "OAuth/CPA 绑定状态。"),
        ("binding_task_id", "绑定任务 ID", "绑定任务 ID。"),
        ("binding_provider", "绑定手机号池", "绑定阶段使用的手机号 provider。"),
        ("binding_phone_number", "绑定手机号", "绑定阶段使用的手机号。"),
        ("registration_sms_api_url", "注册接码 API", "注册阶段手机号的接码/取码 API 地址。"),
        ("binding_sms_api_url", "绑定接码 API", "绑定阶段手机号的接码/取码 API 地址。"),
        ("sms_api_url", "接码 API", "优先返回绑定阶段接码 API；没有绑定号码时返回注册阶段接码 API。"),
        ("binding_started_at", "绑定开始时间", "绑定任务开始时间。"),
        ("binding_completed_at", "绑定完成时间", "绑定完成或 CPA 提交时间。"),
        ("binding_error", "绑定错误", "绑定失败原因。"),
        ("oauth_callback_mode", "OAuth 回调模式", "cpa 或 local。"),
        ("cpa_base_url", "CPA 地址", "CPA API 地址。"),
        ("cpa_submitted_at", "CPA 提交时间", "callback 提交到 CPA 的时间。"),
        ("cpa_submit_status", "CPA 提交状态", "CPA callback 提交状态。"),
        ("cpa_submit_error", "CPA 提交错误", "CPA callback 提交错误。"),
        ("access_token", "Access Token", "ChatGPT/OpenAI access token。"),
        ("cpa_auth_file_name", "CPA Auth 文件名", "CPAPlus/CLIProxyAPI auth file 文件名。"),
        ("cpa_auth_file_json", "CPA Auth 文件 JSON", "从 CPAPlus/CLIProxyAPI 同步回本地的 auth file 内容。"),
        ("cpa_synced_at", "CPA Token 同步时间", "本地同步 CPA auth file 的时间。"),
        ("cpa_sync_error", "CPA Token 同步错误", "同步 CPA auth file 失败原因。"),
        ("refresh_token", "Refresh Token", "本地 OAuth 模式获取到的 refresh token；CPA 模式通常不在本地保存。"),
        ("id_token", "ID Token", "OpenAI OAuth id token。"),
        ("chatgpt_access_token_initial", "初始 ChatGPT Access Token", "注册后从 ChatGPT session 提取的 access token。"),
        ("token_expires_at", "Token 过期时间", "Token 过期时间。"),
        ("oauth_result", "OAuth 结果", "OAuth 返回 token 结构。"),
        ("proxy", "代理信息", "注册和校验使用的代理信息。"),
        ("sms", "短信信息", "短信激活信息。"),
        ("resume_file", "Resume 文件", "继续绑定使用的 resume 文件。"),
        ("storage_file", "浏览器状态文件", "浏览器 storage state 文件。"),
        ("account_file", "账号来源文件", "导入或生成账号文件路径。"),
        ("created_at", "创建时间", "账号记录创建时间。"),
        ("updated_at", "更新时间", "账号记录更新时间。"),
        ("completed_at", "完成时间", "账号完成注册或最后确认完成的时间。"),
        ("stage", "账号阶段", "兼容旧阶段字段。"),
        ("status", "账号状态", "兼容旧状态字段。"),
        ("last_error", "最后错误", "最后一次错误信息。"),
    ]
)


def _canonical_key(key: str) -> str:
    return str(key or "").strip()


@contextmanager
def account_operation_lock(key: str, *, blocking: bool = True) -> Iterator[None]:
    canonical = _canonical_key(key)
    with _ACCOUNT_LOCKS_GUARD:
        lock = _ACCOUNT_LOCKS.get(canonical)
        if lock is None:
            lock = threading.Lock()
            _ACCOUNT_LOCKS[canonical] = lock
    acquired = lock.acquire(blocking=blocking)
    if not acquired:
        raise AccountOperationBusy(canonical)
    try:
        yield
    finally:
        lock.release()


class AccountsService:
    def __init__(self, repo: AccountsRepository | None = None, config_service: ConfigService | None = None):
        self.repo = repo or AccountsRepository()
        self.config_service = config_service or ConfigService()
    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _decode_access_token_payload(self, token: str) -> dict[str, Any]:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        try:
            data = base64.urlsafe_b64decode(payload.encode("ascii"))
            parsed = json.loads(data.decode("utf-8"))
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    def _token_fingerprint(self, token: str) -> str:
        return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()[:16]

    def _looks_like_access_token(self, value: str) -> bool:
        token = str(value or "").strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        return token.startswith("eyJ") and token.count(".") >= 2 and len(token) > 100

    def _extract_access_tokens_from_value(self, value: Any) -> list[str]:
        found: list[str] = []
        if isinstance(value, dict):
            preferred_keys = {
                "access_token",
                "accesstoken",
                "accessToken",
                "token",
                "chatgpt_access_token_initial",
                "chatgptAccessTokenInitial",
            }
            for key, item in value.items():
                if str(key) in preferred_keys and isinstance(item, str) and self._looks_like_access_token(item):
                    found.append(item[7:].strip() if item.lower().startswith("bearer ") else item.strip())
                found.extend(self._extract_access_tokens_from_value(item))
        elif isinstance(value, list):
            for item in value:
                found.extend(self._extract_access_tokens_from_value(item))
        elif isinstance(value, str):
            text = value.strip()
            if self._looks_like_access_token(text):
                found.append(text[7:].strip() if text.lower().startswith("bearer ") else text)
            for match in re.findall(r"(eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)", text):
                if self._looks_like_access_token(match):
                    found.append(match.strip())
        return found

    def _parse_at_import_text(self, text: str) -> list[dict[str, Any]]:
        raw = str(text or "").strip()
        if not raw:
            return []

        records: list[dict[str, Any]] = []
        parsed_json: Any = None
        try:
            parsed_json = json.loads(raw)
        except Exception:
            parsed_json = None

        if parsed_json is not None:
            for token in self._extract_access_tokens_from_value(parsed_json):
                records.append({"access_token": token, "source": "json"})

        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            tokens = self._extract_access_tokens_from_value(line)
            if not tokens:
                continue
            separators = "----" if "----" in line else ("---" if "---" in line else None)
            pieces = [part.strip() for part in line.split(separators)] if separators else []
            email = next((part for part in pieces if "@" in part and "." in part), "")
            password = ""
            if email and len(pieces) > 1:
                try:
                    idx = pieces.index(email)
                    if idx + 1 < len(pieces) and not self._looks_like_access_token(pieces[idx + 1]):
                        password = pieces[idx + 1]
                except ValueError:
                    password = ""
            for token in tokens:
                records.append({
                    "access_token": token,
                    "email": email,
                    "password": password,
                    "source": "line",
                })

        unique: dict[str, dict[str, Any]] = {}
        for record in records:
            token = str(record.get("access_token") or "").strip()
            if token:
                unique[token] = {**unique.get(token, {}), **record, "access_token": token}
        return list(unique.values())

    def import_at_accounts(self, text: str) -> dict[str, Any]:
        parsed = self._parse_at_import_text(text)
        if not parsed:
            return {"ok": False, "imported": 0, "items": [], "message": "No valid access token found."}

        imported: list[dict[str, Any]] = []
        for item in parsed:
            token = str(item.get("access_token") or "").strip()
            payload = self._decode_access_token_payload(token)
            profile = payload.get("https://api.openai.com/profile") if isinstance(payload.get("https://api.openai.com/profile"), dict) else {}
            auth = payload.get("https://api.openai.com/auth") if isinstance(payload.get("https://api.openai.com/auth"), dict) else {}
            email = str(item.get("email") or profile.get("email") or "").strip()
            account_id = str(auth.get("chatgpt_account_id") or auth.get("chatgpt_account_user_id") or payload.get("sub") or "").strip()
            plan_type = str(auth.get("chatgpt_plan_type") or "").strip() or "free"
            account_key = account_id or email or f"at-{self._token_fingerprint(token)}"
            exp = payload.get("exp")
            token_expires_at = ""
            if exp is not None:
                try:
                    token_expires_at = datetime.fromtimestamp(int(exp), timezone.utc).isoformat()
                except Exception:
                    token_expires_at = ""

            record = {
                "account_key": account_key,
                "account_id": account_id,
                "email": email,
                "billing_email": email,
                "codex_email": email,
                "password": str(item.get("password") or ""),
                "plan_type": plan_type,
                "status": "email_registered",
                "stage": "email_registered",
                "registration_mode": "at_import",
                "registration_status": "registered",
                "plus_status": "verified_plus" if plan_type.lower() in {"plus", "pro", "team", "business", "enterprise", "paid"} else "needs_plus",
                "binding_status": "pending",
                "account_file": "manual_at_import",
                "access_token": token,
                "chatgpt_access_token_initial": token,
                "token_expires_at": token_expires_at,
            }
            saved = self.repo.upsert(record).to_dict()
            summary = {
                "account_key": saved.get("account_key") or account_key,
                "key": saved.get("key") or saved.get("account_key") or account_key,
                "account_id": saved.get("account_id") or account_id,
                "email": saved.get("email") or email,
                "plan_type": saved.get("plan_type") or plan_type,
                "status": saved.get("status") or "email_registered",
                "registration_status": saved.get("registration_status") or "registered",
            }
            imported.append(summary)
            try:
                db.add_account_event(str(summary["account_key"]), "at_imported", status="registered", message="Imported from manual AT text")
            except Exception:
                pass

        return {"ok": True, "imported": len(imported), "items": imported}



    def list_accounts(self, query: AccountQuery | None = None) -> list[dict]:
        return [item.to_dict() for item in self.repo.list(query)]

    def get_account(self, key: str) -> dict:
        return self.repo.get(key).to_dict()

    def mark_plus(self, key: str) -> dict:
        with account_operation_lock(key):
            account = self.repo.get(key).to_dict()
            if not account.get("account_key"):
                return {}
            account["stage"] = "manual_plus_confirmed"
            account["status"] = "manual_plus_confirmed"
            account["plus_status"] = "manual_confirmed"
            account["plan_type"] = "plus"
            account["plus_verified_at"] = self._now()
            account["plus_check_error"] = ""
            if not account.get("binding_status") or account.get("binding_status") == "not_ready":
                account["binding_status"] = "pending"
            db.add_account_event(account["account_key"], "plus_marked_manual", status="manual_confirmed", message="手动确认 Plus")
            return self.repo.upsert(account).to_dict()
    def verify_plus(self, key: str, *, proxy_region: str = "JP", max_attempts_override: int | None = None, retry_interval_override: int | None = None, wait_for_lock: bool = False) -> tuple[dict, int]:
        try:
            with account_operation_lock(key, blocking=wait_for_lock):
                account = self.repo.get(key).to_dict()
                if not account.get("account_key"):
                    return {"ok": False, "message": "未找到账号"}, 404
                normalized_proxy_region = self._normalize_subscription_proxy_region(proxy_region)
                if not normalized_proxy_region:
                    return {"ok": False, "message": "Plus 校验代理出口地区必须是两个大写字母的国家代码"}, 400

                access_token = self._account_access_token(account)
                if not access_token:
                    account["last_error"] = "缺少 access_token，无法自动校验 Plus"
                    account["plus_status"] = "check_failed"
                    account["plus_check_error"] = account["last_error"]
                    saved = self.repo.upsert(account).to_dict()
                    db.add_account_event(account["account_key"], "plus_check_failed", status="check_failed", message=account["last_error"])
                    return {"ok": False, "message": account["last_error"], "error_code": "missing_access_token", "account": saved}, 400

                try:
                    from platforms.chatgpt.payment import fetch_subscription_status_details
                except Exception as exc:
                    account["last_error"] = f"payment 依赖不可用: {exc}"
                    account["plus_status"] = "check_failed"
                    account["plus_check_error"] = account["last_error"]
                    saved = self.repo.upsert(account).to_dict()
                    db.add_account_event(account["account_key"], "plus_check_failed", status="check_failed", message=account["last_error"])
                    return {"ok": False, "message": account["last_error"], "error_code": "payment_unavailable", "account": saved}, 503

                details: dict[str, Any] | None = None
                paid_plans = {"plus", "pro", "premium", "paid", "team", "business", "enterprise"}
                attempt_limit = 5
                if max_attempts_override is not None:
                    attempt_limit = max(1, int(max_attempts_override))
                proxies = self._subscription_proxies(account, proxy_region=normalized_proxy_region, limit=attempt_limit)[:attempt_limit]
                if not proxies:
                    account["last_error"] = f"Plus 校验失败: 当前没有可用 {normalized_proxy_region} 代理"
                    account["plus_status"] = "check_failed"
                    account["plus_check_error"] = account["last_error"]
                    saved = self.repo.upsert(account).to_dict()
                    db.add_account_event(account["account_key"], "plus_check_failed", status="check_failed", message=account["last_error"])
                    return {"ok": False, "message": account["last_error"], "error_code": "proxy_unavailable", "account": saved}, 502

                errors: list[str] = []
                auth_error = ""
                for proxy in proxies:
                    bridge_runtime = CredentialProxyRuntime({"lajiao_proxy_credential_protocol": "socks5"}, log_fn=lambda _message: None)
                    runtime_proxy = bridge_runtime.runtime_url(proxy) if hasattr(bridge_runtime, "runtime_url") else proxy
                    request_proxy = bridge_runtime.start_browser_bridge(runtime_proxy)
                    try:
                        details = fetch_subscription_status_details(
                            SimpleNamespace(
                                access_token=access_token,
                                chatgpt_account_id=str(account.get("account_id") or ""),
                                cookies="",
                                extra={"id_token": self._account_token(account, "id_token")},
                            ),
                            proxy=request_proxy,
                        )
                        break
                    except Exception as exc:
                        status_code = self._http_status_from_exception(exc)
                        if status_code == 401:
                            auth_error = "Plus 校验失败: access_token 无效或无权限"
                            errors.append(f"{request_proxy}: {exc}")
                            break
                        self._report_subscription_proxy_failure(proxy, str(exc))
                        errors.append(f"{request_proxy}: {exc}")
                    finally:
                        bridge_runtime.cleanup()

                if auth_error:
                    account["last_error"] = auth_error
                    account["plus_status"] = "banned"
                    account["plan_type"] = "banned"
                    account["plus_check_error"] = account["last_error"]
                    saved = self.repo.upsert(account).to_dict()
                    db.add_account_event(account["account_key"], "plus_check_failed", status="banned", message=account["last_error"], payload={"error_code": "auth_failed", "proxy_region": normalized_proxy_region})
                    return {"ok": False, "message": account["last_error"], "error_code": "auth_failed", "account": saved}, 401

                if details is None:
                    account["last_error"] = "Plus 校验失败: " + " | ".join(errors)
                    account["plus_status"] = "check_failed"
                    account["plus_check_error"] = account["last_error"]
                    saved = self.repo.upsert(account).to_dict()
                    db.add_account_event(account["account_key"], "plus_check_failed", status="check_failed", message=account["last_error"])
                    return {"ok": False, "message": account["last_error"], "error_code": "proxy_failed", "account": saved}, 502

                plan = str(details.get("status") or "free").strip().lower() or "free"
                source = str(details.get("source") or "unknown")
                account["plan_type"] = plan
                account["last_error"] = ""
                account["plus_verified_at"] = self._now()
                account["plus_check_source"] = source
                account["plus_check_error"] = ""
                if plan in paid_plans:
                    account["plus_status"] = "verified_plus"
                    if account.get("stage") not in {"complete", "cpa_bound"}:
                        account["stage"] = "plus_verified_needs_oauth"
                        account["status"] = "plus_verified_needs_oauth"
                        account["binding_status"] = "pending"
                else:
                    account["plus_status"] = "free"
                    if account.get("stage") not in {"complete", "cpa_bound"}:
                        account["stage"] = "manual_plus_required"
                        account["status"] = "manual_plus_required"
                        account["binding_status"] = "not_ready"
                proxy_info = account.get("proxy") if isinstance(account.get("proxy"), dict) else {}
                proxy_info["subscription_check_source"] = source
                proxy_info["subscription_check_region"] = normalized_proxy_region
                account["proxy"] = proxy_info
                saved = self.repo.upsert(account).to_dict()
                db.add_account_event(account["account_key"], "plus_check_succeeded", status=str(account.get("plus_status") or ""), message=plan, payload={"source": source, "proxy_region": normalized_proxy_region})
                return {"ok": True, "plan_type": plan, "source": source, "paid": plan != "free", "proxy_region": normalized_proxy_region, "account": saved}, 200
        except AccountOperationBusy:
            return {"ok": False, "message": "账号正在校验，请稍后重试", "error_code": "account_busy"}, 409

    def verify_plus_batch(self, keys: Iterable[str], *, proxy_region: str = "JP") -> dict:
        keys_list: list[str] = []
        seen: set[str] = set()
        for raw_key in keys:
            key = _canonical_key(str(raw_key or ""))
            if not key or key in seen:
                continue
            seen.add(key)
            keys_list.append(key)
        if not keys_list:
            return {"ok": False, "checked": 0, "paid": 0, "results": []}

        # Prefer Go multi-worker path (32–100 concurrent HTTP). Python ThreadPool
        # is only a fallback when the worker is down or rejects the request.
        go_result = self._verify_plus_batch_via_go(keys_list, proxy_region=proxy_region)
        if go_result is not None:
            return go_result

        results_by_key: dict[str, dict] = {}
        max_workers = max(1, min(32, len(keys_list)))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(self.verify_plus, key, proxy_region=proxy_region, max_attempts_override=2, retry_interval_override=2): key
                for key in keys_list
            }
            for future in as_completed(futures):
                key = futures[future]
                try:
                    payload, status_code = future.result()
                except Exception as exc:
                    payload, status_code = {"ok": False, "message": str(exc)}, 500
                results_by_key[key] = {"key": key, "status_code": status_code, **payload}
        results = [results_by_key[key] for key in keys_list]
        return {
            "ok": all(item.get("ok") for item in results),
            "checked": len(results),
            "paid": sum(1 for item in results if item.get("paid")),
            "results": results,
            "backend": "python",
        }

    def _verify_plus_batch_via_go(
        self,
        keys_list: list[str],
        *,
        proxy_region: str = "JP",
        bridge_urls: list[str] | None = None,
        bridge_runtimes: list[Any] | None = None,
        own_bridges: bool | None = None,
        timeout_ms: int = 12000,
    ) -> dict | None:
        """Build items + shared HTTP bridge, call Go /v2/plus-verify, persist results.

        When ``bridge_urls`` is provided (progressive multi-chunk verify), bridges are
        reused across chunks and NOT cleaned up here (caller owns lifecycle).
        """
        try:
            from services.go_plus_verify_runner import check_go_plus_verify_available, go_plus_verify_batch
        except Exception:
            return None

        cfg = {}
        try:
            cfg = self.config_service.merged_config() if hasattr(self, "config_service") and self.config_service else ConfigService().merged_config()
        except Exception:
            cfg = {}

        try:
            health = check_go_plus_verify_available(cfg, timeout=2.0)
            features = health.get("_features") or health.get("features") or []
            # New worker advertises plus-verify; older builds 404 the route and we fall back.
            if features and "plus-verify" not in {str(x) for x in features}:
                return None
        except Exception:
            return None

        region = self._normalize_subscription_proxy_region(proxy_region) or "JP"
        # Shared-bridge mode (progressive chunks): caller owns lifecycle.
        shared_bridges = bool(bridge_urls)
        if own_bridges is None:
            own_bridges = not shared_bridges

        # Pull several distinct proxies so network failures can 换代理/换 sid 重试.
        pool_proxies = self._pool_proxy_candidates(proxy_region=region, limit=max(12, min(48, len(keys_list) + 8)))
        if not pool_proxies:
            pool_proxies = [""]

        def _rotate_proxy_sid(proxy_value: str) -> str:
            """Rewrite lajiao/kookeey-style sid_XXXXXXXX so exit IP changes on retry."""
            import re
            import random

            text = str(proxy_value or "").strip()
            if not text:
                return text
            if re.search(r"sid_\d+", text, flags=re.IGNORECASE):
                new_sid = str(random.randint(10_000_000, 99_999_999))
                return re.sub(r"sid_\d+", f"sid_{new_sid}", text, count=1, flags=re.IGNORECASE)
            return text

        # Build 3 local HTTP CONNECT bridges across different upstreams / sids.
        # Go retries walk this list on connection timeout / upstream reset.
        # When shared bridge_urls are passed in, reuse them (no rebuild / no cleanup).
        local_bridge_runtimes: list[Any] = list(bridge_runtimes or [])
        local_bridge_urls: list[str] = [str(u).strip() for u in (bridge_urls or []) if str(u or "").strip()]
        if not local_bridge_urls:
            upstream_candidates: list[str] = []
            for raw in pool_proxies:
                if raw and raw not in upstream_candidates:
                    upstream_candidates.append(raw)
                if len(upstream_candidates) >= 3:
                    break
            if not upstream_candidates:
                upstream_candidates = [""]

            # Prefer distinct upstreams; if only one, synthesize sid rotations.
            attempt_upstreams: list[str] = []
            for up in upstream_candidates:
                attempt_upstreams.append(up)
            while len(attempt_upstreams) < 3 and upstream_candidates[0]:
                rotated = _rotate_proxy_sid(upstream_candidates[0])
                if rotated and rotated not in attempt_upstreams:
                    attempt_upstreams.append(rotated)
                else:
                    break

            for up in attempt_upstreams:
                if not up:
                    continue
                runtime = CredentialProxyRuntime({"lajiao_proxy_credential_protocol": "socks5"}, log_fn=lambda _m: None)
                try:
                    runtime_proxy = runtime.runtime_url(up) if hasattr(runtime, "runtime_url") else up
                    bridge_url = runtime.start_browser_bridge(runtime_proxy)
                    # Go only accepts http(s) CONNECT (local bridge). Raw socks5 without
                    # credentials is returned unchanged by start_browser_bridge and must be dropped.
                    if (
                        bridge_url
                        and str(bridge_url).startswith("http://127.0.0.1:")
                        and bridge_url not in local_bridge_urls
                    ):
                        local_bridge_runtimes.append(runtime)
                        local_bridge_urls.append(bridge_url)
                    else:
                        try:
                            runtime.cleanup()
                        except Exception:
                            pass
                except Exception:
                    try:
                        runtime.cleanup()
                    except Exception:
                        pass

        if not local_bridge_urls:
            # No usable local bridge → refuse Go path so Python fallback (or fail fast)
            # can surface a clear proxy error instead of 1500× proxy_invalid.
            return None

        primary_bridge = local_bridge_urls[0]
        alt_bridges = local_bridge_urls[1:] if len(local_bridge_urls) > 1 else []


        items: list[dict[str, Any]] = []
        accounts_by_key: dict[str, dict[str, Any]] = {}
        # Bulk-load account rows + credentials in one connection (list_accounts strips token values).
        try:
            key_set = set(keys_list)
            with db.connect(getattr(self.repo, "db_path", None)) as conn:
                rows = conn.execute(
                    """
                    SELECT a.*, c.access_token, c.refresh_token, c.id_token, c.chatgpt_access_token_initial
                    FROM accounts a
                    LEFT JOIN account_credentials c ON c.account_id_ref=a.id
                    WHERE a.account_key IN ({placeholders})
                    """.format(placeholders=",".join("?" for _ in keys_list)),
                    tuple(keys_list),
                ).fetchall()
                for row in rows:
                    account = dict(row)
                    ak = str(account.get("account_key") or "").strip()
                    if not ak or ak not in key_set:
                        continue
                    account["tokens"] = {
                        "access_token": str(account.pop("access_token", "") or ""),
                        "refresh_token": str(account.pop("refresh_token", "") or ""),
                        "id_token": str(account.pop("id_token", "") or ""),
                        "chatgpt_access_token_initial": str(account.pop("chatgpt_access_token_initial", "") or ""),
                    }
                    accounts_by_key[ak] = account
        except Exception:
            accounts_by_key = {}

        for idx, key in enumerate(keys_list):
            account = accounts_by_key.get(key)
            if not account:
                account = self.repo.get(key).to_dict()
                if account.get("account_key"):
                    accounts_by_key[key] = account
            # Spread load across local bridges (each → distinct bestgo SID).
            # Putting all N on bridge[0] saturates one exit and freezes the chunk.
            bridge = local_bridge_urls[idx % len(local_bridge_urls)]
            alts = [u for u in local_bridge_urls if u != bridge]
            if not account or not account.get("account_key"):
                items.append({"key": key, "access_token": "", "account_id": "", "proxy": bridge, "proxies": alts})
                continue
            token = self._account_access_token(account)
            items.append(
                {
                    "key": key,
                    "access_token": token,
                    "account_id": str(account.get("account_id") or ""),
                    "proxy": bridge,
                    "proxies": alts,
                }
            )

        workers = max(1, min(100, len(items), int(cfg.get("max_plus_verify_workers") or 64) or 64))
        try:
            raw = go_plus_verify_batch(
                items,
                workers=workers,
                timeout_ms=max(3000, min(int(timeout_ms or 12000), 60000)),
                config=cfg,
            )
        except Exception:
            if own_bridges:
                for runtime in local_bridge_runtimes:
                    try:
                        runtime.cleanup()
                    except Exception:
                        pass
            return None
        finally:
            if own_bridges:
                for runtime in local_bridge_runtimes:
                    try:
                        runtime.cleanup()
                    except Exception:
                        pass

        go_results = raw.get("results") if isinstance(raw.get("results"), list) else []
        by_key = {str(item.get("key") or ""): item for item in go_results if isinstance(item, dict)}
        results: list[dict[str, Any]] = []
        paid_plans = {"plus", "pro", "premium", "paid", "team", "business", "enterprise"}

        for key in keys_list:
            item = by_key.get(key) or {}
            account = accounts_by_key.get(key)
            if not account:
                results.append(
                    {
                        "key": key,
                        "ok": False,
                        "status_code": 404,
                        "message": "未找到账号",
                        "error_code": "not_found",
                    }
                )
                continue

            ok = bool(item.get("ok"))
            plan = str(item.get("plan_type") or ("free" if ok else "")).strip().lower() or "free"
            source = str(item.get("source") or "go/wham/usage")
            message = str(item.get("message") or "")
            error_code = str(item.get("error_code") or "")
            status_code = int(item.get("status_code") or (200 if ok else 502))

            if ok:
                account["plan_type"] = plan
                account["last_error"] = ""
                account["plus_verified_at"] = self._now()
                account["plus_check_source"] = source
                account["plus_check_error"] = ""
                if plan in paid_plans:
                    account["plus_status"] = "verified_plus"
                    if account.get("stage") not in {"complete", "cpa_bound"}:
                        account["stage"] = "plus_verified_needs_oauth"
                        account["status"] = "plus_verified_needs_oauth"
                        account["binding_status"] = "pending"
                else:
                    account["plus_status"] = "free"
                    if account.get("stage") not in {"complete", "cpa_bound"}:
                        account["stage"] = "manual_plus_required"
                        account["status"] = "manual_plus_required"
                        account["binding_status"] = "not_ready"
                proxy_info = account.get("proxy") if isinstance(account.get("proxy"), dict) else {}
                proxy_info["subscription_check_source"] = source
                proxy_info["subscription_check_region"] = region
                account["proxy"] = proxy_info
                saved = self.repo.upsert(account).to_dict()
                db.add_account_event(account["account_key"], "plus_check_succeeded", status=str(account.get("plus_status") or ""), message=plan, payload={"source": source, "proxy_region": region, "backend": "go"})
                results.append(
                    {
                        "key": key,
                        "ok": True,
                        "status_code": 200,
                        "plan_type": plan,
                        "source": source,
                        "paid": plan in paid_plans or plan != "free",
                        "proxy_region": region,
                        "account": saved,
                        "backend": "go",
                    }
                )
            else:
                if not self._account_access_token(account) and not error_code:
                    error_code = "missing_access_token"
                    message = message or "缺少 access_token，无法自动校验 Plus"
                banned = status_code == 401
                account["last_error"] = message or "Plus 校验失败"
                account["plus_status"] = "banned" if banned else "check_failed"
                if banned:
                    account["plan_type"] = "banned"
                account["plus_check_error"] = account["last_error"]
                saved = self.repo.upsert(account).to_dict()
                db.add_account_event(account["account_key"], "plus_check_failed", status=str(account.get("plus_status") or "check_failed"), message=account["last_error"], payload={"error_code": error_code, "proxy_region": region, "backend": "go"})
                results.append(
                    {
                        "key": key,
                        "ok": False,
                        "status_code": status_code,
                        "message": account["last_error"],
                        "error_code": error_code or "proxy_failed",
                        "account": saved,
                        "backend": "go",
                    }
                )

        return {
            "ok": all(item.get("ok") for item in results),
            "checked": len(results),
            "paid": sum(1 for item in results if item.get("paid")),
            "results": results,
            "backend": "go",
            "workers": int(raw.get("workers") or workers),
            "duration": str(raw.get("duration") or ""),
        }


    def _account_token(self, account: dict, token_name: str) -> str:
        tokens = account.get("tokens") if isinstance(account.get("tokens"), dict) else {}
        for raw in (tokens.get(token_name), account.get(token_name)):
            if isinstance(raw, str):
                text = raw.strip()
                if text and text.lower() not in {"true", "false"}:
                    return text
        return ""

    def _account_access_token(self, account: dict) -> str:
        return self._account_token(account, "access_token") or self._account_token(account, "chatgpt_access_token_initial")

    def archive_invalid_accounts(self) -> dict:
        archived = []
        for record in self.repo.list():
            account = record.to_dict()
            key = str(account.get("account_key") or "")
            stage = str(account.get("stage") or "").lower()
            status = str(account.get("status") or "").lower()
            has_identity = bool(account.get("email") or account.get("phone_number"))
            timestamp_key = key.startswith("202") and "T" in key
            test_key = key in {"acct_123", "+15550000000", "+15550001111", "mailbox@example.com"}
            if (not has_identity) or timestamp_key or test_key or stage in {"failed", "error"} or status in {"failed", "error"}:
                if stage == "archived" or status == "archived":
                    continue
                account["stage"] = "archived"
                account["status"] = "archived"
                account["binding_status"] = "archived"
                account["registration_status"] = "archived"
                account["last_error"] = account.get("last_error") or "invalid account archived by cleanup"
                self.repo.upsert(account)
                archived.append(key)
        return {"ok": True, "archived": len(archived), "keys": archived}
    def _read_account_artifact(self, path_value: str) -> dict[str, Any]:
        text = str(path_value or "").strip()
        if not text:
            return {}
        path = Path(text)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        try:
            resolved = path.resolve()
            root = PROJECT_ROOT.resolve()
            if root not in resolved.parents and resolved != root:
                return {}
            data = json.loads(resolved.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _proxy_from_payload(self, payload: dict[str, Any]) -> str:
        proxy = payload.get("proxy") if isinstance(payload.get("proxy"), dict) else {}
        for value in (
            proxy.get("subscription_check_proxy"),
            proxy.get("registration_proxy"),
            payload.get("subscription_check_proxy"),
            payload.get("registration_proxy"),
        ):
            text = str(value or "").strip()
            if text and "127.0.0.1" not in text and "localhost" not in text:
                return text
        return ""

    @staticmethod
    def _normalize_subscription_proxy_region(value: str) -> str:
        region = str(value or "").strip().upper()
        return region if len(region) == 2 and region.isascii() and region.isalpha() else ""

    def _subscription_proxies(self, account: dict, *, proxy_region: str = "JP", limit: int = 5) -> list[str]:
        proxies = self._pool_proxy_candidates(proxy_region=proxy_region, limit=limit)
        if len(proxies) < 2:
            return proxies
        key = str(account.get("account_key") or account.get("email") or "")
        offset = sum(ord(ch) for ch in key) % len(proxies) if key else 0
        return proxies[offset:] + proxies[:offset]

    @staticmethod
    def _proxy_row_matches_region(row: dict[str, Any], proxy_region: str) -> bool:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        direct_regions = (
            str(payload.get("region") or "").strip().upper(),
            str(payload.get("country") or "").strip().upper(),
        )
        listed_regions = str(payload.get("regions") or "").replace(",", " ").replace(";", " ").upper().split()
        return proxy_region in direct_regions or proxy_region in listed_regions

    def _pool_proxy_candidates(self, *, proxy_region: str = "JP", limit: int = 5) -> list[str]:
        candidates: list[str] = []

        def _is_junk_proxy(value: str) -> bool:
            text = str(value or "").strip().lower()
            if not text:
                return True
            # Local test placeholders / dead auth bridges — never usable for live Plus checks.
            junk_markers = (
                "proxy.local",
                ".invalid",
                "auth-0.local",
                "auth-1.local",
                "127.0.0.1",
                "localhost",
            )
            return any(marker in text for marker in junk_markers)

        try:
            seed_rows = db.list_resources(resource_type="proxy", provider="proxy_seed", status="available", path=getattr(self.repo, "db_path", None))
        except Exception:
            seed_rows = []
        for row in seed_rows:
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            has_region_hint = bool(str(payload.get("region") or payload.get("country") or payload.get("regions") or "").strip())
            if has_region_hint and not self._proxy_row_matches_region(row, proxy_region):
                continue
            # One seed can mint many sticky SIDs — expand to fill limit (bestgo-only pool often has 1 seed).
            mint_n = max(1, min(int(limit), 8))
            for _ in range(mint_n):
                try:
                    seed = seed_from_payload(payload, resource_key=str(row.get("resource_key") or ""))
                    session = build_session(seed, region=proxy_region, ttl=30)
                    value = session.url
                except Exception:
                    continue
                if value and not _is_junk_proxy(value) and value not in candidates:
                    candidates.append(value)
                if len(candidates) >= limit:
                    return candidates

        try:
            rows = db.list_resources(resource_type="proxy", provider="lajiao_credentials", status="available", path=getattr(self.repo, "db_path", None))
        except Exception:
            return candidates
        for row in rows:
            if not self._proxy_row_matches_region(row, proxy_region):
                continue
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            value = str(payload.get("url") or row.get("resource_key") or "").strip()
            if value and not _is_junk_proxy(value) and value not in candidates:
                candidates.append(value)
            if len(candidates) >= limit:
                break
        return candidates

    @staticmethod
    def _http_status_from_exception(exc: Exception) -> int:
        sources = [exc]
        try:
            response = getattr(exc, "response", None)
        except Exception:
            response = None
        if response is not None:
            sources.append(response)
        for source in sources:
            for attr in ("status_code", "status", "code"):
                try:
                    value = getattr(source, attr, None)
                except Exception:
                    continue
                try:
                    code = int(value)
                except (TypeError, ValueError):
                    continue
                if code > 0:
                    return code
        return 0

    def _report_subscription_proxy_failure(self, proxy: str, error: str) -> None:
        cooldown_until = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        try:
            rows = db.list_resources(resource_type="proxy", provider="lajiao_credentials", path=getattr(self.repo, "db_path", None))
            for row in rows:
                payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
                resource_key = str(row.get("resource_key") or "").strip()
                url = str(payload.get("url") or resource_key).strip()
                if proxy not in {resource_key, url}:
                    continue
                db.report_resource("", resource_key, success=False, cooldown_until=cooldown_until, error=error[:500], path=getattr(self.repo, "db_path", None))
                return
        except Exception:
            return

    def archive(self, key: str) -> bool:
        with account_operation_lock(key):
            return self.repo.archive(key)

    def archive_many(self, keys: Iterable[str]) -> dict:
        archived: list[str] = []
        missing: list[str] = []
        seen: set[str] = set()
        for raw_key in keys:
            key = _canonical_key(str(raw_key or ""))
            if not key or key in seen:
                continue
            seen.add(key)
            if self.archive(key):
                archived.append(key)
            else:
                missing.append(key)
        return {"ok": not missing, "archived": len(archived), "keys": archived, "missing": missing}


    def resume_oauth(self, key: str, headed: bool, tasks_service, *, oauth_callback_mode: str = "", cpa_base_url: str = "", cpa_management_key: str = "", sms_provider: str = "", sms_phone_url: str = "", sms_country: str = "", sms_service: str = "", bind_sms_provider: str = "", bind_sms_phone_url: str = "", bind_sms_country: str = "", bind_sms_service: str = "", bind_country_code: str = "") -> tuple[dict, int]:
        with account_operation_lock(key):
            account = self.repo.get(key).to_dict()
            if not account.get("account_key"):
                return {"ok": False, "message": "未找到账号"}, 404
            resume = str((account.get("paths") or {}).get("resume") or "")
            if not resume:
                return {"ok": False, "message": "缺少 resume 文件"}, 400
            task = tasks_service.start_resume({"resume_file": resume, "headed": headed, "oauth_callback_mode": oauth_callback_mode, "cpa_base_url": cpa_base_url, "cpa_management_key": cpa_management_key, "sms_provider": sms_provider, "sms_phone_url": sms_phone_url, "sms_country": sms_country, "sms_service": sms_service, "bind_sms_provider": bind_sms_provider, "bind_sms_phone_url": bind_sms_phone_url, "bind_sms_country": bind_sms_country, "bind_sms_service": bind_sms_service, "bind_country_code": bind_country_code})
            account["binding_status"] = "binding_queued"
            account["binding_task_id"] = str(task.get("id") or "")
            account["binding_provider"] = str(bind_sms_provider or "")
            account["binding_started_at"] = self._now()
            account["oauth_callback_mode"] = str(oauth_callback_mode or "")
            account["cpa_base_url"] = str(cpa_base_url or "")
            account["binding_error"] = ""
            self.repo.upsert(account)
            db.add_account_event(account["account_key"], "binding_queued", task_id=str(task.get("id") or ""), status="binding_queued", message="OAuth/CPA 绑定任务已排队", payload={"provider": bind_sms_provider, "oauth_callback_mode": oauth_callback_mode})
            return {"ok": True, "task": task}, 200

    def protocol_cpa_bind(self, key: str, tasks_service, *, oauth_callback_mode: str = "", cpa_base_url: str = "", cpa_management_key: str = "", sms_provider: str = "", sms_phone_url: str = "", sms_country: str = "", sms_service: str = "", bind_sms_provider: str = "", bind_sms_phone_url: str = "", bind_sms_country: str = "", bind_sms_service: str = "", bind_country_code: str = "") -> tuple[dict, int]:
        with account_operation_lock(key):
            account = self.repo.get(key).to_dict()
            if not account.get("account_key"):
                return {"ok": False, "message": "未找到账号"}, 404
            email = str(account.get("email") or account.get("login_identifier") or "").strip()
            password = str(account.get("password") or "").strip()
            if not email:
                return {"ok": False, "message": "该账号缺少邮箱，无法协议绑定"}, 400
            if not password:
                return {"ok": False, "message": "该账号缺少密码，无法协议绑定"}, 400
            callback_mode = normalize_oauth_callback_mode(oauth_callback_mode)
            task = tasks_service.start_protocol_cpa_bind({"account_key": account.get("account_key"), "oauth_callback_mode": callback_mode, "cpa_base_url": cpa_base_url if callback_mode == "cpa" else "", "cpa_management_key": cpa_management_key if callback_mode == "cpa" else "", "sms_provider": sms_provider, "sms_phone_url": sms_phone_url, "sms_country": sms_country, "sms_service": sms_service, "bind_sms_provider": bind_sms_provider, "bind_sms_phone_url": bind_sms_phone_url, "bind_sms_country": bind_sms_country, "bind_sms_service": bind_sms_service, "bind_country_code": bind_country_code}, defer_start=True)
            account["binding_status"] = "binding_queued"
            account["binding_task_id"] = str(task.get("id") or "")
            account["binding_provider"] = str(bind_sms_provider or "")
            account["binding_started_at"] = self._now()
            account["oauth_callback_mode"] = callback_mode
            if callback_mode == "cpa":
                account["cpa_base_url"] = str(cpa_base_url or "")
            account["binding_error"] = ""
            self.repo.upsert(account)
            mode_label = "本地 OAuth" if callback_mode == "local" else "CPA"
            db.add_account_event(account["account_key"], "binding_queued", task_id=str(task.get("id") or ""), status="binding_queued", message=f"协议 {mode_label} 绑定任务已排队", payload={"provider": bind_sms_provider, "oauth_callback_mode": callback_mode})
            tasks_service.drain_queue_async()
            return {"ok": True, "task": task}, 200

    def sync_cpa_token(self, key: str) -> tuple[dict, int]:
        with account_operation_lock(key):
            account = self.repo.get(key).to_dict()
            if not account.get("account_key"):
                return {"ok": False, "message": "未找到账号"}, 404
            cfg = self.config_service.merged_config()
            base_url = str(account.get("cpa_base_url") or cfg.get("cpa_base_url") or "").rstrip("/")
            admin_key = str(cfg.get("cpa_manager_admin_key") or cfg.get("cpa_management_key") or "")
            if not base_url or not admin_key:
                return {"ok": False, "message": "缺少 CPA 地址或 CPAPlus Admin Key"}, 400
            try:
                try:
                    auth_file = self._find_cpa_auth_file(base_url, admin_key, account)
                except Exception:
                    auth_file = {"name": self._candidate_cpa_auth_file_name(account)}
                file_name = str(auth_file.get("name") or auth_file.get("id") or "")
                if not file_name:
                    raise RuntimeError("CPAPlus 未返回 auth file 名称")
                raw = self._read_cpa_auth_file(file_name, cfg)
                auth_json = json.loads(raw)
                tokens = {"access_token": str(auth_json.get("access_token") or ""), "refresh_token": str(auth_json.get("refresh_token") or ""), "id_token": str(auth_json.get("id_token") or "")}
                id_claims = self._decode_jwt_payload(tokens["id_token"])
                plan_type = str(id_claims.get("https://api.openai.com/auth", {}).get("chatgpt_plan_type") or id_claims.get("chatgpt_plan_type") or account.get("plan_type") or "").lower()
                update = {"cpa_auth_file_name": file_name, "cpa_auth_file_json": raw, "cpa_synced_at": self._now(), "cpa_sync_error": "", "oauth_result": auth_json, **{k: v for k, v in tokens.items() if v}}
                if auth_json.get("account_id"):
                    update["account_id"] = str(auth_json.get("account_id") or "")
                if auth_json.get("email"):
                    update["email"] = str(auth_json.get("email") or "")
                if plan_type:
                    update["plan_type"] = plan_type
                    if plan_type in {"plus", "pro", "premium", "paid", "team", "business", "enterprise"}:
                        update["plus_status"] = "verified_plus"
                account.update(update)
                if account.get("binding_status") in {"", "pending", "binding_started", "cpa_submitted"}:
                    account["binding_status"] = "bound"
                saved = self.repo.upsert(account).to_dict()
                db.add_account_event(account["account_key"], "cpa_token_synced", status="synced", message="CPA auth file 已同步到本地", payload={"file": file_name, "has_refresh_token": bool(tokens["refresh_token"])})
                return {"ok": True, "account": saved, "file": file_name, "has_refresh_token": bool(tokens["refresh_token"])}, 200
            except Exception as exc:
                account["cpa_sync_error"] = str(exc)
                self.repo.upsert(account)
                db.add_account_event(account["account_key"], "cpa_token_sync_failed", status="failed", message=str(exc))
                return {"ok": False, "message": str(exc), "account": account}, 502

    def _find_cpa_auth_file(self, base_url: str, management_key: str, account: dict) -> dict:
        request = urllib.request.Request(f"{base_url}/v0/management/auth-files", headers={"Authorization": f"Bearer {management_key}"})
        payload = json.loads(urllib.request.urlopen(request, timeout=20).read().decode("utf-8"))
        email = str(account.get("email") or account.get("login_identifier") or "").lower()
        account_id = str(account.get("account_id") or "")
        for item in payload.get("files", []):
            blob = json.dumps(item, ensure_ascii=False).lower()
            if (email and email in blob) or (account_id and account_id in blob):
                return item
        raise RuntimeError("CPAPlus 未找到该账号的 auth file")

    def _candidate_cpa_auth_file_name(self, account: dict) -> str:
        email = str(account.get("email") or account.get("login_identifier") or "").strip()
        if not email:
            raise RuntimeError("CPAPlus 未找到 auth file，且账号缺少邮箱，无法推断文件名")
        return f"codex-{email}-plus.json"

    def _decode_jwt_payload(self, token: str) -> dict[str, Any]:
        try:
            part = str(token or "").split(".")[1]
            padded = part + "=" * (-len(part) % 4)
            return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        except Exception:
            return {}

    def _read_cpa_auth_file(self, file_name: str, cfg: dict) -> str:
        admin_key = str(cfg.get("cpa_manager_admin_key") or cfg.get("cpa_management_key") or "")
        base_url = str(cfg.get("cpa_base_url") or "").rstrip("/")
        if base_url and admin_key:
            request = urllib.request.Request(
                f"{base_url}/v0/management/auth-files/download?name={urllib.parse.quote(file_name)}",
                headers={"Authorization": f"Bearer {admin_key}"},
            )
            try:
                return urllib.request.urlopen(request, timeout=20).read().decode("utf-8")
            except Exception:
                pass
        sync_url = str(cfg.get("cpa_auth_file_sync_url") or "").rstrip("/")
        if sync_url:
            request = urllib.request.Request(f"{sync_url}?name={urllib.parse.quote(file_name)}", headers={"Authorization": f"Bearer {admin_key}"})
            return urllib.request.urlopen(request, timeout=20).read().decode("utf-8")
        ssh_host = str(cfg.get("cpa_sync_ssh_host") or "myserver")
        container = str(cfg.get("cpa_sync_container") or "cli-proxy-api")
        proc = subprocess.run(["ssh", ssh_host, "docker", "exec", container, "cat", f"/root/.cli-proxy-api/{file_name}"], capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or "SSH 读取 CPA auth file 失败；请配置 cpa_auth_file_sync_url 或 cpa_sync_ssh_host")
        return proc.stdout

    def _phone_resource_sms_url(self, resource_id: int, phone_number: str = "") -> str:
        resource_id = int(resource_id or 0)
        phone_number = str(phone_number or "").strip().lstrip("+")
        if not resource_id and not phone_number:
            return ""
        with db.connect(self.repo.db_path) as conn:
            row = None
            if resource_id:
                row = conn.execute(
                    "SELECT payload_json FROM resource_pool WHERE id=? AND resource_type='phone'",
                    (resource_id,),
                ).fetchone()
            if row is None and phone_number:
                row = conn.execute(
                    """
                    SELECT payload_json FROM resource_pool
                    WHERE resource_type='phone' AND resource_key=?
                    ORDER BY updated_at DESC, id DESC LIMIT 1
                    """,
                    (phone_number,),
                ).fetchone()
        payload = db.loads(row["payload_json"], {}) if row else {}
        if not isinstance(payload, dict):
            return ""
        return str(payload.get("sms_url") or payload.get("api_url") or payload.get("url") or "")

    def _enrich_sms_api_export_fields(self, product: dict, account: dict) -> None:
        registration_url = self._phone_resource_sms_url(
            int(account.get("registration_phone_resource_id") or 0),
            str(account.get("phone_number") or ""),
        )
        binding_url = self._phone_resource_sms_url(
            int(account.get("binding_phone_resource_id") or 0),
            str(account.get("binding_phone_number") or account.get("phone_number") or ""),
        )
        product["registration_sms_api_url"] = registration_url
        product["binding_sms_api_url"] = binding_url
        product["sms_api_url"] = binding_url or registration_url


    def available_export_fields(self) -> list[dict[str, str]]:
        return [dict(item) for item in EXPORT_FIELD_DEFINITIONS]

    def export_product(self, key: str, fields: Iterable[str] | None = None) -> dict:
        # Read-only export: no account_operation_lock. Holding per-key locks while
        # re-opening SQLite for every account made bulk export hang for minutes and
        # blocked the rest of the UI.
        account = self.repo.get(key).to_dict()
        if not account.get("account_key"):
            return {}
        product = product_export(account)
        selected = self._normalize_export_fields(fields)
        if any(field in selected for field in ("registration_sms_api_url", "binding_sms_api_url", "sms_api_url")):
            self._enrich_sms_api_export_fields(product, account)
        body = {field: product.get(field, "") for field in selected}
        descriptions = {item["key"]: {"label": item["label"], "description": item["description"]}
                        for item in EXPORT_FIELD_DEFINITIONS if item["key"] in selected}
        body["_field_descriptions"] = descriptions
        self._mark_accounts_exported([str(account.get("account_key") or key)], kind="bulk")
        return body

    def export_products(self, keys: Iterable[str], fields: Iterable[str] | None = None) -> dict[str, Any]:
        key_list: list[str] = []
        seen: set[str] = set()
        for raw_key in keys:
            key = str(raw_key or "").strip()
            if key and key not in seen:
                seen.add(key)
                key_list.append(key)
        empty_result = {"count": 0, "products": [], "exported_keys": [], "missing": []}
        if not key_list:
            return empty_result
        selected = self._normalize_export_fields(fields)
        need_sms = any(field in selected for field in ("registration_sms_api_url", "binding_sms_api_url", "sms_api_url"))
        descriptions = {item["key"]: {"label": item["label"], "description": item["description"]}
                        for item in EXPORT_FIELD_DEFINITIONS if item["key"] in selected}

        # Load selected accounts with real credentials in one pass (not UI-summary shape).
        by_key: dict[str, dict] = {}
        try:
            with db.connect(getattr(self.repo, "db_path", None)) as conn:
                chunk = 400
                for offset in range(0, len(key_list), chunk):
                    part = key_list[offset: offset + chunk]
                    placeholders = ",".join("?" for _ in part)
                    rows = conn.execute(
                        f"""
                        SELECT a.*,
                               c.access_token AS cred_access_token,
                               c.refresh_token AS cred_refresh_token,
                               c.id_token AS cred_id_token,
                               c.chatgpt_access_token_initial AS cred_chatgpt_access_token_initial,
                               c.token_expires_at AS cred_token_expires_at,
                               p.registration_proxy, p.registration_exit_ip, p.registration_country,
                               p.subscription_check_proxy, p.subscription_check_source
                        FROM accounts a
                        LEFT JOIN account_credentials c ON c.account_id_ref=a.id
                        LEFT JOIN account_proxy p ON p.account_id_ref=a.id
                        WHERE a.account_key IN ({placeholders})
                           OR a.email IN ({placeholders})
                           OR a.login_identifier IN ({placeholders})
                        """,
                        tuple(part) * 3,
                    ).fetchall()
                    for row in rows:
                        account = dict(row)
                        account["tokens"] = {
                            "access_token": str(account.pop("cred_access_token", "") or ""),
                            "refresh_token": str(account.pop("cred_refresh_token", "") or ""),
                            "id_token": str(account.pop("cred_id_token", "") or ""),
                            "chatgpt_access_token_initial": str(account.pop("cred_chatgpt_access_token_initial", "") or ""),
                            "token_expires_at": str(account.pop("cred_token_expires_at", "") or ""),
                        }
                        account["proxy"] = {
                            "registration_proxy": str(account.pop("registration_proxy", "") or ""),
                            "registration_exit_ip": str(account.pop("registration_exit_ip", "") or ""),
                            "registration_country": str(account.pop("registration_country", "") or ""),
                            "subscription_check_proxy": str(account.pop("subscription_check_proxy", "") or ""),
                            "subscription_check_source": str(account.pop("subscription_check_source", "") or ""),
                        }
                        for ak in (
                            str(account.get("account_key") or "").strip(),
                            str(account.get("email") or "").strip(),
                            str(account.get("login_identifier") or "").strip(),
                        ):
                            if ak:
                                by_key[ak] = account
        except Exception:
            by_key = {}

        products: list[dict] = []
        exported_keys: list[str] = []
        missing: list[str] = []
        for key in key_list:
            account = by_key.get(key)
            if not account:
                # Fallback for keys not present in list_accounts shape.
                account = self.repo.get(key).to_dict()
            if not account or not (account.get("account_key") or account.get("key")):
                missing.append(key)
                continue
            product = product_export(account)
            if need_sms:
                self._enrich_sms_api_export_fields(product, account)
            body = {field: product.get(field, "") for field in selected}
            body["_field_descriptions"] = descriptions
            products.append(body)
            exported_keys.append(str(account.get("account_key") or key))
        if products:
            # 批量导出 JSON = 已批量导出
            self._mark_accounts_exported(exported_keys, kind="bulk")
        return {
            "count": len(products),
            "products": products,
            "exported_keys": exported_keys,
            "missing": missing,
        }

    def _mark_accounts_exported(self, keys: Iterable[str], *, kind: str) -> int:
        """Persist export status for UI.

        kind:
          - bulk  -> 已批量导出 (JSON 字段导出)
          - plus  -> 已导出plus成品 (Plus TXT 成品号)
          - at    -> 已导出AT成品 (邮箱四段 + access_token)
        """
        key_list = [str(k or "").strip() for k in keys if str(k or "").strip()]
        if not key_list:
            return 0
        kind_norm = str(kind or "").strip().lower()
        if kind_norm not in {"bulk", "plus", "at"}:
            kind_norm = "bulk"
        status = {
            "plus": "plus_exported",
            "at": "at_exported",
        }.get(kind_norm, "bulk_exported")
        now = self._now()
        updated = 0
        try:
            with db.connect(getattr(self.repo, "db_path", None)) as conn:
                # SQLite variable limit is high enough for hundreds; chunk for safety.
                chunk = 400
                for offset in range(0, len(key_list), chunk):
                    part = key_list[offset: offset + chunk]
                    placeholders = ",".join("?" for _ in part)
                    cur = conn.execute(
                        f"""
                        UPDATE accounts
                        SET export_status=?,
                            export_kind=?,
                            exported_at=?,
                            updated_at=?
                        WHERE account_key IN ({placeholders})
                        """,
                        (status, kind_norm, now, now, *part),
                    )
                    updated += int(cur.rowcount or 0)
            # Avoid N event inserts on large bulk exports (status column is enough for UI).
            if len(key_list) <= 20:
                for key in key_list:
                    try:
                        msg = {
                            "plus": "已导出plus成品",
                            "at": "已导出AT成品",
                        }.get(kind_norm, "已批量导出")
                        db.add_account_event(
                            key,
                            "account_exported",
                            status=status,
                            message=msg,
                            payload={"export_kind": kind_norm},
                            path=getattr(self.repo, "db_path", None),
                        )
                    except Exception:
                        pass
        except Exception:
            return updated
        return updated

    def _normalize_export_fields(self, fields: Iterable[str] | None) -> list[str]:
        allowed = {item["key"] for item in EXPORT_FIELD_DEFINITIONS}
        selected = [str(field or "").strip() for field in (fields or [])]
        result = [field for field in selected if field in allowed]
        return result or [item["key"] for item in EXPORT_FIELD_DEFINITIONS]

    _PLUS_EXPORT_STATUSES = frozenset({"verified_plus", "manual_confirmed"})
    _PLUS_EXPORT_PLANS = frozenset({"plus", "pro", "premium", "paid", "team", "business", "enterprise"})
    _OUTLOOK_DOMAINS = frozenset({"outlook.com", "hotmail.com", "live.com", "msn.com"})

    def _is_plus_product_account(self, account: dict[str, Any]) -> bool:
        plus_status = str(account.get("plus_status") or "").strip().lower()
        plan_type = str(account.get("plan_type") or "").strip().lower()
        return plus_status in self._PLUS_EXPORT_STATUSES or plan_type in self._PLUS_EXPORT_PLANS
    def _is_archived_account(self, account: dict[str, Any]) -> bool:
        archived_markers = {"archived", "deleted"}
        return any(
            str(account.get(field) or "").strip().lower() in archived_markers
            for field in ("stage", "status", "registration_status", "binding_status")
        )


    def _account_email(self, account: dict[str, Any]) -> str:
        return str(account.get("email") or account.get("outlook_email") or account.get("login_identifier") or "").strip()

    def _account_password(self, account: dict[str, Any]) -> str:
        return str(account.get("password") or account.get("generated_chatgpt_password") or "").strip()

    def _email_domain(self, email: str) -> str:
        value = str(email or "").strip().lower()
        if "@" not in value:
            return ""
        return value.rsplit("@", 1)[-1]

    def _load_email_resource(self, email: str, provider: str, cache: dict[tuple[str, str], dict[str, Any]] | None = None) -> dict[str, Any]:
        email_key = str(email or "").strip().lower()
        if not email_key:
            return {}
        if cache is not None:
            row = cache.get((provider, email_key))
            if row:
                return row
            # Some imports keep original casing as resource_key.
            return cache.get((provider, str(email or "").strip())) or {}
        repo = ResourcePoolRepository(getattr(self.repo, "db_path", None))
        row = repo.get("email", provider, email_key)
        if not row:
            # Some imports keep original casing as resource_key.
            row = repo.get("email", provider, str(email or "").strip())
        return row if isinstance(row, dict) else {}

    def _email_resource_cache(self) -> dict[tuple[str, str], dict[str, Any]]:
        """One-shot load of email pool rows. Export used to hit SQLite per account."""
        cache: dict[tuple[str, str], dict[str, Any]] = {}
        try:
            rows = ResourcePoolRepository(getattr(self.repo, "db_path", None)).list(resource_type="email")
        except Exception:
            return cache
        for row in rows:
            if not isinstance(row, dict):
                continue
            provider = str(row.get("provider") or "").strip()
            resource_key = str(row.get("resource_key") or "").strip()
            if not provider or not resource_key:
                continue
            # Index both original and lowercased keys so mixed-case imports still hit.
            cache[(provider, resource_key)] = row
            lower = resource_key.lower()
            if lower != resource_key:
                cache.setdefault((provider, lower), row)
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            payload_email = str(payload.get("email") or "").strip()
            if payload_email:
                cache.setdefault((provider, payload_email), row)
                cache.setdefault((provider, payload_email.lower()), row)
        return cache

    def _icloud_api_line(self, email: str, payload: dict[str, Any]) -> str:
        parts = [email]
        inbox_url = str(payload.get("inbox_url") or "").strip()
        code_url = str(payload.get("code_url") or "").strip()
        mail_url = str(payload.get("mail_url") or "").strip()
        if inbox_url:
            parts.append(inbox_url)
        if code_url:
            parts.append(code_url if code_url.lower().startswith("code:") else f"code:{code_url}")
        if mail_url:
            parts.append(mail_url if mail_url.lower().startswith("mail:") else f"mail:{mail_url}")
        if len(parts) < 2:
            return ""
        return "----".join(parts)

    def _outlook_token_line(self, email: str, payload: dict[str, Any]) -> str:
        outlook_password = str(payload.get("password") or "").strip()
        client_id = str(payload.get("client_id") or "").strip()
        refresh_token = str(payload.get("refresh_token") or "").strip()
        if not (email and outlook_password and client_id and refresh_token):
            return ""
        # OT order: email----Outlook登录密码----client_id----refresh_token
        return "----".join([email, outlook_password, client_id, refresh_token])

    def _format_plus_product_line(
        self,
        account: dict[str, Any],
        *,
        resource_cache: dict[tuple[str, str], dict[str, Any]] | None = None,
    ) -> tuple[str, str, str]:
        """Return (line, kind, error). Empty line means skip."""
        email = self._account_email(account)
        if not email:
            return "", "", "缺少邮箱"

        domain = self._email_domain(email)
        if domain in self._OUTLOOK_DOMAINS or domain.endswith(".outlook.com"):
            row = self._load_email_resource(email, "outlook_token", resource_cache)
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            line = self._outlook_token_line(email, payload)
            if not line:
                return "", "outlook", "资源池缺少 Outlook 密码/client_id/refresh_token"
            return line, "outlook", ""

        if "icloud.com" in domain or domain.endswith(".icloud.com"):
            api_row = self._load_email_resource(email, "icloud_api", resource_cache)
            api_payload = api_row.get("payload") if isinstance(api_row.get("payload"), dict) else {}
            line = self._icloud_api_line(email, api_payload)
            if line:
                return line, "icloud_api", ""
            privacy_row = self._load_email_resource(email, "icloud_privacy", resource_cache)
            if privacy_row:
                # 隐私邮箱没有 API 链接，只导出邮箱本身
                return email, "icloud_privacy", ""
            return "", "icloud", "资源池缺少 iCloud API/隐私邮箱凭据"

        # 其他邮箱：尽量按 outlook_token / icloud_api 资源探测
        outlook_row = self._load_email_resource(email, "outlook_token", resource_cache)
        if outlook_row:
            payload = outlook_row.get("payload") if isinstance(outlook_row.get("payload"), dict) else {}
            line = self._outlook_token_line(email, payload)
            if line:
                return line, "outlook", ""
        api_row = self._load_email_resource(email, "icloud_api", resource_cache)
        if api_row:
            payload = api_row.get("payload") if isinstance(api_row.get("payload"), dict) else {}
            line = self._icloud_api_line(email, payload)
            if line:
                return line, "icloud_api", ""
        return "", "unknown", f"不支持的邮箱类型: {domain or email}"

    def export_plus_products_txt(
        self,
        keys: Iterable[str] | None = None,
        *,
        only_verified: bool = True,
        archive_after_export: bool = False,
    ) -> dict[str, Any]:
        selected_keys = [str(key or "").strip() for key in (keys or []) if str(key or "").strip()]
        selected_set = set(selected_keys)

        # Bulk-load accounts + email resources once. Old path did repo.get / resource get per row (~15s for 2k accounts).
        if selected_keys:
            accounts: list[dict[str, Any]] = []
            try:
                with db.connect(getattr(self.repo, "db_path", None)) as conn:
                    placeholders = ",".join("?" for _ in selected_keys)
                    # Match either account_key or email so UI keys and emails both work.
                    rows = conn.execute(
                        f"""
                        SELECT * FROM accounts
                        WHERE account_key IN ({placeholders})
                           OR email IN ({placeholders})
                           OR login_identifier IN ({placeholders})
                           OR outlook_email IN ({placeholders})
                        """,
                        tuple(selected_keys) * 4,
                    ).fetchall()
                    accounts = [dict(row) for row in rows]
            except Exception:
                accounts = []
            if not accounts:
                # Fallback for non-sqlite / missing columns edge cases.
                for key in selected_keys:
                    account = self.repo.get(key).to_dict()
                    if account.get("account_key") or account.get("key"):
                        accounts.append(account)
        else:
            # Full export must use this service's DB and must not truncate at the UI list limit.
            accounts = db.list_accounts(path=getattr(self.repo, "db_path", None))

        resource_cache = self._email_resource_cache()

        lines: list[str] = []
        items: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        kind_counts: dict[str, int] = {}
        skipped_count = 0

        for account in accounts:
            key = str(account.get("account_key") or account.get("key") or "").strip()
            email = self._account_email(account)
            if selected_set and key not in selected_set and email not in selected_set:
                continue
            if self._is_archived_account(account):
                skipped_count += 1
                if len(skipped) < 200:
                    skipped.append({"key": key, "email": email, "reason": "账号已归档"})
                continue
            if only_verified and not self._is_plus_product_account(account):
                skipped_count += 1
                # Keep skipped sample compact — full reason list was multi-MB JSON on large exports.
                if len(skipped) < 200:
                    skipped.append({
                        "key": key,
                        "email": email,
                        "reason": f"不是 Plus 成品号 (plus_status={account.get('plus_status') or ''}, plan_type={account.get('plan_type') or ''})",
                    })
                continue

            line, kind, error = self._format_plus_product_line(account, resource_cache=resource_cache)
            if not line:
                skipped_count += 1
                if len(skipped) < 500:
                    skipped.append({"key": key, "email": email, "reason": error or "无法组装导出行"})
                continue

            lines.append(line)
            kind_counts[kind] = kind_counts.get(kind, 0) + 1
            # items only for debugging/API consumers; keep lean (no full line duplicate beyond text).
            items.append({
                "key": key,
                "email": email,
                "kind": kind,
                "plus_status": account.get("plus_status") or "",
                "plan_type": account.get("plan_type") or "",
            })

        text = ("\n".join(lines) + ("\n" if lines else ""))
        exported_keys = [str(item.get("key") or "") for item in items if str(item.get("key") or "").strip()]
        if exported_keys:
            # Plus 成品号 TXT 导出 = 已导出plus成品
            self._mark_accounts_exported(exported_keys, kind="plus")
        archived_keys: list[str] = []
        archive_missing: list[str] = []
        if archive_after_export and exported_keys:
            archive_result = self.archive_many(exported_keys)
            archived_keys = list(archive_result.get("keys") or [])
            archive_missing = list(archive_result.get("missing") or [])
        return {
            "ok": True,
            "count": len(lines),
            "skipped_count": skipped_count,
            "kind_counts": kind_counts,
            "text": text,
            "items": items,
            "skipped": skipped,
            "exported_keys": exported_keys,
            "archived": len(archived_keys),
            "archived_keys": archived_keys,
            "archive_missing": archive_missing,
        }

    def _account_access_token(self, account: dict[str, Any]) -> str:
        """Prefer live access_token; fall back to initial registration token."""
        tokens = account.get("tokens") if isinstance(account.get("tokens"), dict) else {}
        for source in (
            tokens.get("access_token"),
            account.get("access_token"),
            tokens.get("chatgpt_access_token_initial"),
            account.get("chatgpt_access_token_initial"),
        ):
            value = str(source or "").strip()
            if value:
                return value
        return ""

    def _load_accounts_with_tokens(self, selected_keys: list[str] | None = None) -> list[dict[str, Any]]:
        """Bulk-load accounts joined with credentials (one query). Critical for AT export speed."""
        keys = [str(k or "").strip() for k in (selected_keys or []) if str(k or "").strip()]
        path = getattr(self.repo, "db_path", None)
        sql_base = """
            SELECT a.*,
                   c.access_token AS _cred_access_token,
                   c.refresh_token AS _cred_refresh_token,
                   c.id_token AS _cred_id_token,
                   c.chatgpt_access_token_initial AS _cred_chatgpt_access_token_initial
            FROM accounts a
            LEFT JOIN account_credentials c ON c.account_id_ref = a.id
        """
        try:
            with db.connect(path) as conn:
                if keys:
                    placeholders = ",".join("?" for _ in keys)
                    rows = conn.execute(
                        f"""
                        {sql_base}
                        WHERE a.account_key IN ({placeholders})
                           OR a.email IN ({placeholders})
                           OR a.login_identifier IN ({placeholders})
                           OR a.outlook_email IN ({placeholders})
                        """,
                        tuple(keys) * 4,
                    ).fetchall()
                else:
                    rows = conn.execute(
                        f"{sql_base} ORDER BY a.updated_at DESC, a.id DESC"
                    ).fetchall()
                accounts: list[dict[str, Any]] = []
                for row in rows:
                    item = dict(row)
                    at = str(item.pop("_cred_access_token", "") or "")
                    rt = str(item.pop("_cred_refresh_token", "") or "")
                    idt = str(item.pop("_cred_id_token", "") or "")
                    init_at = str(item.pop("_cred_chatgpt_access_token_initial", "") or "")
                    item["tokens"] = {
                        "access_token": at,
                        "refresh_token": rt,
                        "id_token": idt,
                        "chatgpt_access_token_initial": init_at,
                        "has_access_token": bool(at),
                        "has_refresh_token": bool(rt),
                        "has_id_token": bool(idt),
                        "has_initial_access_token": bool(init_at),
                    }
                    accounts.append(item)
                if accounts or not keys:
                    return accounts
        except Exception:
            pass
        # Fallback: list_accounts already joins credentials.
        if not keys:
            return db.list_accounts(path=path)
        accounts = []
        for key in keys:
            account = self.repo.get(key).to_dict()
            if account.get("account_key") or account.get("key"):
                accounts.append(account)
        return accounts

    def export_at_products_txt(
        self,
        keys: Iterable[str] | None = None,
        *,
        archive_after_export: bool = False,
    ) -> dict[str, Any]:
        """Bulk export email credential line + ChatGPT access_token.

        Format (same base as Plus TXT, then append AT):
          email----password----client_id----refresh_token----access_token

        No Plus verification. Requires non-empty access_token (or initial AT).
        Same bulk-load path as export_plus_products_txt for speed.
        """
        selected_keys = [str(key or "").strip() for key in (keys or []) if str(key or "").strip()]
        selected_set = set(selected_keys)

        accounts = self._load_accounts_with_tokens(selected_keys or None)
        resource_cache = self._email_resource_cache()

        lines: list[str] = []
        items: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        kind_counts: dict[str, int] = {}
        skipped_count = 0

        for account in accounts:
            key = str(account.get("account_key") or account.get("key") or "").strip()
            email = self._account_email(account)
            if selected_set and key not in selected_set and email not in selected_set:
                continue
            if self._is_archived_account(account):
                skipped_count += 1
                if len(skipped) < 200:
                    skipped.append({"key": key, "email": email, "reason": "账号已归档"})
                continue

            access_token = self._account_access_token(account)
            if not access_token:
                skipped_count += 1
                if len(skipped) < 500:
                    skipped.append({"key": key, "email": email, "reason": "缺少 access_token"})
                continue

            base_line, kind, error = self._format_plus_product_line(account, resource_cache=resource_cache)
            if not base_line:
                # Registration AT export must not require Outlook/iCloud resource rows.
                # Fallback: email----chatgpt_password----access_token for any domain
                # (forwarded_domain / custom domain / incomplete pool metadata).
                password = self._account_password(account)
                if email and password:
                    base_line = f"{email}----{password}"
                    kind = "account"
                    error = ""
                else:
                    skipped_count += 1
                    if len(skipped) < 500:
                        reason = error or "无法组装邮箱凭据"
                        if not password:
                            reason = "缺少账号密码"
                        skipped.append({"key": key, "email": email, "reason": reason})
                    continue

            line = f"{base_line}----{access_token}"
            lines.append(line)
            kind_counts[kind] = kind_counts.get(kind, 0) + 1
            items.append({
                "key": key,
                "email": email,
                "kind": kind,
                "plan_type": account.get("plan_type") or "",
                "has_access_token": True,
            })

        text = ("\n".join(lines) + ("\n" if lines else ""))
        exported_keys = [str(item.get("key") or "") for item in items if str(item.get("key") or "").strip()]
        if exported_keys:
            self._mark_accounts_exported(exported_keys, kind="at")
        archived_keys: list[str] = []
        archive_missing: list[str] = []
        if archive_after_export and exported_keys:
            archive_result = self.archive_many(exported_keys)
            archived_keys = list(archive_result.get("keys") or [])
            archive_missing = list(archive_result.get("missing") or [])
        return {
            "ok": True,
            "count": len(lines),
            "skipped_count": skipped_count,
            "kind_counts": kind_counts,
            "text": text,
            "items": items,
            "skipped": skipped,
            "exported_keys": exported_keys,
            "archived": len(archived_keys),
            "archived_keys": archived_keys,
            "archive_missing": archive_missing,
        }


