from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.mailbox.outlook_token import OutlookTokenAccount, OutlookTokenMailbox


def test_outlook_token_candidates_skip_used_rows(tmp_path: Path) -> None:
    order_file = tmp_path / "order.txt"
    order_file.write_text(
        "used@example.com----pw1----cid1----rt1\n"
        "free@example.com----pw2----cid2----rt2\n",
        encoding="utf-8",
    )
    state_file = tmp_path / "pool.jsonl"
    state_file.write_text('{"email":"used@example.com","status":"registered","updated_at":"2026-01-01T00:00:00"}\n', encoding="utf-8")
    mailbox = OutlookTokenMailbox({"outlook_token_order_file": str(order_file), "outlook_pool_state_file": str(state_file)})
    candidates = mailbox.candidates()
    assert candidates == [OutlookTokenAccount("free@example.com", "pw2", "cid2", "rt2")]


def test_prepared_outlook_resource_lease_bypasses_legacy_cooldown(tmp_path: Path) -> None:
    state_file = tmp_path / "pool.jsonl"
    state_file.write_text(
        '{"email":"leased@example.com","status":"failed_retryable","updated_at":"2026-01-01T00:00:00"}\n'
        '{"email":"leased@example.com","status":"failed_retryable","updated_at":"2026-01-01T00:01:00"}\n',
        encoding="utf-8",
    )

    mailbox = OutlookTokenMailbox({
        "_resources_prepared": True,
        "resource_leases": [{"type": "email", "provider": "outlook_token", "key": "leased@example.com"}],
        "outlook_email": "leased@example.com",
        "outlook_password": "pw",
        "outlook_client_id": "cid",
        "outlook_refresh_token": "rt",
        "outlook_pool_state_file": str(state_file),
    })

    assert mailbox.candidates() == [OutlookTokenAccount("leased@example.com", "pw", "cid", "rt")]


def test_outlook_token_candidates_require_canonical_four_field_records(tmp_path: Path) -> None:
    order_file = tmp_path / "order.txt"
    order_file.write_text(
        "not-an-email----pw----cid----rt\n"
        "extra@example.com----pw----cid----rt----unexpected\n"
        "valid@example.com--------cid----rt\n",
        encoding="utf-8",
    )

    candidates = OutlookTokenMailbox({"outlook_token_order_file": str(order_file)}).candidates()

    assert candidates == [OutlookTokenAccount("valid@example.com", "", "cid", "rt")]




def test_outlook_wait_for_openai_code_extracts_fake_graph_message(monkeypatch) -> None:
    message = {
        "from": {"emailAddress": {"address": "noreply@openai.com"}},
        "subject": "ChatGPT code",
        "receivedDateTime": datetime.now(timezone.utc).isoformat(),
        "body": {"content": "Your ChatGPT verification code is 737171."},
    }
    mailbox = OutlookTokenMailbox({})
    monkeypatch.setattr(mailbox, "refresh_graph_access_token", lambda client_id, refresh_token: "access-token")
    monkeypatch.setattr(mailbox, "_list_graph_messages", lambda access_token: [message])

    code = mailbox.wait_for_openai_code(OutlookTokenAccount("a@example.com", "pw", "cid", "rt"), timeout=1)

    assert code == "737171"


def test_outlook_wait_for_openai_code_skips_stale_and_rejected(monkeypatch) -> None:
    stale = {
        "from": {"emailAddress": {"address": "noreply@openai.com"}},
        "subject": "ChatGPT code",
        "receivedDateTime": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
        "body": {"content": "Your ChatGPT verification code is 111111."},
    }
    rejected = {
        "from": {"emailAddress": {"address": "noreply@openai.com"}},
        "subject": "ChatGPT code",
        "receivedDateTime": datetime.now(timezone.utc).isoformat(),
        "body": {"content": "Your ChatGPT verification code is 222222."},
    }
    fresh = {
        "from": {"emailAddress": {"address": "noreply@openai.com"}},
        "subject": "ChatGPT code",
        "receivedDateTime": datetime.now(timezone.utc).isoformat(),
        "body": {"content": "Your ChatGPT verification code is 333333."},
    }
    mailbox = OutlookTokenMailbox({})
    monkeypatch.setattr(mailbox, "refresh_graph_access_token", lambda client_id, refresh_token: "access-token")
    monkeypatch.setattr(mailbox, "_list_graph_messages", lambda access_token: [stale, rejected, fresh])

    code = mailbox.wait_for_openai_code(
        OutlookTokenAccount("a@example.com", "pw", "cid", "rt"),
        timeout=1,
        reject_codes={"222222"},
    )
    assert code == "333333"


def test_outlook_graph_refresh_requests_mail_read(monkeypatch) -> None:
    captured = {}

    class FakeResponse:
        status_code = 200
        content = b'{"access_token":"access-token"}'

        @staticmethod
        def json():
            return {"access_token": "access-token"}

    class FakeSession:
        trust_env = True

        def post(self, url, *, data, timeout, proxies=None):
            captured.update(url=url, data=data, timeout=timeout, proxies=proxies)
            return FakeResponse()

        @staticmethod
        def close():
            pass

    session = FakeSession()
    monkeypatch.setattr("requests.Session", lambda: session)

    mailbox = OutlookTokenMailbox(
        {"mailat_protocol_proxy": "socks5://user:pass@us.rrp.bestgo.work:10000"}
    )
    assert mailbox.refresh_graph_access_token("client", "refresh") == "access-token"
    assert session.trust_env is False
    assert captured["data"]["scope"] == "https://graph.microsoft.com/.default offline_access"
    assert captured["proxies"] is not None
    assert "bestgo" in str(captured["proxies"].get("https") or "")


def test_outlook_graph_reader_uses_inbox_text_body(monkeypatch) -> None:
    captured = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"value": []}

    class FakeSession:
        trust_env = True

        def get(self, url, *, headers, params, timeout, proxies=None):
            captured.update(url=url, headers=headers, params=params, timeout=timeout, proxies=proxies)
            return FakeResponse()

        @staticmethod
        def close():
            pass

    session = FakeSession()
    monkeypatch.setattr("requests.Session", lambda: session)

    mailbox = OutlookTokenMailbox(
        {"mailat_protocol_proxy": "socks5://u:p@us.1024proxy.io:3000"}
    )
    assert mailbox._list_graph_messages("access-token") == []
    assert session.trust_env is False
    assert captured["url"] == "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages"
    assert captured["headers"]["Prefer"] == 'outlook.body-content-type="text"'
    assert captured["params"]["$select"] == "from,subject,body,receivedDateTime"
    assert captured["proxies"] is not None
    assert "1024" in str(captured["proxies"].get("https") or "")
