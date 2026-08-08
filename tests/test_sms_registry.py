"""
Test: SMS Provider Registry — unit tests (no network, no proxy)
"""
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.base_sms import create_sms_provider, HeroSmsProvider, SmsBowerProvider, UserProvidedSmsProvider


def test_herosms_provider_creation():
    config = {
        "sms_provider": "herosms_api",
        "herosms_api_key": "test_key",
        "sms_country": "73",
        "herosms_max_price": 0.045,
        "herosms_fixed_price": True,
    }
    sms = create_sms_provider("herosms_api", config)
    assert isinstance(sms, HeroSmsProvider)
    assert sms.default_country == "73"
    assert sms.max_price == 0.045


def test_smsbower_provider_creation():
    config = {
        "sms_provider": "smsbower_api",
        "smsbower_api_key": "test_key",
        "smsbower_country": "73",
    }
    sms = create_sms_provider("smsbower_api", config)
    assert isinstance(sms, SmsBowerProvider)
    assert sms.BASE_URL == "https://smsbower.page/stubs/handler_api.php"


def test_smsbower_acquisition_uses_openai_brazil_gold_price_constraints(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        text = "ACCESS_NUMBER:activation-123:5511999999999"

        def json(self) -> dict[str, str]:
            return {"activationId": "activation-123", "phoneNumber": "5511999999999"}

        def raise_for_status(self) -> None:
            pass

    class FakeSession:
        def get(self, url, *, params, timeout, proxies):
            captured["url"] = url
            captured["params"] = params
            captured["timeout"] = timeout
            captured["proxies"] = proxies
            return FakeResponse()

    provider = SmsBowerProvider(
        "smsbower-test-key",
        default_service="dr",
        default_country="73",
        min_price=0.054,
        max_price=0.054,
        provider_ids="3160",
        reuse_phone_to_max=False,
    )
    provider.session = FakeSession()
    monkeypatch.setattr(provider, "_save_cache", lambda _cache: None)

    activation = provider.get_number(service="", country="")

    assert activation.activation_id == "activation-123"
    assert activation.phone_number == "+5511999999999"
    assert captured["url"] == provider.BASE_URL
    assert captured["timeout"] == 30
    assert captured["proxies"] is None
    assert captured["params"] == {
        "action": "getNumberV2",
        "service": "dr",
        "country": "73",
        "minPrice": 0.054,
        "maxPrice": 0.054,
        "providerIds": "3160",
        "api_key": "smsbower-test-key",
    }


def test_smsbower_completion_uses_documented_set_status_six() -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        text = "ACCESS_ACTIVATION"

        def raise_for_status(self) -> None:
            pass

    class FakeSession:
        def get(self, url, *, params, timeout, proxies):
            captured["url"] = url
            captured["params"] = params
            return FakeResponse()

    provider = SmsBowerProvider("smsbower-test-key")
    provider.session = FakeSession()

    assert provider.finish_activation("activation-123") is True
    assert captured["url"] == provider.BASE_URL
    assert captured["params"] == {
        "action": "setStatus",
        "id": "activation-123",
        "status": 6,
        "api_key": "smsbower-test-key",
    }


def test_user_provided_creation():
    config = {
        "sms_provider": "user_phone_url",
        "sms_phone_url": "+55|11999999999|https://example.com/sms",
    }
    sms = create_sms_provider("user_phone_url", config)
    assert isinstance(sms, UserProvidedSmsProvider)


def test_sms_providers_do_not_inherit_registration_proxy():
    config = {
        "sms_provider": "user_phone_url",
        "sms_phone_url": "+55|11999999999|https://example.com/sms",
        "proxy": "socks5h://register-proxy.example:2000",
    }
    sms = create_sms_provider("user_phone_url", config)
    assert sms.proxies is None


def test_sms_provider_uses_explicit_sms_proxy_only():
    config = {
        "sms_provider": "user_phone_url",
        "sms_phone_url": "+55|11999999999|https://example.com/sms",
        "proxy": "socks5h://register-proxy.example:2000",
        "sms_proxy": "http://127.0.0.1:8080",
    }
    sms = create_sms_provider("user_phone_url", config)
    assert sms.proxies == {"http": "http://127.0.0.1:8080", "https": "http://127.0.0.1:8080"}


def test_sms_factory_raises_on_unknown():
    with pytest.raises(RuntimeError, match="未知的接码服务"):
        create_sms_provider("nonexistent", {})


def test_hero_sms_cache_alive():
    from core.base_sms import is_herosms_phone_cache_alive
    alive, info = is_herosms_phone_cache_alive({})
    # No cache file → not alive
    assert alive is False
    assert info.get("alive") is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
