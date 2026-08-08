from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from full_pipeline import RegisterPipeline
from core.base_sms import SmsActivation

from core.base_sms import UserProvidedSmsProvider



def test_phone_recently_used_japanese_error_is_retryable_phone_use() -> None:
    from platforms.chatgpt.browser_register import _is_phone_recently_or_already_used_error

    assert _is_phone_recently_or_already_used_error(
        "この電話番号は最近使用されたため、しばらくしてからもう一度お試しください。"
    )


def test_authorize_url_with_chatgpt_redirect_is_not_chatgpt_home() -> None:
    from platforms.chatgpt.browser_register import _infer_page_type

    url = "https://auth.openai.com/api/accounts/authorize?client_id=app&redirect_uri=https%3A%2F%2Fchatgpt.com%2Fapi%2Fauth%2Fcallback%2Fopenai&scope=openid"

    assert _infer_page_type(None, url) == ""



def test_japanese_about_you_name_and_birthday_mode() -> None:
    from platforms.chatgpt.browser_register import _pick_best_about_you_input, _select_about_you_mode

    entries = [
        {"visibleIndex": 0, "labels": ["氏名"], "name": "name"},
        {"visibleIndex": 1, "labels": ["生年月日"], "placeholder": "YYYY/MM/DD"},
    ]

    name_entry = _pick_best_about_you_input(entries, "name")
    assert name_entry is not None
    assert name_entry["visibleIndex"] == 0
    assert _select_about_you_mode(
        has_age_label=True,
        has_birthday_label=True,
        has_age_field=False,
        has_birthday_field=True,
        has_birthday_select=False,
    ) == "birthday"


def test_japanese_about_you_heading_does_not_force_age_mode() -> None:
    from platforms.chatgpt.browser_register import _select_about_you_mode

    assert _select_about_you_mode(
        has_age_label=True,
        has_birthday_label=False,
        has_age_field=False,
        has_birthday_field=True,
        has_birthday_select=False,
    ) == "birthday"


def test_japanese_about_you_birth_year_mode_uses_year_only() -> None:
    from platforms.chatgpt.browser_register import _pick_best_about_you_input, _select_about_you_mode

    entries = [
        {"visibleIndex": 0, "labels": ["氏名"], "name": "name"},
        {"visibleIndex": 1, "labels": ["生年"], "placeholder": "YYYY"},
    ]

    birth_year_entry = _pick_best_about_you_input(entries, "birth_year", exclude_visible_indices={0})
    assert birth_year_entry is not None
    assert birth_year_entry["visibleIndex"] == 1
    assert _select_about_you_mode(
        has_age_label=True,
        has_birthday_label=False,
        has_birth_year_label=True,
        has_age_field=False,
        has_birthday_field=False,
        has_birth_year_field=True,
        has_birthday_select=False,
    ) == "birth_year"


def test_japanese_about_you_age_label_stays_age_mode() -> None:
    from platforms.chatgpt.browser_register import _pick_best_about_you_input, _select_about_you_mode

    entries = [
        {"visibleIndex": 0, "labels": ["氏名"], "name": "name"},
        {"visibleIndex": 1, "labels": ["年齢"], "name": "age"},
    ]

    assert _pick_best_about_you_input(entries, "birth_year", exclude_visible_indices={0}) is None
    assert _select_about_you_mode(
        has_age_label=True,
        has_birthday_label=False,
        has_age_field=True,
        has_birthday_field=False,
        has_birthday_select=False,
        has_birth_year_label=False,
        has_birth_year_field=False,
    ) == "age"


def test_japanese_about_you_birth_year_month_mode() -> None:
    from platforms.chatgpt.browser_register import _pick_best_about_you_input, _select_about_you_mode

    entries = [
        {"visibleIndex": 0, "labels": ["氏名"], "name": "name"},
        {"visibleIndex": 1, "labels": ["生年月"], "placeholder": "YYYY/MM"},
    ]

    birth_year_month_entry = _pick_best_about_you_input(entries, "birth_year_month", exclude_visible_indices={0})
    assert birth_year_month_entry is not None
    assert birth_year_month_entry["visibleIndex"] == 1
    assert _select_about_you_mode(
        has_age_label=True,
        has_birthday_label=False,
        has_birth_year_month_label=True,
        has_age_field=False,
        has_birthday_field=False,
        has_birth_year_month_field=True,
        has_birthday_select=False,
    ) == "birth_year_month"



def test_fast_email_wrong_japanese_otp_is_retryable() -> None:
    from platforms.chatgpt.fast_email_register import _is_wrong_email_otp_text

    assert _is_wrong_email_otp_text("不正確なコード")
    assert _is_wrong_email_otp_text("Invalid code")


def test_fast_email_otp_timeout_clicks_resend_then_waits_again(monkeypatch) -> None:
    from platforms.chatgpt.fast_email_register import FastEmailRegistrationFlow
    import platforms.chatgpt.fast_email_register as fast_email_register

    calls = {"otp": 0, "resend": 0}

    def otp_callback() -> str:
        calls["otp"] += 1
        if calls["otp"] == 1:
            raise TimeoutError("等待转发邮箱验证码超时")
        return "123456"

    def click_resend(_page) -> bool:
        calls["resend"] += 1
        return True

    monkeypatch.setattr(fast_email_register, "_click_email_otp_resend", click_resend)
    monkeypatch.setattr(fast_email_register.time, "sleep", lambda _seconds: None)

    code = FastEmailRegistrationFlow(log_fn=lambda _msg: None)._wait_email_code_with_resend(SimpleNamespace(), otp_callback)

    assert code == "123456"
    assert calls == {"otp": 2, "resend": 1}

def test_fast_email_registration_clicks_password_continue_before_otp():
    from platforms.chatgpt.fast_email_register import FastEmailRegistrationFlow

    events = []

    class FakePage(SimpleNamespace):
        def goto(self, url, **kwargs):
            events.append(("goto", url))
            self.url = url
        def evaluate(self, js, *args, **kwargs):
            return False

    page = FakePage(url="about:blank")
    session = SimpleNamespace(page=page, device_id="device-1")

    def derive_state(_page):
        url = str(_page.url or "")
        if "email-verification" in url:
            return {"page_type": "email_otp_verification", "current_url": url, "continue_url": ""}
        if "create-account/password" in url:
            return {"page_type": "create_account_password", "current_url": url, "continue_url": ""}
        if "about-you" in url:
            return {"page_type": "about_you", "current_url": url, "continue_url": ""}
        if "chatgpt.com" in url:
            return {"page_type": "chatgpt_home", "current_url": url, "continue_url": ""}
        return {}

    def click_password_continue(_page, _log, *, context):
        events.append(("continue", context))
        _page.url = "https://auth.openai.com/create-account/password"
        return True

    def submit_password(_page, _password, _log):
        events.append(("password", _page.url))
        _page.url = "https://auth.openai.com/about-you"
        return {"ok": True, "status": 200, "url": _page.url, "data": {"page_type": "about_you", "current_url": _page.url}, "text": ""}

    def submit_about_you(_page, _log):
        events.append(("about_you", _page.url))
        _page.url = "https://chatgpt.com/"
        return {"ok": True, "status": 200, "url": _page.url, "data": {"page_type": "chatgpt_home", "current_url": _page.url}, "text": ""}

    with patch("platforms.chatgpt.fast_email_register._get_browser_csrf_token", return_value="csrf"), patch(
        "platforms.chatgpt.fast_email_register._start_browser_signin", return_value="https://auth.openai.com/email-verification"
    ), patch("platforms.chatgpt.fast_email_register._browser_authorize", side_effect=lambda _page, _url, _log: setattr(_page, "url", "https://auth.openai.com/email-verification")), patch(
        "platforms.chatgpt.fast_email_register._derive_registration_state_from_page", side_effect=derive_state
    ), patch("platforms.chatgpt.fast_email_register._click_password_continue_if_available", side_effect=click_password_continue), patch(
        "platforms.chatgpt.fast_email_register._submit_password_via_page", side_effect=submit_password
    ), patch("platforms.chatgpt.fast_email_register._submit_about_you_via_page", side_effect=submit_about_you), patch(
        "platforms.chatgpt.fast_email_register._is_registration_complete", side_effect=lambda state: state.get("page_type") == "chatgpt_home"
    ), patch("platforms.chatgpt.fast_email_register._handle_post_signup_onboarding", lambda _page, _log: None), patch(
        "platforms.chatgpt.fast_email_register.FastEmailRegistrationFlow._extract_session", return_value={"access_token": "tok", "cookies": {}, "state": {"page_type": "chatgpt_home"}}
    ), patch("platforms.chatgpt.fast_email_register._validate_browser_email_otp", side_effect=AssertionError("OTP should not be used before password continue")):
        result = FastEmailRegistrationFlow(log_fn=lambda _msg: None).run(session, email="u@example.com", password="secret", otp_callback=lambda: "123456", timeout=10)

    assert result["access_token"] == "tok"
    assert events[:3] == [("goto", "https://chatgpt.com"), ("continue", "快速邮箱注册邮箱页"), ("password", "https://auth.openai.com/create-account/password")]




def test_phone_otp_fallback_uses_phone_validate_endpoint():
    from platforms.chatgpt import browser_register


    calls = []

    def fake_fetch(_page, url, **kwargs):
        calls.append((url, kwargs))
        return {"ok": True, "status": 200, "url": "https://auth.openai.com/authorize/resume", "data": {}}

    with patch("platforms.chatgpt.browser_register._browser_fetch", side_effect=fake_fetch), patch(
        "platforms.chatgpt.browser_register._build_browser_sentinel_token", return_value="sentinel"
    ), patch("platforms.chatgpt.browser_register._browser_pause", lambda _page: None):
        result = browser_register._validate_browser_phone_otp(SimpleNamespace(), "737171", "device-id", "ua", "https://auth.openai.com/phone-verification")

    assert result["ok"] is True
    assert calls[0][0].endswith("/api/accounts/phone-otp/validate")
    assert '"code": "737171"' in calls[0][1]["body"]



class FakeSms:
    def wait_for_code(self, activation_id, timeout=180, poll_interval=3):
        assert activation_id == "act-1"
        return {"code": "123456"}

    def get_status(self, activation_id):
        assert activation_id == "act-1"
        return {"code": "123456"}

    def mark_send_failed(self, activation_id, reason):
        raise AssertionError("successful path must not mark phone bad")

class FakeContext:
    def cookies(self):
        return [{"name": "login_session", "value": "abc", "domain": ".auth.openai.com", "path": "/"}]


class FakeLocator:
    first = None

    def __init__(self, page=None, selector=""):
        self.first = self
        self.page = page
        self.selector = selector
        self.value = ""

    def wait_for(self, **kwargs):
        return None

    def nth(self, index):
        assert index == 0
        return self

    def count(self):
        return 1

    def fill(self, value):
        self.value = value

    def type(self, value, **kwargs):
        self.value += value

    def input_value(self, **kwargs):
        return self.value

    def click(self, **kwargs):
        if self.page and "button" in self.selector:
            self.page.url = "https://auth.openai.com/create-account/contact-verification"
        return None

    def press(self, key, **kwargs):
        if self.page and key == "Enter":
            self.page.url = "https://auth.openai.com/create-account/contact-verification"
        return None

    def is_visible(self, **kwargs):
        return True

    def is_enabled(self, **kwargs):
        return True



class FakePage:
    url = "https://auth.openai.com/create-account/password"

    def goto(self, url, **kwargs):
        self.url = url

    def title(self):
        return ""

    def locator(self, selector):
        return FakeLocator(self, selector)

    def evaluate(self, script, *args):
        return ""

def fake_launch(self, headed=False):
    self.browser_context = FakeContext()
    self.page = FakePage()


def fake_continue_after_sms(page, code, log=None):
    assert code == "123456"
    return SimpleNamespace(success=True, access_token="access.hybrid.token", account_id="acct", email="", plan_type="free")



def test_password_continue_checked_before_passwordless():
    from platforms.chatgpt import browser_register

    calls = []

    def fake_password_continue(page, log, *, context):
        calls.append(("continue", context))
        return False

    def fake_passwordless(page, log, *, context):
        calls.append(("passwordless", context))
        return False

    def fake_state(page):
        return {"page_type": "login_password", "current_url": "https://auth.openai.com/log-in/password"}

    page = SimpleNamespace(url="https://auth.openai.com/log-in/password")

    with patch("platforms.chatgpt.browser_register._click_password_continue_if_available", side_effect=fake_password_continue), patch(
        "platforms.chatgpt.browser_register._click_passwordless_login_if_available", side_effect=fake_passwordless
    ), patch("platforms.chatgpt.browser_register._is_session_ended_page", return_value=False), patch(
        "platforms.chatgpt.browser_register._derive_registration_state_from_page", side_effect=fake_state
    ), patch("platforms.chatgpt.browser_register._recover_signup_password_page", return_value=False):
        state = browser_register._wait_for_signup_entry_transition(page, lambda _msg: None, timeout=1)

    assert state["page_type"] == "login_password"
    assert calls[:2] == [("continue", "邮箱页提交后"), ("passwordless", "邮箱页提交后")]


class RetrySms:
    def __init__(self):
        self.failed = []

    def wait_for_code(self, activation_id, timeout=180, poll_interval=3):
        assert activation_id == "act-2"
        return {"code": "654321"}

    def mark_send_failed(self, activation_id, reason):
        self.failed.append((activation_id, reason))


class RentSms(RetrySms):
    def __init__(self, activations):
        super().__init__()
        self.activations = list(activations)
        self.cancelled = []
        self.phone_exceptions = []

    def get_balance(self):
        return 1.0

    def get_active_activations(self, limit=20):
        return []

    def _normalize_phone_exceptions(self, values):
        return list(values)

    def get_number(self, *, service, country):
        assert service == "dr"
        assert country == "73"
        if not self.activations:
            raise RuntimeError("NO_NUMBERS")
        return self.activations.pop(0)

    def cancel_activation(self, activation_id):
        self.cancelled.append(activation_id)
        return True


def fake_continue_after_sms_retry(page, code, log=None):
    assert code == "654321"
    return SimpleNamespace(success=True, access_token="access.retry.token", account_id="acct", email="", plan_type="free")


def retry_phone_without_relaunching_browser():
    pipeline = RegisterPipeline({"proxy": "http://127.0.0.1:7897", "use_camoufox": True, "phone_retry_limit": 2, "sms_activation_pool_enabled": False})
    pipeline.sms_provider = RetrySms()
    pipeline.result["phone_number"] = "+819011111111"
    pipeline.result["activation_id"] = "act-1"
    launches = []
    cleanups = []
    submitted = []

    def launch_once(self, headed=False):
        launches.append(1)
        self.browser_context = FakeContext()
        self.page = FakePage()

    def get_second_phone(self):
        self.result["phone_number"] = "+819022222222"
        self.result["activation_id"] = "act-2"
        self.result["steps"].append("get_phone_number")
        return self.result["phone_number"]

    def start_signin(page, phone, device_id, csrf, **kwargs):
        submitted.append(phone)
        if len(submitted) == 1:
            return "https://auth.openai.com/log-in/password"
        return "https://auth.openai.com/create-account/password"

    with patch("platforms.chatgpt.browser_register._seed_browser_device_id", lambda page, device_id: None), \
         patch("platforms.chatgpt.browser_register._get_browser_csrf_token", lambda page: "csrf"), \
         patch("platforms.chatgpt.browser_register._start_browser_signin", start_signin), \
         patch("platforms.chatgpt.phone_register.continue_after_sms", fake_continue_after_sms_retry), \
         patch.object(RegisterPipeline, "_ensure_phone_sms_send_clicked", lambda self: (True, "mock sms sent")), \
         patch.object(RegisterPipeline, "_launch_camoufox", launch_once), \
         patch.object(RegisterPipeline, "_cleanup", lambda self: cleanups.append(1)), \
         patch.object(RegisterPipeline, "step_get_phone_number", get_second_phone):
        result = pipeline.step_hybrid_register("+819011111111")

    assert result["access_token"] == "access.retry.token"
    assert submitted == ["+819011111111", "+819022222222"]
    assert len(launches) == 1
    assert cleanups == []
    assert pipeline.sms_provider.failed and pipeline.sms_provider.failed[0][0] == "act-1"


class TimeoutThenCodeSms:
    def __init__(self):
        self.calls = []
        self.stopped = []

    def wait_for_code(self, activation_id, timeout=180, poll_interval=3):
        self.calls.append(activation_id)
        if activation_id == "act-1":
            return None
        assert activation_id == "act-2"
        return {"code": "654321"}

    def _stop_reuse(self, reason):
        self.stopped.append(reason)


def sms_timeout_retries_with_new_phone():
    pipeline = RegisterPipeline({"proxy": "http://127.0.0.1:7897", "use_camoufox": True, "phone_retry_limit": 2, "sms_code_timeout": 120, "sms_first_poll_delay": 0, "sms_activation_pool_enabled": False})
    pipeline.sms_provider = TimeoutThenCodeSms()
    pipeline.result["phone_number"] = "+819011111111"
    pipeline.result["activation_id"] = "act-1"
    submitted = []

    def launch_once(self, headed=False):
        self.browser_context = FakeContext()
        self.page = FakePage()

    def get_second_phone(self):
        self.result["phone_number"] = "+819022222222"
        self.result["activation_id"] = "act-2"
        self.result["steps"].append("get_phone_number")
        return self.result["phone_number"]

    def start_signin(page, phone, device_id, csrf, **kwargs):
        submitted.append(phone)
        return "https://auth.openai.com/create-account/password"

    with patch("platforms.chatgpt.browser_register._seed_browser_device_id", lambda page, device_id: None), \
         patch("platforms.chatgpt.browser_register._get_browser_csrf_token", lambda page: "csrf"), \
         patch("platforms.chatgpt.browser_register._start_browser_signin", start_signin), \
         patch("platforms.chatgpt.phone_register.continue_after_sms", fake_continue_after_sms_retry), \
         patch.object(RegisterPipeline, "_ensure_phone_sms_send_clicked", lambda self: (True, "mock sms sent")), \
         patch.object(RegisterPipeline, "_launch_camoufox", launch_once), \
         patch.object(RegisterPipeline, "step_get_phone_number", get_second_phone):
        result = pipeline.step_hybrid_register("+819011111111")

    assert result["access_token"] == "access.retry.token"
    assert submitted == ["+819011111111", "+819022222222"]
    assert pipeline.sms_provider.calls == ["act-1", "act-2"]
    assert pipeline.sms_provider.stopped == ["sms timeout after password submitted"]
    assert pipeline.result["sms_timeout_handoffs"][0]["activation_id"] == "act-1"

def precheck_skips_registered_phone_before_sms():
    pipeline = RegisterPipeline({
        "sms_api_key": "key",
        "sms_service": "dr",
        "sms_country": "73",
        "prepare_registration_before_phone": False,
        "precheck_phone_before_sms": True,
        "herosms_presend_cancel_delay": 0,
        "sms_activation_pool_enabled": False,
    })
    rented_sms = RentSms([
        SmsActivation(activation_id="act-old", phone_number="+55511110866", country="73"),
        SmsActivation(activation_id="act-new", phone_number="+55522220999", country="73"),
    ])
    states = ["registered", "new"]

    def make_provider(config):
        return rented_sms

    def precheck(self, phone_number):
        state = states.pop(0)
        if state == "registered":
            return "registered", "authorize resolved to existing-account login page: https://auth.openai.com/log-in/password"
        self._prechecked_phone_number = phone_number
        return "new", "https://auth.openai.com/create-account/password"

    with patch("core.base_sms.HeroSmsProvider.from_config", make_provider), \
         patch("core.base_sms.hero_sms_cache_file", lambda: Path("output/simulated/no-cache.json")), \
         patch.object(RegisterPipeline, "_precheck_phone_registration_state", precheck):
        phone = pipeline.step_get_phone_number()

    assert phone == "+55522220999"
    assert pipeline.result["activation_id"] == "act-new"
    assert rented_sms.cancelled == ["act-old"]
    assert rented_sms.failed and rented_sms.failed[0][0] == "act-old"


def reject_non_create_account_password_state():
    pipeline = RegisterPipeline({"proxy": "http://127.0.0.1:7897", "use_camoufox": True, "phone_retry_limit": 1, "sms_activation_pool_enabled": False})
    pipeline.sms_provider = RetrySms()
    pipeline.result["phone_number"] = "+819011111111"
    pipeline.result["activation_id"] = "act-1"

    def launch_once(self, headed=False):
        self.browser_context = FakeContext()
        self.page = FakePage()

    with patch("platforms.chatgpt.browser_register._seed_browser_device_id", lambda page, device_id: None), \
         patch("platforms.chatgpt.browser_register._get_browser_csrf_token", lambda page: "csrf"), \
         patch("platforms.chatgpt.browser_register._start_browser_signin", lambda page, phone, device_id, csrf, **kwargs: "https://auth.openai.com/authorize/resume"), \
         patch.object(RegisterPipeline, "_wait_for_auth_password_state", lambda self, timeout=35: None), \
         patch.object(RegisterPipeline, "_launch_camoufox", launch_once):
        try:
            pipeline.step_hybrid_register("+819011111111")
        except RuntimeError as exc:
            assert "unexpected auth step before password entry" in str(exc)
        else:
            raise AssertionError("unexpected auth state must not proceed to password entry")

    assert not pipeline.sms_provider.failed



def precheck_treats_post_password_states_as_not_new():
    pipeline = RegisterPipeline({"precheck_phone_before_sms": True, "sms_activation_pool_enabled": False})
    pipeline.page = FakePage()

    with patch.object(RegisterPipeline, "step_prepare_registration_environment", lambda self, reset_auth=False: ("device", "csrf")), \
         patch("platforms.chatgpt.browser_register._start_browser_signin", lambda page, phone, device_id, csrf, **kwargs: "https://auth.openai.com/add-phone"), \
         patch.object(RegisterPipeline, "_debug_page_state", lambda self, label: None):
        state, detail = pipeline._precheck_phone_registration_state("+5511999999999")

    assert state == "registered"
    assert "add_phone" in detail


def test_precheck_login_url_does_not_navigate_visible_page():
    pipeline = RegisterPipeline({"precheck_phone_before_sms": True, "sms_activation_pool_enabled": False})

    class GuardedPage(FakePage):
        url = "https://chatgpt.com/"

        def goto(self, url, **kwargs):
            raise AssertionError("login classification must not navigate the visible page")

    pipeline.page = GuardedPage()

    state, detail = pipeline._precheck_authorize_redirect_by_request("https://auth.openai.com/log-in/password")

    assert state == "registered"
    assert "log-in/password" in detail
    assert pipeline.page.url == "https://chatgpt.com/"


def test_authorize_api_url_not_misclassified_by_redirect_uri():
    pipeline = RegisterPipeline({"precheck_phone_before_sms": True, "sms_activation_pool_enabled": False})
    state, detail = pipeline._precheck_authorize_redirect_by_request(
        "https://auth.openai.com/api/accounts/authorize?client_id=x&redirect_uri=https%3A%2F%2Fchatgpt.com%2Fauth%2Fcallback"
    )

    assert state == "unknown"
    assert "missing browser context" in detail


def test_precheck_unknown_keeps_browser_and_gets_next_phone():
    pipeline = RegisterPipeline({
        "sms_api_key": "key",
        "sms_service": "dr",
        "sms_country": "73",
        "prepare_registration_before_phone": False,
        "precheck_phone_before_sms": True,
        "herosms_presend_cancel_delay": 75,
        "sms_activation_pool_enabled": False,
    })
    rented_sms = RentSms([
        SmsActivation(activation_id="act-unknown", phone_number="+55511110866", country="73"),
        SmsActivation(activation_id="act-new", phone_number="+55522220999", country="73"),
    ])
    states = ["unknown", "new"]

    def make_provider(config):
        return rented_sms

    def precheck(self, phone_number):
        state = states.pop(0)
        if state == "unknown":
            return "unknown", "temporary auth state"
        self._prechecked_phone_number = phone_number
        return "new", "https://auth.openai.com/create-account/password"

    with patch("core.base_sms.HeroSmsProvider.from_config", make_provider), \
         patch("core.base_sms.hero_sms_cache_file", lambda: Path("output/simulated/no-cache.json")), \
         patch.object(RegisterPipeline, "_precheck_phone_registration_state", precheck), \
         patch.object(RegisterPipeline, "_cleanup", lambda self: (_ for _ in ()).throw(AssertionError("precheck retry must not close browser"))):
        phone = pipeline.step_get_phone_number()

    assert phone == "+55522220999"
    assert pipeline.result["activation_id"] == "act-new"
    assert rented_sms.cancelled == []

def phone_retry_markers_do_not_match_generic_phone_text():
    pipeline = RegisterPipeline({"sms_activation_pool_enabled": False})
    assert not pipeline._is_phone_retryable_registration_error("Número de telefone")
    assert not pipeline._is_phone_retryable_registration_error("numero de telefone")
    assert pipeline._is_phone_retryable_registration_error("já existe uma conta para este número")


def lajiao_region_config_overrides_query_url():
    pipeline = RegisterPipeline({
        "lajiao_proxy_api_url": "http://api.lajiaohttp.com/api/extract_ip?regions=BR&num=1&protocol=socks5&type=txt&cate=2&t=10&lb=1",
        "lajiao_proxy_regions": "JP",
        "sms_activation_pool_enabled": False,
    })
    captured = {}

    class FakeResponse:
        headers = {"content-type": "text/plain"}
        text = "1.2.3.4:19001\n"

        def raise_for_status(self):
            return None

    class FakeSession:
        trust_env = True

        def get(self, url, params=None, timeout=30):
            captured["url"] = url
            captured["params"] = params
            return FakeResponse()

    with patch("requests.Session", lambda: FakeSession()):
        assert pipeline._fetch_lajiao_proxy_candidates() == ["1.2.3.4:19001"]

    assert "regions=JP" in captured["url"]
    assert "regions=BR" not in captured["url"]
    assert captured["params"] is None


def user_phone_url_provider_polls_code():
    provider = UserProvidedSmsProvider.parse_entries("19043296030|https://smscloud.sbs/api/system/get_sms/token")
    assert provider == [("19043296030", "https://smscloud.sbs/api/system/get_sms/token")]
    sms = UserProvidedSmsProvider(provider)
    activation = sms.get_number(service="dr", country="1")
    assert activation.phone_number == "19043296030"
    assert activation.activation_id == "https://smscloud.sbs/api/system/get_sms/token"

def user_phone_url_allows_dynamic_resource_pool_without_static_url():
    sms = UserProvidedSmsProvider.from_config({"_resource_provider": "bind_user_phone_url", "dashboard_task_id": "task-test", "country_code": "1"})
    assert sms.resource_provider == "bind_user_phone_url"
    assert sms.task_id == "task-test"


def lajiao_credentials_mode_uses_auth_proxy_urls():
    pipeline = RegisterPipeline({
        "rotate_proxy_each_attempt": True,
        "lajiao_proxy_mode": "credentials",
        "lajiao_proxy_credentials": "user-region-JP-sid-a-t-30:pass@us.lajiaohttp.net:2000\nuser-region-JP-sid-b-t-30:pass@us.lajiaohttp.net:2000",
        "sms_activation_pool_enabled": False,
    })
    assert pipeline._credential_proxy_candidates() == [
        "http://user-region-JP-sid-a-t-30:pass@us.lajiaohttp.net:2000",
        "http://user-region-JP-sid-b-t-30:pass@us.lajiaohttp.net:2000",
    ]
    assert pipeline._proxy_check_url("socks5://user:pass@us.lajiaohttp.net:2000") == "socks5h://user:pass@us.lajiaohttp.net:2000"
    assert pipeline._proxy_check_url("http://user:pass@us.lajiaohttp.net:2000") == "http://user:pass@us.lajiaohttp.net:2000"
    assert pipeline._proxy_runtime_url("user:pass@us.lajiaohttp.net:2000") == "socks5h://user:pass@us.lajiaohttp.net:2000"
    assert pipeline._proxy_runtime_url("http://user:pass@us.lajiaohttp.net:2000") == "socks5h://user:pass@us.lajiaohttp.net:2000"
    assert pipeline._proxy_runtime_url("http://user:pass@la.residential.rayobyte.com:8000") == "socks5h://user:pass@la.residential.rayobyte.com:8000"
    from core.proxy.credential_runtime import CredentialProxyRuntime

    runtime = CredentialProxyRuntime({"lajiao_proxy_mode": "credentials"})
    bridge_url = runtime.start_browser_bridge("socks5h://user:pass@127.0.0.1:9")
    try:
        assert bridge_url.startswith("http://127.0.0.1:")
    finally:
        runtime.cleanup()



def user_phone_url_extracts_text_not_status_code():
    payload = '{"code":0,"message":"操作成功","data":{"isReceived":"yes","phoneNumber":"15185121182","text":"あなたのChatGPT 認証コード： 537179","exTime":"2026-07-12 22:00:00"}}'
    assert UserProvidedSmsProvider._extract_code(payload) == "537179"
    assert UserProvidedSmsProvider._extract_code('{"code":0,"message":"操作成功","data":{"isReceived":"no","phoneNumber":"15185121182"}}') == ""


def user_phone_url_ignores_existing_code_until_new_code():
    provider = UserProvidedSmsProvider([("15185121182", "unused")], country_code="1")
    old_payload = '{"code":0,"data":{"text":"あなたのChatGPT 認証コード： 111111"}}'
    new_payload = '{"code":0,"data":{"text":"あなたのChatGPT 認証コード： 222222"}}'
    provider._read_current_code_with_raw = lambda activation_id: (UserProvidedSmsProvider._extract_code(old_payload), old_payload)
    activation = provider.get_number(service="dr", country="1")
    provider._read_current_code_with_raw = lambda activation_id: (UserProvidedSmsProvider._extract_code(new_payload), new_payload)
    assert provider.get_code(activation.activation_id, timeout=1) == "222222"



def main():
    pipeline = RegisterPipeline({"proxy": "http://127.0.0.1:7897", "use_camoufox": True, "phone_retry_limit": 1, "sms_activation_pool_enabled": False})
    pipeline.sms_provider = FakeSms()
    pipeline.result["activation_id"] = "act-1"
    with patch("platforms.chatgpt.browser_register._seed_browser_device_id", lambda page, device_id: None), \
         patch("platforms.chatgpt.browser_register._get_browser_csrf_token", lambda page: "csrf"), \
         patch("platforms.chatgpt.browser_register._start_browser_signin", lambda page, phone, device_id, csrf, **kwargs: "https://auth.openai.com/create-account/password"), \
         patch("platforms.chatgpt.phone_register.continue_after_sms", fake_continue_after_sms), \
         patch.object(RegisterPipeline, "_ensure_phone_sms_send_clicked", lambda self: (True, "mock sms sent")), \
         patch.object(RegisterPipeline, "_launch_camoufox", fake_launch):
        result = pipeline.step_hybrid_register("+5511999999999")
    assert result["access_token"] == "access.hybrid.token"
    assert pipeline.result["access_token"] == "access.hybrid.token"
    assert pipeline.result["session_token"] == ""
    assert pipeline.result["password"]
    assert "hybrid_browser_password_register" in pipeline.result["steps"]
    user_phone_url_provider_polls_code()
    user_phone_url_allows_dynamic_resource_pool_without_static_url()
    lajiao_credentials_mode_uses_auth_proxy_urls()
    user_phone_url_extracts_text_not_status_code()
    user_phone_url_ignores_existing_code_until_new_code()
    precheck_skips_registered_phone_before_sms()
    precheck_treats_post_password_states_as_not_new()
    reject_non_create_account_password_state()
    phone_retry_markers_do_not_match_generic_phone_text()
    lajiao_region_config_overrides_query_url()
    retry_phone_without_relaunching_browser()
    sms_timeout_retries_with_new_phone()
    print("hybrid registration unit check passed")


if __name__ == "__main__":
    main()
