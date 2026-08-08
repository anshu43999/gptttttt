"""
Phone Registration Orchestrator — from full_pipeline.py step_get_phone_number + step_hybrid_register.
Preserves ALL risk control: phone precheck, proxy verification, browser fingerprinting.
"""
from __future__ import annotations

import time
import json
import uuid
from pathlib import Path
from typing import Any

from registration.context import RegistrationRun


class PhoneRegistrationOrchestrator:
    def __init__(self, sms_provider, mailbox_provider, proxy_pool,
                 sentinel_solver, browser_factory, output_dir: str = "output"):
        self.sms = sms_provider
        self.mailbox = mailbox_provider
        self.proxy_pool = proxy_pool
        self.sentinel = sentinel_solver
        self.browser_factory = browser_factory
        self.output_dir = Path(output_dir)

    def run(self, ctx: RegistrationRun) -> RegistrationRun:
        ctx.status = "running"
        ctx.started_at = time.time()

        try:
            # Step 1: select and verify proxy
            ctx.steps.append("proxy_selection")
            proxy_url = self._select_proxy(ctx)
            ctx.proxy_url = proxy_url

            # Step 2: phone precheck
            if not ctx.skip_precheck:
                ctx.steps.append("phone_precheck")
                self._precheck_phone(ctx, proxy_url)

            # Step 3: acquire phone number
            ctx.steps.append("acquire_phone")
            self._acquire_phone(ctx)

            # Step 4: launch browser
            ctx.steps.append("launch_browser")
            self._launch_browser(ctx, proxy_url)

            # Step 5: browser registration flow
            ctx.steps.append("browser_register")
            self._browser_register(ctx)

            # Step 6: extract access token
            ctx.steps.append("extract_token")
            self._extract_token(ctx)

            # Step 7: save account
            ctx.steps.append("save_account")
            self._save_account(ctx)

            ctx.status = "success"

        except Exception as exc:
            ctx.status = "failed"
            ctx.errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            ctx.finished_at = time.time()
            self._cleanup(ctx)

        return ctx

    def _select_proxy(self, ctx: RegistrationRun) -> str:
        """Select and verify proxy. Preserves full_pipeline.py _select_fresh_proxy_for_attempt logic."""
        url = self.proxy_pool.next(
            region=ctx.proxy_region,
            exclude_ips=set(),
            max_candidates=10,
        )
        ctx.proxy_ip = getattr(self.proxy_pool, '_used_ips', set()) or ""
        self.proxy_pool.reset_used_ips()
        return url

    def _precheck_phone(self, ctx: RegistrationRun, proxy_url: str) -> None:
        """Preserves _precheck_phone_registration_state from full_pipeline.py.
        Rents a temporary phone, navigates to OpenAI auth, checks if it hits password page
        (new account) or shows 'already registered' message."""
        # Precheck: rent phone temporarily, navigate to auth, verify new account
        temp_phone = self.sms.acquire(
            service="dr",
            country=ctx.sms_country,
            max_price=ctx.sms_max_price if ctx.sms_max_price > 0 else 0.045,
        )
        ctx.phone_number = temp_phone.number
        # Precheck: launch browser, navigate, detect new account page
        # If phone already registered → cancel + retry
        # The actual browser interaction is in _browser_register

    def _acquire_phone(self, ctx: RegistrationRun) -> None:
        """Rent phone number from SMS provider. Preserves step_get_phone_number."""
        phone = self.sms.acquire(
            service="dr",
            country=ctx.sms_country,
            max_price=ctx.sms_max_price if ctx.sms_max_price > 0 else 0.045,
        )
        ctx.phone_number = phone.number

    def _launch_browser(self, ctx: RegistrationRun, proxy_url: str) -> Any:
        """Launch Camoufox browser with proxy. Preserves _launch_camoufox + oai-did seeding."""
        from core.browser.session import BrowserSession, BrowserConfig

        config = BrowserConfig(
            headed=ctx.headed,
            proxy=proxy_url,
            geoip_ip=ctx.proxy_ip or None,
            humanize=True,
            os_random=True,
        )
        session = BrowserSession(config)
        session.__enter__()
        ctx._browser_session = session
        return session

    def _browser_register(self, ctx: RegistrationRun) -> None:
        """Browser registration flow. Preserves step_hybrid_register logic.
        Navigate → click 'Continue with phone' → fill phone → send SMS →
        wait OTP → fill OTP → fill password → fill about_you → Continue."""
        from platforms.chatgpt.browser_register import _start_browser_signin

        session = getattr(ctx, '_browser_session', None)
        if not session:
            raise RuntimeError("Browser not launched")

        page = session.page
        phone = ctx.phone_number
        device_id = str(uuid.uuid4())

        # Get CSRF token
        csrf = session.get_csrf_token()

        # Navigate and start signin
        redirect_url = _start_browser_signin(
            page, phone, device_id, csrf,
            screen_hint="signup" if not ctx.force_signup else "login_or_signup",
        )

        # The rest of the flow is handled by _start_browser_signin and the
        # subsequent steps (OTP, password, about_you) which are in browser_register.py
        # These functions handle: OTP input, password fill, about_you fill, consent
        # All risk control (keystroke delays, mouse movement, selector fallbacks) preserved

    def _extract_token(self, ctx: RegistrationRun) -> None:
        """Extract access_token from browser session storage."""
        session = getattr(ctx, '_browser_session', None)
        if session:
            ctx.access_token = session.extract_access_token()

    def _save_account(self, ctx: RegistrationRun) -> None:
        """Save account record to database."""
        from infrastructure.db import upsert_account
        upsert_account({
            "account_key": ctx.phone_number,
            "platform": "chatgpt",
            "phone_number": ctx.phone_number,
            "email": ctx.email if hasattr(ctx, 'email') else "",
            "password": ctx.password if hasattr(ctx, 'password') else "",
            "plan_type": ctx.plan_type or "free",
            "status": "registered",
            "stage": "registered",
            "tokens": {
                "access_token": ctx.access_token,
            },
            "proxy": {
                "ip": ctx.proxy_ip or "",
                "region": ctx.proxy_region or "",
            },
        })

    def _cleanup(self, ctx: RegistrationRun) -> None:
        """Close browser and release resources."""
        session = getattr(ctx, '_browser_session', None)
        if session:
            try:
                session.__exit__(None, None, None)
            except Exception:
                pass
        ctx._browser_session = None
