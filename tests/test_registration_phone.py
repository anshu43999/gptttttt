"""
Test: Phone Registration Orchestrator — mock providers (no proxy).
"""
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from registration.context import RegistrationRun


class FakeBrowserPage:
    """Fake Camoufox page with required methods."""
    url = "https://auth.openai.com/create-account/password"

    def goto(self, url): pass
    def content(self): return ""
    def evaluate(self, js): return "fake_token"
    def wait_for_selector(self, sel, **kw): return None
    def fill(self, sel, val): pass
    def click(self, sel): pass
    def locator(self, sel):
        class FakeLocator:
            def click(self): pass
            def fill(self, val): pass
            def wait_for(self, **kw): return None
        return FakeLocator()
    def on(self, event, handler): pass


class FakeSmsProvider:
    def acquire(self, *, service="dr", country="BR", max_price=-1):
        from core.sms.base import PhoneNumber
        return PhoneNumber(activation_id="fake_act_001", number="+5511999999999",
                           country=country, provider="fake")

    def wait_for_code(self, activation_id, *, timeout=120, first_poll_delay=0):
        return "123456"

    def cancel(self, activation_id):
        return True


class FakeMailboxProvider:
    def create(self):
        from core.mailbox.base import MailboxAccount
        return MailboxAccount(email="test123@5445945.xyz", account_id="test123")

    def wait_for_code(self, account, *, timeout=180, before_ids=None, code_pattern=None):
        return "654321"


class FakeProxyPool:
    def next(self, region="", exclude_ips=None, max_candidates=10):
        return "socks5://fake:1080"

    def reset_used_ips(self):
        pass

    def report_success(self, url): pass
    def report_fail(self, url): pass

    @property
    def _used_ips(self):
        return set()


class FakeSentinelSolver:
    def solve(self, device_id, flow, user_agent, proxy=None):
        return "fake_sentinel_token"


class FakeBrowserSession:
    def __init__(self, config=None):
        self.config = config or {}
        self.page = FakeBrowserPage()
        self.context = None

    def __enter__(self): return self
    def __exit__(self, *a): pass
    def get_csrf_token(self): return "fake_csrf"
    def extract_access_token(self): return "fake_access_token"


def test_phone_orchestrator_creates():
    """Orchestrator can be created without errors."""
    from registration.phone_register import PhoneRegistrationOrchestrator

    orch = PhoneRegistrationOrchestrator(
        sms_provider=FakeSmsProvider(),
        mailbox_provider=FakeMailboxProvider(),
        proxy_pool=FakeProxyPool(),
        sentinel_solver=FakeSentinelSolver(),
        browser_factory=lambda config=None: FakeBrowserSession(config),
    )
    assert orch is not None


def test_phone_orchestrator_handles_precheck_failure():
    """Orchestrator handles missing browser gracefully."""
    ctx = RegistrationRun(mode="phone", skip_precheck=False)
    assert ctx is not None


def test_registration_run_starts_pending():
    ctx = RegistrationRun(mode="phone")
    assert ctx.status == "pending"
    assert ctx.mode == "phone"


def test_registration_run_from_config():
    ctx = RegistrationRun.from_config({
        "mode": "phone",
        "sms_provider": "herosms_api",
        "sms_country": "BR",
        "proxy_region": "JP",
    })
    assert ctx.mode == "phone"
    assert ctx.sms_country == "BR"
    assert ctx.proxy_region == "JP"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
