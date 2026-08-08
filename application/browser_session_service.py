from __future__ import annotations

import json
import queue
import shutil
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from core.proxy_utils import build_playwright_proxy_config, is_authenticated_socks5_proxy
from core.proxy.credential_runtime import CredentialProxyRuntime
from application.config_service import ConfigService
from core.browser.session import extract_chatgpt_access_token
from infrastructure import db

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHATGPT_HOME = "https://chatgpt.com/"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _public_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


@dataclass
class BrowserCommand:
    name: str
    payload: dict[str, Any] = field(default_factory=dict)
    response: queue.Queue = field(default_factory=queue.Queue)


@dataclass
class BrowserSessionHandle:
    id: str
    account_key: str
    account_label: str
    storage_path: Path
    target_url: str
    proxy: str
    engine: str
    headed: bool
    save_on_close: bool
    status: str = "launching"
    message: str = "正在启动浏览器"
    url: str = ""
    title: str = ""
    opened_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    closed_at: str = ""
    saved_at: str = ""
    saved_path: str = ""
    backup_path: str = ""
    error: str = ""
    commands: queue.Queue[BrowserCommand] = field(default_factory=queue.Queue)
    thread: threading.Thread | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "account_key": self.account_key,
            "account_label": self.account_label,
            "storage_file": _public_path(self.storage_path),
            "target_url": self.target_url,
            "proxy_enabled": bool(self.proxy),
            "proxy_hint": self._proxy_hint(),
            "engine": self.engine,
            "headed": self.headed,
            "save_on_close": self.save_on_close,
            "status": self.status,
            "message": self.message,
            "url": self.url,
            "title": self.title,
            "opened_at": self.opened_at,
            "updated_at": self.updated_at,
            "closed_at": self.closed_at,
            "saved_at": self.saved_at,
            "saved_path": self.saved_path,
            "backup_path": self.backup_path,
            "error": self.error,
        }

    def _proxy_hint(self) -> str:
        if not self.proxy:
            return ""
        if "@" in self.proxy:
            scheme, rest = self.proxy.split("://", 1) if "://" in self.proxy else ("", self.proxy)
            host = rest.rsplit("@", 1)[-1]
            return f"{scheme}://***@{host}" if scheme else f"***@{host}"
        return self.proxy


class BrowserSessionService:
    """Open and manage headed ChatGPT browser sessions for saved accounts.

    Playwright/Camoufox objects stay inside the session worker thread. API threads
    communicate through commands, avoiding cross-thread browser calls.
    """

    def __init__(self, max_sessions: int = 3, config_service: ConfigService | None = None):
        self.max_sessions = max(1, int(max_sessions or 3))
        self.config_service = config_service or ConfigService()
        self._sessions: dict[str, BrowserSessionHandle] = {}
        self._lock = threading.Lock()
    def open_for_account(
        self,
        account: dict[str, Any],
        *,
        target_url: str = CHATGPT_HOME,
        use_saved_proxy: bool = True,
        engine: str = "auto",
        headed: bool = True,
        save_on_close: bool = False,
    ) -> tuple[dict[str, Any], int]:
        account_key = str(account.get("account_key") or account.get("key") or "").strip()
        if not account_key:
            return {"ok": False, "message": "未找到账号"}, 404
        storage_path, error = self._resolve_storage_path(account)
        if error or storage_path is None:
            return {"ok": False, "message": error or "缺少浏览器 storage_state 文件"}, 400
        valid, validation_error = self._validate_storage_state(storage_path)
        if not valid:
            return {"ok": False, "message": validation_error}, 400
        if str(engine or "camoufox").strip().lower() not in {"auto", "camoufox"}:
            return {"ok": False, "message": "仅支持 Camoufox 指纹浏览器"}, 400
        normalized_engine = "camoufox"
        url = str(target_url or CHATGPT_HOME).strip()
        if not url.startswith(("https://chatgpt.com", "https://chat.openai.com")):
            return {"ok": False, "message": "只允许打开 ChatGPT 域名"}, 400
        proxy = self._account_proxy(account) if use_saved_proxy else ""
        with self._lock:
            active_count = sum(1 for item in self._sessions.values() if item.status in {"launching", "active"})
            if active_count >= self.max_sessions:
                return {"ok": False, "message": f"浏览器会话已达上限 {self.max_sessions}，请先关闭其他会话"}, 429
            session_id = uuid.uuid4().hex[:12]
            handle = BrowserSessionHandle(
                id=session_id,
                account_key=account_key,
                account_label=str(account.get("email") or account.get("login_identifier") or account_key),
                storage_path=storage_path,
                target_url=url,
                proxy=proxy,
                engine=normalized_engine,
                headed=bool(headed),
                save_on_close=bool(save_on_close),
            )
            thread = threading.Thread(target=self._worker, args=(handle,), name=f"account-browser-{session_id}", daemon=True)
            handle.thread = thread
            self._sessions[session_id] = handle
            thread.start()
            return {"ok": True, "session": handle.to_dict()}, 202

    def list_sessions(self) -> dict[str, Any]:
        with self._lock:
            sessions = [item.to_dict() for item in sorted(self._sessions.values(), key=lambda x: x.opened_at, reverse=True)]
        return {"ok": True, "items": sessions, "max_sessions": self.max_sessions}

    def save_session(self, session_id: str) -> tuple[dict[str, Any], int]:
        handle = self._get(session_id)
        if handle is None:
            return {"ok": False, "message": "未找到浏览器会话"}, 404
        return self._send_command(handle, "save")

    def close_session(self, session_id: str, *, save: bool = False) -> tuple[dict[str, Any], int]:
        handle = self._get(session_id)
        if handle is None:
            return {"ok": False, "message": "未找到浏览器会话"}, 404
        return self._send_command(handle, "close", {"save": bool(save)})

    def _get(self, session_id: str) -> BrowserSessionHandle | None:
        with self._lock:
            return self._sessions.get(str(session_id or ""))

    def _send_command(self, handle: BrowserSessionHandle, name: str, payload: dict[str, Any] | None = None) -> tuple[dict[str, Any], int]:
        if handle.status not in {"launching", "active"}:
            return {"ok": False, "message": f"会话当前状态为 {handle.status}", "session": handle.to_dict()}, 409
        command = BrowserCommand(name=name, payload=dict(payload or {}))
        handle.commands.put(command)
        try:
            result = command.response.get(timeout=45)
        except queue.Empty:
            return {"ok": False, "message": "浏览器会话未及时响应", "session": handle.to_dict()}, 504
        return result, 200 if result.get("ok") else 500

    def _resolve_storage_path(self, account: dict[str, Any]) -> tuple[Path | None, str]:
        paths = account.get("paths") if isinstance(account.get("paths"), dict) else {}
        candidates = [
            account.get("storage_file"),
            paths.get("storage_state"),
            account.get("browser_storage_state_path"),
        ]
        for value in candidates:
            text = str(value or "").strip()
            if not text:
                continue
            path = Path(text)
            if not path.is_absolute():
                path = PROJECT_ROOT / path
            try:
                resolved = path.resolve()
                resolved.relative_to(PROJECT_ROOT.resolve())
            except Exception:
                return None, "storage_state 路径不在项目目录内，已拒绝打开"
            if resolved.exists() and resolved.is_file():
                return resolved, ""
        return None, "该账号缺少可用的 storage_state 文件"

    def _validate_storage_state(self, path: Path) -> tuple[bool, str]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            return False, f"storage_state 不是有效 JSON: {exc}"
        if not isinstance(data, dict):
            return False, "storage_state 格式错误：根节点不是对象"
        if not isinstance(data.get("cookies"), list) or not isinstance(data.get("origins"), list):
            return False, "storage_state 格式错误：缺少 cookies/origins"
        return True, ""

    def refresh_access_token_for_account(
        self,
        account: dict[str, Any],
        *,
        use_saved_proxy: bool = True,
        save_storage: bool = True,
    ) -> tuple[dict[str, Any], int]:
        account_key = str(account.get("account_key") or account.get("key") or "").strip()
        if not account_key:
            return {"ok": False, "message": "未找到账号"}, 404
        storage_path, error = self._resolve_storage_path(account)
        if error or storage_path is None:
            return {"ok": False, "message": error or "缺少浏览器 storage_state 文件"}, 400
        valid, validation_error = self._validate_storage_state(storage_path)
        if not valid:
            return {"ok": False, "message": validation_error}, 400
        proxy = self._account_proxy(account) if use_saved_proxy else ""
        browser_ctx = None
        browser = None
        context = None
        proxy_runtime = None
        try:
            proxy_for_browser = proxy
            if proxy and is_authenticated_socks5_proxy(proxy):
                proxy_runtime = CredentialProxyRuntime({"lajiao_proxy_credential_protocol": "socks5"}, log_fn=lambda _message: None)
                proxy_for_browser = proxy_runtime.start_browser_bridge(proxy)
            proxy_config = build_playwright_proxy_config(proxy_for_browser) if proxy_for_browser else None
            from camoufox.sync_api import Camoufox

            kwargs: dict[str, Any] = {"headless": True, "os": ["windows", "macos", "linux"], "enable_cache": False, "humanize": True}
            if proxy_config:
                kwargs["proxy"] = proxy_config
                kwargs["geoip"] = True
            browser_ctx = Camoufox(**kwargs)
            browser = browser_ctx.__enter__()
            context = browser.new_context(no_viewport=True, storage_state=str(storage_path))
            page = context.new_page()
            page.goto(CHATGPT_HOME, wait_until="domcontentloaded", timeout=60000)
            token_result = extract_chatgpt_access_token(page, attempts=12, delay=2.0)
            if not token_result.success:
                db.add_account_event(account_key, "access_token_refresh_failed", status=token_result.status or "failed", message=token_result.failure_reason[:240], payload={"http_status": token_result.http_status, "proxy_enabled": bool(proxy)})
                return {"ok": False, "message": token_result.failure_reason or "未从缓存浏览器 session 获取到 access_token", "status": token_result.status, "http_status": token_result.http_status}, 502
            if save_storage and context is not None:
                context.storage_state(path=str(storage_path))
            account["access_token"] = token_result.access_token
            account["chatgpt_access_token_initial"] = token_result.access_token
            account["token_refreshed_at"] = _now()
            account["storage_file"] = _public_path(storage_path)
            paths = account.get("paths") if isinstance(account.get("paths"), dict) else {}
            account["paths"] = {**paths, "storage_state": _public_path(storage_path)}
            db.upsert_account(account)
            db.add_account_event(account_key, "access_token_refreshed", status="success", message="已通过缓存浏览器 session 刷新 access_token", payload={"token_length": len(token_result.access_token), "http_status": token_result.http_status, "proxy_enabled": bool(proxy), "storage_file": _public_path(storage_path)})
            updated = db.get_account(account_key)
            return {"ok": True, "account": updated, "has_access_token": True, "token_length": len(token_result.access_token), "storage_file": _public_path(storage_path), "proxy_enabled": bool(proxy)}, 200
        except Exception as exc:
            db.add_account_event(account_key, "access_token_refresh_failed", status="failed", message=str(exc)[:240], payload={"proxy_enabled": bool(proxy)})
            return {"ok": False, "message": str(exc)}, 500
        finally:
            if proxy_runtime is not None:
                proxy_runtime.cleanup()
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass
            try:
                if browser_ctx is not None:
                    browser_ctx.__exit__(None, None, None)
            except Exception:
                pass

    def _account_proxy(self, account: dict[str, Any]) -> str:
        proxy = account.get("proxy") if isinstance(account.get("proxy"), dict) else {}
        for value in (proxy.get("registration_proxy"), proxy.get("subscription_check_proxy"), account.get("registration_proxy")):
            text = str(value or "").strip()
            if text and "127.0.0.1" not in text and "localhost" not in text:
                return text
        return ""

    def _worker(self, handle: BrowserSessionHandle) -> None:
        browser_ctx = None
        browser = None
        context = None
        page = None
        playwright_instance = None
        proxy_runtime = None
        engine_used = handle.engine
        try:
            browser_ctx, browser, context, page, playwright_instance, engine_used, proxy_runtime = self._launch(handle)
            handle.engine = engine_used
            handle.status = "active"
            handle.message = "浏览器已打开"
            handle.updated_at = _now()
            while True:
                command = handle.commands.get()
                if command.name == "save":
                    command.response.put(self._save(handle, context, page))
                elif command.name == "close":
                    result = {"ok": True, "session": handle.to_dict()}
                    if command.payload.get("save") or handle.save_on_close:
                        result = self._save(handle, context, page)
                    command.response.put(result)
                    break
        except Exception as exc:
            handle.status = "failed"
            handle.message = "浏览器启动失败"
            handle.error = str(exc)
            handle.updated_at = _now()
        finally:
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass
            try:
                if browser_ctx is not None:
                    browser_ctx.__exit__(None, None, None)
            except Exception:
                pass
            try:
                if proxy_runtime is not None:
                    proxy_runtime.cleanup()
            except Exception:
                pass
            try:
                if playwright_instance is not None:
                    playwright_instance.stop()
            except Exception:
                pass
            if handle.status in {"launching", "active"}:
                handle.status = "closed"
                handle.message = "浏览器已关闭"
                handle.closed_at = _now()
                handle.updated_at = handle.closed_at
            db.add_account_event(handle.account_key, "browser_session_closed", status=handle.status, message=handle.message, payload={"session_id": handle.id, "saved_path": handle.saved_path})

    def _launch(self, handle: BrowserSessionHandle):
        errors: list[str] = []
        tried: set[str] = set()

        def attempt(label: str, proxy: str, geoip_ip: str = ""):
            browser_ctx = browser = context = page = proxy_runtime = None
            try:
                browser_ctx, browser, context, page, proxy_runtime = self._launch_once(handle, proxy, geoip_ip)
                page.goto(handle.target_url, wait_until="domcontentloaded", timeout=60000)
                handle.url = str(getattr(page, "url", "") or "")
                try:
                    handle.title = str(page.title() or "")
                except Exception:
                    handle.title = ""
                previous_proxy = handle.proxy
                handle.proxy = proxy
                if label != "saved":
                    handle.message = "浏览器已按代理 fallback 打开"
                    db.add_account_event(
                        handle.account_key,
                        "browser_session_proxy_fallback",
                        status="active",
                        message=f"指纹浏览器已切换代理策略: {label}",
                        payload={"session_id": handle.id, "from_saved_proxy": bool(previous_proxy), "proxy_enabled": bool(proxy)},
                    )
                db.add_account_event(handle.account_key, "browser_session_opened", status="active", message="账号指纹浏览器会话已打开", payload={"session_id": handle.id, "engine": "camoufox", "storage_file": _public_path(handle.storage_path), "proxy_enabled": bool(proxy), "proxy_strategy": label})
                return browser_ctx, browser, context, page, None, "camoufox", proxy_runtime
            except Exception as exc:
                errors.append(f"{label}: {exc}")
                db.add_account_event(handle.account_key, "browser_session_launch_attempt_failed", status="failed", message=f"指纹浏览器启动/导航失败，代理策略 {label}: {exc}", payload={"session_id": handle.id, "proxy_enabled": bool(proxy)})
                self._cleanup_launch_attempt(browser_ctx, browser, proxy_runtime)
                return None

        if handle.proxy:
            tried.add(handle.proxy)
            saved_exit_ip = self._check_browser_proxy(handle, handle.proxy, "saved")
            if saved_exit_ip:
                result = attempt("saved", handle.proxy, saved_exit_ip)
                if result is not None:
                    return result
            else:
                errors.append("saved: proxy precheck failed")

        fresh_proxy, fresh_exit_ip = self._select_fresh_browser_proxy(handle)
        if fresh_proxy and fresh_proxy not in tried:
            result = attempt("pool", fresh_proxy, fresh_exit_ip)
            if result is not None:
                return result

        result = attempt("direct", "")
        if result is not None:
            return result
        raise RuntimeError("; ".join(errors) or "浏览器启动失败")

    def _check_browser_proxy(self, handle: BrowserSessionHandle, proxy: str, label: str) -> str:
        try:
            config = self.config_service.merged_config()
            runtime = CredentialProxyRuntime(config, log_fn=lambda message: db.add_account_event(handle.account_key, "browser_proxy_check", status="info", message=message[:240], payload={"session_id": handle.id, "proxy_strategy": label}))
            try:
                ok, exit_ip = runtime.check(proxy)
                if ok:
                    return str(exit_ip or "").strip() or "checked"
                db.add_account_event(handle.account_key, "browser_proxy_check_failed", status="failed", message=f"保存代理预检失败: {label}", payload={"session_id": handle.id, "proxy_strategy": label})
                return ""
            finally:
                runtime.cleanup()
        except Exception as exc:
            db.add_account_event(handle.account_key, "browser_proxy_check_failed", status="failed", message=f"保存代理预检异常: {exc}", payload={"session_id": handle.id, "proxy_strategy": label})
            return ""

    def _select_fresh_browser_proxy(self, handle: BrowserSessionHandle) -> tuple[str, str]:
        try:
            config = self.config_service.merged_config()
            config["rotate_proxy_each_attempt"] = True
            runtime = CredentialProxyRuntime(config, log_fn=lambda message: db.add_account_event(handle.account_key, "browser_proxy_select", status="info", message=message[:240], payload={"session_id": handle.id}))
            try:
                proxy, exit_ip = runtime.select()
                return str(proxy or "").strip(), str(exit_ip or "").strip()
            finally:
                runtime.cleanup()
        except Exception as exc:
            db.add_account_event(handle.account_key, "browser_proxy_select_failed", status="failed", message=f"代理池新代理不可用: {exc}", payload={"session_id": handle.id})
            return "", ""

    def _launch_once(self, handle: BrowserSessionHandle, proxy: str, geoip_ip: str = ""):
        proxy_runtime = None
        proxy_for_browser = proxy
        if proxy_for_browser and is_authenticated_socks5_proxy(proxy_for_browser):
            proxy_runtime = CredentialProxyRuntime({"lajiao_proxy_credential_protocol": "socks5"}, log_fn=lambda _message: None)
            proxy_for_browser = proxy_runtime.start_browser_bridge(proxy_for_browser)
        proxy_config = build_playwright_proxy_config(proxy_for_browser) if proxy_for_browser else None
        from camoufox.sync_api import Camoufox

        kwargs: dict[str, Any] = {"headless": not handle.headed, "os": ["windows", "macos", "linux"], "enable_cache": False, "humanize": True}
        if proxy_config:
            kwargs["proxy"] = proxy_config
            if geoip_ip and geoip_ip != "checked":
                kwargs["geoip"] = geoip_ip
        browser_ctx = Camoufox(**kwargs)
        browser = browser_ctx.__enter__()
        context = browser.new_context(no_viewport=True, storage_state=str(handle.storage_path))
        page = context.new_page()
        return browser_ctx, browser, context, page, proxy_runtime

    def _cleanup_launch_attempt(self, browser_ctx: Any, browser: Any, proxy_runtime: Any) -> None:
        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass
        try:
            if browser_ctx is not None:
                browser_ctx.__exit__(None, None, None)
        except Exception:
            pass
        try:
            if proxy_runtime is not None:
                proxy_runtime.cleanup()
        except Exception:
            pass

    def _save(self, handle: BrowserSessionHandle, context: Any, page: Any) -> dict[str, Any]:
        try:
            if page is not None:
                handle.url = str(getattr(page, "url", "") or "")
                try:
                    handle.title = str(page.title() or "")
                except Exception:
                    pass
            backup = handle.storage_path.with_suffix(handle.storage_path.suffix + f".bak-{datetime.now().strftime('%Y%m%d_%H%M%S')}")
            if handle.storage_path.exists():
                shutil.copy2(handle.storage_path, backup)
            context.storage_state(path=str(handle.storage_path))
            handle.saved_at = _now()
            handle.saved_path = _public_path(handle.storage_path)
            handle.backup_path = _public_path(backup)
            handle.updated_at = handle.saved_at
            handle.message = "浏览器状态已保存"
            db.add_account_event(handle.account_key, "browser_session_saved", status="saved", message="浏览器 storage_state 已保存", payload={"session_id": handle.id, "path": handle.saved_path, "backup": handle.backup_path})
            return {"ok": True, "session": handle.to_dict()}
        except Exception as exc:
            handle.error = str(exc)
            handle.updated_at = _now()
            return {"ok": False, "message": str(exc), "session": handle.to_dict()}
