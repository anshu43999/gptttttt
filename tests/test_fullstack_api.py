from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import time
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from api.register import RegisterRequest, OAuthBindRequest, config_overrides
from application.accounts_service import AccountsService, account_operation_lock
from application.config_service import ConfigService, is_sensitive_key, mask_value
from application.providers_service import ProvidersService
from application.resource_pool_service import ResourcePoolService
from infrastructure.repositories.accounts_repository import AccountsRepository
from infrastructure.repositories.config_repository import ConfigRepository
from infrastructure.repositories.providers_repository import ProvidersRepository
from infrastructure.repositories.resource_pool_repository import ResourcePoolRepository
from core.proxy.credential_runtime import CredentialProxyRuntime
from infrastructure import db


class DummyContext:
    def __init__(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        self.config = ConfigService(ConfigRepository(db_path))
        self.resources = ResourcePoolService(ResourcePoolRepository(db_path))
        self.providers = ProvidersService(ProvidersRepository(db_path), self.config, self.resources)


def test_register_overrides_use_brazil_price_and_credentials() -> None:
    payload = {
        "sms_mode": "herosms_api",
        "phone_country": "BR",
        "proxy_country": "JP",
        "proxy_mode": "credentials",
        "lajiao_credentials": "user:pass@host:2000",
        "headed": True,
    }
    overrides = config_overrides(payload)
    assert overrides["herosms_fixed_price"] is False
    assert overrides["herosms_max_price"] == 0.0999
    assert overrides["lajiao_proxy_mode"] == "credentials"
    assert overrides["lajiao_proxy_credential_protocol"] == "http"
    assert overrides["lajiao_proxy_credentials"] == "user:pass@host:2000"
    assert overrides["lajiao_proxy_expected_country"] == "JP"

    ui_payload = {
        "sms_provider": "herosms_api",
        "sms_country": "BR",
        "proxy_mode": "credentials",
        "proxy_region": "JP",
        "force_signup": True,
        "skip_precheck": True,
    }
    ui_overrides = config_overrides(ui_payload)
    assert ui_overrides["sms_country"] == "73"
    assert ui_overrides["country_code"] == "55"
    assert ui_overrides["lajiao_proxy_mode"] == "credentials"
    assert ui_overrides["lajiao_proxy_regions"] == "JP"
    assert ui_overrides["force_signup_from_login_password"] is True
    assert ui_overrides["precheck_phone_before_sms"] is False
    assert ui_overrides["herosms_max_price"] == 0.0999
    assert ui_overrides["herosms_max_price"] < 0.1



def test_register_overrides_select_smsbower_for_brazil_without_hero_prices() -> None:
    overrides = config_overrides({"sms_provider": "smsbower_api", "sms_country": "BR"})

    assert overrides["sms_provider"] == "smsbower_api"
    assert overrides["country_code"] == "55"
    assert overrides["country_name"] == "Brazil"
    assert overrides["sms_country"] == "73"
    assert "herosms_fixed_price" not in overrides
    assert "herosms_max_price" not in overrides

def test_register_overrides_clear_proxy_country_fallbacks_for_auto_region() -> None:
    overrides = config_overrides({"proxy_region": "auto"})

    assert {key: overrides[key] for key in (
        "lajiao_proxy_regions",
        "lajiao_proxy_expected_country",
        "proxy_region",
        "lajiao_proxy_region",
    )} == {
        "lajiao_proxy_regions": "",
        "lajiao_proxy_expected_country": "",
        "proxy_region": "",
        "lajiao_proxy_region": "",
    }


def test_register_overrides_preserve_manual_proxy_country_regions() -> None:
    for region in ("TR", "BR"):
        overrides = config_overrides({"proxy_region": region.lower()})

        assert overrides["lajiao_proxy_regions"] == region
        assert overrides["lajiao_proxy_expected_country"] == region


def test_register_request_defaults_to_headed_browser() -> None:
    assert RegisterRequest().headed is True
    assert OAuthBindRequest().headed is True


def test_email_protocol_overrides_inherit_backend_when_unspecified() -> None:
    overrides = config_overrides({
        "mode": "email_protocol",
        "mailbox_provider": "outlook_token",
    })
    assert overrides["registration_engine"] == "email_protocol"
    # Unspecified request no longer forces python; task config inherits file/db default (go).
    assert "email_protocol_backend" not in overrides
    assert overrides["mailbox_provider"] == "outlook_token"


def test_email_protocol_overrides_accept_go_backend() -> None:
    overrides = config_overrides({
        "mode": "email_protocol",
        "email_protocol_backend": "go",
        "go_email_protocol_url": "http://127.0.0.1:19001",
        "go_email_protocol_timeout_seconds": 600,
        "mailbox_provider": "icloud_api",
    })
    assert overrides["email_protocol_backend"] == "go"
    assert overrides["go_email_protocol_url"] == "http://127.0.0.1:19001"
    assert overrides["go_email_protocol_timeout_seconds"] == 600
    assert overrides["mailbox_provider"] == "icloud_api"


def test_fast_email_register_reports_authorize_challenge() -> None:
    from platforms.chatgpt.fast_email_register import FastEmailRegistrationFlow

    class BodyLocator:
        def inner_text(self, timeout: int = 0) -> str:
            return "Cloudflare verify you are human"

    class FakePage:
        url = "https://auth.openai.com/api/accounts/authorize?client_id=x"

        def locator(self, selector: str):
            assert selector == "body"
            return BodyLocator()

    reason = FastEmailRegistrationFlow(log_fn=lambda _: None)._blocked_authorize_reason(FakePage(), {"page_type": ""})
    assert "OpenAI/Cloudflare" in reason
    assert "显示浏览器窗口" in reason



def test_herosms_provider_clamps_price_below_ten_cents() -> None:
    from core.base_sms import HeroSmsProvider

    provider = HeroSmsProvider.from_config({"sms_api_key": "key", "herosms_max_price": 0.5})
    assert provider.max_price == 0.0999

    provider = HeroSmsProvider.from_config({"sms_api_key": "key", "herosms_max_price": 0.08})
    assert provider.max_price == 0.08


def test_config_api_returns_source_values(tmp_path: Path) -> None:
    ctx = DummyContext(tmp_path)
    saved = ctx.config.save_overrides({"plain": "ok", "api_key": "secret"})
    assert isinstance(saved, dict)
    current = ctx.config.safe_db_config()
    assert current["plain"] == "ok"
    assert current["api_key"] == "secret"


def test_provider_defaults_and_dry_tests(tmp_path: Path) -> None:
    ctx = DummyContext(tmp_path)
    items = ctx.providers.list_providers()
    names = {(item["provider_type"], item["provider_name"]) for item in items}
    assert ("sms", "herosms_api") in names
    assert ("proxy", "lajiao_credentials") in names
    herosms = next(item for item in items if item["provider_name"] == "herosms_api")
    assert herosms["settings"]["max_price"] == 0.0999
    assert herosms["definition"]["label"] == "HeroSMS 接码"
    assert any(field["key"] == "sms_api_key" and field["label"] == "HeroSMS API Key" and field["secret"] is True for field in herosms["definition"]["fields"])
    proxy = next(item for item in items if item["provider_type"] == "proxy" and item["provider_name"] == "lajiao_credentials")
    assert proxy["definition"]["label"] == "账密代理池"
    assert any(field["key"] == "lajiao_proxy_credentials_text" and field["multiline"] is True for field in proxy["definition"]["fields"])


def test_provider_settings_merge_with_definitions(tmp_path: Path) -> None:
    ctx = DummyContext(tmp_path)
    ctx.config.save_overrides({"sms_api_key": "runtime-secret", "sms_service": "dr", "sms_country": "73"})
    ctx.providers.save_provider("sms", "herosms_api", {"herosms_max_price": 0.05, "country_name": "Brazil"})

    herosms = next(item for item in ctx.providers.list_providers() if item["provider_type"] == "sms" and item["provider_name"] == "herosms_api")
    assert herosms["settings"]["sms_api_key"] == "***"
    assert herosms["settings"]["sms_service"] == "dr"
    assert herosms["settings"]["sms_country"] == "73"
    assert herosms["settings"]["country_name"] == "Brazil"
    assert herosms["settings"]["herosms_max_price"] == 0.05
    assert herosms["definition"]["provider_type"] == "sms"
    assert herosms["definition"]["provider_name"] == "herosms_api"



def test_provider_save_imports_user_phone_and_proxy(tmp_path: Path) -> None:
    ctx = DummyContext(tmp_path)

    ctx.providers.save_provider("sms", "user_phone_url", {
        "sms_phone_urls_text": "15555550100|https://sms.example.invalid/messages/alpha\n15555550101|https://sms.example.invalid/messages/beta",
    })
    config = ctx.config.db_config()
    assert config["sms_provider"] == "user_phone_url"
    assert config["sms_phone_url"] == "15555550100|https://sms.example.invalid/messages/alpha"
    assert "15555550101|https://sms.example.invalid/messages/beta" in config["sms_phone_urls"]

    ctx.providers.save_provider("proxy", "lajiao_credentials", {
        "lajiao_proxy_credentials_text": "proxy-user-a:proxy-password@proxy.example.invalid:1080\nproxy-user-b:proxy-password@proxy.example.invalid:1080",
        "lajiao_proxy_regions": "JP",
    })
    config = ctx.config.db_config()
    assert config["lajiao_proxy_mode"] == "credentials"
    assert "proxy-user-a" in config["lajiao_proxy_credentials"]
    phones = ctx.resources.list_resources("phone", "user_phone_url")
    proxies = ctx.resources.list_resources("proxy", "lajiao_credentials")
    assert len(phones) == 2
    assert len(proxies) == 2

    lease_overrides, leases = ctx.resources.lease_for_task("task-test", {"sms_provider": "user_phone_url", "lajiao_proxy_mode": "credentials", "lajiao_proxy_regions": "JP"})
    assert lease_overrides["sms_phone_url"].startswith("15555550100|")
    assert "proxy-user-a" in lease_overrides["lajiao_proxy_credentials"]
    assert len(leases) == 2
    assert sum(1 for lease in leases if lease.resource_type == "proxy") == 1

    ctx.providers.save_provider("sms", "bind_user_phone_url", {
        "bind_sms_phone_urls_text": "15555550102----https://sms.example.invalid/messages/bind0\n15555550103----https://sms.example.invalid/messages/bind1",
    })
    config = ctx.config.db_config()
    assert config["bind_sms_provider"] == "bind_user_phone_url"
    assert config["bind_sms_phone_url"] == "15555550102|https://sms.example.invalid/messages/bind0"
    bind_phones = ctx.resources.list_resources("phone", "bind_user_phone_url")
    assert len(bind_phones) == 2

    bind_overrides, bind_leases = ctx.resources.lease_for_task("task-bind", {"bind_sms_provider": "bind_user_phone_url"})
    assert bind_overrides["bind_sms_phone_url"].startswith("15555550102|")
    assert "sms_phone_url" not in bind_overrides
    assert len(bind_leases) == 1


def test_protocol_binding_leases_bind_phone_without_new_registration_identity(tmp_path: Path) -> None:
    pool = ResourcePoolService(ResourcePoolRepository(tmp_path / "protocol-bind-pools.db"))
    pool.import_phone_urls("15555550100|https://sms.example.invalid/register", provider="user_phone_url")
    pool.import_phone_urls("15555550101|https://sms.example.invalid/bind", provider="bind_user_phone_url")
    pool.import_outlook_tokens("existing@example.com----password----client-id----refresh-token")

    overrides, leases = pool.lease_for_task("task-protocol-bind", {
        "_protocol_cpa_bind_task": True,
        "sms_provider": "user_phone_url",
        "bind_sms_provider": "bind_user_phone_url",
        "mailbox_provider": "outlook_token",
    })

    assert [(lease.resource_type, lease.provider) for lease in leases] == [("phone", "bind_user_phone_url")]
    assert overrides["bind_sms_phone_url"].startswith("15555550101|")
    assert "sms_phone_url" not in overrides
    assert "outlook_email" not in overrides
    assert pool.list_resources("phone", "user_phone_url")[0]["status"] == "available"
    assert pool.list_resources("email", "outlook_token")[0]["status"] == "available"


def test_resource_import_skips_existing_rows_without_reactivating(tmp_path: Path) -> None:
    pool = ResourcePoolService(ResourcePoolRepository(tmp_path / "dedupe-import.db"))
    pool.import_icloud_privacy_mailboxes("alias@example.invalid\nnew@example.invalid")
    existing = pool.repo.get("email", "icloud_privacy", "alias@example.invalid")
    pool.set_status(int(existing["id"]), "disabled", error="already consumed")

    imported = pool.import_icloud_privacy_mailboxes("alias@example.invalid\nnew@example.invalid\nthird@example.invalid")

    assert imported == 1
    assert pool.repo.get("email", "icloud_privacy", "alias@example.invalid")["status"] == "disabled"
    assert pool.repo.get("email", "icloud_privacy", "new@example.invalid")["status"] == "available"
    assert pool.repo.get("email", "icloud_privacy", "third@example.invalid")["status"] == "available"


def test_resource_import_dedupes_rows_in_same_payload(tmp_path: Path) -> None:
    pool = ResourcePoolService(ResourcePoolRepository(tmp_path / "dedupe-payload.db"))

    imported = pool.import_icloud_privacy_mailboxes("alias@example.invalid\nalias@example.invalid\nsecond@example.invalid")

    assert imported == 2
    assert len(pool.list_resources("email", "icloud_privacy")) == 2

def test_bind_phone_can_be_reused_three_times(tmp_path: Path) -> None:
    pool = ResourcePoolService(ResourcePoolRepository(tmp_path / "quota.db"))
    pool.import_phone_urls("15555550102|https://sms.example.invalid/messages/bind", provider="bind_user_phone_url")

    for index in range(3):
        overrides, leases = pool.lease_for_task(f"task-bind-{index}", {"bind_sms_provider": "bind_user_phone_url"})
        assert leases[0].resource_key == "15555550102"
        reports = pool.report_for_task(f"task-bind-{index}", "succeeded", {"resource_leases": overrides["resource_leases"]})
        expected = "available" if index < 2 else "used"
        assert reports[0]["status"] == expected
        current = pool.list_resources("phone", "bind_user_phone_url")[0]
        assert current["success_count"] == index + 1
        assert current["status"] == expected


def test_bind_phone_capacity_counts_remaining_quota(tmp_path: Path) -> None:
    pool = ResourcePoolService(ResourcePoolRepository(tmp_path / "quota-capacity.db"))
    pool.import_phone_urls("15555550102|https://sms.example.invalid/messages/bind", provider="bind_user_phone_url")
    for index in range(2):
        overrides, _leases = pool.lease_for_task(f"task-bind-quota-{index}", {"bind_sms_provider": "bind_user_phone_url"})
        pool.report_for_task(f"task-bind-quota-{index}", "succeeded", {"resource_leases": overrides["resource_leases"]})

    summary = pool.capacity_summary(need_bind_phone=1)
    bind_phone = next(item for item in summary["resources"] if item["provider"] == "bind_user_phone_url")

    assert bind_phone["available"] == 1
    assert bind_phone["enough"] is True


def test_proxy_lease_provides_single_initial_candidate(tmp_path: Path) -> None:
    pool = ResourcePoolService(ResourcePoolRepository(tmp_path / "proxy-batch.db"))
    pool.import_lajiao_credentials("\n".join(f"user-region-JP-sid-{i}-t-10:pass@proxy.local:2000" for i in range(6)))

    overrides, leases = pool.lease_for_task("task-proxy-batch", {
        "lajiao_proxy_mode": "credentials",
        "lajiao_proxy_regions": "JP",
        "lajiao_proxy_max_batches": 5,
    })

    assert len([lease for lease in leases if lease.resource_type == "proxy"]) == 1
    assert len(overrides["lajiao_proxy_credentials"].splitlines()) == 1



def test_proxy_lease_ignores_max_candidates_for_initial_lease(tmp_path: Path) -> None:
    pool = ResourcePoolService(ResourcePoolRepository(tmp_path / "proxy-max-candidates.db"))
    pool.import_lajiao_credentials("\n".join(f"user-region-JP-sid-{i}-t-10:pass@proxy.local:2000" for i in range(70)))

    overrides, leases = pool.lease_for_task("task-proxy-max-candidates", {
        "lajiao_proxy_mode": "credentials",
        "lajiao_proxy_regions": "JP",
        "lajiao_proxy_task_batch_size": 10,
        "lajiao_proxy_max_candidates": 60,
    })

    proxy_leases = [lease for lease in leases if lease.resource_type == "proxy"]
    assert len(proxy_leases) == 1
    assert len(overrides["lajiao_proxy_credentials"].splitlines()) == 1

def test_proxy_runtime_cools_failed_probe_and_leases_next(tmp_path: Path) -> None:
    db_path = tmp_path / "dynamic-proxy.db"
    task_id = "task-dynamic-proxy"
    config_path = tmp_path / f"{task_id}_config.yaml"
    pool = ResourcePoolService(ResourcePoolRepository(db_path))
    pool.import_lajiao_credentials("\n".join([
        "user-region-JP-sid-bad-t-10:pass@proxy.local:2000",
        "user-region-JP-sid-good-t-10:pass@proxy.local:2000",
    ]))
    config = {
        "dashboard_task_id": task_id,
        "_task_config_path": str(config_path),
        "_resource_pool_db_path": str(db_path),
        "rotate_proxy_each_attempt": True,
        "lajiao_proxy_mode": "credentials",
        "lajiao_proxy_regions": "JP",
        "lajiao_proxy_max_candidates": 2,
        "lajiao_proxy_credential_protocol": "socks5",
        "resource_leases": [],
    }
    config_path.write_text("resource_leases: []\n", encoding="utf-8")
    runtime = CredentialProxyRuntime(config)
    checked: list[str] = []

    def fake_check(proxy: str) -> tuple[bool, str]:
        checked.append(proxy)
        return ("sid-good" in proxy, "198.51.100.4" if "sid-good" in proxy else "")

    runtime.check = fake_check  # type: ignore[method-assign]

    selected, exit_ip = runtime.select()

    assert "sid-good" in selected
    assert exit_ip == "198.51.100.4"
    assert len(checked) == 2
    cooldown = pool.list_resources("proxy", "lajiao_credentials", "cooldown")
    leased = pool.list_resources("proxy", "lajiao_credentials", "leased")
    assert len(cooldown) == 1
    assert "sid-bad" in cooldown[0]["resource_key"]
    assert len(leased) == 1
    assert "sid-good" in leased[0]["resource_key"]


def test_proxy_failure_cools_only_selected_proxy(tmp_path: Path) -> None:
    pool = ResourcePoolService(ResourcePoolRepository(tmp_path / "proxy-selected.db"))
    pool.import_lajiao_credentials("\n".join(f"user-region-JP-sid-{i}-t-30:pass@proxy.local:2000" for i in range(3)))

    selected = "user-region-JP-sid-1-t-30:pass@proxy.local:2000"
    resource_leases = [
        {"type": "proxy", "provider": "lajiao_credentials", "key": f"user-region-JP-sid-{i}-t-30:pass@proxy.local:2000"}
        for i in range(3)
    ]
    reports = pool.report_for_task(
        "task-proxy-selected",
        "failed",
        {"resource_leases": resource_leases},
        log_text=f"  使用新代理: {selected} exit_ip=198.51.100.4\nCloudflare status=403",
    )

    by_key = {item["key"]: item for item in reports if item["type"] == "proxy"}
    assert by_key[selected]["status"] == "cooldown"
    assert by_key[selected]["classification"] == "proxy_failure"
    assert all(item["status"] == "available" for key, item in by_key.items() if key != selected)
    assert all(item["classification"] == "proxy_not_selected" for key, item in by_key.items() if key != selected)


def test_proxy_failure_matches_selected_proxy_with_protocol_prefix(tmp_path: Path) -> None:
    pool = ResourcePoolService(ResourcePoolRepository(tmp_path / "proxy-selected-scheme.db"))
    pool.import_lajiao_credentials("user-region-JP-sid-a-t-30:pass@proxy.local:2000")

    overrides, leases = pool.lease_for_task("task-proxy-selected-scheme", {
        "lajiao_proxy_mode": "credentials",
        "lajiao_proxy_regions": "JP",
    })
    selected = leases[0].resource_key
    reports = pool.report_for_task(
        "task-proxy-selected-scheme",
        "failed",
        {"resource_leases": overrides["resource_leases"]},
        log_text=f"  使用新代理: socks5://{selected} exit_ip=198.51.100.4\nCloudflare status=403",
    )

    assert reports[0]["status"] == "cooldown"
    assert reports[0]["classification"] == "proxy_failure"


def test_proxy_cloudflare_failure_cools_proxy_for_sticky_window(tmp_path: Path) -> None:
    pool = ResourcePoolService(ResourcePoolRepository(tmp_path / "proxy-cloudflare.db"))
    pool.import_lajiao_credentials("user-region-JP-sid-a-t-30:pass@proxy.local:2000")
    overrides, leases = pool.lease_for_task("task-proxy-cf", {"lajiao_proxy_mode": "credentials", "lajiao_proxy_regions": "JP"})

    reports = pool.report_for_task(
        "task-proxy-cf",
        "failed",
        {"resource_leases": overrides["resource_leases"]},
        log_text=f"使用新代理: {leases[0].resource_key} exit_ip=198.51.100.4\nserver responded with a status of 403",
    )

    assert reports[0]["status"] == "cooldown"
    assert reports[0]["classification"] == "proxy_failure"


def test_resource_import_accepts_frontend_alias_types(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient
    from main import app
    from api.resources import get_resource_pool_service

    svc = ResourcePoolService(ResourcePoolRepository(tmp_path / "resource-api.db"))
    app.dependency_overrides[get_resource_pool_service] = lambda: svc
    try:
        with TestClient(app) as client:
            bind_resp = client.post("/api/resources/import", json={"resource_type": "bind_phone", "provider": "bind_user_phone_url", "text": "15555550102----https://sms.example.invalid/messages/bind"})
            bad_bind_resp = client.post("/api/resources/import", json={"resource_type": "bind_phone", "provider": "bind_user_phone_url", "text": "15555550103 https://sms.example.invalid/messages/bind"})
            icloud_resp = client.post("/api/resources/import", json={"resource_type": "icloud_email", "provider": "icloud_api", "text": "mailbox@example.invalid----https://mail.example.invalid/inbox"})
    finally:
        app.dependency_overrides.pop(get_resource_pool_service, None)

    assert bind_resp.status_code == 200
    assert bad_bind_resp.status_code == 400
    assert "phone----sms_url" in bad_bind_resp.json()["message"]
    assert icloud_resp.status_code == 200
    assert svc.list_resources("phone", "bind_user_phone_url")[0]["resource_key"] == "15555550102"
    assert svc.list_resources("email", "icloud_api")[0]["resource_key"] == "mailbox@example.invalid"


def test_icloud_privacy_provider_imports_email_pool(tmp_path: Path) -> None:
    ctx = DummyContext(tmp_path)
    ctx.providers.save_provider("mailbox", "icloud_privacy", {
        "icloud_privacy_order_text": "alias-one@example.invalid\nalias-two@example.invalid",
        "mailbox_imap_user": "inbox@example.invalid",
        "mailbox_imap_pass": "imap-pass",
        "mailbox_imap_host": "imap.example.invalid",
    })

    config = ctx.config.db_config()
    items = ctx.resources.list_resources("email", "icloud_privacy")

    assert config["mailbox_provider"] == "icloud_privacy"
    assert config["icloud_privacy_order_file"].endswith("icloud_privacy_order.txt")
    assert len(items) == 2
    assert items[0]["provider"] == "icloud_privacy"

def test_icloud_privacy_lease_skips_jsonl_consumed_mailboxes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "icloud_privacy_pool_state.jsonl").write_text(
        json.dumps({"email": "used@example.invalid", "status": "consumed"}) + "\n"
        + json.dumps({"email": "cool@example.invalid", "status": "cooldown"}) + "\n",
        encoding="utf-8",
    )
    pool = ResourcePoolService(ResourcePoolRepository(tmp_path / "privacy-lease.db"))
    pool.import_icloud_privacy_mailboxes("used@example.invalid\ncool@example.invalid\nfresh@example.invalid")

    overrides, leases = pool.lease_for_task("task-privacy", {"mailbox_provider": "icloud_privacy"})

    assert overrides["icloud_privacy_order_text"] == "fresh@example.invalid"
    assert [lease.resource_key for lease in leases] == ["fresh@example.invalid"]
    assert pool.repo.get("email", "icloud_privacy", "used@example.invalid")["status"] == "used"
    assert pool.repo.get("email", "icloud_privacy", "cool@example.invalid")["status"] == "cooldown"



def test_lajiao_provider_save_accepts_masked_credentials_with_existing_pool(tmp_path: Path) -> None:
    ctx = DummyContext(tmp_path)
    ctx.resources.import_lajiao_credentials("user-region-JP-sid-a-t-20:pass@proxy.local:2000", provider="lajiao_credentials")
    provider = ctx.providers.save_provider("proxy", "lajiao_credentials", {
        "lajiao_proxy_credentials": "***",
        "lajiao_proxy_credential_protocol": "http",
        "lajiao_proxy_regions": "JP",
        "lajiao_proxy_timeout": "15",
    })

    config = ctx.config.db_config()
    assert provider["provider_name"] == "lajiao_credentials"
    assert config["lajiao_proxy_mode"] == "credentials"
    assert config["lajiao_proxy_regions"] == "JP"
    assert "lajiao_proxy_credentials" not in config



def test_lajiao_proxy_import_repairs_missing_colon_before_port(tmp_path: Path) -> None:
    pool = ResourcePoolService(ResourcePoolRepository(tmp_path / "test.db"))
    count = pool.import_lajiao_credentials("proxy-user:proxy-password@proxy.example.invalid2000")

    assert count == 1
    item = pool.list_resources("proxy", "lajiao_credentials", "available")[0]
    assert item["resource_key"].endswith("@proxy.example.invalid:2000")

def test_resource_report_bind_phone_invalid_marks_disabled(tmp_path: Path) -> None:
    pool = ResourcePoolService(ResourcePoolRepository(tmp_path / "test.db"))
    pool.import_phone_urls("15555550102|https://sms.example.invalid/messages/bind", provider="bind_user_phone_url")
    overrides, _leases = pool.lease_for_task("task-bind-invalid", {"bind_sms_provider": "bind_user_phone_url"})

    reports = pool.report_for_task(
        "task-bind-invalid",
        "failed",
        {"resource_leases": overrides["resource_leases"]},
        log_text="入力した電話番号は無効です。確認してもう一度お試しください。",
    )

    disabled = pool.list_resources("phone", "bind_user_phone_url", "disabled")
    assert len(disabled) == 1
    assert disabled[0]["resource_key"] == "15555550102"
    assert reports[0]["classification"] == "phone_invalid"


def test_resource_report_bind_phone_used_japanese_limit(tmp_path: Path) -> None:
    pool = ResourcePoolService(ResourcePoolRepository(tmp_path / "test.db"))
    pool.import_phone_urls("15555550102|https://sms.example.invalid/messages/bind", provider="bind_user_phone_url")
    overrides, _leases = pool.lease_for_task("task-bind-used", {"bind_sms_provider": "bind_user_phone_url"})

    reports = pool.report_for_task(
        "task-bind-used",
        "failed",
        {"resource_leases": overrides["resource_leases"]},
        log_text="この電話番号は、すでに上限数のアカウントに関連付けられています。",
    )

    used = pool.list_resources("phone", "bind_user_phone_url", "used")
    assert len(used) == 1
    assert used[0]["resource_key"] == "15555550102"
    assert reports[0]["classification"] == "phone_already_used"


def test_resource_report_bind_phone_recently_used_japanese_marks_used(tmp_path: Path) -> None:
    pool = ResourcePoolService(ResourcePoolRepository(tmp_path / "test.db"))
    pool.import_phone_urls("15555550102|https://sms.example.invalid/messages/bind", provider="bind_user_phone_url")
    overrides, _leases = pool.lease_for_task("task-bind-recent", {"bind_sms_provider": "bind_user_phone_url"})

    reports = pool.report_for_task(
        "task-bind-recent",
        "failed",
        {"resource_leases": overrides["resource_leases"]},
        log_text="短信验证码校验失败: この電話番号は最近使用されたため、しばらくしてからもう一度お試しください。",
    )

    used = pool.list_resources("phone", "bind_user_phone_url", "used")
    assert len(used) == 1
    assert used[0]["resource_key"] == "15555550102"
    assert reports[0]["classification"] == "phone_already_used"



def test_resource_report_bind_phone_unknown_failure_cools_resource(tmp_path: Path) -> None:
    pool = ResourcePoolService(ResourcePoolRepository(tmp_path / "test.db"))
    pool.import_phone_urls("15555550102|https://sms.example.invalid/messages/bind", provider="bind_user_phone_url")
    overrides, _leases = pool.lease_for_task("task-bind-network", {"bind_sms_provider": "bind_user_phone_url"})

    reports = pool.report_for_task(
        "task-bind-network",
        "failed",
        {"resource_leases": overrides["resource_leases"]},
        log_text="browser closed before sms send",
    )

    cooldown = pool.list_resources("phone", "bind_user_phone_url", "cooldown")
    assert len(cooldown) == 1
    assert reports[0]["status"] == "cooldown"

def test_provider_save_imports_outlook_token_file(tmp_path: Path) -> None:
    ctx = DummyContext(tmp_path)
    ctx.providers.save_provider("mailbox", "outlook_token", {
        "outlook_token_order_text": "mailbox@example.invalid----pass----client----refresh",
    })
    config = ctx.config.db_config()
    order_file = Path(config["outlook_token_order_file"])
    assert order_file.exists()
    emails = ctx.resources.list_resources("email", "outlook_token")
    assert len(emails) == 1
    lease_overrides, leases = ctx.resources.lease_for_task("task-oauth", {"mailbox_provider": "outlook_token"})
    assert lease_overrides["outlook_email"] == "mailbox@example.invalid"
    assert lease_overrides["outlook_client_id"] == "client"
    assert len(leases) == 1
    assert "mailbox@example.invalid----pass----client----refresh" in order_file.read_text(encoding="utf-8")

def test_provider_save_imports_icloud_api_to_email_pool(tmp_path: Path) -> None:
    ctx = DummyContext(tmp_path)
    ctx.providers.save_provider("mailbox", "icloud_api", {
        "icloud_api_order_text": "mailbox@example.invalid----https://mail.example.invalid/inbox/mailbox",
    })

    emails = ctx.resources.list_resources("email", "icloud_api")
    assert len(emails) == 1
    assert emails[0]["resource_key"] == "mailbox@example.invalid"
    categories = ctx.resources.category_options()
    icloud = next(item for item in categories if item["key"] == "email/icloud_api")
    assert icloud["total"] == 1
    assert icloud["available"] == 1



def test_resource_pool_lease_is_atomic_under_threads(tmp_path: Path) -> None:
    pool = ResourcePoolService(ResourcePoolRepository(tmp_path / "test.db"))
    pool.import_phone_urls("\n".join(f"155555501{i:02d}|https://sms.example.invalid/messages/{i}" for i in range(10)))

    def lease(i: int) -> str:
        overrides, _leases = pool.lease_for_task(f"task-{i}", {"sms_provider": "user_phone_url"})
        return str(overrides.get("sms_phone_url") or "").split("|", 1)[0]

    with ThreadPoolExecutor(max_workers=10) as executor:
        phones = list(executor.map(lease, range(10)))

    assert len([phone for phone in phones if phone]) == 10
    assert len(set(phones)) == 10
    leased = pool.list_resources("phone", "user_phone_url", "leased")
    assert len(leased) == 10


def test_resource_capacity_summary_counts_available_resources(tmp_path: Path) -> None:
    pool = ResourcePoolService(ResourcePoolRepository(tmp_path / "capacity.db"))
    pool.import_phone_urls("15555550200|https://sms.example.invalid/messages/0\n15555550201|https://sms.example.invalid/messages/1")
    pool.import_lajiao_credentials("proxy-user:proxy-password@proxy.example.invalid:1080")

    summary = pool.capacity_summary(need_phone=2, need_proxy=2)

    phone = next(item for item in summary["resources"] if item["resource_type"] == "phone")
    proxy = next(item for item in summary["resources"] if item["resource_type"] == "proxy")
    assert summary["ok"] is False
    assert phone["available"] == 2
    assert phone["enough"] is True
    assert proxy["available"] == 1
    assert proxy["enough"] is False


def test_resource_pool_accepts_timezone_aware_cooldowns(tmp_path: Path) -> None:
    pool = ResourcePoolService(ResourcePoolRepository(tmp_path / "aware-cooldown.db"))
    pool.import_lajiao_credentials("past-user:pass@proxy-a.local:1080\nfuture-user:pass@proxy-b.local:1080")
    items = pool.list_resources("proxy", "lajiao_credentials")
    past = next(item for item in items if item["resource_key"].startswith("past-user"))
    future = next(item for item in items if item["resource_key"].startswith("future-user"))
    pool.repo.set_status(
        int(past["id"]),
        status="cooldown",
        cooldown_until=(datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(),
        error="expired aware cooldown",
    )
    pool.repo.set_status(
        int(future["id"]),
        status="cooldown",
        cooldown_until=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        error="future aware cooldown",
    )

    categories = pool.category_options()

    proxy_category = next(item for item in categories if item["key"] == "proxy/lajiao_credentials")
    assert proxy_category["total"] == 2
    assert proxy_category["available"] == 1
    assert pool.repo.get("proxy", "lajiao_credentials", past["resource_key"])["status"] == "available"
    assert pool.repo.get("proxy", "lajiao_credentials", future["resource_key"])["status"] == "cooldown"


def test_resource_pool_reports_success_and_failure(tmp_path: Path) -> None:
    pool = ResourcePoolService(ResourcePoolRepository(tmp_path / "test.db"))
    pool.import_phone_urls("15555550210|https://sms.example.invalid/messages/0")
    overrides, _leases = pool.lease_for_task("task-success", {"sms_provider": "user_phone_url"})
    config = {"resource_leases": overrides["resource_leases"]}
    pool.report_for_task("task-success", "succeeded", config)
    used = pool.list_resources("phone", "user_phone_url", "used")
    assert len(used) == 1
    assert used[0]["success_count"] == 1

    pool.import_phone_urls("15555550211|https://sms.example.invalid/messages/1")
    overrides, _leases = pool.lease_for_task("task-failed", {"sms_provider": "user_phone_url"})
    config = {"resource_leases": overrides["resource_leases"]}
    pool.report_for_task("task-failed", "failed", config, error="sms timeout")
    cooldown = pool.list_resources("phone", "user_phone_url", "cooldown")
    assert len(cooldown) == 1
    assert cooldown[0]["fail_count"] == 1
    assert cooldown[0]["last_error"] == "sms timeout"



def test_resource_report_sms_timeout_does_not_penalize_proxy(tmp_path: Path) -> None:
    pool = ResourcePoolService(ResourcePoolRepository(tmp_path / "test.db"))
    pool.import_phone_urls("15555550212|https://sms.example.invalid/messages/2")
    pool.import_lajiao_credentials("proxy-user:proxy-password@proxy.example.invalid:1080")
    overrides, _leases = pool.lease_for_task(
        "task-sms-timeout",
        {"sms_provider": "user_phone_url", "lajiao_proxy_mode": "credentials"},
    )

    reports = pool.report_for_task("task-sms-timeout", "failed", {"resource_leases": overrides["resource_leases"]}, error="SMS timeout")

    phone = pool.list_resources("phone", "user_phone_url", "cooldown")
    proxy = pool.list_resources("proxy", "lajiao_credentials", "available")
    assert len(phone) == 1
    assert len(proxy) == 1
    assert phone[0]["fail_count"] == 1
    assert proxy[0]["fail_count"] == 0
    assert {item["type"]: item["classification"] for item in reports} == {"phone": "sms_timeout", "proxy": "sms_timeout"}


def test_resource_report_browser_start_failure_releases_email(tmp_path: Path) -> None:
    pool = ResourcePoolService(ResourcePoolRepository(tmp_path / "browser-start-failed.db"))
    pool.import_link_api_mailboxes("icloud@example.com----https://mail.local/show/token")
    overrides, _leases = pool.lease_for_task("task-browser-failed", {"mailbox_provider": "icloud_api"})

    reports = pool.report_for_task(
        "task-browser-failed",
        "failed",
        {"resource_leases": overrides["resource_leases"]},
        log_text="BrowserType.launch_persistent_context: Target page, context or browser has been closed",
    )

    available_keys = {item["resource_key"] for item in pool.list_resources("email", "icloud_api", "available")}
    assert "icloud@example.com" in available_keys
    assert reports[0]["classification"] == "browser_start_failure"
    assert reports[0]["status"] == "available"

def test_resource_report_proxy_failure_cools_only_proxy(tmp_path: Path) -> None:
    pool = ResourcePoolService(ResourcePoolRepository(tmp_path / "test.db"))
    pool.import_phone_urls("15555550213|https://sms.example.invalid/messages/3")
    pool.import_lajiao_credentials("proxy-user:proxy-password@proxy.example.invalid:1081")
    overrides, _leases = pool.lease_for_task(
        "task-proxy-failed",
        {"sms_provider": "user_phone_url", "lajiao_proxy_mode": "credentials"},
    )

    pool.report_for_task("task-proxy-failed", "failed", {"resource_leases": overrides["resource_leases"]}, log_text="代理连接失败")

    phone = pool.list_resources("phone", "user_phone_url", "available")
    proxy = pool.list_resources("proxy", "lajiao_credentials", "cooldown")
    assert len(phone) == 1
    assert len(proxy) == 1
    assert phone[0]["fail_count"] == 0
    assert proxy[0]["fail_count"] == 1


def test_resource_report_email_already_used_marks_email_used(tmp_path: Path) -> None:
    pool = ResourcePoolService(ResourcePoolRepository(tmp_path / "test.db"))
    pool.import_outlook_tokens("used@example.invalid----pass----client----refresh")
    overrides, _leases = pool.lease_for_task("task-email-used", {"mailbox_provider": "outlook_token"})

    reports = pool.report_for_task("task-email-used", "failed", {"resource_leases": overrides["resource_leases"]}, log_text="email already in use")

    used = pool.list_resources("email", "outlook_token", "used")
    assert len(used) == 1
    assert used[0]["resource_key"] == "used@example.invalid"
    assert used[0]["success_count"] == 1
    assert reports[0]["classification"] == "email_already_used"



def test_resource_report_registered_without_token_marks_email_used(tmp_path: Path) -> None:
    pool = ResourcePoolService(ResourcePoolRepository(tmp_path / "test.db"))
    pool.import_outlook_tokens("partial@example.invalid----pass----client----refresh")
    overrides, _leases = pool.lease_for_task("task-registered-no-token", {"mailbox_provider": "outlook_token"})

    reports = pool.report_for_task(
        "task-registered-no-token",
        "failed",
        {"resource_leases": overrides["resource_leases"]},
        log_text="[注册成功] 邮箱：partial@example.invalid 密码：pw\nChatGPT session 中缺少 accessToken",
    )

    used = pool.list_resources("email", "outlook_token", "used")
    assert len(used) == 1
    assert used[0]["resource_key"] == "partial@example.invalid"
    assert used[0]["success_count"] == 1
    assert reports[0]["classification"] == "registered_without_token"


def test_resource_report_registered_without_token_disables_icloud_privacy(tmp_path: Path) -> None:
    pool = ResourcePoolService(ResourcePoolRepository(tmp_path / "test.db"))
    pool.repo.upsert("email", "icloud_privacy", "partial@example.invalid", {}, status="available")
    overrides, _leases = pool.lease_for_task("task-icloud-registered-no-token", {"mailbox_provider": "icloud_privacy"})

    reports = pool.report_for_task(
        "task-icloud-registered-no-token",
        "failed",
        {"resource_leases": overrides["resource_leases"]},
        log_text="[✅️注册成功] 邮箱：partial@example.invalid 密码：pw\nChatGPT session 中缺少 accessToken",
    )

    disabled = pool.list_resources("email", "icloud_privacy", "disabled")
    assert len(disabled) == 1
    assert disabled[0]["resource_key"] == "partial@example.invalid"
    assert disabled[0]["success_count"] == 1
    assert reports[0]["classification"] == "registered_without_token"
    assert reports[0]["status"] == "disabled"

def test_resource_report_success_marks_consumed_and_proxy_cooldown(tmp_path: Path) -> None:
    pool = ResourcePoolService(ResourcePoolRepository(tmp_path / "test.db"))
    pool.import_phone_urls("15555550214|https://sms.example.invalid/messages/4")
    pool.import_outlook_tokens("ok@example.invalid----pass----client----refresh")
    pool.import_lajiao_credentials("proxy-user:proxy-password@proxy.example.invalid:1082")
    overrides, _leases = pool.lease_for_task(
        "task-all-success",
        {"sms_provider": "user_phone_url", "mailbox_provider": "outlook_token", "lajiao_proxy_mode": "credentials"},
    )
    selected = next(item["key"] for item in overrides["resource_leases"] if item["type"] == "proxy")

    reports = pool.report_for_task("task-all-success", "succeeded", {"resource_leases": overrides["resource_leases"]}, log_text=f"使用新代理: {selected} exit_ip=198.51.100.4")

    assert len(pool.list_resources("phone", "user_phone_url", "used")) == 1
    assert len(pool.list_resources("email", "outlook_token", "used")) == 1
    proxy = pool.list_resources("proxy", "lajiao_credentials", "cooldown")
    assert len(proxy) == 1
    assert proxy[0]["success_count"] == 1
    assert proxy[0]["payload"]["exit_ip"] == "198.51.100.4"
    assert {item["type"]: item["status"] for item in reports} == {"phone": "used", "email": "used", "proxy": "cooldown"}



def test_success_cools_only_selected_proxy_from_batch(tmp_path: Path) -> None:
    pool = ResourcePoolService(ResourcePoolRepository(tmp_path / "success-selected-proxy.db"))
    pool.import_lajiao_credentials("\n".join(f"user-region-JP-sid-{i}-t-30:pass@proxy.local:2000" for i in range(3)))

    selected = "user-region-JP-sid-1-t-30:pass@proxy.local:2000"
    resource_leases = [
        {"type": "proxy", "provider": "lajiao_credentials", "key": f"user-region-JP-sid-{i}-t-30:pass@proxy.local:2000"}
        for i in range(3)
    ]

    reports = pool.report_for_task(
        "task-success-selected",
        "succeeded",
        {"resource_leases": resource_leases},
        log_text=f"使用新代理: {selected} exit_ip=198.51.100.4",
    )

    by_key = {item["key"]: item for item in reports if item["type"] == "proxy"}
    assert by_key[selected]["status"] == "cooldown"
    assert by_key[selected]["classification"] == "success"
    assert all(item["status"] == "available" for key, item in by_key.items() if key != selected)
    assert all(item["classification"] == "proxy_not_selected" for key, item in by_key.items() if key != selected)
    assert len(pool.list_resources("proxy", "lajiao_credentials", "cooldown")) == 1
    assert len(pool.list_resources("proxy", "lajiao_credentials", "available")) == 2

def test_interrupted_registration_cools_proxy(tmp_path: Path) -> None:
    pool = ResourcePoolService(ResourcePoolRepository(tmp_path / "interrupted-proxy.db"))
    pool.import_lajiao_credentials("user-region-JP-sid-a-t-30:pass@proxy.local:2000")
    overrides, leases = pool.lease_for_task("task-interrupted", {"lajiao_proxy_mode": "credentials", "lajiao_proxy_regions": "JP"})

    reports = pool.report_for_task("task-interrupted", "interrupted", {"resource_leases": overrides["resource_leases"]}, error="process disappeared")

    assert reports[0]["status"] == "cooldown"
    assert reports[0]["classification"] == "interrupted"
    cooldown = pool.list_resources("proxy", "lajiao_credentials", "cooldown")
    assert len(cooldown) == 1
    assert cooldown[0]["resource_key"] == leases[0].resource_key


def test_success_preserves_precheck_cooled_unselected_proxy(tmp_path: Path) -> None:
    pool = ResourcePoolService(ResourcePoolRepository(tmp_path / "precheck-cooled-proxy.db"))
    pool.import_lajiao_credentials("bad-region-JP-sid-a-t-30:pass@proxy.local:2000\ngood-region-JP-sid-b-t-30:pass@proxy.local:2000")
    bad = pool.repo.lease("proxy", "lajiao_credentials", "task-precheck")
    good = pool.repo.lease("proxy", "lajiao_credentials", "task-precheck")
    pool.repo.set_status(bad.id, status="cooldown", cooldown_until=pool.cooldown_until(1800), error="proxy country mismatch")
    leases = [
        {"type": "proxy", "provider": "lajiao_credentials", "key": bad.resource_key},
        {"type": "proxy", "provider": "lajiao_credentials", "key": good.resource_key},
    ]

    reports = pool.report_for_task("task-precheck", "succeeded", {"resource_leases": leases}, log_text=f"使用新代理: {good.resource_key} exit_ip=203.0.113.5")

    by_key = {item["key"]: item for item in reports if item["type"] == "proxy"}
    assert by_key[bad.resource_key]["status"] == "cooldown"
    assert by_key[bad.resource_key]["classification"] == "proxy_not_selected_cooldown_preserved"
    assert by_key[good.resource_key]["status"] == "cooldown"
    assert pool.repo.get("proxy", "lajiao_credentials", bad.resource_key)["status"] == "cooldown"


def test_selected_proxy_appended_when_dynamic_lease_not_in_config(tmp_path: Path) -> None:
    pool = ResourcePoolService(ResourcePoolRepository(tmp_path / "dynamic-selected-proxy.db"))
    pool.import_lajiao_credentials("initial-region-JP-sid-a-t-30:pass@proxy.local:2000\ndynamic-region-JP-sid-b-t-30:pass@proxy.local:2000")
    initial = pool.repo.lease("proxy", "lajiao_credentials", "task-dynamic-selected")
    dynamic = pool.repo.lease("proxy", "lajiao_credentials", "task-dynamic-selected")
    pool.repo.set_status(initial.id, status="cooldown", cooldown_until=pool.cooldown_until(1800), error="proxy country mismatch")

    reports = pool.report_for_task(
        "task-dynamic-selected",
        "succeeded",
        {"resource_leases": [{"type": "proxy", "provider": "lajiao_credentials", "key": initial.resource_key}]},
        log_text=f"使用新代理: {dynamic.resource_key} exit_ip=203.0.113.6",
    )

    by_key = {item["key"]: item for item in reports if item["type"] == "proxy"}
    assert by_key[initial.resource_key]["status"] == "cooldown"
    assert by_key[dynamic.resource_key]["status"] == "cooldown"
    assert by_key[dynamic.resource_key]["classification"] == "success"


def test_proxy_exit_ip_cooldown_blocks_siblings(tmp_path: Path) -> None:
    pool = ResourcePoolService(ResourcePoolRepository(tmp_path / "proxy-exit-ip.db"))
    pool.repo.upsert("proxy", "lajiao_credentials", "socks5://user:pass@proxy-a.local:2000", {"url": "socks5://user:pass@proxy-a.local:2000", "region": "JP", "exit_ip": "198.51.100.4"})
    pool.repo.upsert("proxy", "lajiao_credentials", "socks5://user:pass@proxy-b.local:2000", {"url": "socks5://user:pass@proxy-b.local:2000", "region": "JP", "exit_ip": "198.51.100.4"})

    overrides, leases = pool.lease_for_task("task-exit-ip", {"lajiao_proxy_mode": "credentials", "lajiao_proxy_regions": "JP", "lajiao_proxy_task_batch_size": 1})
    selected = leases[0].resource_key

    reports = pool.report_for_task("task-exit-ip", "succeeded", {"resource_leases": overrides["resource_leases"]}, log_text=f"使用新代理: {selected} exit_ip=198.51.100.4")

    assert reports[0]["status"] == "cooldown"
    assert len(pool.list_resources("proxy", "lajiao_credentials", "cooldown")) == 2
    next_overrides, next_leases = pool.lease_for_task("task-next", {"lajiao_proxy_mode": "credentials", "lajiao_proxy_regions": "JP", "lajiao_proxy_task_batch_size": 1})
    assert next_overrides == {}
    assert next_leases == []

def test_resource_pool_manual_status_changes(tmp_path: Path) -> None:
    pool = ResourcePoolService(ResourcePoolRepository(tmp_path / "test.db"))
    pool.import_phone_urls("15555550220|https://sms.example.invalid/messages/0")
    resource = pool.list_resources("phone", "user_phone_url")[0]
    pool.set_status(resource["id"], "disabled", error="manual disable")
    disabled = pool.list_resources("phone", "user_phone_url", "disabled")
    assert len(disabled) == 1
    assert disabled[0]["last_error"] == "manual disable"
    pool.set_status(resource["id"], "available")
    available = pool.list_resources("phone", "user_phone_url", "available")
    assert len(available) == 1


def test_resource_pool_bulk_status_by_ids_and_filter(tmp_path: Path) -> None:
    pool = ResourcePoolService(ResourcePoolRepository(tmp_path / "test.db"))
    pool.import_phone_urls("\n".join(f"1555555023{i}|https://sms.example.invalid/messages/{i}" for i in range(3)))
    resources = pool.list_resources("phone", "user_phone_url")

    count = pool.set_status_bulk(resource_ids=[resources[0]["id"], resources[1]["id"]], status="disabled", error="bulk disable")
    assert count == 2
    disabled = pool.list_resources("phone", "user_phone_url", "disabled")
    assert len(disabled) == 2
    assert all(item["last_error"] == "bulk disable" for item in disabled)

    count = pool.set_status_bulk(resource_type="phone", provider="user_phone_url", current_status="disabled", status="available")
    assert count == 2
    assert len(pool.list_resources("phone", "user_phone_url", "available")) == 3


def test_resource_pool_proxy_health_check_validates_format_only(tmp_path: Path) -> None:
    pool = ResourcePoolService(ResourcePoolRepository(tmp_path / "test.db"))
    result = pool.check_proxy_health("proxy-user:proxy-password@proxy.example.invalid:1080\nbad-proxy")

    assert result["checked"] == 2
    assert result["valid"] == 1
    assert result["external"] is False
    assert result["items"][0]["message"] == "格式有效，未连接外部代理"
    assert result["items"][1]["ok"] is False


def test_resource_api_import_and_bulk_status(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient
    from main import app
    from api.resources import get_resource_pool_service

    svc = ResourcePoolService(ResourcePoolRepository(tmp_path / "api-resources.db"))
    app.dependency_overrides[get_resource_pool_service] = lambda: svc
    try:
        with TestClient(app) as client:
            import_resp = client.post("/api/resources/import", json={
                "resource_type": "phone",
                "provider": "user_phone_url",
                "text": "15555550230|https://sms.example.invalid/messages/0\n15555550231|https://sms.example.invalid/messages/1",
            })
            assert import_resp.status_code == 200
            assert import_resp.json()["count"] == 2

            list_resp = client.get("/api/resources", params={"resource_type": "phone", "provider": "user_phone_url"})
            items = list_resp.json()["items"]
            assert len(items) == 2

            bulk_resp = client.post("/api/resources/status/bulk", json={
                "status": "disabled",
                "resource_ids": [items[0]["id"]],
                "error": "api bulk",
            })
            assert bulk_resp.status_code == 200
            assert bulk_resp.json()["count"] == 1

            disabled_resp = client.get("/api/resources", params={"status": "disabled"})
            disabled = disabled_resp.json()["items"]
            assert len(disabled) == 1
            assert disabled[0]["last_error"] == "api bulk"

            proxy_resp = client.post("/api/resources/proxy/health-check", json={"text": "proxy-user:proxy-password@proxy.example.invalid:1080\nbad"})
            assert proxy_resp.status_code == 200
            proxy_data = proxy_resp.json()
            assert proxy_data["valid"] == 1
            assert proxy_data["external"] is False
    finally:
        app.dependency_overrides.pop(get_resource_pool_service, None)


def test_account_export_selects_fields_with_chinese_descriptions(tmp_path: Path) -> None:
    svc = AccountsService(AccountsRepository(tmp_path / "accounts.db"))
    svc.repo.upsert({
        "account_key": "acct-1",
        "account_id": "auth0|acct-1",
        "email": "user@example.com",
        "password": "secret",
        "phone_number": "+15550000001",
        "stage": "complete",
        "status": "complete",
        "plan_type": "plus",
        "activation_id": "internal-only",
    })
    db.upsert_resource(
        "phone",
        "bind_user_phone_url",
        "15550000001",
        {"phone": "15550000001", "sms_url": "https://sms.example/get/abc"},
        status="used",
        path=tmp_path / "accounts.db",
    )
    with db.connect(tmp_path / "accounts.db") as conn:
        resource_id = int(conn.execute("SELECT id FROM resource_pool WHERE resource_key='15550000001'").fetchone()["id"])
    with db.connect(tmp_path / "accounts.db") as conn:
        conn.execute(
            "UPDATE accounts SET binding_phone_number=?, binding_phone_resource_id=? WHERE account_key=?",
            ("+15550000001", resource_id, "acct-1"),
        )


    fields = svc.available_export_fields()
    email_field = next(item for item in fields if item["key"] == "email")
    assert email_field["label"] == "邮箱"
    assert "账号登录邮箱" in email_field["description"]

    exported = svc.export_product("acct-1", ["email", "plan_type", "activation_id"])
    assert exported["email"] == "user@example.com"
    assert exported["plan_type"] == "plus"
    assert "activation_id" not in exported
    sms_export = svc.export_product("acct-1", ["binding_sms_api_url", "sms_api_url"])
    assert sms_export["binding_sms_api_url"] == "https://sms.example/get/abc"
    assert sms_export["sms_api_url"] == "https://sms.example/get/abc"
    assert sms_export["_field_descriptions"]["binding_sms_api_url"]["label"] == "绑定接码 API"
    assert set(exported) == {"email", "plan_type", "_field_descriptions"}
    assert exported["_field_descriptions"]["email"]["label"] == "邮箱"
    assert "套餐" in exported["_field_descriptions"]["plan_type"]["label"]


def test_account_export_api_accepts_selected_fields(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient
    from main import app
    from api.deps import get_accounts_service

    svc = AccountsService(AccountsRepository(tmp_path / "api-accounts.db"))
    svc.repo.upsert({
        "account_key": "acct-api",
        "account_id": "auth0|acct-api",
        "email": "api@example.com",
        "password": "secret",
        "phone_number": "+15550000002",
        "stage": "complete",
        "status": "complete",
        "plan_type": "free",
    })

    app.dependency_overrides[get_accounts_service] = lambda: svc
    try:
        with TestClient(app) as client:
            fields_resp = client.get("/api/accounts/export-fields")
            assert fields_resp.status_code == 200
            fields = fields_resp.json()["fields"]
            assert any(item["key"] == "password" and item["label"] == "密码" for item in fields)

            export_resp = client.post("/api/accounts/acct-api/export", json={"fields": ["email", "phone_number"]})
            assert export_resp.status_code == 200
            product = export_resp.json()["product"]
            bulk_resp = client.post("/api/accounts-bulk/export", json={"keys": ["acct-api", "missing-account"], "fields": ["email"]})
            assert bulk_resp.status_code == 200
            bulk_data = bulk_resp.json()
    finally:
        app.dependency_overrides.pop(get_accounts_service, None)

    assert product["email"] == "api@example.com"
    assert product["phone_number"] == "+15550000002"
    assert "password" not in product
    assert product["_field_descriptions"]["phone_number"]["label"] == "手机号"
    assert "注册账号时使用" in product["_field_descriptions"]["phone_number"]["description"]
    assert bulk_data["count"] == 1
    assert bulk_data["exported_keys"] == ["acct-api"]
    assert bulk_data["missing"] == ["missing-account"]
    assert len(bulk_data["products"]) == bulk_data["count"]


def test_account_list_reports_filtered_total_and_truncation(monkeypatch) -> None:
    from fastapi.testclient import TestClient
    from main import app
    import api.accounts as accounts_api

    rows = [
        {"account_key": "list-complete-1", "email": "one@example.com", "stage": "complete", "status": "complete"},
        {"account_key": "list-complete-2", "email": "two@example.com", "stage": "complete", "status": "complete"},
        {"account_key": "list-pending", "email": "pending@example.com", "stage": "pending", "status": "pending"},
    ]
    monkeypatch.setattr(accounts_api.account_store, "list_accounts", lambda refresh_legacy=False: list(rows))

    with TestClient(app) as client:
        response = client.get("/api/accounts", params={"status": "complete", "limit": 1})

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    assert data["truncated"] is True
    assert len(data["items"]) == 1


def test_account_batch_actions_report_partial_failure_at_top_level(monkeypatch) -> None:
    from fastapi.testclient import TestClient
    from main import app
    import api.accounts as accounts_api
    from api.deps import get_accounts_service, get_browser_session_service, get_tasks_service

    class PartialCpaService:
        def sync_cpa_token(self, key):
            if key == "cpa-fail":
                return {"ok": False, "message": "CPA unavailable"}, 502
            return {"ok": True, "file": f"{key}.json"}, 200

    class PartialTasksService:
        def start_billing_email_bind(self, payload):
            if payload["resume_file"] == "error-resume.json":
                raise RuntimeError("mailbox unavailable")
            return {"id": f"task-{payload['resume_file']}"}

    class PartialBrowserService:
        def refresh_access_token_for_account(self, account):
            if account["account_key"] == "refresh-fail":
                return {"ok": False, "message": "refresh failed"}, 502
            return {"ok": True, "token_length": 12}, 200

    accounts = {
        "billing-ok": {"account_key": "billing-ok", "resume_file": "ok-resume.json"},
        "billing-error": {"account_key": "billing-error", "resume_file": "error-resume.json"},
        "refresh-ok": {"account_key": "refresh-ok"},
        "refresh-fail": {"account_key": "refresh-fail"},
    }
    monkeypatch.setattr(accounts_api.account_store, "get_account", lambda key: dict(accounts.get(key) or {}))
    app.dependency_overrides[get_accounts_service] = lambda: PartialCpaService()
    app.dependency_overrides[get_tasks_service] = lambda: PartialTasksService()
    app.dependency_overrides[get_browser_session_service] = lambda: PartialBrowserService()
    try:
        with TestClient(app) as client:
            cpa = client.post("/api/accounts-bulk/sync-cpa-token", json={"keys": ["cpa-ok", "cpa-fail"]})
            billing = client.post("/api/accounts-bulk/bind-billing-email", json={"keys": ["billing-ok", "billing-error", "missing-billing"]})
            refresh = client.post("/api/accounts-bulk/refresh-access-token", json={"keys": ["refresh-ok", "refresh-fail", "missing-refresh"]})
    finally:
        app.dependency_overrides.pop(get_accounts_service, None)
        app.dependency_overrides.pop(get_tasks_service, None)
        app.dependency_overrides.pop(get_browser_session_service, None)

    assert cpa.status_code == 200
    assert cpa.json()["ok"] is False
    assert len(cpa.json()["results"]) == 2
    assert billing.status_code == 200
    assert billing.json()["ok"] is False
    assert len(billing.json()["results"]) == 3
    assert refresh.status_code == 200
    assert refresh.json()["ok"] is False
    assert len(refresh.json()["results"]) == 3

def test_account_archive_batch_api_hides_selected_accounts(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient
    from main import app
    from api.deps import get_accounts_service

    svc = AccountsService(AccountsRepository(tmp_path / "archive-api.db"))
    for key in ("acct-delete-1", "acct-delete-2"):
        svc.repo.upsert({"account_key": key, "email": f"{key}@example.com", "stage": "complete", "status": "complete"})

    app.dependency_overrides[get_accounts_service] = lambda: svc
    try:
        with TestClient(app) as client:
            resp = client.post("/api/accounts-bulk/archive", json={"keys": ["acct-delete-1", "acct-delete-2"]})
            assert resp.status_code == 200
            data = resp.json()
    finally:
        app.dependency_overrides.pop(get_accounts_service, None)

    assert data["archived"] == 2
    assert set(data["keys"]) == {"acct-delete-1", "acct-delete-2"}
    assert svc.repo.get("acct-delete-1").status == "archived"
    assert svc.repo.get("acct-delete-2").status == "archived"


def test_verify_plus_updates_account_from_existing_checker(tmp_path: Path, monkeypatch) -> None:
    import types

    calls = []
    fake_payment = types.ModuleType("platforms.chatgpt.payment")

    def fake_fetch(account, proxy=None):
        calls.append((account.access_token, account.chatgpt_account_id, proxy))
        return {"status": "plus", "source": "backend-api/wham/usage"}

    fake_payment.fetch_subscription_status_details = fake_fetch
    monkeypatch.setitem(sys.modules, "platforms.chatgpt.payment", fake_payment)

    svc = AccountsService(AccountsRepository(tmp_path / "plus.db"), ConfigService(ConfigRepository(tmp_path / "plus.db")))
    svc.repo.upsert({
        "account_key": "acct-plus",
        "account_id": "auth0|acct-plus",
        "email": "plus@example.com",
        "stage": "manual_plus_required",
        "status": "manual_plus_required",
        "plan_type": "free",
        "access_token": "access.plus.token",
    })

    svc._pool_proxy_candidates = lambda *, proxy_region="JP", limit=5: ["socks5://jp:pass@pool.local:3000"]  # type: ignore[method-assign]
    payload, status_code = svc.verify_plus("acct-plus")

    assert status_code == 200
    assert payload["paid"] is True
    assert payload["plan_type"] == "plus"
    assert payload["account"]["stage"] == "plus_verified_needs_oauth"
    assert calls[0][0:2] == ("access.plus.token", "auth0|acct-plus")
    assert str(calls[0][2]).startswith("http://127.0.0.1:")



def test_verify_plus_retries_next_jp_proxy_after_timeout(tmp_path: Path, monkeypatch) -> None:
    import types
    import application.accounts_service as accounts_service_module

    calls: list[str] = []
    cleaned: list[str] = []
    fake_payment = types.ModuleType("platforms.chatgpt.payment")

    class FakeBridgeRuntime:
        def __init__(self, *_args, **_kwargs):
            self.proxy = ""

        def start_browser_bridge(self, proxy: str) -> str:
            self.proxy = proxy
            return f"http://127.0.0.1:18080/{proxy}"

        def cleanup(self) -> None:
            cleaned.append(self.proxy)

    def fake_fetch(_account, proxy=None):
        calls.append(str(proxy))
        if "bad-jp" in str(proxy):
            raise TimeoutError("proxy timeout")
        return {"status": "plus", "source": "backend-api/wham/usage"}

    fake_payment.fetch_subscription_status_details = fake_fetch
    monkeypatch.setitem(sys.modules, "platforms.chatgpt.payment", fake_payment)
    monkeypatch.setattr(accounts_service_module, "CredentialProxyRuntime", FakeBridgeRuntime)

    svc = AccountsService(AccountsRepository(tmp_path / "plus-retry.db"), ConfigService(ConfigRepository(tmp_path / "plus-retry.db")))
    monkeypatch.setattr(svc, "_subscription_proxies", lambda _account, *, proxy_region="JP", limit=5: ["socks5://bad-jp", "socks5://good-jp"])
    svc.repo.upsert({
        "account_key": "acct-plus-retry",
        "account_id": "auth0|acct-plus-retry",
        "email": "retry@example.com",
        "stage": "manual_plus_required",
        "status": "manual_plus_required",
        "plan_type": "free",
        "access_token": "access.retry.token",
    })

    payload, status_code = svc.verify_plus("acct-plus-retry")

    assert status_code == 200
    assert payload["paid"] is True
    assert calls == ["http://127.0.0.1:18080/socks5://bad-jp", "http://127.0.0.1:18080/socks5://good-jp"]
    assert cleaned == ["socks5://bad-jp", "socks5://good-jp"]


def test_verify_plus_proxy_candidates_rotate_by_account(tmp_path: Path) -> None:
    svc = AccountsService(AccountsRepository(tmp_path / "plus-rotate.db"), ConfigService(ConfigRepository(tmp_path / "plus-rotate.db")))
    svc._pool_proxy_candidates = lambda *, proxy_region="JP", limit=5: ["proxy-a", "proxy-b", "proxy-c"]  # type: ignore[method-assign]

    first = svc._subscription_proxies({"account_key": "a@example.com"})
    second = svc._subscription_proxies({"account_key": "b@example.com"})

    assert sorted(first) == ["proxy-a", "proxy-b", "proxy-c"]
    assert sorted(second) == ["proxy-a", "proxy-b", "proxy-c"]
    assert first[0] != second[0]

def test_account_store_preserves_top_level_registration_proxy() -> None:
    from core.account_store import normalize_record

    record = normalize_record({
        "account_key": "proxy@example.com",
        "email": "proxy@example.com",
        "registration_proxy": "socks5://user:pass@proxy.local:3000",
        "registration_proxy_exit_ip": "203.0.113.10",
    })

    assert record["proxy"]["registration_proxy"] == "socks5://user:pass@proxy.local:3000"
    assert record["proxy"]["registration_exit_ip"] == "203.0.113.10"


def test_verify_plus_batch_uses_live_pool_proxy_not_resume_registration_proxy(tmp_path: Path, monkeypatch) -> None:
    import types
    import application.accounts_service as accounts_service_module

    calls = []
    fake_payment = types.ModuleType("platforms.chatgpt.payment")

    def fake_fetch(account, proxy=None):
        calls.append(proxy)
        if not proxy:
            raise RuntimeError("direct blocked")
        return {"status": "plus", "source": "backend-api/wham/usage"}

    fake_payment.fetch_subscription_status_details = fake_fetch
    monkeypatch.setitem(sys.modules, "platforms.chatgpt.payment", fake_payment)
    monkeypatch.setattr(accounts_service_module, "PROJECT_ROOT", tmp_path)
    resume = tmp_path / "resume.json"
    resume.write_text(json.dumps({"registration_proxy": "socks5://user:pass@proxy.local:3000"}), encoding="utf-8")

    svc = AccountsService(AccountsRepository(tmp_path / "plus-proxy.db"), ConfigService(ConfigRepository(tmp_path / "plus-proxy.db")))
    monkeypatch.setattr(svc, "_pool_proxy_candidates", lambda *, proxy_region="JP", limit=5: ["socks5://live:pass@pool.local:3000"])
    svc.repo.upsert({
        "account_key": "acct-proxy",
        "email": "proxy@example.com",
        "stage": "manual_plus_confirmed",
        "status": "manual_plus_confirmed",
        "plan_type": "plus",
        "access_token": "access.proxy.token",
        "resume_file": str(resume),
    })

    data = svc.verify_plus_batch(["acct-proxy"])

    assert data["ok"] is True
    assert data["paid"] == 1
    assert len(calls) == 1
    assert str(calls[0]).startswith("http://127.0.0.1:")


def test_verify_plus_pool_candidates_filter_to_requested_region(tmp_path: Path) -> None:
    svc = AccountsService(AccountsRepository(tmp_path / "plus-region.db"), ConfigService(ConfigRepository(tmp_path / "plus-region.db")))
    from infrastructure import db as db_module

    db_module.upsert_resource("proxy", "lajiao_credentials", "user-region-US-sid-a-t-10:pass@proxy.local:2000", {"url": "socks5://user-region-US-sid-a-t-10:pass@proxy.local:2000", "region": "US"}, path=svc.repo.db_path)
    db_module.upsert_resource("proxy", "lajiao_credentials", "user-region-JP-sid-b-t-10:pass@proxy.local:2000", {"url": "socks5://user-region-JP-sid-b-t-10:pass@proxy.local:2000", "region": "JP"}, path=svc.repo.db_path)
    db_module.upsert_resource("proxy", "lajiao_credentials", "user-region-VN-sid-c-t-10:pass@proxy.local:2000", {"url": "socks5://user-region-VN-sid-c-t-10:pass@proxy.local:2000", "region": "VN"}, path=svc.repo.db_path)

    assert svc._subscription_proxies({}) == ["socks5://user-region-JP-sid-b-t-10:pass@proxy.local:2000"]
    assert svc._subscription_proxies({}, proxy_region="VN") == ["socks5://user-region-VN-sid-c-t-10:pass@proxy.local:2000"]


def test_payment_subscription_check_uses_direct_usage_only(monkeypatch) -> None:
    from types import SimpleNamespace
    import platforms.chatgpt.payment as payment

    monkeypatch.setattr(payment, "_fetch_usage_data", lambda account, proxy=None: {"plan_type": "plus"})

    def fail_request(*_args, **_kwargs):
        raise AssertionError("backend-api/me should not be called")

    monkeypatch.setattr(payment, "_request", fail_request)

    details = payment.fetch_subscription_status_details(SimpleNamespace(access_token="token", chatgpt_account_id=""), proxy="socks5://jp:pass@proxy.local:3000")

    assert details["status"] == "plus"
    assert details["source"] == "backend-api/wham/usage"
    assert details["me"] is None


def test_verify_plus_batch_api(tmp_path: Path, monkeypatch) -> None:
    import types
    from fastapi.testclient import TestClient
    from main import app
    from api.deps import get_accounts_service

    fake_payment = types.ModuleType("platforms.chatgpt.payment")
    fake_payment.fetch_subscription_status_details = lambda account, proxy=None: {"status": "free", "source": "backend-api/wham/usage"}
    monkeypatch.setitem(sys.modules, "platforms.chatgpt.payment", fake_payment)

    svc = AccountsService(AccountsRepository(tmp_path / "plus-api.db"), ConfigService(ConfigRepository(tmp_path / "plus-api.db")))
    svc.repo.upsert({
        "account_key": "acct-free",
        "email": "free@example.com",
        "stage": "manual_plus_confirmed",
        "status": "manual_plus_confirmed",
        "plan_type": "plus",
        "access_token": "access.free.token",
    })
    svc._pool_proxy_candidates = lambda *, proxy_region="JP", limit=5: ["socks5://jp:pass@pool.local:3000"]  # type: ignore[method-assign]

    app.dependency_overrides[get_accounts_service] = lambda: svc
    try:
        with TestClient(app) as client:
            resp = client.post("/api/accounts/verify-plus", json={"keys": ["acct-free"]})
            assert resp.status_code == 200
            data = resp.json()
    finally:
        app.dependency_overrides.pop(get_accounts_service, None)

    assert data["checked"] == 1
    assert data["paid"] == 0
    result = data["results"][0]
    assert result["plan_type"] == "free"
    assert result["account"]["stage"] == "manual_plus_required"


def test_verify_plus_batch_api_runs_checker_off_event_loop(monkeypatch) -> None:
    from fastapi.testclient import TestClient
    from main import app
    import api.accounts as accounts_api
    from api.deps import get_accounts_service

    calls: list[tuple[str, tuple]] = []

    class BlockingPlusService:
        def verify_plus_batch(self, keys, *, proxy_region="JP"):
            raise AssertionError("route must call through run_in_threadpool")

    async def fake_run_in_threadpool(func, *args, **kwargs):
        calls.append((getattr(func, "__name__", ""), args, kwargs))
        return {"ok": True, "checked": len(args[0]), "paid": 0, "results": []}

    monkeypatch.setattr(accounts_api, "run_in_threadpool", fake_run_in_threadpool)
    app.dependency_overrides[get_accounts_service] = lambda: BlockingPlusService()
    try:
        with TestClient(app) as client:
            resp = client.post("/api/accounts-bulk/verify-plus", json={"keys": ["acct-1"], "proxy_region": "VN"})
            assert resp.status_code == 200
            data = resp.json()
    finally:
        app.dependency_overrides.pop(get_accounts_service, None)

    assert data["ok"] is True
    assert calls == [("verify_plus_batch", (["acct-1"],), {"proxy_region": "VN"})]


def test_verify_plus_batch_honors_two_proxy_attempt_cap(tmp_path: Path, monkeypatch) -> None:
    import types
    import application.accounts_service as accounts_service_module

    calls: list[str] = []
    fake_payment = types.ModuleType("platforms.chatgpt.payment")

    class FakeBridgeRuntime:
        def __init__(self, *_args, **_kwargs):
            self.proxy = ""

        def start_browser_bridge(self, proxy: str) -> str:
            self.proxy = proxy
            return f"http://127.0.0.1:18080/{proxy}"

        def cleanup(self) -> None:
            pass

    def fake_fetch(_account, proxy=None):
        calls.append(str(proxy))
        raise TimeoutError("proxy timeout")

    fake_payment.fetch_subscription_status_details = fake_fetch
    monkeypatch.setitem(sys.modules, "platforms.chatgpt.payment", fake_payment)
    monkeypatch.setattr(accounts_service_module, "CredentialProxyRuntime", FakeBridgeRuntime)

    svc = AccountsService(AccountsRepository(tmp_path / "plus-cap.db"), ConfigService(ConfigRepository(tmp_path / "plus-cap.db")))
    svc.repo.upsert({"account_key": "acct-cap", "email": "cap@example.com", "access_token": "access.cap.token"})
    for idx in range(4):
        url = f"socks5://proxy-{idx}.local:3000"
        db.upsert_resource("proxy", "lajiao_credentials", url, {"url": url, "region": "JP"}, path=svc.repo.db_path)

    data = svc.verify_plus_batch(["acct-cap"])
    resources = db.list_resources(resource_type="proxy", provider="lajiao_credentials", path=svc.repo.db_path)

    assert data["checked"] == 1
    assert data["results"][0]["status_code"] == 502
    assert len(calls) == 2
    assert sum(1 for item in resources if item["status"] == "cooldown") == 2


def test_verify_plus_single_keeps_five_attempt_default(tmp_path: Path, monkeypatch) -> None:
    import types
    import application.accounts_service as accounts_service_module

    calls: list[str] = []
    fake_payment = types.ModuleType("platforms.chatgpt.payment")

    class FakeBridgeRuntime:
        def __init__(self, *_args, **_kwargs):
            pass

        def start_browser_bridge(self, proxy: str) -> str:
            return f"http://127.0.0.1:18080/{proxy}"

        def cleanup(self) -> None:
            pass

    def fake_fetch(_account, proxy=None):
        calls.append(str(proxy))
        raise TimeoutError("proxy timeout")

    fake_payment.fetch_subscription_status_details = fake_fetch
    monkeypatch.setitem(sys.modules, "platforms.chatgpt.payment", fake_payment)
    monkeypatch.setattr(accounts_service_module, "CredentialProxyRuntime", FakeBridgeRuntime)

    svc = AccountsService(AccountsRepository(tmp_path / "plus-five.db"), ConfigService(ConfigRepository(tmp_path / "plus-five.db")))
    svc.repo.upsert({"account_key": "acct-five", "email": "five@example.com", "access_token": "access.five.token"})
    for idx in range(6):
        url = f"socks5://single-{idx}.local:3000"
        db.upsert_resource("proxy", "lajiao_credentials", url, {"url": url, "region": "JP"}, path=svc.repo.db_path)

    payload, status_code = svc.verify_plus("acct-five")
    resources = db.list_resources(resource_type="proxy", provider="lajiao_credentials", path=svc.repo.db_path)

    assert status_code == 502
    assert payload["error_code"] == "proxy_failed"
    assert len(calls) == 5
    assert sum(1 for item in resources if item["status"] == "cooldown") == 5
    assert sum(1 for item in resources if item["status"] == "available") == 1


def test_verify_plus_auth_errors_are_terminal_and_classified(tmp_path: Path, monkeypatch) -> None:
    import types
    import urllib.error
    import application.accounts_service as accounts_service_module

    calls: list[str] = []
    fake_payment = types.ModuleType("platforms.chatgpt.payment")

    class FakeBridgeRuntime:
        def __init__(self, *_args, **_kwargs):
            pass

        def start_browser_bridge(self, proxy: str) -> str:
            return f"http://127.0.0.1:18080/{proxy}"

        def cleanup(self) -> None:
            pass

    def fake_fetch(_account, proxy=None):
        calls.append(str(proxy))
        raise urllib.error.HTTPError("https://chatgpt.com/backend-api/wham/usage", 401, "Unauthorized", {}, None)

    fake_payment.fetch_subscription_status_details = fake_fetch
    monkeypatch.setitem(sys.modules, "platforms.chatgpt.payment", fake_payment)
    monkeypatch.setattr(accounts_service_module, "CredentialProxyRuntime", FakeBridgeRuntime)

    svc = AccountsService(AccountsRepository(tmp_path / "plus-auth.db"), ConfigService(ConfigRepository(tmp_path / "plus-auth.db")))
    svc.repo.upsert({"account_key": "acct-auth", "email": "auth@example.com", "access_token": "access.auth.token"})
    for idx in range(2):
        url = f"socks5://auth-{idx}.local:3000"
        db.upsert_resource("proxy", "lajiao_credentials", url, {"url": url, "region": "JP"}, path=svc.repo.db_path)

    payload, status_code = svc.verify_plus("acct-auth")
    saved = svc.repo.get("acct-auth").to_dict()

    assert status_code == 401
    assert payload["error_code"] == "auth_failed"
    assert payload["message"] == "Plus 校验失败: access_token 无效或无权限"
    assert saved["plus_status"] == "banned"
    assert saved["plan_type"] == "banned"
    assert saved["plus_check_error"] == "Plus 校验失败: access_token 无效或无权限"
    assert len(calls) == 1


def test_verify_plus_batch_deduplicates_canonical_keys(tmp_path: Path, monkeypatch) -> None:
    svc = AccountsService(AccountsRepository(tmp_path / "plus-dedupe.db"), ConfigService(ConfigRepository(tmp_path / "plus-dedupe.db")))
    seen: list[tuple[str, int | None]] = []

    def fake_verify(key: str, *, proxy_region: str = "JP", max_attempts_override: int | None = None, retry_interval_override: int | None = None):
        seen.append((key, max_attempts_override))
        return {"ok": True, "paid": key == "acct-a", "account": {"key": key}}, 200

    monkeypatch.setattr(svc, "verify_plus", fake_verify)

    data = svc.verify_plus_batch([" acct-a ", "acct-a", "acct-b", "acct-a"])

    assert data["checked"] == 2
    assert [item["key"] for item in data["results"]] == ["acct-a", "acct-b"]
    assert seen == [("acct-a", 2), ("acct-b", 2)]


def test_verify_plus_returns_409_when_account_is_already_validating(tmp_path: Path) -> None:
    import threading

    svc = AccountsService(AccountsRepository(tmp_path / "plus-busy.db"), ConfigService(ConfigRepository(tmp_path / "plus-busy.db")))
    key = "acct-busy"
    svc.repo.upsert({"account_key": key, "email": "busy@example.com", "access_token": "access.busy.token"})
    entered = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with account_operation_lock(key):
            entered.set()
            release.wait(timeout=5)

    thread = threading.Thread(target=hold_lock)
    thread.start()
    try:
        assert entered.wait(timeout=2)
        payload, status_code = svc.verify_plus(key)
    finally:
        release.set()
        thread.join(timeout=2)

    assert status_code == 409
    assert payload["error_code"] == "account_busy"
    assert payload["message"] == "账号正在校验，请稍后重试"


def test_verify_plus_async_api_starts_statuses_and_cancels(monkeypatch) -> None:
    import threading
    import time
    from fastapi.testclient import TestClient
    from main import app
    from api.deps import get_accounts_service

    started = threading.Event()
    release = threading.Event()

    class SlowPlusService:
        def verify_plus(self, key, *, proxy_region="JP", max_attempts_override=None, retry_interval_override=None):
            started.set()
            release.wait(timeout=5)
            return {"ok": True, "paid": True, "plan_type": "plus", "source": "fake", "account": {"key": key}}, 200

    app.dependency_overrides[get_accounts_service] = lambda: SlowPlusService()
    try:
        with TestClient(app) as client:
            start = client.post("/api/accounts-bulk/verify-plus", json={"keys": ["acct-async"], "proxy_region": "JP", "async_mode": True})
            assert start.status_code == 202
            data = start.json()
            assert data["running"] is True
            assert data["total"] == 1
            assert started.wait(timeout=2)

            status = client.get(f"/api/accounts-bulk/verify-plus/{data['task_id']}")
            assert status.status_code == 200
            assert status.json()["running"] is True

            cancelled = client.post(f"/api/accounts-bulk/verify-plus/{data['task_id']}/cancel")
            assert cancelled.status_code == 200
            assert cancelled.json()["cancelled"] is True
            release.set()
            deadline = time.monotonic() + 2
            final = cancelled.json()
            while time.monotonic() < deadline:
                final = client.get(f"/api/accounts-bulk/verify-plus/{data['task_id']}").json()
                if not final["running"]:
                    break
                time.sleep(0.05)
    finally:
        release.set()
        app.dependency_overrides.pop(get_accounts_service, None)

    assert final["running"] is False
    assert final["completed"] == 1


def test_open_browser_api_runs_launch_off_event_loop(monkeypatch) -> None:
    from fastapi.testclient import TestClient
    from main import app
    import api.accounts as accounts_api
    from api.deps import get_browser_session_service

    calls: list[tuple[str, tuple, dict]] = []

    class BlockingBrowserService:
        def open_for_account(self, *_args, **_kwargs):
            raise AssertionError("route must call through run_in_threadpool")

    async def fake_run_in_threadpool(func, *args, **kwargs):
        calls.append((getattr(func, "__name__", ""), args, kwargs))
        return {"ok": True, "session": {"id": "session-1"}}, 202

    monkeypatch.setattr(accounts_api, "run_in_threadpool", fake_run_in_threadpool)
    monkeypatch.setattr(accounts_api.account_store, "get_account", lambda key: {"account_key": key, "email": "user@example.com"})
    app.dependency_overrides[get_browser_session_service] = lambda: BlockingBrowserService()
    try:
        with TestClient(app) as client:
            resp = client.post("/api/accounts/acct-1/open-browser", json={"target_url": "https://chatgpt.com/", "browser_engine": "camoufox"})
            assert resp.status_code == 200
            data = resp.json()
    finally:
        app.dependency_overrides.pop(get_browser_session_service, None)

    assert data["session"]["id"] == "session-1"
    assert calls[0][0] == "open_for_account"
    assert calls[0][1][0]["account_key"] == "acct-1"
    assert calls[0][2]["target_url"] == "https://chatgpt.com/"


def test_browser_save_close_api_run_commands_off_event_loop(monkeypatch) -> None:
    from fastapi.testclient import TestClient
    from main import app
    import api.accounts as accounts_api
    from api.deps import get_browser_session_service

    calls: list[tuple[str, tuple, dict]] = []

    class BlockingBrowserService:
        def save_session(self, *_args, **_kwargs):
            raise AssertionError("save must call through run_in_threadpool")
        def close_session(self, *_args, **_kwargs):
            raise AssertionError("close must call through run_in_threadpool")

    async def fake_run_in_threadpool(func, *args, **kwargs):
        calls.append((getattr(func, "__name__", ""), args, kwargs))
        return {"ok": True, "session": {"id": args[0]}}, 200

    monkeypatch.setattr(accounts_api, "run_in_threadpool", fake_run_in_threadpool)
    app.dependency_overrides[get_browser_session_service] = lambda: BlockingBrowserService()
    try:
        with TestClient(app) as client:
            save_resp = client.post("/api/account-browser-sessions/session-1/save")
            close_resp = client.post("/api/account-browser-sessions/session-1/close", json={"save": True})
            assert save_resp.status_code == 200
            assert close_resp.status_code == 200
    finally:
        app.dependency_overrides.pop(get_browser_session_service, None)

    assert calls == [
        ("save_session", ("session-1",), {}),
        ("close_session", ("session-1",), {"save": True}),
    ]



def test_resume_oauth_prefers_verified_subscription_proxy(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    from full_pipeline import RegisterPipeline

    pipeline = RegisterPipeline({"proxy": "socks5://old-config", "rotate_proxy_each_attempt": False, "output_dir": str(tmp_path / "output")})
    pipeline.result["registration_proxy"] = "socks5://expired-registration"
    pipeline.result["subscription_check_proxy"] = "socks5://fresh-subscription"
    checked = []

    def fake_check(proxy: str):
        checked.append(proxy)
        return True, "203.0.113.20"

    monkeypatch.setattr(pipeline, "_check_lajiao_proxy", fake_check)


    pipeline._select_resume_oauth_proxy_for_session()

    assert pipeline.config["proxy"] == "socks5://fresh-subscription"
    assert checked == ["socks5://fresh-subscription"]


def test_registration_proxy_precheck_rejects_bad_configured_proxy(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    from full_pipeline import RegisterPipeline

    pipeline = RegisterPipeline({"proxy": "socks5://bad-configured", "rotate_proxy_each_attempt": False, "output_dir": str(tmp_path / "output")})
    monkeypatch.setattr(pipeline, "_check_lajiao_proxy", lambda proxy: (False, ""))

    try:
        pipeline._select_fresh_proxy_for_attempt()
    except RuntimeError as exc:
        assert "代理 OpenAI 探针失败" in str(exc)
    else:
        raise AssertionError("bad configured proxy must fail before browser launch")


def test_resume_oauth_skips_bad_verified_proxy_and_selects_fresh(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    from full_pipeline import RegisterPipeline

    pipeline = RegisterPipeline({"proxy": "socks5://old-config", "rotate_proxy_each_attempt": True, "output_dir": str(tmp_path / "output")})
    pipeline.result["registration_proxy"] = "socks5://expired-registration"
    pipeline.result["subscription_check_proxy"] = "socks5://bad-subscription"
    checked = []

    def fake_check(proxy: str):
        checked.append(proxy)
        return False, ""

    monkeypatch.setattr(pipeline, "_check_lajiao_proxy", fake_check)
    monkeypatch.setattr(pipeline, "_select_fresh_proxy_for_attempt", lambda: pipeline.config.__setitem__("proxy", "socks5://fresh-selected") or "socks5://fresh-selected")

    pipeline._select_resume_oauth_proxy_for_session()

    assert pipeline.config["proxy"] == "socks5://fresh-selected"
    assert checked == ["socks5://bad-subscription", "socks5://expired-registration"]


def test_resume_oauth_retries_fresh_proxy_on_cloudflare_challenge(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    import full_pipeline as module
    import registration.patch_resume_bind as patch_module
    import platforms.chatgpt.browser_register as browser_register
    from full_pipeline import RegisterPipeline
    from registration.patch_resume_bind import ResumeOAuthProxyChallenge

    storage = tmp_path / "storage.json"
    storage.write_text('{"cookies":[]}', encoding="utf-8")
    resume = tmp_path / "resume.json"
    resume.write_text('{"email":"user@example.com"}', encoding="utf-8")

    class FakeSession:
        def __init__(self, config):
            self.config = dict(config)
            self.browser = object()
            self.browser_context = object()
            self.page = object()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    calls = []

    def fake_run_patch_resume_bind(*args, **kwargs):
        calls.append(kwargs.get("proxy"))
        if len(calls) == 1:
            raise ResumeOAuthProxyChallenge("resume-oauth 触发 OpenAI/Cloudflare 验证")
        return {"cpa_submitted": True}

    pipeline = RegisterPipeline({"rotate_proxy_each_attempt": True, "resume_oauth_proxy_attempts": 2, "oauth_callback_mode": "cpa", "output_dir": str(tmp_path / "output")})
    pipeline.result.update({
        "email": "user@example.com",
        "password": "secret",
        "browser_storage_state_path": str(storage),
        "resume_file": str(resume),
    })
    monkeypatch.setattr(module, "BrowserSession", FakeSession)
    monkeypatch.setattr(patch_module, "run_patch_resume_bind", fake_run_patch_resume_bind)
    monkeypatch.setattr(browser_register, "_get_cookies", lambda page: {})
    monkeypatch.setattr(pipeline, "_select_resume_oauth_proxy_for_session", lambda: pipeline.config.__setitem__("proxy", "socks5://first-proxy"))
    monkeypatch.setattr(pipeline, "_select_fresh_proxy_for_attempt", lambda: pipeline.config.__setitem__("proxy", "socks5://second-proxy") or "socks5://second-proxy")
    monkeypatch.setattr(pipeline, "_request_cpa_codex_auth_url", lambda: "https://auth.openai.com/oauth/authorize?state=s")

    result = pipeline.step_oauth_from_saved_session(headed=False)

    assert result["cpa_submitted"] is True
    assert calls == ["socks5://first-proxy", "socks5://second-proxy"]


def test_patch_resume_bind_classifies_cloudflare_challenge(monkeypatch) -> None:
    import platforms.chatgpt.browser_register as browser_register
    from platforms.chatgpt.oauth import OAuthStart
    from registration.patch_resume_bind import PatchResumeBindEngine, ResumeOAuthProxyChallenge

    monkeypatch.setattr(browser_register, "_do_codex_oauth", lambda *args, **kwargs: None)

    class Body:
        def inner_text(self, timeout: int = 0) -> str:
            return "なぜ検証に時間がかかるのでしょうか? Cloudflare"

    class FakePage:
        url = "https://auth.openai.com/oauth/authorize?state=s"

        def locator(self, selector: str):
            return Body()

    class FakeSession:
        page = FakePage()

    engine = PatchResumeBindEngine(FakeSession(), log_fn=lambda _msg: None)
    monkeypatch.setattr(engine, "_get_cookies", lambda: {})
    try:
        engine._run_page_fallback(
            oauth_start=OAuthStart(auth_url="https://auth.openai.com/oauth/authorize?state=s", state="s", code_verifier="", redirect_uri="https://callback", client_id="client"),
            login_identity="user@example.com",
            password="secret",
            bind_email=None,
            otp_callback=None,
            phone_callback=None,
            proxy="socks5://proxy",
        )
    except ResumeOAuthProxyChallenge as exc:
        assert "Cloudflare" in str(exc) or "cloudflare" in str(exc)
    else:
        raise AssertionError("Cloudflare challenge must raise ResumeOAuthProxyChallenge")



def test_patch_resume_bind_uses_captured_failed_localhost_callback(monkeypatch) -> None:
    import platforms.chatgpt.browser_register as browser_register
    from platforms.chatgpt.oauth import OAuthStart
    from registration.patch_resume_bind import PatchResumeBindEngine

    captured = "http://localhost:1455/auth/callback?code=abc123&state=s"
    monkeypatch.setattr(browser_register, "_do_codex_oauth", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        browser_register,
        "_submit_callback_result",
        lambda callback_url, oauth_start, proxy, callback_handler=None: {"callback_url": callback_url, "cpa_submitted": True},
    )

    class FakePage:
        url = "chrome-error://chromewebdata/"
        _omp_last_oauth_callback_url = captured

        def locator(self, selector: str):
            class Body:
                def inner_text(self, timeout: int = 0) -> str:
                    return ""
            return Body()

    class FakeSession:
        page = FakePage()

    engine = PatchResumeBindEngine(FakeSession(), log_fn=lambda _msg: None)
    engine._get_cookies = lambda: {}
    result = engine._run_page_fallback(
        oauth_start=OAuthStart(auth_url="https://auth.openai.com/oauth/authorize?state=s", state="s", code_verifier="", redirect_uri="http://localhost:1455/auth/callback", client_id="client"),
        login_identity="user@example.com",
        password="secret",
        bind_email=None,
        otp_callback=None,
        phone_callback=None,
        proxy="socks5://proxy",
        callback_handler=lambda callback_url, _oauth_start, _proxy: {"callback_url": callback_url, "cpa_submitted": True},
    )

    assert result["cpa_submitted"] is True
    assert result["callback_url"] == captured
    assert result["flow_path"] == "captured_callback"


def test_patch_resume_bind_submits_captured_error_callback_to_cpa(monkeypatch) -> None:
    import platforms.chatgpt.browser_register as browser_register
    from platforms.chatgpt.oauth import OAuthStart
    from registration.patch_resume_bind import PatchResumeBindEngine

    captured = "http://localhost:1455/auth/callback?error=access_denied&error_description=The+consent+verifier+has+already+been+used.&state=s"
    monkeypatch.setattr(browser_register, "_do_codex_oauth", lambda *args, **kwargs: None)

    class FakePage:
        url = "chrome-error://chromewebdata/"
        _omp_last_oauth_callback_url = captured

        def locator(self, selector: str):
            class Body:
                def inner_text(self, timeout: int = 0) -> str:
                    return ""
            return Body()

    class FakeSession:
        page = FakePage()

    engine = PatchResumeBindEngine(FakeSession(), log_fn=lambda _msg: None)
    engine._get_cookies = lambda: {}
    result = engine._run_page_fallback(
        oauth_start=OAuthStart(auth_url="https://auth.openai.com/oauth/authorize?state=s", state="s", code_verifier="", redirect_uri="http://localhost:1455/auth/callback", client_id="client"),
        login_identity="user@example.com",
        password="secret",
        bind_email=None,
        otp_callback=None,
        phone_callback=None,
        proxy="socks5://proxy",
        callback_handler=lambda callback_url, _oauth_start, _proxy: {"callback_url": callback_url, "cpa_submitted": True},
    )

    assert result["cpa_submitted"] is True
    assert result["callback_url"] == captured
    assert result["flow_path"] == "captured_callback"

def test_patch_resume_bind_api_transport_falls_back_without_burning_phone() -> None:
    from registration.patch_resume_bind import PatchResumeBindEngine, _PatchResumeBindPageFallback

    class FakePage:
        url = "https://auth.openai.com/add-phone"

    class FakeSession:
        page = FakePage()

    class FakePhoneCallback:
        def __init__(self):
            self.send_failed = False

        def __call__(self):
            return "+15551234567"

        def mark_send_failed(self, reason: str = ""):
            self.send_failed = True

    engine = PatchResumeBindEngine(FakeSession(), log_fn=lambda _msg: None)
    engine._send_phone_otp = lambda phone_number, state: {"status": 0, "text": "Failed to fetch"}
    callback = FakePhoneCallback()

    try:
        engine._handle_add_phone_json(callback, {"current_url": "https://auth.openai.com/add-phone"})
    except _PatchResumeBindPageFallback as exc:
        assert "transport_failed" in str(exc)
    else:
        raise AssertionError("transport failure should enter page fallback")

    assert callback.send_failed is False

def test_account_health_check_persists_active_plus(tmp_path: Path, monkeypatch) -> None:
    import types

    fake_payment = types.ModuleType("platforms.chatgpt.payment")
    fake_payment.fetch_subscription_status_details = lambda account, proxy=None: {"status": "plus", "source": "backend-api/wham/usage"}
    monkeypatch.setitem(sys.modules, "platforms.chatgpt.payment", fake_payment)

    from application.account_health_service import AccountHealthService

    svc = AccountHealthService(AccountsRepository(tmp_path / "health.db"), ConfigService(ConfigRepository(tmp_path / "health.db")))
    svc.repo.upsert({
        "account_key": "health-plus",
        "email": "health-plus@example.com",
        "access_token": "access.health.plus",
    })

    payload, status_code = svc.check_account("health-plus")

    assert status_code == 200
    assert payload["health_status"] == "active_plus"
    assert payload["plan_type"] == "plus"
    assert payload["account"]["account_health_status"] == "active_plus"
    assert payload["account"]["plus_status"] == "verified_plus"


def test_account_health_check_api_403_is_not_banned(tmp_path: Path, monkeypatch) -> None:
    import types

    fake_payment = types.ModuleType("platforms.chatgpt.payment")

    def fake_fetch(account, proxy=None):
        raise RuntimeError("HTTP Error 403: Forbidden")

    fake_payment.fetch_subscription_status_details = fake_fetch
    monkeypatch.setitem(sys.modules, "platforms.chatgpt.payment", fake_payment)

    from application.account_health_service import AccountHealthService

    svc = AccountHealthService(AccountsRepository(tmp_path / "health-403.db"), ConfigService(ConfigRepository(tmp_path / "health-403.db")))
    svc.repo.upsert({
        "account_key": "health-403",
        "email": "health-403@example.com",
        "access_token": "access.health.403",
    })

    payload, status_code = svc.check_account("health-403")

    assert status_code == 200
    assert payload["health_status"] == "api_forbidden"
    assert payload["account"]["account_health_status"] == "api_forbidden"
    assert payload["health_status"] not in {"account_suspended", "account_disabled", "account_deactivated"}


def test_account_health_prefers_direct_403_over_proxy_failure(tmp_path: Path, monkeypatch) -> None:
    import types

    calls = []
    fake_payment = types.ModuleType("platforms.chatgpt.payment")

    def fake_fetch(account, proxy=None):
        calls.append(proxy)
        if proxy:
            raise RuntimeError("SOCKS proxy connection timed out")
        raise RuntimeError("HTTP Error 403: Forbidden")

    fake_payment.fetch_subscription_status_details = fake_fetch
    monkeypatch.setitem(sys.modules, "platforms.chatgpt.payment", fake_payment)

    from application.account_health_service import AccountHealthService

    svc = AccountHealthService(AccountsRepository(tmp_path / "health-proxy-403.db"), ConfigService(ConfigRepository(tmp_path / "health-proxy-403.db")))
    svc.repo.upsert({
        "account_key": "health-proxy-403",
        "email": "health-proxy-403@example.com",
        "access_token": "access.health.proxy.403",
        "proxy": {"registration_proxy": "socks5://user:pass@proxy.local:3000"},
    })

    payload, status_code = svc.check_account("health-proxy-403")

    assert status_code == 200
    assert calls == ["socks5://user:pass@proxy.local:3000", None]
    assert payload["health_status"] == "api_forbidden"
    assert payload["account"]["account_health_status"] == "api_forbidden"


def test_account_health_browser_check_does_not_autosave_storage_state(tmp_path: Path, monkeypatch) -> None:
    import types
    import application.account_health_service as health_module
    from application.account_health_service import AccountHealthService

    captured = {}
    storage = tmp_path / "storage.json"
    storage.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")

    class FakePage:
        url = "https://chatgpt.com/"

        def goto(self, *args, **kwargs):
            return None

        def title(self):
            return "ChatGPT"

        def evaluate(self, script):
            return False if "querySelector" in str(script) else ""

    class FakeBrowserSession:
        def __init__(self, config):
            captured.update(config)
            self.page = FakePage()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return False

    monkeypatch.setattr(health_module, "BrowserSession", FakeBrowserSession)
    monkeypatch.setattr(health_module, "extract_chatgpt_access_token", lambda page, attempts, delay: types.SimpleNamespace(success=False, failure_reason="login required"))
    monkeypatch.setattr(health_module, "PROJECT_ROOT", tmp_path)

    svc = AccountHealthService(AccountsRepository(tmp_path / "health-storage.db"), ConfigService(ConfigRepository(tmp_path / "health-storage.db")))
    svc.repo.upsert({
        "account_key": "health-storage",
        "email": "health-storage@example.com",
        "storage_file": str(storage),
    })

    payload, status_code = svc.check_account("health-storage")

    assert status_code == 200
    assert payload["health_status"] == "login_required"
    assert captured["_browser_storage_state"] == str(storage.resolve())
    assert "browser_storage_state_path" not in captured


def test_account_health_batch_api(tmp_path: Path, monkeypatch) -> None:
    import types
    from fastapi.testclient import TestClient
    from main import app
    from api.deps import get_account_health_service
    from application.account_health_service import AccountHealthService

    fake_payment = types.ModuleType("platforms.chatgpt.payment")
    fake_payment.fetch_subscription_status_details = lambda account, proxy=None: {"status": "free", "source": "backend-api/me"}
    monkeypatch.setitem(sys.modules, "platforms.chatgpt.payment", fake_payment)

    svc = AccountHealthService(AccountsRepository(tmp_path / "health-api.db"), ConfigService(ConfigRepository(tmp_path / "health-api.db")))
    svc.repo.upsert({
        "account_key": "health-free",
        "email": "health-free@example.com",
        "access_token": "access.health.free",
    })

    app.dependency_overrides[get_account_health_service] = lambda: svc
    try:
        with TestClient(app) as client:
            resp = client.post("/api/accounts-bulk/check-health", json={"keys": ["health-free"]})
            assert resp.status_code == 200
            data = resp.json()
    finally:
        app.dependency_overrides.pop(get_account_health_service, None)

    assert data["checked"] == 1
    assert data["counts"] == {"active_free": 1}
    assert data["results"][0]["account"]["account_health_status"] == "active_free"


def test_account_health_retries_with_available_proxy_pool(tmp_path: Path, monkeypatch) -> None:
    import types
    from application.account_health_service import AccountHealthService
    from infrastructure.repositories.resource_pool_repository import ResourcePoolRepository

    calls = []

    def fake_fetch(account, proxy=None):
        calls.append(proxy)
        if proxy == "http://pool-proxy.local:2000":
            return {"status": "plus", "source": "backend-api/me"}
        raise TimeoutError("proxy connection timed out")

    fake_payment = types.ModuleType("platforms.chatgpt.payment")
    fake_payment.fetch_subscription_status_details = fake_fetch
    monkeypatch.setitem(sys.modules, "platforms.chatgpt.payment", fake_payment)

    db_path = tmp_path / "health-pool.db"
    resource_repo = ResourcePoolRepository(db_path)
    resource_repo.upsert("proxy", "lajiao_credentials", "http://pool-proxy.local:2000", {"url": "http://pool-proxy.local:2000", "region": "JP"})

    svc = AccountHealthService(AccountsRepository(db_path), ConfigService(ConfigRepository(db_path)), resource_repo)
    svc.repo.upsert({
        "account_key": "health-pool",
        "email": "health-pool@example.com",
        "access_token": "access.health.pool",
        "proxy": {"registration_proxy": "http://dead-proxy.local:2000"},
    })

    payload, status_code = svc.check_account("health-pool")

    assert status_code == 200
    assert calls == ["http://dead-proxy.local:2000", "http://pool-proxy.local:2000"]
    assert payload["health_status"] == "active_plus"
    assert payload["account"]["account_health_status"] == "active_plus"


def test_account_health_batch_uses_eight_worker_pool(tmp_path: Path, monkeypatch) -> None:
    import application.account_health_service as health_module
    from application.account_health_service import AccountHealthService

    max_workers_seen = []
    submitted_keys = []

    class FakeFuture:
        def __init__(self, value):
            self._value = value

        def result(self):
            return self._value

    class FakeExecutor:
        def __init__(self, max_workers):
            max_workers_seen.append(max_workers)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return False

        def submit(self, fn, key):
            submitted_keys.append(key)
            return FakeFuture(fn(key))

    monkeypatch.setattr(health_module, "ThreadPoolExecutor", FakeExecutor)
    monkeypatch.setattr(health_module, "as_completed", lambda futures: list(reversed(list(futures))))

    svc = AccountHealthService(AccountsRepository(tmp_path / "health-workers.db"), ConfigService(ConfigRepository(tmp_path / "health-workers.db")))
    monkeypatch.setattr(svc, "check_account", lambda key: ({"ok": True, "health_status": "active_free"}, 200))

    keys = [f"health-{index}" for index in range(10)]
    result = svc.check_batch(keys)

    assert max_workers_seen == [8]
    assert submitted_keys == keys
    assert result["checked"] == 10
    assert result["counts"] == {"active_free": 10}
    assert [item["key"] for item in result["results"]] == keys


def test_account_lifecycle_fields_and_events_persist(tmp_path: Path) -> None:
    db_path = tmp_path / "lifecycle.db"
    account = {
        "account_key": "life@example.com",
        "email": "life@example.com",
        "password": "secret",
        "registration_mode": "email",
        "registration_status": "registered",
        "plus_status": "verified_plus",
        "binding_status": "pending",
        "resume_file": "output/resume.json",
    }

    account_id = db.upsert_account(account, path=db_path)
    db.add_account_event("life@example.com", "registration_succeeded", task_id="task-life", status="registered", path=db_path)
    rows = db.list_accounts(path=db_path)
    events = db.list_account_events("life@example.com", path=db_path)

    assert account_id > 0
    assert rows[0]["login_identifier"] == "life@example.com"
    assert rows[0]["registration_status"] == "registered"
    assert rows[0]["plus_status"] == "verified_plus"
    assert rows[0]["binding_status"] == "pending"
    assert rows[0]["resume_file"] == "output/resume.json"
    assert events[0]["event_type"] == "registration_succeeded"


def test_account_upsert_does_not_reencode_existing_sms_ignored_codes(tmp_path: Path) -> None:
    db_path = tmp_path / "sms-normalize.db"
    account = {
        "account_key": "sms@example.com",
        "email": "sms@example.com",
        "activation_id": "act-1",
        "sms": {"ignored_codes": ["111111", "222222"]},
    }
    db.upsert_account(account, path=db_path)
    stored = db.get_account("sms@example.com", path=db_path)
    db.upsert_account(stored, path=db_path)

    with db.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT ignored_codes FROM sms_activations WHERE account_id_ref=? ORDER BY id",
            (stored["id"],),
        ).fetchall()
    assert [json.loads(row["ignored_codes"]) for row in rows] == [["111111", "222222"], ["111111", "222222"]]


def test_resume_oauth_sets_binding_lifecycle(tmp_path: Path) -> None:
    class DummyTasks:
        def start_resume(self, payload):
            assert payload["resume_file"] == "output/resume.json"
            return {"id": "task-bind-life"}

    svc = AccountsService(AccountsRepository(tmp_path / "binding-life.db"), ConfigService(ConfigRepository(tmp_path / "binding-life.db")))
    svc.repo.upsert({
        "account_key": "bind@example.com",
        "email": "bind@example.com",
        "stage": "plus_verified_needs_oauth",
        "status": "plus_verified_needs_oauth",
        "plus_status": "verified_plus",
        "binding_status": "pending",
        "paths": {"resume": "output/resume.json"},
    })

    payload, status_code = svc.resume_oauth("bind@example.com", True, DummyTasks(), oauth_callback_mode="cpa", cpa_base_url="https://cpa.local", bind_sms_provider="bind_user_phone_url")
    saved = svc.repo.get("bind@example.com").to_dict()

    assert status_code == 200
    assert payload["task"]["id"] == "task-bind-life"
    assert saved["binding_status"] == "binding_queued"
    assert saved["binding_task_id"] == "task-bind-life"
    assert saved["binding_provider"] == "bind_user_phone_url"



def test_protocol_local_bind_persists_lifecycle_before_dispatch(tmp_path: Path, monkeypatch) -> None:
    queued_payloads: list[dict[str, object]] = []
    drain_states: list[dict[str, object]] = []
    events: list[tuple[str, str, dict[str, object]]] = []

    svc = AccountsService(AccountsRepository(tmp_path / "protocol-local-life.db"), ConfigService(ConfigRepository(tmp_path / "protocol-local-life.db")))
    svc.repo.upsert({
        "account_key": "protocol-local@example.com",
        "email": "protocol-local@example.com",
        "password": "account-password",
        "binding_status": "pending",
    })

    class DummyTasks:
        def start_protocol_cpa_bind(self, payload, *, defer_start=False):
            assert defer_start is True
            queued_payloads.append(payload)
            return {"id": "task-protocol-local"}

        def drain_queue_async(self):
            drain_states.append(svc.repo.get("protocol-local@example.com").to_dict())

    monkeypatch.setattr(
        db,
        "add_account_event",
        lambda account_key, event_type, **kwargs: events.append((account_key, event_type, kwargs)),
    )

    payload, status_code = svc.protocol_cpa_bind(
        "protocol-local@example.com",
        DummyTasks(),
        oauth_callback_mode="local",
        cpa_base_url="https://must-not-forward.invalid",
        cpa_management_key="must-not-forward",
        bind_sms_provider="smsbower_api",
        bind_sms_country="BR",
        bind_sms_service="dr",
    )

    assert status_code == 200
    assert payload["task"]["id"] == "task-protocol-local"
    assert queued_payloads == [{
        "account_key": "protocol-local@example.com",
        "oauth_callback_mode": "local",
        "cpa_base_url": "",
        "cpa_management_key": "",
        "sms_provider": "",
        "sms_phone_url": "",
        "sms_country": "",
        "sms_service": "",
        "bind_sms_provider": "smsbower_api",
        "bind_sms_phone_url": "",
        "bind_sms_country": "BR",
        "bind_sms_service": "dr",
        "bind_country_code": "",
    }]
    assert drain_states[0]["binding_status"] == "binding_queued"
    assert drain_states[0]["binding_task_id"] == "task-protocol-local"
    assert drain_states[0]["oauth_callback_mode"] == "local"
    assert events[0][0:2] == ("protocol-local@example.com", "binding_queued")
    assert events[0][2]["payload"]["oauth_callback_mode"] == "local"

def test_account_operation_lock_serializes_same_key() -> None:
    events: list[str] = []

    def run(name: str) -> None:
        with account_operation_lock("same-account"):
            events.append(f"{name}-start")
            time.sleep(0.01)
            events.append(f"{name}-end")

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(run, ["a", "b"]))

    assert events in (["a-start", "a-end", "b-start", "b-end"], ["b-start", "b-end", "a-start", "a-end"])


def test_browser_session_requires_project_storage_and_camoufox(tmp_path: Path) -> None:
    from application.browser_session_service import BrowserSessionService

    outside = tmp_path / "storage_state.json"
    outside.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
    svc = BrowserSessionService(max_sessions=1)
    payload, status_code = svc.open_for_account({"account_key": "safe@example.com", "storage_file": str(outside)}, engine="playwright")

    assert status_code == 400
    assert "项目目录" in payload["message"]


def test_browser_session_rejects_non_camoufox_engine() -> None:
    from application.browser_session_service import BrowserSessionService, PROJECT_ROOT

    storage = PROJECT_ROOT / "tmp" / "test_browser_storage_state.json"
    storage.parent.mkdir(parents=True, exist_ok=True)
    storage.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
    svc = BrowserSessionService(max_sessions=1)
    payload, status_code = svc.open_for_account({"account_key": "safe@example.com", "storage_file": str(storage)}, engine="playwright")

    assert status_code == 400
    assert "Camoufox" in payload["message"]

if __name__ == "__main__":
    root = Path("tmp/test_fullstack_api")
    root.mkdir(parents=True, exist_ok=True)
    test_register_overrides_use_brazil_price_and_credentials()
    test_config_api_masks_sensitive_values(root)
    test_provider_defaults_and_dry_tests(root)
    test_provider_save_imports_user_phone_and_proxy(root)
    test_provider_save_imports_outlook_token_file(root)
    test_resource_pool_lease_is_atomic_under_threads(root)
    test_resource_pool_reports_success_and_failure(root)
    test_resource_pool_manual_status_changes(root)
    test_account_export_selects_fields_with_chinese_descriptions(root)
    test_account_export_api_accepts_selected_fields(root)
    test_account_operation_lock_serializes_same_key()
    print("fullstack api tests passed")
