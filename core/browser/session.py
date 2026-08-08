"""Browser session manager — Camoufox/Playwright browser lifecycle with anti-fingerprinting.

Extracts browser launch, device identity seeding, debug event attachment,
and storage state management patterns from the archived RegisterPipeline
(_archive/refactor_2026/full_pipeline.py) and the ChatGPT OAuth client.

Import strategy: lean browser-launch logic lives here (~130 lines).
Long-running helper functions are imported from their canonical homes:
  - _seed_browser_device_id  → platforms.chatgpt.browser_register
  - _get_browser_csrf_token  → platforms.chatgpt.browser_register
  - build_playwright_proxy_config → core.proxy_utils
"""

from __future__ import annotations

import json
import os
import secrets
import uuid
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from core.proxy_utils import build_playwright_proxy_config



@dataclass(frozen=True)
class BrowserAccessTokenResult:
    access_token: str = ""
    status: str = ""
    http_status: int = 0
    failure_reason: str = ""
    retryable: bool = True

    @property
    def success(self) -> bool:
        return bool(self.access_token)


def extract_chatgpt_access_token(page: Any, *, attempts: int = 30, delay: float = 2.0, log_fn: Callable[[str], None] | None = None) -> BrowserAccessTokenResult:
    """Extract ChatGPT accessToken from the current browser session.

    This is the reusable module boundary for post-registration token extraction:
    it reads chatgpt.com/api/auth/session from the authenticated browser context,
    without OAuth login and without phone binding.
    """
    from platforms.chatgpt.phone_register import _fetch_access_token

    result = _fetch_access_token(page, attempts=attempts, delay=delay)
    if result.success:
        return BrowserAccessTokenResult(
            access_token=result.access_token,
            status=result.status,
            http_status=result.http_status,
            failure_reason="",
            retryable=False,
        )
    reason = result.failure_reason or result.status or "access token missing"
    if log_fn:
        log_fn(f"  ChatGPT session access_token 提取失败: {reason[:200]}")
    return BrowserAccessTokenResult(
        status=result.status,
        http_status=result.http_status,
        failure_reason=reason,
        retryable=result.retryable,
    )
class BrowserSession:
    """Context manager for a browser session with anti-fingerprinting.

    Launches Camoufox (falling back to Playwright), seeds the oai-did
    device identity cookie across all OpenAI domains, attaches debug
    event listeners, and provides helpers for CSRF token and access
    token extraction.

    Usage::

        config = {"headed": False, "proxy": "socks5h://user:pass@host:port"}
        with BrowserSession(config) as session:
            session.page.goto("https://chatgpt.com/auth/login")
            csrf = session.get_csrf_token()
            access_token = session.extract_access_token()
            session.save_storage_state("output/storage.json")
    """

    def __init__(self, config: dict):
        self.config = dict(config or {})
        self.browser: Any = None
        self.browser_context: Any = None
        self.page: Any = None
        self._camoufox_ctx: Any = None
        self.playwright_instance: Any = None
        self.device_id: str = ""
        self._log_messages: list[str] = []
        self._proxy_runtime: Any = None
        self._external_log_fn: Callable[[str], None] | None = self.config.get("_log_fn") if callable(self.config.get("_log_fn")) else None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> BrowserSession:
        self._launch()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        # Auto-save storage state if a path is configured
        storage_path = self.config.get("browser_storage_state_path")
        if storage_path:
            self.save_storage_state(str(storage_path))
        self._cleanup()
        return False

    # ------------------------------------------------------------------
    # Launch
    # ------------------------------------------------------------------

    def _launch(self) -> None:
        headed = bool(self.config.get("headed", False))
        engine = str(
            self.config.get("browser_engine")
            or ("camoufox" if self.config.get("use_camoufox", True) else "playwright")
        ).strip().lower()
        if engine in {"patchright", "patched", "chrome"}:
            self._launch_patchright(headed)
        elif engine in {"playwright", "chromium"}:
            self._launch_playwright(headed)
        elif engine in {"camoufox", "firefox"}:
            try:
                self._launch_camoufox(headed)
            except ImportError:
                if self.config.get("require_camoufox"):
                    raise
                self._launch_playwright(headed)
        else:
            raise ValueError(f"unsupported browser_engine: {engine}")

        self.device_id = str(uuid.uuid4())
        self._seed_oai_device_id(self.device_id)

    def _launch_camoufox(self, headed: bool) -> None:
        """Launch Camoufox anti-fingerprinting browser.

        Mirrors RegisterPipeline._launch_camoufox from
        _archive/refactor_2026/full_pipeline.py:2829-2867.
        """
        from camoufox.sync_api import Camoufox

        proxy_config = self._build_proxy_config()

        launch_kwargs: dict[str, Any] = {
            "headless": not headed,
            "os": ["windows", "macos", "linux"],
            "enable_cache": bool(self.config.get("camoufox_enable_cache", False)),
            "humanize": self.config.get("camoufox_humanize", True),
        }
        if proxy_config:
            launch_kwargs["proxy"] = proxy_config
            if self.config.get("camoufox_geoip", True):
                launch_kwargs["geoip"] = (
                    self.config.get("_camoufox_geoip_ip") or True
                )

        self._camoufox_ctx = Camoufox(**launch_kwargs)
        self.browser = self._camoufox_ctx.__enter__()

        context_kwargs = self._context_kwargs()
        context_kwargs["no_viewport"] = True
        self.browser_context = self.browser.new_context(**context_kwargs)
        self.page = self.browser_context.new_page()
        self._attach_debug_events()

    def _launch_playwright(self, headed: bool) -> None:
        """Launch Playwright Chromium (fallback, no anti-fingerprinting).

        Mirrors RegisterPipeline._launch_playwright from
        _archive/refactor_2026/full_pipeline.py:2870-2896.
        """
        from playwright.sync_api import sync_playwright

        self.playwright_instance = sync_playwright().start()
        launch_kwargs: dict[str, Any] = {"headless": not headed}
        launch_args = self._chromium_launch_args()
        if launch_args:
            launch_kwargs["args"] = launch_args

        proxy_config = self._build_proxy_config()
        if proxy_config:
            launch_kwargs["proxy"] = proxy_config

        self.browser = self.playwright_instance.chromium.launch(**launch_kwargs)

        context_kwargs = self._context_kwargs()
        self.browser_context = self.browser.new_context(**context_kwargs)
        self.page = self.browser_context.new_page()
        self._attach_debug_events()

    def _launch_patchright(self, headed: bool) -> None:
        """Launch Patchright Chromium/Chrome with the same proxy/storage contract."""
        try:
            from patchright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "browser_engine=patchright requires `pip install patchright` and `patchright install chrome` or chromium"
            ) from exc

        self.playwright_instance = sync_playwright().start()
        proxy_config = self._build_proxy_config()
        context_kwargs = self._context_kwargs()
        if proxy_config:
            context_kwargs["proxy"] = proxy_config
        context_kwargs["headless"] = not headed
        context_kwargs["no_viewport"] = bool(self.config.get("browser_no_viewport", True))
        launch_args = self._chromium_launch_args()
        if launch_args:
            context_kwargs["args"] = launch_args
        channel = str(self.config.get("browser_channel") or "chrome").strip().lower()
        if channel and channel not in {"chromium", "default"}:
            context_kwargs["channel"] = channel

        profile_dir = str(self.config.get("browser_profile_dir") or "").strip()
        storage_state = self._resolve_storage_state()
        if profile_dir:
            context_kwargs.pop("storage_state", None)
            Path(profile_dir).mkdir(parents=True, exist_ok=True)
            self.browser_context = self.playwright_instance.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                **context_kwargs,
            )
            self.browser = self.browser_context.browser
            if storage_state:
                self._restore_storage_state_into_context(storage_state)
        else:
            launch_kwargs = {"headless": not headed}
            if launch_args:
                launch_kwargs["args"] = launch_args
            if channel and channel not in {"chromium", "default"}:
                launch_kwargs["channel"] = channel
            if proxy_config:
                launch_kwargs["proxy"] = proxy_config
            self.browser = self.playwright_instance.chromium.launch(**launch_kwargs)
            new_context_kwargs = self._context_kwargs()
            new_context_kwargs["no_viewport"] = bool(self.config.get("browser_no_viewport", True))
            self.browser_context = self.browser.new_context(**new_context_kwargs)
        self.page = self.browser_context.pages[0] if self.browser_context.pages else self.browser_context.new_page()
        self._attach_debug_events()

    def _context_kwargs(self) -> dict[str, Any]:
        storage_state = self._resolve_storage_state()
        context_kwargs: dict[str, Any] = {}
        if storage_state:
            context_kwargs["storage_state"] = storage_state
        locale = self.config.get("browser_locale") or self.config.get("locale")
        timezone = self.config.get("browser_timezone") or self.config.get("timezone_id")
        if locale:
            context_kwargs["locale"] = locale
        if timezone:
            context_kwargs["timezone_id"] = timezone
        accept_language = str(self.config.get("accept_language") or "").strip()
        if accept_language:
            context_kwargs["extra_http_headers"] = {"Accept-Language": accept_language}
        return context_kwargs

    def _chromium_launch_args(self) -> list[str]:
        raw = self.config.get("browser_launch_args") or self.config.get("chromium_args") or []
        if isinstance(raw, str):
            args = [part.strip() for part in raw.replace("\\n", ",").split(",") if part.strip()]
        elif isinstance(raw, (list, tuple, set)):
            args = [str(part).strip() for part in raw if str(part).strip()]
        else:
            args = []
        if os.name != "nt":
            args.extend([
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-background-networking",
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--no-first-run",
                "--no-default-browser-check",
            ])
        seen: set[str] = set()
        return [arg for arg in args if not (arg in seen or seen.add(arg))]

    def _build_proxy_config(self) -> Optional[dict[str, str]]:
        proxy_url = str(self.config.get("proxy") or "").strip()
        if not proxy_url:
            return None
        mode = str(self.config.get("lajiao_proxy_mode") or "").strip().lower()
        if mode in {"credential", "credentials", "account", "auth"} and "@" in proxy_url:
            from core.proxy.credential_runtime import CredentialProxyRuntime

            runtime = CredentialProxyRuntime(self.config, log_fn=self._log)
            runtime_proxy_url = runtime.runtime_url(proxy_url)
            proxy_url = runtime.start_browser_bridge(runtime_proxy_url)
            self._proxy_runtime = runtime
        return build_playwright_proxy_config(proxy_url)

    def _resolve_storage_state(self) -> Optional[str]:
        """Resolve storage state path, respecting force-fresh override.

        Mirrors the inline _force_fresh_browser_context logic from
        full_pipeline.py:2855-2856 / 2886-2887.
        """
        if self.config.get("_force_fresh_browser_context"):
            return None
        storage_state = str(
            self.config.get("_browser_storage_state")
            or self.config.get("browser_storage_state_path")
            or ""
        ).strip()
        return storage_state or None

    def _load_storage_state_payload(self, storage_state: str) -> dict[str, Any]:
        try:
            with open(storage_state, "r", encoding="utf-8") as f:
                payload = json.load(f) or {}
        except Exception as exc:
            self._log(f"  load browser storage_state failed: {exc}")
            return {}
        if not isinstance(payload, dict):
            self._log("  load browser storage_state failed: invalid payload")
            return {}
        return payload

    def _restore_storage_state_into_context(self, storage_state: str) -> None:
        if not self.browser_context:
            return
        payload = self._load_storage_state_payload(storage_state)
        if not payload:
            return
        cookies = payload.get("cookies") or []
        if cookies:
            try:
                self.browser_context.add_cookies(cookies)
            except Exception as exc:
                self._log(f"  restore browser cookies failed: {exc}")
        origins = payload.get("origins") or []
        if not origins:
            return
        scratch_page = self.browser_context.new_page()
        try:
            for origin_state in origins:
                origin_url = str(origin_state.get("origin") or "").strip()
                local_storage = origin_state.get("localStorage") or []
                if not origin_url or not local_storage:
                    continue
                try:
                    scratch_page.goto(origin_url, wait_until="domcontentloaded")
                    scratch_page.evaluate(
                        """entries => {
                            for (const entry of entries || []) {
                                if (!entry || !entry.name) continue;
                                localStorage.setItem(entry.name, String(entry.value ?? ""));
                            }
                        }""",
                        local_storage,
                    )
                except Exception as exc:
                    self._log(f"  restore browser storage origin failed: {origin_url} ({exc})")
        finally:
            try:
                scratch_page.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Device identity (risk control)
    # ------------------------------------------------------------------

    def _seed_oai_device_id(self, device_id: str) -> None:
        """Seed oai-did cookie across all 5 OpenAI domains.

        Delegates to _seed_browser_device_id from browser_register,
        which sets the cookie on: chatgpt.com, .chatgpt.com,
        openai.com, auth.openai.com, .auth.openai.com.

        This is the same function used by the archived RegisterPipeline
        in step_prepare_registration_environment (full_pipeline.py:770).
        """
        if not self.page:
            return
        from platforms.chatgpt.browser_register import _seed_browser_device_id

        _seed_browser_device_id(self.page, device_id)

    # ------------------------------------------------------------------
    # Debug events
    # ------------------------------------------------------------------

    def _attach_debug_events(self) -> None:
        """Attach page debug event listeners.

        Mirrors RegisterPipeline._attach_page_debug_events from
        _archive/refactor_2026/full_pipeline.py:790-813.
        """
        if not self.page:
            return
        try:
            page = self.page

            def _remember_oauth_callback(url: str) -> None:
                value = str(url or "")
                if "state=" not in value or ("code=" not in value and "error=" not in value):
                    return
                if "/auth/callback" not in value and "localhost" not in value:
                    return
                try:
                    setattr(page, "_omp_last_oauth_callback_url", value)
                except Exception:
                    pass

            def _request_failed(req: Any) -> None:
                try:
                    _remember_oauth_callback(req.url)
                except Exception:
                    pass
                failure = ""
                try:
                    value = req.failure
                    if callable(value):
                        value = value()
                    failure = str(value or "")
                except Exception as exc:
                    failure = f"failure-read-error: {exc}"
                self._log(
                    f"  [requestfailed] {req.method} {req.url[:220]} -> {failure[:240]}"
                )

            page.on(
                "pageerror",
                lambda exc: self._log(f"  [pageerror] {str(exc)[:500]}"),
            )
            page.on(
                "console",
                lambda msg: (
                    self._log(f"  [console:{msg.type}] {str(msg.text)[:500]}")
                    if msg.type in ("error", "warning")
                    else None
                ),
            )
            page.on("requestfailed", _request_failed)
            def _frame_navigated(frame: Any) -> None:
                if frame != page.main_frame:
                    return
                try:
                    _remember_oauth_callback(frame.url)
                except Exception:
                    pass
                self._log(f"  [navigated] {frame.url[:220]}")

            page.on("framenavigated", _frame_navigated)
            page.on("close", lambda: self._log("  [page] closed"))
            try:
                page.on("crash", lambda: self._log("  [page] crash"))
            except Exception:
                pass
        except Exception as exc:
            self._log(f"  [debug] attach page events failed: {exc}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_csrf_token(self) -> str:
        """Fetch the ChatGPT CSRF token from the browser page.

        Delegates to _get_browser_csrf_token from browser_register,
        same helper used by the archived RegisterPipeline.
        """
        if not self.page:
            return ""
        from platforms.chatgpt.browser_register import _get_browser_csrf_token

        return _get_browser_csrf_token(self.page)
    def extract_access_token(self, timeout: int = 60) -> str:
        """Poll /api/auth/session until an access token is available."""
        if not self.page:
            return ""
        attempts = max(1, int(timeout / 2))
        result = extract_chatgpt_access_token(self.page, attempts=attempts, delay=2.0, log_fn=self._log)
        return result.access_token

    def save_storage_state(self, path: str) -> str:
        """Persist browser context storage state to a JSON file.

        Mirrors RegisterPipeline._save_browser_storage_state from
        _archive/refactor_2026/full_pipeline.py:3160-3168.
        """
        if not self.browser_context:
            return ""
        try:
            self.browser_context.storage_state(path=path)
            return str(path)
        except Exception as exc:
            self._log(f"  save browser storage_state failed: {exc}")
            return ""

    # ------------------------------------------------------------------
    # Fingerprint helpers (static — usable without a browser instance)
    # ------------------------------------------------------------------

    @staticmethod
    def random_chrome_fingerprint() -> tuple[str, str, str]:
        """Generate a random Chrome fingerprint (UA, sec-ch-ua, impersonate).

        Mirrors OAuthClient._random_chrome_fingerprint from
        platforms/chatgpt/oauth_client.py:144-178.
        """
        import random

        profiles = [
            {
                "major": 131,
                "impersonate": "chrome131",
                "build": 6778,
                "patch_range": (69, 205),
                "sec_ch_ua": (
                    '"Google Chrome";v="131", "Chromium";v="131", '
                    '"Not_A Brand";v="24"'
                ),
            },
            {
                "major": 133,
                "impersonate": "chrome133a",
                "build": 6943,
                "patch_range": (33, 153),
                "sec_ch_ua": (
                    '"Not(A:Brand";v="99", "Google Chrome";v="133", '
                    '"Chromium";v="133"'
                ),
            },
            {
                "major": 136,
                "impersonate": "chrome136",
                "build": 7103,
                "patch_range": (48, 175),
                "sec_ch_ua": (
                    '"Chromium";v="136", "Google Chrome";v="136", '
                    '"Not.A/Brand";v="99"'
                ),
            },
        ]
        profile = random.choice(profiles)
        major = profile["major"]
        build = profile["build"]
        patch = random.randint(*profile["patch_range"])
        full_ver = f"{major}.0.{build}.{patch}"
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{full_ver} Safari/537.36"
        )
        return ua, profile["sec_ch_ua"], profile["impersonate"]

    @staticmethod
    def generate_datadog_trace() -> dict[str, str]:
        """Generate Datadog APM trace headers for API requests.

        Mirrors the generate_datadog_trace pattern from
        platforms/chatgpt/utils.py:79-92 and
        _generate_datadog_trace_headers from browser_register.py:1240-1252.
        """
        import random

        trace_id = str(random.getrandbits(64))
        parent_id = str(random.getrandbits(64))
        trace_hex = format(int(trace_id), "016x")
        parent_hex = format(int(parent_id), "016x")
        return {
            "traceparent": f"00-0000000000000000{trace_hex}-{parent_hex}-01",
            "tracestate": "dd=s:1;o:rum",
            "x-datadog-origin": "rum",
            "x-datadog-parent-id": parent_id,
            "x-datadog-sampling-priority": "1",
            "x-datadog-trace-id": trace_id,
        }

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def _cleanup(self) -> None:
        if self.browser_context:
            try:
                self.browser_context.close()
            except Exception:
                pass
        if self.browser:
            try:
                self.browser.close()
            except Exception:
                pass
        if self._camoufox_ctx:
            try:
                self._camoufox_ctx.__exit__(None, None, None)
            except Exception:
                pass
        if self.playwright_instance:
            try:
                self.playwright_instance.stop()
            except Exception:
                pass
        if self._proxy_runtime:
            try:
                self._proxy_runtime.cleanup()
            except Exception:
                pass

    def _log(self, msg: str) -> None:
        self._log_messages.append(msg)
        if self._external_log_fn:
            self._external_log_fn(msg)
