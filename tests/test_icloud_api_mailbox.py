from __future__ import annotations

from pathlib import Path
import json
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from application.resource_pool_service import ResourcePoolService
from core.mailbox.forwarded_domain import MailboxAccount
from core.mailbox_providers import LinkApiMailbox
from infrastructure.repositories.resource_pool_repository import ResourcePoolRepository


ICLOUD_ROW = (
    "icloud@example.com"
    "----https://mail.local/show/token"
    "----code:https://mail.local/api/code/token"
    "----mail:https://mail.local/api/mail/token"
)


def test_link_api_mailbox_row_preserves_show_code_and_mail_urls() -> None:
    mailbox = LinkApiMailbox(order_text=ICLOUD_ROW)

    account = mailbox.account_for_email("icloud@example.com")

    assert account.email == "icloud@example.com"
    assert account.extra == {
        "provider_name": "icloud_api",
        "email": "icloud@example.com",
        "inbox_url": "https://mail.local/show/token",
        "code_url": "https://mail.local/api/code/token",
        "mail_url": "https://mail.local/api/mail/token",
    }


POUALIIS_ROW = (
    "02-bulks-taker@icloud.com"
    "----http://poualiis.xyz/api/mails?recipient=02-bulks-taker@icloud.com&top=1"
)


def test_link_api_parses_poualiis_recipient_mails_url() -> None:
    mailbox = LinkApiMailbox(order_text=POUALIIS_ROW)
    account = mailbox.account_for_email("02-bulks-taker@icloud.com")
    assert account.extra["mail_url"] == "http://poualiis.xyz/api/mails?recipient=02-bulks-taker@icloud.com&top=1"
    assert account.extra["inbox_url"] == ""
    assert account.extra["code_url"] == ""


def test_link_api_wait_for_code_reads_poualiis_msg_payload(monkeypatch) -> None:
    mailbox = LinkApiMailbox()
    account = MailboxAccount(
        email="02-bulks-taker@icloud.com",
        extra={"mail_url": "http://poualiis.xyz/api/mails?recipient=02-bulks-taker@icloud.com&top=1"},
    )
    responses = iter(
        [
            {"mailbox": "", "msg": "", "status": False, "time": ""},
            {
                "mailbox": "INBOX",
                "msg": "Verify your email address.\nEnter the code below:\n482917\nApple Support",
                "status": True,
                "time": "2026-07-17 16:00:00",
            },
        ]
    )

    def fetch_json(url: str, label: str) -> dict:
        assert label == "mail"
        assert "poualiis.xyz/api/mails" in url
        return next(responses)

    monkeypatch.setattr(mailbox, "_fetch_json_url", fetch_json)
    monkeypatch.setattr("core.mailbox_providers.time.sleep", lambda _seconds: None)
    assert mailbox.wait_for_code(account, timeout=2) == "482917"


def test_resource_pool_import_poualiis_recipient_url(tmp_path: Path, monkeypatch) -> None:
    service = ResourcePoolService(ResourcePoolRepository(tmp_path / "poualiis.db"))
    assert service.import_link_api_mailboxes(POUALIIS_ROW) == 1
    [resource] = service.list_resources("email", "icloud_api")
    assert resource["resource_key"] == "02-bulks-taker@icloud.com"
    assert resource["payload"]["mail_url"].startswith("http://poualiis.xyz/api/mails?recipient=")
    assert resource["payload"]["inbox_url"] == ""
    # Do not read the live process-wide outlook_pool_state.jsonl in unit tests.
    monkeypatch.setattr(ResourcePoolService, "_load_icloud_api_pool_state", lambda self: {})
    overrides, leases = service.lease_for_task("task-poualiis", {"mailbox_provider": "icloud_api"})
    assert [lease.resource_key for lease in leases] == ["02-bulks-taker@icloud.com"]
    assert "mail:http://poualiis.xyz/api/mails?recipient=02-bulks-taker@icloud.com&top=1" in overrides["icloud_api_order_text"]
    assert overrides["icloud_api_email"] == "02-bulks-taker@icloud.com"


def test_poualiis_empty_payload_does_not_create_marker() -> None:
    mailbox = LinkApiMailbox()
    code, markers = mailbox._extract_code_from_mail_payload(
        {"mailbox": "", "msg": "", "status": False, "time": ""}
    )
    assert code == ""
    assert markers == set()


def test_poualiis_get_current_ids_skips_latest_mail_baseline(monkeypatch) -> None:
    mailbox = LinkApiMailbox()
    account = MailboxAccount(
        email="02-bulks-taker@icloud.com",
        extra={"mail_url": "http://poualiis.xyz/api/mails?recipient=02-bulks-taker@icloud.com&top=1"},
    )

    def fetch_json(url: str, label: str) -> dict:
        assert label == "mail"
        return {
            "mailbox": "INBOX",
            "msg": "Enter this temporary verification code to continue:\n448478\n",
            "status": True,
            "time": "2026-07-17 15:36:03",
        }

    monkeypatch.setattr(mailbox, "_fetch_json_url", fetch_json)
    assert mailbox.get_current_ids(account) == set()


def test_poualiis_wait_for_code_returns_latest_even_if_marker_seen(monkeypatch) -> None:
    mailbox = LinkApiMailbox()
    account = MailboxAccount(
        email="02-bulks-taker@icloud.com",
        extra={"mail_url": "http://poualiis.xyz/api/mails?recipient=02-bulks-taker@icloud.com&top=1"},
    )
    payload = {
        "mailbox": "INBOX",
        "msg": "Enter this temporary verification code to continue:\n448478\n",
        "status": True,
        "time": "2026-07-17 15:36:03",
    }
    code, markers = mailbox._extract_code_from_mail_payload(payload)
    assert code == "448478"
    stale_baseline = set(markers)

    monkeypatch.setattr(mailbox, "_fetch_json_url", lambda url, label: payload)
    monkeypatch.setattr("core.mailbox_providers.time.sleep", lambda _seconds: None)
    assert mailbox.wait_for_code(account, timeout=2, before_ids=stale_baseline) == "448478"


def test_link_api_wait_for_code_returns_code_api_before_mail_api(monkeypatch) -> None:
    mailbox = LinkApiMailbox()
    account = MailboxAccount(
        email="icloud@example.com",
        extra={
            "code_url": "https://mail.local/api/code/token",
            "mail_url": "https://mail.local/api/mail/token",
        },
    )

    def fetch_json(url: str, label: str) -> dict:
        if label == "mail":
            raise AssertionError("mail API must not be fetched when code API has a fresh code")
        assert url == "https://mail.local/api/code/token"
        return {"data": {"code": "123456", "found": True, "message_id": "code-message"}}

    monkeypatch.setattr(mailbox, "_fetch_json_url", fetch_json)

    assert mailbox.wait_for_code(account, timeout=1) == "123456"


def test_link_api_wait_for_code_ignores_stale_and_not_found_code_payloads(monkeypatch) -> None:
    mailbox = LinkApiMailbox()
    account = MailboxAccount(
        email="icloud@example.com",
        extra={"code_url": "https://mail.local/api/code/token"},
    )
    responses = iter(
        [
            {"data": {"code": "111111", "found": True, "stale_code": True, "message_id": "stale"}},
            {"data": {"verification_code": "222222", "found": False, "message_id": "not-found"}},
            {"data": {"code": "333333", "found": True, "message_id": "fresh"}},
        ]
    )
    requested_labels: list[str] = []

    def fetch_json(_url: str, label: str) -> dict:
        requested_labels.append(label)
        return next(responses)

    monkeypatch.setattr(mailbox, "_fetch_json_url", fetch_json)
    monkeypatch.setattr("core.mailbox_providers.time.sleep", lambda _seconds: None)

    assert mailbox.wait_for_code(account, timeout=1) == "333333"
    assert requested_labels == ["code", "code", "code"]



def test_link_api_wait_for_code_falls_back_to_mail_api_when_code_api_fails(monkeypatch) -> None:
    mailbox = LinkApiMailbox()
    account = MailboxAccount(
        email="icloud@example.com",
        extra={
            "code_url": "https://mail.local/api/code/token",
            "mail_url": "https://mail.local/api/mail/token",
        },
    )
    requested_labels: list[str] = []

    def fetch_json(url: str, label: str) -> dict:
        requested_labels.append(label)
        if label == "code":
            assert url == "https://mail.local/api/code/token"
            raise RuntimeError("code API unavailable")
        assert url == "https://mail.local/api/mail/token"
        return {"data": {"messages": [{"message_id": "mail-1", "text": "Your code is 654321"}]}}

    def fetch_text(_account: MailboxAccount) -> str:
        raise AssertionError("show inbox must not be fetched when account has no inbox_url")

    monkeypatch.setattr(mailbox, "_fetch_json_url", fetch_json)
    monkeypatch.setattr(mailbox, "_fetch_text", fetch_text)

    assert mailbox.wait_for_code(account, timeout=1) == "654321"
    assert requested_labels == ["code", "mail"]


def test_link_api_wait_for_code_falls_back_to_show_inbox_when_code_api_is_stale(monkeypatch) -> None:
    mailbox = LinkApiMailbox()
    account = MailboxAccount(
        email="icloud@example.com",
        extra={
            "code_url": "https://mail.local/api/code/token",
            "inbox_url": "https://mail.local/show/token",
        },
    )
    requested_labels: list[str] = []

    def fetch_json(_url: str, label: str) -> dict:
        requested_labels.append(label)
        return {"data": {"code": "111111", "found": True, "stale_code": True, "message_id": "old-code"}}

    def fetch_text(fetch_account: MailboxAccount) -> str:
        assert fetch_account is account
        return '<html><body><div class="card">Verification code: 987654</div></body></html>'

    monkeypatch.setattr(mailbox, "_fetch_json_url", fetch_json)
    monkeypatch.setattr(mailbox, "_fetch_text", fetch_text)

    assert mailbox.wait_for_code(account, timeout=1) == "987654"
    assert requested_labels == ["code"]

def test_resource_pool_import_and_lease_preserve_code_and_mail_urls(tmp_path: Path) -> None:
    service = ResourcePoolService(ResourcePoolRepository(tmp_path / "resources.db"))

    assert service.import_link_api_mailboxes(ICLOUD_ROW) == 1
    [resource] = service.list_resources("email", "icloud_api")
    assert resource["resource_key"] == "icloud@example.com"
    assert resource["payload"] == {
        "email": "icloud@example.com",
        "inbox_url": "https://mail.local/show/token",
        "code_url": "https://mail.local/api/code/token",
        "mail_url": "https://mail.local/api/mail/token",
    }

    overrides, leases = service.lease_for_task("task-icloud", {"mailbox_provider": "icloud_api"})

    assert [lease.resource_key for lease in leases] == ["icloud@example.com"]
    assert overrides["icloud_api_order_text"] == ICLOUD_ROW
    assert overrides["icloud_api_order_file"] == ""


def test_link_api_create_account_allows_stale_reserved_state(tmp_path: Path, monkeypatch) -> None:
    state_file = tmp_path / "outlook_pool_state.jsonl"
    state_file.write_text(
        json.dumps(
            {
                "email": "icloud@example.com",
                "status": "reserved",
                "updated_at": "2026-01-01T00:00:00",
                "last_error": "stale lease",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    mailbox = LinkApiMailbox(order_text=ICLOUD_ROW)
    monkeypatch.setattr(mailbox, "_state_path", lambda: state_file)

    account = mailbox.create_account()

    assert account.email == "icloud@example.com"


def test_lease_for_task_sets_icloud_api_email_and_skips_consumed(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "icloud-lease.db"
    service = ResourcePoolService(ResourcePoolRepository(db_path))
    service.import_link_api_mailboxes(
        "\n".join(
            [
                "used@example.com----https://mail.local/show/used----code:https://mail.local/api/code/used----mail:https://mail.local/api/mail/used",
                ICLOUD_ROW,
            ]
        )
    )
    monkeypatch.setattr(
        ResourcePoolService,
        "_load_icloud_api_pool_state",
        lambda self: {
            "used@example.com": "consumed",
            "icloud@example.com": "reserved",
        },
    )

    overrides, leases = service.lease_for_task("task-icloud-2", {"mailbox_provider": "icloud_api"})

    assert [lease.resource_key for lease in leases] == ["icloud@example.com"]
    assert overrides["icloud_api_email"] == "icloud@example.com"
    assert overrides["email"] == "icloud@example.com"
    assert overrides["icloud_api_order_text"].startswith("icloud@example.com----")
    assert service.list_resources("email", "icloud_api", "used")[0]["resource_key"] == "used@example.com"
