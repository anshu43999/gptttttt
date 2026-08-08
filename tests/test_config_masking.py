"""
Test: Config masking and API endpoints (no proxy)
"""
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from application.config_service import is_sensitive_key, mask_value


def test_sensitive_key_detection():
    assert is_sensitive_key("herosms_api_key")
    assert is_sensitive_key("mailbox_imap_pass")
    assert is_sensitive_key("iceaix_api_key")
    assert is_sensitive_key("cfworker_admin_token")
    assert is_sensitive_key("outlook_password")
    assert is_sensitive_key("lajiao_proxy_credentials")
    assert not is_sensitive_key("phone_retry_limit")
    assert not is_sensitive_key("sms_country")
    assert not is_sensitive_key("headed")


def test_mask_value_preserves_source_fields():
    assert mask_value("herosms_api_key", "real_key_123") == "real_key_123"
    assert mask_value("phone_retry_limit", 20) == 20
    assert mask_value("sms_country", "73") == "73"
    assert mask_value("api_key", "") == ""
    assert mask_value("token", None) is None


def test_safe_config_returns_source_values():
    from application.config_service import ConfigService
    svc = ConfigService(base_config="config.example.yaml")
    safe = svc.safe_file_config()
    assert safe.get("herosms_api_key") != "***"
    svc2 = ConfigService(base_config="config.yaml")
    safe2 = svc2.safe_file_config()
    assert safe2.get("sms_api_key") != "***"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
