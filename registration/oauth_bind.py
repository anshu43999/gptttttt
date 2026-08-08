"""
OAuth Binding Orchestrator — from full_pipeline.py step_oauth_from_saved_session + _do_codex_oauth.
Preserves ALL risk control: 60-step state machine, consent multi-strategy, Outlook cooldown.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from registration.context import RegistrationRun


class OAuthBindingOrchestrator:
    def __init__(self, mailbox_provider, oauth_client, browser_factory,
                 output_dir: str = "output"):
        self.mailbox = mailbox_provider
        self.oauth = oauth_client
        self.browser_factory = browser_factory
        self.output_dir = Path(output_dir)

    def run(self, ctx: RegistrationRun, resume_state_path: str | None = None) -> RegistrationRun:
        ctx.status = "running"
        ctx.started_at = time.time()

        try:
            # Step 1: create mailbox account for OAuth binding
            ctx.steps.append("create_mailbox")
            mailbox_account = self.mailbox.create()
            ctx.email = mailbox_account.email

            # Step 2: generate PKCE OAuth URL
            ctx.steps.append("generate_oauth_url")
            code_verifier, code_challenge = self.oauth.generate_pkce()
            authorize_url = self.oauth.build_authorize_url(code_challenge)

            # Step 3: launch browser with saved session
            ctx.steps.append("launch_browser")
            session = self._launch_browser(ctx, resume_state_path)

            # Step 4: OAuth state machine (60 steps max)
            ctx.steps.append("oauth_state_machine")
            oauth_result = self._run_oauth_state_machine(
                session, authorize_url, code_verifier, mailbox_account, ctx,
            )

            # Step 5: exchange code for tokens, unless browser helper already returned tokens.
            ctx.steps.append("exchange_code")
            if isinstance(oauth_result, dict):
                tokens = oauth_result
            else:
                tokens = self.oauth.exchange_code(oauth_result, code_verifier)
            ctx.access_token = tokens.get("access_token", "")
            ctx.refresh_token = tokens.get("refresh_token", "")
            ctx.id_token = tokens.get("id_token", "")

            # Step 6: verify plan type
            ctx.steps.append("verify_plan")
            self._verify_plan(ctx)

            # Step 7: save to database
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

    def _launch_browser(self, ctx: RegistrationRun, resume_path: str | None):
        from core.browser.session import BrowserSession, BrowserConfig

        config = BrowserConfig(
            headed=ctx.headed,
            proxy=getattr(ctx, 'proxy_url', None),
            storage_state=resume_path if resume_path and Path(resume_path).exists() else None,
            humanize=True,
            os_random=True,
        )
        session = BrowserSession(config)
        session.__enter__()
        ctx._browser_session = session
        return session

    def _run_oauth_state_machine(self, session, authorize_url: str, code_verifier: str,
                                  mailbox_account, ctx: RegistrationRun) -> str | dict:
        """60-step OAuth state machine. Preserves _do_codex_oauth logic from full_pipeline.py.

        States: choose_account → login_email → login_password → add_email →
                email_otp_verification → consent → callback → extract code.
        """
        from platforms.chatgpt.browser_register import _do_codex_oauth

        otp_seen_ids: set[str] = set()

        def reset_otp_baseline() -> None:
            nonlocal otp_seen_ids
            getter = getattr(self.mailbox, "get_current_ids", None)
            if callable(getter):
                try:
                    otp_seen_ids = set(getter(mailbox_account) or set())
                    print(f"  OAuth 邮箱验证码等待基线: {len(otp_seen_ids)} 封历史邮件")
                    return
                except Exception as exc:
                    print(f"  OAuth 邮箱验证码基线获取失败: {str(exc).splitlines()[0][:160]}")
            otp_seen_ids = set()

        def otp_callback() -> str:
            for name in ("wait_for_code", "get_code", "poll_code"):
                callback = getattr(self.mailbox, name, None)
                if callable(callback):
                    try:
                        return str(callback(mailbox_account, before_ids=otp_seen_ids) or "")
                    except TypeError:
                        return str(callback(mailbox_account) or "")
            return ""

        reset_otp_baseline()
        cookies_dict = {}
        try:
            cookies_dict = {item["name"]: item["value"] for item in session.page.context.cookies()}
        except Exception:
            cookies_dict = {}
        result = _do_codex_oauth(
            page=session.page,
            cookies_dict=cookies_dict,
            email=ctx.email or ctx.phone_number,
            password=ctx.password,
            otp_callback=otp_callback,
            phone_callback=None,
            proxy=ctx.proxy_url or None,
            log=print,
            bind_email=mailbox_account.email,
            authorize_url=authorize_url,
            otp_baseline_callback=reset_otp_baseline,
        )
        if isinstance(result, dict) and result.get("code"):
            return str(result["code"])
        if isinstance(result, dict) and result.get("access_token"):
            return result
        raise RuntimeError("OAuth browser state machine did not return code or tokens")

    def _verify_plan(self, ctx: RegistrationRun) -> None:
        """Verify account plan type via subscription API."""
        if not ctx.access_token:
            return
        import requests
        try:
            resp = requests.get(
                "https://chatgpt.com/backend-api/accounts/check",
                headers={"Authorization": f"Bearer {ctx.access_token}"},
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                ctx.plan_type = data.get("plan_type", "free")
        except Exception:
            ctx.plan_type = "unknown"

    def _save_account(self, ctx: RegistrationRun) -> None:
        from infrastructure.db import upsert_account
        upsert_account({
            "account_key": ctx.phone_number or ctx.email,
            "platform": "chatgpt",
            "phone_number": ctx.phone_number or "",
            "email": ctx.email or "",
            "password": ctx.password or "",
            "plan_type": ctx.plan_type or "free",
            "status": "complete" if ctx.refresh_token else "oauth_complete",
            "stage": "complete" if ctx.refresh_token else "oauth_complete",
            "tokens": {
                "access_token": ctx.access_token,
                "refresh_token": ctx.refresh_token,
                "id_token": ctx.id_token,
            },
        })

    def _cleanup(self, ctx: RegistrationRun) -> None:
        session = getattr(ctx, '_browser_session', None)
        if session:
            try:
                session.__exit__(None, None, None)
            except Exception:
                pass
        ctx._browser_session = None
