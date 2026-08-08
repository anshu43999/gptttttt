"""
注册任务服务 — 直接调用编排器, 不走 CLI subprocess。
替代 full_pipeline.py 的 subprocess 调用路径。
"""
from __future__ import annotations

import threading
import time
from typing import Any

from core.config_loader import load_config

from registration.context import RegistrationRun
from infrastructure.db import (
    create_registration_run, update_registration_run, get_registration_run,
    list_registration_runs,
)


class RegisterService:
    def __init__(self, db_path=None):
        self.db_path = db_path
        self._runs: dict[str, RegistrationRun] = {}
        self._lock = threading.Lock()

    # ── public API ─────────────────────────────────────────

    def start_register(self, config: dict[str, Any]) -> str:
        resolved_config = self._resolved_config(config)
        run = RegistrationRun.from_config(resolved_config)
        self._runs[run.run_id] = run
        create_registration_run(run.to_dict(), path=self.db_path)

        thread = threading.Thread(target=self._execute, args=(run, resolved_config), daemon=True)
        thread.start()
        return run.run_id

    def start_oauth_bind(self, account_key: str, config: dict[str, Any]) -> str:
        resolved_config = self._resolved_config(config)
        run = RegistrationRun.from_config(resolved_config)
        run.mode = "oauth_bind"
        run.email = account_key
        self._runs[run.run_id] = run
        create_registration_run(run.to_dict(), path=self.db_path)

        thread = threading.Thread(target=self._execute_oauth_bind,
                                  args=(run, account_key, resolved_config), daemon=True)
        thread.start()
        return run.run_id

    def get_status(self, run_id: str, since_id: int = 0) -> dict[str, Any]:
        run = self._runs.get(run_id)
        if run:
            return {
                "status": run.status,
                "step": run.steps[-1] if run.steps else "initializing",
                "progress": len(run.steps) / max(len(run.steps) + 3, 1),
                "steps_completed": run.steps.copy(),
                "errors": run.errors.copy(),
            }
        db_run = get_registration_run(run_id, path=self.db_path)
        return db_run if db_run else {"status": "not_found"}

    def cancel(self, run_id: str) -> bool:
        run = self._runs.get(run_id)
        if run and run.status in ("pending", "running"):
            run.status = "cancelled"
            update_registration_run(run_id, status="cancelled",
                                    finished_at=time.time(), path=self.db_path)
            return True
        return False

    def list_runs(self, status: str = "", limit: int = 50) -> list[dict[str, Any]]:
        return list_registration_runs(status=status, limit=limit, path=self.db_path)

    # ── execution ──────────────────────────────────────────

    def _execute(self, run: RegistrationRun, config: dict[str, Any]) -> None:
        run.status = "running"
        run.started_at = time.time()
        self._update_db(run, status="running", started_at=run.started_at)

        try:
            self._execute_phone_register(run, config)
            run.status = "success"
        except Exception as exc:
            run.status = "failed"
            run.errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            run.finished_at = time.time()
            self._update_db(run, status=run.status,
                            steps_completed=str(run.steps),
                            errors=str(run.errors),
                            finished_at=run.finished_at)

    @staticmethod
    def _resolved_config(config: dict[str, Any]) -> dict[str, Any]:
        resolved_config = load_config()
        resolved_config.update(config)
        return resolved_config

    @staticmethod
    def _configured_forwarded_mailbox(config: dict[str, Any]) -> Any:
        from core.mailbox_providers import ForwardedDomainMailbox

        mailbox = ForwardedDomainMailbox.from_config(config)
        missing = [
            key for key, value in {
                "mailbox_domain": mailbox.domain,
                "mailbox_imap_user": mailbox.imap_user,
                "mailbox_imap_pass": mailbox.imap_pass,
            }.items() if not str(value or "").strip()
        ]
        if missing:
            raise RuntimeError(
                "Missing required forwarded mailbox configuration: " + ", ".join(missing)
            )
        return mailbox

    def _execute_phone_register(self, run: RegistrationRun, config: dict[str, Any]) -> None:
        mailbox = self._configured_forwarded_mailbox(config)

        from core.base_sms import create_sms_provider
        from core.proxy.pool import ProxyPool
        from core.sentinel.solver import SentinelSolver
        from registration.phone_register import PhoneRegistrationOrchestrator

        run.steps.append("init_providers")

        sms = create_sms_provider(run.sms_provider_key, config)

        proxy_pool = ProxyPool()
        sentinel = SentinelSolver()

        def browser_factory(browser_config=None):
            from core.browser.session import BrowserSession
            return BrowserSession(browser_config or {})

        orchestrator = PhoneRegistrationOrchestrator(
            sms_provider=sms,
            mailbox_provider=mailbox,
            proxy_pool=proxy_pool,
            sentinel_solver=sentinel,
            browser_factory=browser_factory,
        )
        orchestrator.run(run)

    def _execute_oauth_bind(
        self, run: RegistrationRun, account_key: str, config: dict[str, Any]
    ) -> None:
        run.status = "running"
        run.started_at = time.time()
        self._update_db(run, status="running", started_at=run.started_at)

        try:
            mailbox = self._configured_forwarded_mailbox(config)

            from core.oauth.client import OAuthClient
            from core.browser.session import BrowserSession
            from registration.oauth_bind import OAuthBindingOrchestrator

            oauth = OAuthClient()

            def browser_factory(browser_config=None):
                return BrowserSession(browser_config or {})

            orchestrator = OAuthBindingOrchestrator(
                mailbox_provider=mailbox,
                oauth_client=oauth,
                browser_factory=browser_factory,
            )

            # Find resume state path for this account
            resume_path = None  # TODO: lookup from account artifacts

            orchestrator.run(run, resume_path)
            run.status = "success"
        except Exception as exc:
            run.status = "failed"
            run.errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            run.finished_at = time.time()
            self._update_db(run, status=run.status,
                            steps_completed=str(run.steps),
                            errors=str(run.errors),
                            finished_at=run.finished_at)

    def _update_db(self, run: RegistrationRun, **kwargs) -> None:
        try:
            update_registration_run(
                run.run_id,
                phone=run.phone_number,
                email=run.email,
                proxy_ip=run.proxy_ip or "",
                proxy_region=run.proxy_region or "",
                plan_type=run.plan_type or "",
                access_token_obtained=int(bool(run.access_token)),
                refresh_token_obtained=int(bool(run.refresh_token)),
                **kwargs,
                path=self.db_path,
            )
        except Exception:
            pass
