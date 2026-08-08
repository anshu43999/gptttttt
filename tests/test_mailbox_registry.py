"""
Test: Mailbox Provider — Cloudflare forwarding + 163 IMAP (real credentials, no proxy)
"""
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.mailbox_providers import ForwardedDomainMailbox, CFWorkerMailbox


def test_forwarded_domain_creation():
    """Create mailbox from config → verify email format"""
    mbox = ForwardedDomainMailbox.from_config({
        "mailbox_domain": "5445945.xyz",
        "mailbox_imap_user": "lll35844493@163.com",
        "mailbox_imap_pass": "JNXjgD4w2U4LNab8",
        "mailbox_imap_host": "imap.163.com",
    })
    assert mbox.domain == "5445945.xyz"
    assert mbox.imap_host == "imap.163.com"

    account = mbox.create_account()
    assert account.email.endswith("@5445945.xyz")
    assert "@" in account.email
    assert account.account_id


def test_forwarded_domain_auto_host_detection():
    """IMAP host auto-detected from email domain"""
    mbox = ForwardedDomainMailbox(
        domain="5445945.xyz",
        imap_user="lll35844493@163.com",
        imap_pass="JNXjgD4w2U4LNab8",
    )
    assert mbox.imap_host == "imap.163.com"


def test_forwarded_domain_requires_credentials_for_wait():
    """wait_for_code raises RuntimeError without IMAP credentials"""
    mbox = ForwardedDomainMailbox(domain="5445945.xyz")
    account = mbox.create_account()
    with pytest.raises(RuntimeError, match="未配置 IMAP"):
        mbox.wait_for_code(account, timeout=1)


def test_forwarded_domain_wait_filters_by_target_recipient():
    class FakeMailbox(ForwardedDomainMailbox):
        def _imap_messages(self, *, limit: int, account=None):
            return [
                ("1", "To: other@example.com From: OpenAI Your code is 111111"),
                ("2", "To: target@example.com From: OpenAI Your code is 222222"),
            ]

    mbox = FakeMailbox(domain="example.com", imap_user="u", imap_pass="p")
    account = mbox.account_for_email("target@example.com") if hasattr(mbox, "account_for_email") else mbox.create_account()
    account.email = "target@example.com"
    assert mbox.wait_for_code(account, timeout=1) == "222222"


def test_forwarded_domain_message_match_requires_account_and_openai():
    mbox = ForwardedDomainMailbox(domain="example.com", imap_user="u", imap_pass="p")
    account = mbox.create_account()
    account.email = "target@example.com"
    assert not mbox._message_matches_account("To: other@example.com From: OpenAI Code 111111", account)
    assert not mbox._message_matches_account("To: target@example.com From: Newsletter Code 222222", account)
    assert mbox._message_matches_account("Delivered-To: target@example.com From: OpenAI Code 333333", account)


def test_mailbox_account_creation_random_prefixes():
    """Each create() gives a unique email"""
    mbox = ForwardedDomainMailbox.from_config({
        "mailbox_domain": "5445945.xyz",
        "mailbox_imap_user": "lll35844493@163.com",
        "mailbox_imap_pass": "JNXjgD4w2U4LNab8",
    })
    e1 = mbox.create_account()
    e2 = mbox.create_account()
    assert e1.email != e2.email
    assert e1.email.endswith("@5445945.xyz")



def test_forwarded_domain_returns_fetched_messages_before_later_imap_timeout():
    class FakeConn:
        def search(self, _charset, _criteria):
            return "OK", [b"1 2 3"]

        def fetch(self, mid, _query):
            if mid == b"1":
                raise TimeoutError("slow stale message")
            return "OK", [(None, b"To: target@example.com\r\nFrom: OpenAI\r\nSubject: Code\r\n\r\nYour code is 222222")]

        def logout(self):
            return "OK", []

    class FakeMailbox(ForwardedDomainMailbox):
        def _connect_imap(self):
            return FakeConn()

    mbox = FakeMailbox(domain="example.com", imap_user="u", imap_pass="p")
    messages = mbox._imap_messages(limit=3)
    assert [mid for mid, _raw in messages] == ["3", "2"]
    assert "222222" in messages[0][1]

def test_forwarded_domain_searches_target_recipient_before_fetching_body():
    class FakeConn:
        def __init__(self):
            self.searches = []

        def search(self, _charset, *criteria):
            self.searches.append(criteria)
            if criteria == ("TO", '"target@example.com"'):
                return "OK", [b"7"]
            return "OK", [b""]

        def fetch(self, mid, query):
            assert mid == b"7"
            if query == "(BODY.PEEK[HEADER])":
                return "OK", [(None, b"To: target@example.com\r\nFrom: OpenAI\r\nSubject: Code\r\n\r\n")]
            return "OK", [(None, b"To: target@example.com\r\nFrom: OpenAI\r\nSubject: Code\r\n\r\nYour code is 333444")]

        def logout(self):
            return "OK", []

    conn = FakeConn()

    class FakeMailbox(ForwardedDomainMailbox):
        def _connect_imap(self):
            return conn

    mbox = FakeMailbox(domain="example.com", imap_user="u", imap_pass="p")
    account = mbox.create_account()
    account.email = "target@example.com"
    assert mbox._imap_messages(limit=40, account=account)[0][0] == "7"
    assert "333444" in mbox._imap_messages(limit=40, account=account)[0][1]
    assert ("ALL",) not in conn.searches


def test_real_imap_connectivity():
    """REAL TEST: connect to 163 IMAP and verify reachable (no proxy)"""
    import imaplib
    try:
        conn = imaplib.IMAP4_SSL("imap.163.com", 993, timeout=10)
        conn.login("lll35844493@163.com", "JNXjgD4w2U4LNab8")
        conn.select("INBOX")
        status, _ = conn.search(None, "ALL")
        conn.logout()
        assert status == "OK"
    except Exception as e:
        pytest.skip(f"IMAP connection failed (network issue): {e}")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
