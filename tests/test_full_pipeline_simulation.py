from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from full_pipeline import RegisterPipeline


class FakePipeline(RegisterPipeline):
    def __init__(self, config):
        super().__init__(config)
        self.launched = False
        self.cleaned = False

    def step_get_phone_number(self):
        self.result["phone_number"] = "+15550001111"
        self.result["activation_id"] = "act-1"
        self.result["steps"].append("get_phone_number")
        return self.result["phone_number"]

    def step_browser_register(self, phone_number):
        self.result.update(
            {
                "access_token": "access.registration.token",
                "account_id": "acct_123",
                "email": "",
                "plan_type": "free",
                "password": "GeneratedPass123!",
            }
        )
        self.result["steps"].append("hybrid_register")
        return {"access_token": self.result["access_token"], "password": self.result["password"], "session_token": "session-token"}

    def _activate_via_api(self, access_token, api_key, paypal_phone):
        assert access_token == "access.registration.token"
        self.result["steps"].append("activate_plus")
        self.result["plan_type"] = "plus"
        return True

    def _launch_camoufox(self, headed=False):
        self.launched = True
        self.page = SimpleNamespace()

    def _save_browser_storage_state(self, resume_id: str) -> str:
        path = Path(self.config.get("output_dir", "output/simulated")) / f"storage_{resume_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
        return str(path)

    def step_oauth_from_saved_session(self, *, headed=False):
        assert self.config.get("manual_plus_confirmed") is True
        assert self.result["access_token"] == "access.registration.token"
        assert self.result["password"] == "GeneratedPass123!"
        assert Path(self.result["browser_storage_state_path"]).exists()
        result = {
            "access_token": "access.oauth.token",
            "refresh_token": "refresh.oauth.token",
            "id_token": "id.oauth.token",
            "email": "bound@example.com",
            "account_id": "acct_123",
        }
        self.result.update(result)
        self.result["steps"].append("resume_oauth_session")
        return result

    def _fetch_live_subscription_plan(self):
        return str(self.config.get("live_plan_type") or "").strip()

    def _cleanup(self):
        self.cleaned = True


def fake_login_and_get_tokens(self, **kwargs):
    assert kwargs["email"] == "outlook@example.com"
    assert kwargs["password"] == "GeneratedPass123!"
    return {
        "access_token": "access.oauth.token",
        "refresh_token": "refresh.oauth.token",
        "id_token": "id.oauth.token",
        "email": kwargs["email"],
    }


def fake_upload(account_data, url, key):
    assert account_data["access_token"] == "access.oauth.token"
    assert account_data["refresh_token"] == "refresh.oauth.token"
    assert account_data["id_token"] == "id.oauth.token"
    assert account_data["account_id"] == "acct_123"
    assert url == "https://sub2api.example"
    assert key == "admin-key"
    return True, "上传成功"


def fake_verify(account_data, url, key):
    assert account_data["access_token"] == "access.oauth.token"
    assert account_data["refresh_token"] == "refresh.oauth.token"
    assert account_data["id_token"] == "id.oauth.token"
    assert account_data["account_id"] == "acct_123"
    assert url == "https://sub2api.example"
    assert key == "admin-key"
    return True, "回查成功"


def run_full_success():
    cfg = {
        "iceaix_api_key": "ice-key",
        "paypal_phone": "+819012345678",
        "iceaix_sms_api": "https://sms.example/order",
        "outlook_email": "outlook@example.com",
        "sub2api_url": "https://sub2api.example",
        "sub2api_admin_key": "admin-key",
        "save_tokens": False,
        "save_session": False,
        "live_plan_type": "plus",
        "plus_verify_interval": 0,
        "output_dir": "output/simulated",
    }
    pipeline = FakePipeline(cfg)
    with patch("platforms.chatgpt.oauth_client.OAuthClient.login_and_get_tokens", fake_login_and_get_tokens), patch(
        "platforms.chatgpt.sub2api_upload.upload_to_sub2api", fake_upload
    ), patch("platforms.chatgpt.sub2api_upload.verify_sub2api_upload", fake_verify):
        result = pipeline.run(start_step="register", headed=False)
    assert result["success"] is True
    assert result["access_token"] == "access.oauth.token"
    assert result["refresh_token"] == "refresh.oauth.token"
    assert result["steps"] == ["get_phone_number", "hybrid_register", "activate_plus", "oauth_bind_email", "upload_sub2api"]
    registered_file = Path(result["registered_file"])
    assert registered_file.exists()
    assert pipeline.launched is True
    assert pipeline.cleaned is True


def run_manual_cdk_is_not_success():
    cfg = {"outlook_email": "", "sub2api_url": "", "sub2api_admin_key": ""}
    pipeline = FakePipeline(cfg)
    with patch("sys.stdin.isatty", return_value=False):
        result = pipeline.run(start_step="register", headed=False)
    assert result["success"] is False
    assert result["status"] == "manual_plus_flow_required"

def run_oauth_resume_no_phone_prompt():
    cfg = {"outlook_email": "outlook@example.com", "save_tokens": False, "sub2api_url": "", "sub2api_admin_key": ""}
    pipeline = FakePipeline(cfg)
    pipeline.result["access_token"] = "existing.token"
    pipeline.result["password"] = "GeneratedPass123!"
    pipeline.result["account_id"] = "acct_123"
    pipeline.result["plan_type"] = "plus"
    with patch("builtins.input", side_effect=AssertionError("input should not be called")), patch(
        "platforms.chatgpt.oauth_client.OAuthClient.login_and_get_tokens", fake_login_and_get_tokens
    ):
        result = pipeline.run(start_step="oauth", headed=False)
    assert result["success"] is True
    assert result["phone_number"] == ""
    assert pipeline.launched is True

def run_register_token_and_resume_oauth():
    out = Path("output/simulated")
    cfg = {"output_dir": str(out), "resume_id": "sim", "save_session": False, "sub2api_url": "", "sub2api_admin_key": ""}
    pipeline = FakePipeline(cfg)
    result = pipeline.run(start_step="register-token", headed=False)
    assert result["success"] is True
    assert result["status"] == "manual_plus_required"
    resume_file = Path(result["resume_file"])
    assert resume_file.exists()
    registered_file = Path(result["registered_file"])
    assert registered_file.exists()

    import json
    resume = json.loads(resume_file.read_text(encoding="utf-8"))
    assert resume["stage"] == "manual_plus_required"
    assert resume["access_token"] == "access.registration.token"
    assert resume["password"] == "GeneratedPass123!"
    assert Path(resume["browser_storage_state_path"]).exists()

    resume_pipeline = FakePipeline({"output_dir": str(out), "resume_file": str(resume_file), "manual_plus_confirmed": True, "live_plan_type": "plus", "sub2api_url": "", "sub2api_admin_key": ""})
    resumed = resume_pipeline.run(start_step="resume-oauth", headed=False)
    assert resumed["success"] is True
    assert resumed["status"] == "complete"
    assert resumed["plan_type"] == "plus"
    assert resumed["refresh_token"] == "refresh.oauth.token"
    final_file = Path(resumed["final_file"])
    assert final_file.exists()
    saved = json.loads(final_file.read_text(encoding="utf-8"))
    assert saved["plan_type"] == "plus"


def run_resume_oauth_rejects_unverified_plus():
    out = Path("output/simulated_unverified")
    cfg = {"output_dir": str(out), "resume_id": "sim-unverified", "save_session": False, "sub2api_url": "", "sub2api_admin_key": ""}
    pipeline = FakePipeline(cfg)
    result = pipeline.run(start_step="register-token", headed=False)
    resume_file = Path(result["resume_file"])

    import json
    resume_pipeline = FakePipeline({"output_dir": str(out), "resume_file": str(resume_file), "manual_plus_confirmed": True, "live_plan_type": "free", "sub2api_url": "", "sub2api_admin_key": ""})
    resumed = resume_pipeline.run(start_step="resume-oauth", headed=False)
    assert resumed["success"] is False
    assert resumed["status"] == "manual_plus_unverified"
    assert resumed["plan_type"] == "free"
    assert not resumed.get("final_file")




class FakeMailbox:
    @classmethod
    def from_config(cls, config):
        return cls()

    def create_account(self):
        return SimpleNamespace(email="mailbox@example.com")

    def get_current_ids(self, account):
        return set()


def run_email_register_token_stops_before_phone_bind():
    out = Path("output/simulated_email_only")
    cfg = {
        "output_dir": str(out),
        "resume_id": "email-only",
        "mailbox_provider": "icloud_api",
        "save_session": False,
        "sub2api_url": "",
        "sub2api_admin_key": "",
    }
    pipeline = FakePipeline(cfg)
    with patch("core.mailbox_providers.LinkApiMailbox", FakeMailbox), patch(
        "platforms.chatgpt.browser_register._browser_registration_flow", return_value={"page_type": "chatgpt_home"}
    ) as browser_flow, patch(
        "core.base_sms.create_phone_callbacks", side_effect=AssertionError("phone callbacks must not be created")
    ), patch(
        "platforms.chatgpt.browser_register._get_cookies", return_value={"__Secure-next-auth.session-token": "session-token"}
    ), patch(
        "core.browser.session.extract_chatgpt_access_token",
        return_value=SimpleNamespace(success=True, access_token="access.email.token", failure_reason="", status="SESSION_TOKEN_OK"),
    ), patch(
        "platforms.chatgpt.browser_register._do_codex_oauth", side_effect=AssertionError("OAuth must not run during email-only registration")
    ):
        result = pipeline.run(start_step="email-register-token", headed=False)

    assert result["success"] is True
    assert result["access_token"] == "access.email.token"
    assert result["chatgpt_access_token_initial"] == "access.email.token"
    assert result["session_token"] == "session-token"
    assert result["status"] == "email_registered"
    assert result["stage"] == "manual_plus_required"
    assert result["email"] == "mailbox@example.com"
    assert result.get("phone_number", "") == ""
    assert result["steps"] == ["email_browser_register"]
    browser_flow.assert_called_once()
    resume_file = Path(result["resume_file"])
    registered_file = Path(result["registered_file"])
    assert resume_file.exists()
    assert registered_file.exists()
    import json
    resume = json.loads(resume_file.read_text(encoding="utf-8"))
    assert resume["email"] == "mailbox@example.com"
    assert resume["access_token"] == "access.email.token"
    assert resume["session_token"] == "session-token"
    assert resume["stage"] == "manual_plus_required"
    assert resume["phone_number"] == ""

if __name__ == "__main__":
    run_full_success()
    run_manual_cdk_is_not_success()
    run_oauth_resume_no_phone_prompt()
    run_register_token_and_resume_oauth()
    run_email_register_token_stops_before_phone_bind()
    run_resume_oauth_rejects_unverified_plus()
    print("side-effect-free full pipeline simulation passed")
