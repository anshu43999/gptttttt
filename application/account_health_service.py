from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

from application.accounts_service import AccountsService, account_operation_lock
from application.config_service import ConfigService
from core.browser.session import BrowserSession, extract_chatgpt_access_token
from infrastructure import db
from infrastructure.repositories.accounts_repository import AccountsRepository
from infrastructure.repositories.resource_pool_repository import ResourcePoolRepository

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHATGPT_HOME = "https://chatgpt.com"

PAID_PLANS = {"plus", "pro", "premium", "paid", "team", "business", "enterprise"}
MAX_HEALTH_POOL_PROXIES = 3

BAN_MARKERS = (
    ("account_suspended", ("account suspended", "account has been suspended", "your account has been suspended")),
    ("account_disabled", ("account disabled", "account has been disabled", "your account has been disabled", "account disabled or deleted")),
    ("account_deactivated", ("account deactivated", "account has been deactivated", "your account has been deactivated")),
    ("account_suspended", ("account has been blocked", "your account has been blocked", "account blocked")),
)

CHALLENGE_MARKERS = (
    ("phone_verification_required", ("verify your phone", "phone verification", "add a phone number", "enter your phone number", "verify phone")),
    ("email_verification_required", ("verify your email", "verification code", "enter the code", "check your email", "email verification")),
    ("identity_verification_required", ("verify your identity", "identity verification", "additional verification", "security verification")),
    ("captcha_required", ("captcha", "turnstile", "verify you are human", "security check")),
)


@dataclass(frozen=True)
class HealthResult:
    key: str
    ok: bool
    health_status: str
    source: str
    message: str = ""
    plan_type: str = ""
    account: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "key": self.key,
            "ok": self.ok,
            "health_status": self.health_status,
            "source": self.source,
            "message": self.message,
        }
        if not self.ok and self.message:
            data["error"] = self.message
        if self.plan_type:
            data["plan_type"] = self.plan_type
        if self.account is not None:
            data["account"] = self.account
        return data


class AccountHealthService:
    def __init__(self, repo: AccountsRepository | None = None, config_service: ConfigService | None = None, resource_repo: ResourcePoolRepository | None = None):
        self.repo = repo or AccountsRepository()
        self.config_service = config_service or ConfigService()
        self.resource_repo = resource_repo or ResourcePoolRepository(self.repo.db_path)
        self.accounts = AccountsService(self.repo, self.config_service)

    def check_account(self, key: str) -> tuple[dict[str, Any], int]:
        key = str(key or "").strip()
        if not key:
            return {"ok": False, "message": "账号 key 为空"}, 400
        with account_operation_lock(key):
            account = self.repo.get(key).to_dict()
            if not account.get("account_key"):
                return {"ok": False, "message": "未找到账号"}, 404
            result = self._check_loaded_account(account)
            return result.to_dict(), 200

    def check_batch(self, keys: Iterable[str]) -> dict[str, Any]:
        normalized_keys = [str(item or "").strip() for item in keys if str(item or "").strip()]
        results: list[dict[str, Any] | None] = [None] * len(normalized_keys)
        if normalized_keys:
            max_workers = max(1, min(8, len(normalized_keys)))
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {
                    pool.submit(self.check_account, key): (index, key)
                    for index, key in enumerate(normalized_keys)
                }
                for future in as_completed(futures):
                    index, key = futures[future]
                    try:
                        payload, status_code = future.result()
                    except Exception as exc:
                        payload = {"ok": False, "health_status": "failed", "message": str(exc)[:500]}
                        status_code = 500
                    results[index] = {"key": key, "status_code": status_code, **payload}

        completed_results = [item for item in results if item is not None]
        counts: dict[str, int] = {}
        for item in completed_results:
            status = str(item.get("health_status") or "failed")
            counts[status] = counts.get(status, 0) + 1
        return {
            "ok": all(bool(item.get("ok")) for item in completed_results),
            "checked": len(completed_results),
            "counts": counts,
            "results": completed_results,
        }

    def _check_loaded_account(self, account: dict[str, Any]) -> HealthResult:
        key = str(account.get("account_key") or account.get("key") or "")
        access_token = self.accounts._account_access_token(account)
        storage_path = self._storage_path(account)
        if access_token:
            api_result = self._check_api(account, access_token)
            if api_result.health_status in {"active", "active_plus", "active_free"}:
                return self._persist(account, api_result)
            account = self.repo.get(key).to_dict()
            storage_path = self._storage_path(account)
            if not storage_path:
                return self._persist(account, HealthResult(key, False, api_result.health_status, api_result.source, f"{api_result.message}；缺少可用 storage_state，无法浏览器复核"))
        elif not storage_path:
            return self._persist(account, HealthResult(key, False, "missing_material", "static", "缺少 access_token 和可用 storage_state，无法自动检查"))

        return self._persist(account, self._check_browser(account, storage_path))

    def _check_api(self, account: dict[str, Any], access_token: str) -> HealthResult:
        key = str(account.get("account_key") or "")
        errors: list[str] = []
        try:
            from platforms.chatgpt.payment import fetch_subscription_status_details
        except Exception as exc:
            return HealthResult(key, False, "unknown", "api", f"payment 依赖不可用: {exc}")

        attempts: list[tuple[str, str, str]] = []
        for proxy in self._health_proxies(account):
            route = proxy or "direct"
            try:
                details = fetch_subscription_status_details(
                    SimpleNamespace(
                        access_token=access_token,
                        chatgpt_account_id=str(account.get("account_id") or ""),
                        cookies="",
                        extra={"id_token": self.accounts._account_token(account, "id_token")},
                    ),
                    proxy=proxy or None,
                )
                plan = str(details.get("status") or "free").strip().lower() or "free"
                if plan in PAID_PLANS:
                    return HealthResult(key, True, "active_plus", str(details.get("source") or "api"), "API 可访问，账号为付费状态", plan)
                return HealthResult(key, True, "active_free", str(details.get("source") or "api"), "API 可访问，账号为免费状态", plan)
            except Exception as exc:
                status = self._classify_api_error(exc)
                attempts.append((route, status, str(exc)))
        message = " | ".join(f"{route}: {error}" for route, _, error in attempts)[:500]
        return HealthResult(key, False, self._best_api_status(attempts), "api", message)

    def _check_browser(self, account: dict[str, Any], storage_path: str) -> HealthResult:
        key = str(account.get("account_key") or "")
        config = self.config_service.merged_config()
        health_config = {
            "browser_engine": "patchright",
            "browser_channel": str(config.get("health_check_browser_channel") or config.get("browser_channel") or "chrome"),
            "browser_no_viewport": True,
            "headed": False,
            "use_camoufox": False,
            "_browser_storage_state": storage_path,
            "proxy": self._account_proxy(account),
            "locale": config.get("locale") or config.get("browser_locale") or "ja-JP",
            "timezone_id": config.get("timezone_id") or config.get("browser_timezone") or "Asia/Tokyo",
            "accept_language": config.get("accept_language") or "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
        }
        try:
            with BrowserSession(health_config) as session:
                page = session.page
                if page is None:
                    return HealthResult(key, False, "unknown", "patchright_chrome", "Patchright 未创建页面")
                page.goto(CHATGPT_HOME, wait_until="domcontentloaded", timeout=60000)
                token_result = extract_chatgpt_access_token(page, attempts=4, delay=1.0)
                snapshot = self._page_snapshot(page)
                if token_result.success:
                    account["access_token"] = token_result.access_token
                    account["chatgpt_access_token_initial"] = token_result.access_token
                    account["token_refreshed_at"] = self._now()
                    db.upsert_account(account, path=self.repo.db_path)
                    api_result = self._check_api(account, token_result.access_token)
                    if api_result.ok:
                        return HealthResult(key, True, api_result.health_status, f"patchright_chrome/{api_result.source}", "storage_state 可刷新 access_token，且 API 可访问", api_result.plan_type)
                    return HealthResult(key, True, "active", "patchright_chrome", "storage_state 可刷新 access_token，套餐状态暂不可确认")
                status = self._classify_page(snapshot, token_result.failure_reason)
                return HealthResult(key, status == "active", status, "patchright_chrome", self._health_message(snapshot, token_result.failure_reason))
        except Exception as exc:
            status = self._classify_api_error(exc)
            if status == "api_forbidden":
                status = "access_denied"
            return HealthResult(key, False, status, "patchright_chrome", str(exc)[:500])

    def _persist(self, account: dict[str, Any], result: HealthResult) -> HealthResult:
        now = self._now()
        account["account_health_status"] = result.health_status
        account["account_health_checked_at"] = now
        account["account_health_source"] = result.source
        account["account_health_error"] = "" if result.ok else result.message
        account["account_health_detail_json"] = json.dumps(
            {"message": result.message, "plan_type": result.plan_type, "ok": result.ok},
            ensure_ascii=False,
        )
        if result.plan_type:
            account["plan_type"] = result.plan_type
            if result.plan_type in PAID_PLANS:
                account["plus_status"] = "verified_plus"
            elif result.health_status in {"active", "active_free"}:
                account["plus_status"] = "free"
        saved = self.repo.upsert(account).to_dict()
        db.add_account_event(
            str(account.get("account_key") or result.key),
            "account_health_checked",
            status=result.health_status,
            message=result.message[:240],
            payload={"source": result.source, "ok": result.ok, "plan_type": result.plan_type},
            path=self.repo.db_path,
        )
        return HealthResult(result.key, result.ok, result.health_status, result.source, result.message, result.plan_type, saved)

    def _storage_path(self, account: dict[str, Any]) -> str:
        paths = account.get("paths") if isinstance(account.get("paths"), dict) else {}
        for value in (account.get("storage_file"), paths.get("storage_state"), account.get("browser_storage_state_path")):
            text = str(value or "").strip()
            if not text:
                continue
            path = Path(text)
            if not path.is_absolute():
                path = PROJECT_ROOT / path
            try:
                resolved = path.resolve()
                root = PROJECT_ROOT.resolve()
                if root not in resolved.parents and resolved != root:
                    continue
                data = json.loads(resolved.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(data, dict) and isinstance(data.get("cookies"), list) and isinstance(data.get("origins"), list):
                return str(resolved)
        return ""

    def _account_proxy(self, account: dict[str, Any]) -> str:
        proxy = account.get("proxy") if isinstance(account.get("proxy"), dict) else {}
        for value in (proxy.get("registration_proxy"), account.get("registration_proxy"), proxy.get("subscription_check_proxy"), account.get("subscription_check_proxy")):
            text = str(value or "").strip()
            if text and "127.0.0.1" not in text and "localhost" not in text:
                return text
        for path_value in (account.get("resume_file"), account.get("account_file"), (account.get("paths") or {}).get("resume") if isinstance(account.get("paths"), dict) else ""):
            artifact = self.accounts._read_account_artifact(str(path_value or ""))
            artifact_proxy = artifact.get("proxy") if isinstance(artifact.get("proxy"), dict) else {}
            for value in (artifact_proxy.get("registration_proxy"), artifact.get("registration_proxy"), artifact_proxy.get("subscription_check_proxy"), artifact.get("subscription_check_proxy")):
                text = str(value or "").strip()
                if text and "127.0.0.1" not in text and "localhost" not in text:
                    return text
        return ""

    def _health_proxies(self, account: dict[str, Any]) -> list[str]:
        proxies: list[str] = []
        account_proxy = self._account_proxy(account)
        if account_proxy:
            proxies.append(account_proxy)
        for proxy in self._pool_proxy_candidates(account, limit=MAX_HEALTH_POOL_PROXIES):
            if proxy not in proxies:
                proxies.append(proxy)
        proxies.append("")
        return proxies

    def _pool_proxy_candidates(self, account: dict[str, Any], *, limit: int) -> list[str]:
        if limit <= 0:
            return []
        try:
            rows = self.resource_repo.list("proxy", "lajiao_credentials", "available")
        except Exception:
            return []
        candidates: list[str] = []
        for row in rows:
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            value = str(payload.get("url") or row.get("resource_key") or "").strip()
            if value and "127.0.0.1" not in value and "localhost" not in value and value not in candidates:
                candidates.append(value)
        if len(candidates) <= limit:
            return candidates
        seed = sum(ord(ch) for ch in str(account.get("account_key") or account.get("email") or ""))
        start = seed % len(candidates)
        rotated = candidates[start:] + candidates[:start]
        return rotated[:limit]

    def _page_snapshot(self, page: Any) -> dict[str, str]:
        try:
            url = str(page.url or "")
        except Exception:
            url = ""
        try:
            title = str(page.title() or "")
        except Exception:
            title = ""
        try:
            text = str(page.evaluate("() => document.body ? document.body.innerText : ''") or "")
        except Exception:
            text = ""
        try:
            has_turnstile = bool(page.evaluate("() => !!document.querySelector('.cf-turnstile, iframe[src*=turnstile], iframe[src*=captcha]')"))
        except Exception:
            has_turnstile = False
        return {"url": url, "title": title, "text": text[:4000], "has_turnstile": "1" if has_turnstile else ""}

    def _classify_page(self, snapshot: dict[str, str], failure_reason: str = "") -> str:
        combined = " ".join([snapshot.get("url", ""), snapshot.get("title", ""), snapshot.get("text", ""), failure_reason]).lower()
        if snapshot.get("has_turnstile"):
            return "captcha_required"
        for status, markers in BAN_MARKERS:
            if any(marker in combined for marker in markers):
                return status
        for status, markers in CHALLENGE_MARKERS:
            if any(marker in combined for marker in markers):
                return status
        if "auth.openai.com" in combined and ("log-in" in combined or "login" in combined):
            return "login_required"
        if "login required" in combined or "log in to" in combined or "please log in" in combined:
            return "login_required"
        if "logged_out" in combined or "your session has ended" in combined:
            return "session_expired"
        if "access denied" in combined or "forbidden" in combined:
            return "access_denied"
        if "401" in combined or "unauthorized" in combined:
            return "session_expired"
        if "403" in combined:
            return "access_denied"
        return "unknown"

    def _health_message(self, snapshot: dict[str, str], failure_reason: str = "") -> str:
        parts = []
        if snapshot.get("url"):
            parts.append(f"url={snapshot['url'][:160]}")
        if snapshot.get("title"):
            parts.append(f"title={snapshot['title'][:120]}")
        if failure_reason:
            parts.append(failure_reason[:200])
        text = " ".join((snapshot.get("text") or "").split())[:220]
        if text:
            parts.append(text)
        return " | ".join(parts)[:500]

    def _best_api_status(self, attempts: list[tuple[str, str, str]]) -> str:
        statuses = [status for _, status, _ in attempts]
        direct_statuses = [status for route, status, _ in attempts if route == "direct"]
        for candidate in ("token_expired", "api_forbidden", "rate_limited"):
            if candidate in direct_statuses:
                return candidate
        for candidate in ("token_expired", "api_forbidden", "rate_limited", "proxy_failed"):
            if candidate in statuses:
                return candidate
        return statuses[-1] if statuses else "unknown"

    def _classify_api_error(self, exc: Exception) -> str:
        return self._classify_api_error_text(str(exc))

    def _classify_api_error_text(self, text: str) -> str:
        value = str(text or "").lower()
        if any(marker in value for marker in ("proxy", "connection", "connect", "timed out", "timeout", "socks")):
            return "proxy_failed"
        if "429" in value or "rate limit" in value:
            return "rate_limited"
        if "401" in value or "unauthorized" in value or "expired" in value:
            return "token_expired"
        if "403" in value or "forbidden" in value:
            return "api_forbidden"
        return "unknown"

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()
