"""
Test: Email webhook API — Cloudflare Worker callback (no proxy)
"""
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infrastructure.db import init_db, insert_email_otp, get_latest_email_otp, consume_email_otp


def setup_module():
    init_db()


def test_insert_and_retrieve_otp():
    insert_email_otp("test123@example.invalid", "789012", subject="OpenAI verification code")
    otp = get_latest_email_otp("test123@example.invalid")
    assert otp.get("code") == "789012"
    assert otp.get("consumed") == 0


def test_consume_otp():
    insert_email_otp("test456@example.invalid", "345678")
    consume_email_otp("test456@example.invalid", "345678", consumed_by="test")
    otp = get_latest_email_otp("test456@example.invalid")
    # Should be consumed, no result
    assert not otp or otp.get("code") != "345678"


def test_email_case_insensitive():
    insert_email_otp("TestUpper@example.invalid", "111111")
    otp = get_latest_email_otp("testupper@example.invalid")
    assert otp.get("code") == "111111"


def test_no_otp_found():
    otp = get_latest_email_otp("nonexistent@example.invalid")
    assert not otp


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
