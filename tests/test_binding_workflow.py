from __future__ import annotations

from pathlib import Path
import sys

from uuid import uuid4
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from core.base_sms import UserProvidedSmsProvider
from full_pipeline import RegisterPipeline


def test_user_phone_url_uses_first_valid_url() -> None:
    entries = UserProvidedSmsProvider.parse_entries(
        "13522560344|https://smscloud.sbs/api/system/get_sms/abc|https://smscloud.sbs/api/system/get_sms/abc"
    )
    assert entries == [("13522560344", "https://smscloud.sbs/api/system/get_sms/abc")]

def test_user_phone_url_accepts_dashboard_dash_separator() -> None:
    entries = UserProvidedSmsProvider.parse_entries(
        "13522560344----https://smscloud.sbs/api/system/get_sms/abc"
    )
    assert entries == [("13522560344", "https://smscloud.sbs/api/system/get_sms/abc")]


def test_failed_run_persists_generated_password(tmp_path: Path) -> None:
    pipeline = RegisterPipeline({"output_dir": str(tmp_path)})
    pipeline.result["status"] = "error"
    pipeline.result["phone_number"] = "+15550000000"
    pipeline.result["password"] = "GeneratedPass123!"
    pipeline.result["generated_chatgpt_password"] = "GeneratedPass123!"
    failed = pipeline._save_failed_run_json()
    text = failed.read_text(encoding="utf-8")
    assert "GeneratedPass123!" in text
    assert "generated_chatgpt_password" in text


def test_mailbox_provider_candidate() -> None:
    class FakeAccount:
        email = "bind@example.com"
        account_id = "bind@example.com"
        extra = {}

    class FakeMailbox:
        @classmethod
        def from_config(cls, config):
            return cls()

        def create_account(self):
            return FakeAccount()

        def get_current_ids(self, account):
            return {"old"}

    import core.mailbox_providers as mailbox_providers

    original = mailbox_providers.CFWorkerMailbox
    mailbox_providers.CFWorkerMailbox = FakeMailbox
    try:
        pipeline = RegisterPipeline({"mailbox_provider": "cfworker_admin_api"})
        candidates = pipeline._load_mailbox_binding_candidates()
    finally:
        mailbox_providers.CFWorkerMailbox = original
    assert candidates == [("bind@example.com", "", "", "")]
    assert pipeline.result["outlook_email"] == "bind@example.com"
    assert pipeline.result["email_provider"] == "cfworker_admin_api"


def test_forwarded_domain_candidate() -> None:
    pipeline = RegisterPipeline({"mailbox_provider": "forwarded_domain", "mailbox_domain": "@example.com"})
    candidates = pipeline._load_mailbox_binding_candidates()
    assert len(candidates) == 1
    assert candidates[0][0].endswith("@example.com")
    assert pipeline.result["outlook_email"].endswith("@example.com")
    assert pipeline.result["email_provider"] == "forwarded_domain"


def test_icloud_privacy_candidate() -> None:
    alias = f"alias-{uuid4().hex}@icloud.com"
    pipeline = RegisterPipeline({
        "mailbox_provider": "icloud_privacy",
        "icloud_privacy_order_text": alias,
        "mailbox_imap_user": "inbox@163.com",
        "mailbox_imap_pass": "imap-pass",
    })
    candidates = pipeline._load_mailbox_binding_candidates()
    assert candidates == [(alias, "", "", "")]
    assert pipeline.result["email_provider"] == "icloud_privacy"


if __name__ == "__main__":
    root = Path("tmp/test_binding_workflow")
    root.mkdir(parents=True, exist_ok=True)
    test_user_phone_url_uses_first_valid_url()
    test_failed_run_persists_generated_password(root)
    test_mailbox_provider_candidate()
    test_forwarded_domain_candidate()
    print("binding workflow tests passed")
