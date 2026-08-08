from __future__ import annotations

import base64
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.mailbox.outlook_token import OutlookTokenAccount
from registration.email_register import EmailRegistrationOrchestrator


def _fake_jwt() -> str:
    payload = {
        "sub": "auth0_test",
        "email": "unit@example.invalid",
        "https://claims.example.invalid/auth": {"chatgpt_account_id": "acct_unit", "chatgpt_plan_type": "free"},
        "https://claims.example.invalid/profile": {"email": "unit@example.invalid"},
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii").rstrip("=")
    return f"hdr.{encoded}.sig"


class FakeContext:
    def cookies(self):
        return [{"name": "__Secure-next-auth.session-token", "value": "session-token"}]

    def storage_state(self, path: str):
        Path(path).write_text('{"cookies":[]}', encoding="utf-8")

    def close(self):
        pass


class FakePage:
    url = "https://service.example.invalid/"
    context = FakeContext()

    def goto(self, *args, **kwargs):
        self.url = args[0]


class FakeBrowserSession:
    def __init__(self, config):
        self.config = config
        self.page = FakePage()
        self.browser_context = self.page.context
        self.device_id = "device"

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def save_storage_state(self, path: str) -> str:
        self.browser_context.storage_state(path=path)
        return path


class FakeMailbox:
    used: list[str] = []

    def __init__(self, config, log_fn=None):
        self.log_fn = log_fn or (lambda _msg: None)

    def first(self, email=""):
        return OutlookTokenAccount("unit@example.invalid", "mail-pw", "cid", "rt")

    def wait_for_openai_code(self, account, *, timeout=180):
        self.log_fn("  使用 Outlook Graph 自动读取验证码: unit@example.invalid")
        self.log_fn("  Outlook Graph 获取验证码: 123456")
        return "123456"

    def mark_used(self, email, reason="registered"):
        self.used.append(email)

    def mark_cooldown(self, email, reason):
        raise AssertionError("success path must not cooldown")


class FakeTokenResult:
    success = True
    access_token = _fake_jwt()
    status = "ok"
    failure_reason = ""


def test_context_token_fallback_uses_active_browser_origin() -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    class Response:
        status = 200

        def text(self) -> str:
            return '{"accessToken": "fallback-token"}'

        def json(self) -> dict[str, str]:
            return {"accessToken": "fallback-token"}

    class Request:
        def get(self, url: str, *, headers: dict[str, str], timeout: int) -> Response:
            assert timeout == 30000
            calls.append((url, headers))
            return Response()

    class Context:
        request = Request()

    class Session:
        browser_context = Context()
        page = type("Page", (), {"url": "https://service.example.invalid/complete"})()

    assert EmailRegistrationOrchestrator(log_fn=lambda _message: None)._extract_access_token_via_context_request(Session()) == "fallback-token"
    assert calls == [
        (
            "https://service.example.invalid/api/auth/session?refresh=true&reason=email_register_extract",
            {"accept": "application/json", "referer": "https://service.example.invalid/"},
        )
    ]

def test_modular_email_register_success(monkeypatch, tmp_path: Path) -> None:
    import registration.email_register as module
    from platforms.chatgpt import browser_register

    FakeMailbox.used = []
    monkeypatch.setattr(module, "BrowserSession", FakeBrowserSession)
    monkeypatch.setattr(module, "OutlookTokenMailbox", FakeMailbox)
    monkeypatch.setattr(module, "extract_chatgpt_access_token", lambda *args, **kwargs: FakeTokenResult())
    monkeypatch.setattr(module.account_store, "upsert_account", lambda source, **kwargs: dict(source))
    monkeypatch.setattr(browser_register, "_browser_registration_flow", lambda page, email, password, otp, phone, log: {"page_type": "complete"})
    monkeypatch.setattr(browser_register, "_get_cookies", lambda page: {"__Secure-next-auth.session-token": "session-token"})

    result = EmailRegistrationOrchestrator(log_fn=lambda _msg: None).run(
        {
            "mailbox_provider": "outlook_token",
            "output_dir": str(tmp_path / "output"),
            "rotate_proxy_each_attempt": False,
            "proxy": "",
            "chatgpt_password": "GeneratedPw1!",
        },
        headed=False,
        task_id="task-unit",
    )

    assert result["success"] is True
    assert result["email"] == "unit@example.invalid"
    assert result["password"] == "GeneratedPw1!"
    assert result["account_id"] == "acct_unit"
    assert result["access_token"] == FakeTokenResult.access_token
    assert result["registration_status"] == "registered"
    assert result["registration_mode"] == "email"
    assert result["binding_status"] == "not_ready"
    assert Path(result["registered_file"]).exists()
    assert Path(result["resume_file"]).exists()
    assert FakeMailbox.used == ["unit@example.invalid"]


def test_modular_email_register_prechecks_configured_proxy(monkeypatch, tmp_path: Path) -> None:
    import registration.email_register as module

    checks = []

    class FakeRuntime:
        def __init__(self, config, log_fn=None):
            self.config = config

        def check(self, proxy):
            checks.append(proxy)
            return True, "203.0.113.30"

    monkeypatch.setattr(module, "BrowserSession", FakeBrowserSession)
    monkeypatch.setattr(module, "OutlookTokenMailbox", FakeMailbox)
    monkeypatch.setattr(module, "CredentialProxyRuntime", FakeRuntime)
    monkeypatch.setattr(module, "extract_chatgpt_access_token", lambda *args, **kwargs: FakeTokenResult())
    monkeypatch.setattr(module.account_store, "upsert_account", lambda source, **kwargs: dict(source))
    from platforms.chatgpt import browser_register

    monkeypatch.setattr(browser_register, "_browser_registration_flow", lambda page, email, password, otp, phone, log: {"page_type": "complete"})
    monkeypatch.setattr(browser_register, "_get_cookies", lambda page: {"__Secure-next-auth.session-token": "session-token"})

    result = EmailRegistrationOrchestrator(log_fn=lambda _msg: None).run(
        {
            "mailbox_provider": "outlook_token",
            "output_dir": str(tmp_path / "output"),
            "rotate_proxy_each_attempt": False,
            "proxy": "socks5://configured-proxy",
            "chatgpt_password": "GeneratedPw1!",
        },
        headed=False,
        task_id="task-proxy",
    )

    assert result["success"] is True
    assert result["registration_proxy"] == "socks5://configured-proxy"
    assert result["registration_proxy_exit_ip"] == "203.0.113.30"
    assert checks == ["socks5://configured-proxy"]


def test_modular_email_register_rejects_bad_proxy_before_browser(monkeypatch, tmp_path: Path) -> None:
    import registration.email_register as module

    class FailRuntime:
        def __init__(self, config, log_fn=None):
            pass

        def check(self, proxy):
            return False, ""

    class NoLaunchBrowserSession(FakeBrowserSession):
        def __init__(self, config):
            raise AssertionError("browser must not launch before proxy precheck passes")

    monkeypatch.setattr(module, "BrowserSession", NoLaunchBrowserSession)
    monkeypatch.setattr(module, "OutlookTokenMailbox", FakeMailbox)
    monkeypatch.setattr(module, "CredentialProxyRuntime", FailRuntime)

    try:
        EmailRegistrationOrchestrator(log_fn=lambda _msg: None).run(
            {
                "mailbox_provider": "outlook_token",
                "output_dir": str(tmp_path / "output"),
                "rotate_proxy_each_attempt": False,
                "proxy": "socks5://bad-proxy",
                "chatgpt_password": "GeneratedPw1!",
            },
            headed=False,
            task_id="task-bad-proxy",
        )
    except RuntimeError as exc:
        assert "邮箱注册代理 OpenAI 预检失败" in str(exc)
    else:
        raise AssertionError("bad proxy must fail before browser launch")


def test_modular_email_register_supports_icloud_privacy(monkeypatch, tmp_path: Path) -> None:
    import registration.email_register as module
    from platforms.chatgpt import browser_register

    alias = "alias-one@example.invalid"

    class FakePrivacyAccount:
        email = alias
        account_id = alias
        extra = {"provider_name": "icloud_privacy"}

    class FakePrivacyMailbox:
        used: list[str] = []

        @classmethod
        def from_config(cls, config):
            return cls()

        def create_account(self):
            return FakePrivacyAccount()

        def get_current_ids(self, account):
            return {"old"}

        def wait_for_code(self, account, *, timeout=180, before_ids=None, code_pattern=None):
            assert account.email == alias
            assert before_ids == {"old"}
            return "654321"

        def _record_state(self, email, status, *, reason=""):
            if status == "consumed":
                self.used.append(email)

    payload = {
        "sub": "auth0_privacy",
        "email": alias,
        "https://claims.example.invalid/auth": {"chatgpt_account_id": "acct_privacy", "chatgpt_plan_type": "free"},
        "https://claims.example.invalid/profile": {"email": alias},
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii").rstrip("=")

    class PrivacyTokenResult:
        success = True
        access_token = f"hdr.{encoded}.sig"
        status = "ok"
        failure_reason = ""

    FakePrivacyMailbox.used = []
    monkeypatch.setattr(module, "BrowserSession", FakeBrowserSession)
    monkeypatch.setattr(module, "ICloudPrivacyMailbox", FakePrivacyMailbox)
    monkeypatch.setattr(module, "extract_chatgpt_access_token", lambda *args, **kwargs: PrivacyTokenResult())
    monkeypatch.setattr(module.account_store, "upsert_account", lambda source, **kwargs: dict(source))
    monkeypatch.setattr(browser_register, "_browser_registration_flow", lambda page, email, password, otp, phone, log: {"page_type": "complete", "otp": otp()})
    monkeypatch.setattr(browser_register, "_get_cookies", lambda page: {"__Secure-next-auth.session-token": "session-token"})

    result = EmailRegistrationOrchestrator(log_fn=lambda _msg: None).run(
        {
            "mailbox_provider": "icloud_privacy",
            "output_dir": str(tmp_path / "output"),
            "rotate_proxy_each_attempt": False,
            "proxy": "",
            "chatgpt_password": "GeneratedPw1!",
        },
        headed=False,
        task_id="task-privacy",
    )

    assert result["success"] is True
    assert result["email"] == alias
    assert result["mailbox_provider"] == "icloud_privacy"
    assert result["email_provider"] == "icloud_privacy"
    assert result["icloud_privacy_email"] == alias
    assert result["account_id"] == "acct_privacy"
    assert FakePrivacyMailbox.used == [alias]



def test_email_otp_baseline_is_captured_before_browser_flow() -> None:
    class FakeAccount:
        email = "alias-two@example.invalid"

    class FakeMailbox:
        def __init__(self):
            self.ids = {"old"}
            self.wait_before_ids = None

        def get_current_ids(self, account):
            return set(self.ids)

        def wait_for_code(self, account, *, timeout=180, before_ids=None, code_pattern=None):
            self.wait_before_ids = set(before_ids or set())
            return "112233"

    mailbox = FakeMailbox()
    account = FakeAccount()
    orchestrator = EmailRegistrationOrchestrator(log_fn=lambda _msg: None)
    before_ids = mailbox.get_current_ids(account)
    mailbox.ids.add("new-openai-code")

    assert orchestrator._wait_email_code(mailbox, account, timeout=1, before_ids=before_ids) == "112233"
    assert mailbox.wait_before_ids == {"old"}
