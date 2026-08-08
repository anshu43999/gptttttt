from __future__ import annotations

from pathlib import Path
from typing import Any

from core.base_sms import HeroSmsProvider, SmsBowerProvider, UserProvidedSmsProvider
from domain.providers import ProviderDefinition, ProviderFieldDefinition, ProviderSetting
from core.proxy.credential_runtime import CredentialProxyRuntime
from infrastructure.repositories.providers_repository import ProvidersRepository
from application.config_service import ConfigService, is_sensitive_key, mask_value
from application.resource_pool_service import ResourcePoolService


PROVIDER_DEFINITIONS = [
    ProviderDefinition(
        "sms",
        "herosms_api",
        "HeroSMS 接码",
        "通过 HeroSMS API 获取 ChatGPT 接码手机号。",
        (
            ProviderFieldDefinition("sms_api_key", "HeroSMS API Key", secret=True, required=True),
            ProviderFieldDefinition("sms_service", "服务代码", "ChatGPT 通常使用 dr。", "dr"),
            ProviderFieldDefinition("sms_country", "国家代码", "巴西为 73。", "73"),
            ProviderFieldDefinition("country_code", "手机号国家码", placeholder="55"),
            ProviderFieldDefinition("country_name", "国家名称", placeholder="Brazil"),
            ProviderFieldDefinition("herosms_max_price", "最高单价美元", "系统会强制限制在 0.1 美刀以下。", "0.0999"),
        ),
    ),
    ProviderDefinition(
        "sms",
        "smsbower_api",
        "SMSBower 接码",
        "使用 SMSBower 的 OpenAI (ChatGPT) 激活 API；可固定巴西 Gold 号源与价格。",
        (
            ProviderFieldDefinition("smsbower_api_key", "SMSBower API Key", secret=True, required=True),
            ProviderFieldDefinition("sms_service", "服务代码", "OpenAI (ChatGPT) 为 dr。", "dr"),
            ProviderFieldDefinition("sms_country", "国家代码", "巴西为 73。", "73"),
            ProviderFieldDefinition("country_code", "手机号国家码", placeholder="55"),
            ProviderFieldDefinition("country_name", "国家名称", placeholder="Brazil"),
            ProviderFieldDefinition("smsbower_provider_ids", "号源 Provider ID", "Gold 巴西 0.054 美元号源为 3160。", "3160"),
            ProviderFieldDefinition("smsbower_min_price", "最低单价美元", "与最高单价一致可锁定指定价格。", "0.054"),
            ProviderFieldDefinition("smsbower_max_price", "最高单价美元", "SMSBower getNumberV2 的 maxPrice。", "0.054"),
        ),
    ),
    ProviderDefinition(
        "sms",
        "user_phone_url",
        "自备手机号 API",
        "从数据库资源池按任务租用自备手机号和取码 URL。",
        (
            ProviderFieldDefinition("sms_phone_urls_text", "批量手机号 API", "每行一个：手机号|取码URL。保存后注册会按顺序取用。", "15555550100|https://sms.example.invalid/messages/placeholder", multiline=True, required=True),
        ),
    ),
    ProviderDefinition(
        "sms",
        "bind_user_phone_url",
        "绑定手机号 API",
        "仅用于 Plus 后 CPA/OAuth 绑定阶段的手机号池；不会被注册任务使用。",
        (
            ProviderFieldDefinition("bind_sms_phone_urls_text", "批量绑定手机号 API", "每行一个：手机号|取码URL。例：15555550101|https://sms.example.invalid/...；国家码单独在设置里填 +1。", "15555550101|https://sms.example.invalid/messages/placeholder", multiline=True, required=True),
        ),
    ),
    ProviderDefinition(
        "proxy",
        "lajiao_credentials",
        "账密代理池",
        "导入账号密码代理到数据库代理资源池，支持 Kookeey、SOCKS5、HTTP CONNECT 等上游。",
        (
            ProviderFieldDefinition("lajiao_proxy_credentials_text", "批量账密代理", "每行一个 user:pass@host:port；可显式写 socks5:// 或 http://；浏览器统一走本地 bridge。", "proxy-user:proxy-password@proxy.example.invalid:1080", multiline=True, required=True),
            ProviderFieldDefinition("lajiao_proxy_credential_protocol", "默认代理协议", "未显式写 scheme 时使用；auto 会优先按本地 HTTP → 上游 SOCKS5 认证桥尝试，再回退 HTTP CONNECT。", placeholder="auto"),
            ProviderFieldDefinition("lajiao_proxy_regions", "出口地区", placeholder="JP,IN,US"),
            ProviderFieldDefinition("lajiao_proxy_timeout", "检测超时秒数", placeholder="15"),
        ),
    ),
    ProviderDefinition(
        "proxy",
        "lajiao_api",
        "代理 API",
        "通过代理服务商提取 API 获取代理。",
        (
            ProviderFieldDefinition("lajiao_proxy_api_url", "代理 API URL", placeholder="https://proxy.example.invalid/endpoint", required=True),
            ProviderFieldDefinition("lajiao_proxy_regions", "出口地区", placeholder="JP,IN,US"),
            ProviderFieldDefinition("lajiao_proxy_timeout", "检测超时秒数", placeholder="15"),
        ),
    ),
    ProviderDefinition(
        "mailbox",
        "outlook_token",
        "Outlook Graph 令牌邮箱池",
        "导入 Outlook Graph refresh-token 卡密到数据库邮箱资源池；验证码经 Microsoft Graph 读取，不依赖 IMAP。",
        (
            ProviderFieldDefinition("outlook_token_order_text", "批量 Outlook Graph 令牌数据", "每行一个邮箱账号。保存后写入 data/imports/outlook_token_order.txt。", "email----password----client_id----refresh_token", multiline=True, required=True),
            ProviderFieldDefinition("outlook_token_order_file", "或使用已有文件路径", placeholder="data/imports/outlook_tokens.txt"),
        ),
    ),
    ProviderDefinition(
        "mailbox",
        "icloud_api",
        "iCloud API 邮箱池",
        "导入 email----收信URL，可选追加 ----code:验证码API ----mail:邮件API；有 code API 时优先直接取码。",
        (
            ProviderFieldDefinition("icloud_api_order_text", "批量 iCloud API 邮箱", "每行一个邮箱账号；支持 email----showURL----code:https://.../api/code/...----mail:https://.../api/mail/...。保存后写入 data/imports/icloud_api_order.txt。", "mailbox@example.invalid----https://mail.example.invalid/inbox/placeholder----code:https://mail.example.invalid/code/placeholder----mail:https://mail.example.invalid/messages/placeholder", multiline=True),
            ProviderFieldDefinition("icloud_api_order_file", "或使用已有文件路径", placeholder="data/imports/icloud_api.txt"),
        ),
    ),
    ProviderDefinition(
        "mailbox",
        "icloud_privacy",
        "iCloud 隐私邮箱池（旧）",
        "旧方案：导入 iCloud 隐私邮箱账号；验证码转发到 IMAP 收件箱读取。",
        (
            ProviderFieldDefinition("icloud_privacy_order_text", "批量 iCloud 隐私邮箱", "每行一个 iCloud 隐私邮箱账号。验证码会从配置的 IMAP 收件箱读取。", "alias@example.invalid", multiline=True),
            ProviderFieldDefinition("icloud_privacy_order_file", "或使用已有文件路径", placeholder="data/imports/icloud_privacy.txt"),
            ProviderFieldDefinition("mailbox_imap_user", "IMAP 收件箱账号", placeholder="mailbox@example.invalid", required=True),
            ProviderFieldDefinition("mailbox_imap_pass", "IMAP 授权码", secret=True, required=True),
            ProviderFieldDefinition("mailbox_imap_host", "IMAP 主机", placeholder="imap.example.invalid"),
            ProviderFieldDefinition("mailbox_imap_port", "IMAP 端口", placeholder="993"),
        ),
    ),
    ProviderDefinition(
        "mailbox",
        "forwarded_domain",
        "转发域名邮箱",
        "使用 Cloudflare Email Routing 转发到 IMAP 收件箱。",
        (
            ProviderFieldDefinition("mailbox_domain", "转发域名", placeholder="mail.example.invalid", required=True),
            ProviderFieldDefinition("mailbox_imap_user", "IMAP 收件箱账号", placeholder="mailbox@example.invalid", required=True),
            ProviderFieldDefinition("mailbox_imap_pass", "IMAP 授权码", secret=True, required=True),
            ProviderFieldDefinition("mailbox_imap_host", "IMAP 主机", placeholder="imap.example.invalid"),
            ProviderFieldDefinition("mailbox_imap_port", "IMAP 端口", placeholder="993"),
        ),
    ),
    ProviderDefinition(
        "mailbox",
        "cfworker_admin_api",
        "CFWorker / Cloud Mail",
        "通过 CFWorker 或 Cloud Mail 管理 API 创建邮箱。",
        (
            ProviderFieldDefinition("cfworker_api_url", "CFWorker / Cloud Mail API URL", placeholder="https://mail.example.invalid/api", required=True),
            ProviderFieldDefinition("cfworker_admin_token", "Admin/Open API Token", secret=True, required=True),
            ProviderFieldDefinition("cfworker_domain", "邮箱域名", placeholder="mail.example.invalid"),
        ),
    ),
]

PROVIDER_DEFINITION_BY_KEY = {(item.provider_type, item.provider_name): item for item in PROVIDER_DEFINITIONS}

DEFAULT_PROVIDERS = [
    ProviderSetting("sms", "herosms_api", True, {"service": "dr", "countries": ["BR", "US"], "max_price": 0.0999}),
    ProviderSetting("sms", "smsbower_api", True, {"service": "dr", "countries": ["BR"], "provider_ids": "3160", "min_price": 0.054, "max_price": 0.054}),
    ProviderSetting("sms", "user_phone_url", True, {"format": "phone|sms_url", "import": "每行一个手机号|取码URL"}),
    ProviderSetting("sms", "bind_user_phone_url", True, {"format": "phone|sms_url", "import": "仅绑定阶段使用"}),
    ProviderSetting("proxy", "lajiao_credentials", True, {"countries": ["JP", "IN", "US"], "browser_bridge": "local_http_to_authenticated_socks5"}),
    ProviderSetting("proxy", "lajiao_api", True, {"countries": ["JP", "IN", "US"]}),
    ProviderSetting("mailbox", "outlook_token", True, {"format": "email----password----client_id----refresh_token", "reader": "Microsoft Graph Mail.Read"}),
    ProviderSetting("mailbox", "icloud_api", True, {"format": "email----收信URL----code:验证码API----mail:邮件API", "reader": "Code API preferred, mail/show fallback"}),
    ProviderSetting("mailbox", "icloud_privacy", True, {"format": "每行一个 iCloud 隐私邮箱账号", "reader": "IMAP forwarded inbox"}),
    ProviderSetting("mailbox", "forwarded_domain", True, {"source": "random_local_part_at_domain", "reader": "IMAP forwarded inbox"}),
    ProviderSetting("mailbox", "cfworker_admin_api", True, {"api_modes": ["cfworker", "cloud_mail"]}),
]


class ProvidersService:
    def __init__(self, repo: ProvidersRepository | None = None, config_service: ConfigService | None = None, resource_pool: ResourcePoolService | None = None):
        self.repo = repo or ProvidersRepository()
        self.config_service = config_service or ConfigService()
        self.resource_pool = resource_pool or ResourcePoolService()

    def list_providers(self) -> list[dict[str, Any]]:
        saved = {(p.provider_type, p.provider_name): p for p in self.repo.list()}
        providers: list[ProviderSetting] = []
        for default in DEFAULT_PROVIDERS:
            stored = saved.get((default.provider_type, default.provider_name))
            merged = dict(default.settings)
            enabled = default.enabled
            if stored:
                merged.update(stored.settings)
                enabled = stored.enabled
            merged.update(self._runtime_settings(default.provider_type, default.provider_name))
            providers.append(ProviderSetting(default.provider_type, default.provider_name, enabled, merged, self._definition_for(default.provider_type, default.provider_name)))
        for key, stored in saved.items():
            if key not in {(p.provider_type, p.provider_name) for p in DEFAULT_PROVIDERS}:
                providers.append(stored)
        return [self._safe_provider(item).to_dict() for item in providers]

    def save_provider(self, provider_type: str, provider_name: str, settings: dict[str, Any], enabled: bool = True) -> dict[str, Any]:
        settings = dict(settings or {})
        overrides = self._settings_to_config(provider_type, provider_name, settings)
        if overrides:
            self.config_service.save_overrides(overrides)
        stored_settings = dict(settings)
        stored_settings.pop("sms_phone_urls_text", None)
        stored_settings.pop("outlook_token_order_text", None)
        stored_settings.pop("icloud_privacy_order_text", None)
        stored_settings.pop("lajiao_proxy_credentials_text", None)
        provider = self.repo.upsert(ProviderSetting(provider_type, provider_name, enabled, stored_settings))
        return self._safe_provider(provider).to_dict()

    def test_provider(self, provider_type: str, provider_name: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload if isinstance(payload, dict) else {}
        config = self.config_service.merged_config()
        config.update(payload.get("overrides") if isinstance(payload.get("overrides"), dict) else {})
        if provider_type == "sms" and provider_name == "herosms_api":
            return self._test_herosms(config)
        if provider_type == "sms" and provider_name == "smsbower_api":
            return self._test_smsbower(config)
        if provider_type == "sms" and provider_name == "user_phone_url":
            return self._test_user_phone_url(config)
        if provider_type == "proxy" and provider_name in {"lajiao_credentials", "lajiao_api"}:
            return self._test_lajiao(config, credentials=(provider_name == "lajiao_credentials"))
        if provider_type == "mailbox" and provider_name == "cfworker_admin_api":
            return self._test_cfworker(config)
        if provider_type == "mailbox" and provider_name == "forwarded_domain":
            return self._test_forwarded_domain(config)
        if provider_type == "mailbox" and provider_name == "outlook_token":
            return self._test_outlook_token(config)
        return {"ok": False, "message": f"未知 provider: {provider_type}/{provider_name}"}

    def _safe_provider(self, provider: ProviderSetting) -> ProviderSetting:
        return ProviderSetting(
            provider.provider_type,
            provider.provider_name,
            provider.enabled,
            {key: mask_value(key, value) for key, value in provider.settings.items()},
            self._definition_for(provider.provider_type, provider.provider_name),
        )

    def _definition_for(self, provider_type: str, provider_name: str) -> ProviderDefinition | None:
        return PROVIDER_DEFINITION_BY_KEY.get((provider_type, provider_name))

    def _runtime_settings(self, provider_type: str, provider_name: str) -> dict[str, Any]:
        config = self.config_service.merged_config()
        keys = self._config_keys(provider_type, provider_name)
        return {key: config.get(key) for key in keys if config.get(key) not in (None, "")}

    def _config_keys(self, provider_type: str, provider_name: str) -> list[str]:
        if provider_type == "sms" and provider_name == "herosms_api":
            return ["sms_api_key", "sms_service", "sms_country", "country_code", "country_name", "herosms_fixed_price", "herosms_max_price"]
        if provider_type == "sms" and provider_name == "smsbower_api":
            return ["smsbower_api_key", "sms_service", "sms_country", "country_code", "country_name", "smsbower_provider_ids", "smsbower_min_price", "smsbower_max_price"]
        if provider_type == "sms" and provider_name == "user_phone_url":
            return ["sms_phone_url", "sms_phone_urls", "sms_phone_url_file"]
        if provider_type == "sms" and provider_name == "bind_user_phone_url":
            return ["bind_sms_provider", "bind_sms_phone_url", "bind_sms_phone_urls", "bind_sms_phone_url_file", "bind_sms_country", "bind_sms_service", "bind_country_code"]
        if provider_type == "proxy" and provider_name == "lajiao_credentials":
            return ["lajiao_proxy_mode", "lajiao_proxy_credential_protocol", "lajiao_proxy_credentials", "lajiao_proxy_credentials_file", "lajiao_proxy_regions", "lajiao_proxy_timeout"]
        if provider_type == "proxy" and provider_name == "lajiao_api":
            return ["lajiao_proxy_api_url", "lajiao_proxy_regions", "lajiao_proxy_timeout"]
        if provider_type == "mailbox" and provider_name == "outlook_token":
            return ["outlook_token_order_file", "outlook_email", "outlook_password", "oauth_client_id"]
        if provider_type == "mailbox" and provider_name == "icloud_api":
            return ["icloud_api_order_file", "icloud_api_order_text", "mailbox_provider"]
        if provider_type == "mailbox" and provider_name == "icloud_privacy":
            return ["icloud_privacy_order_file", "icloud_privacy_order_text", "mailbox_provider", "mailbox_imap_user", "mailbox_imap_pass", "mailbox_imap_host", "mailbox_imap_port"]
        if provider_type == "mailbox" and provider_name == "forwarded_domain":
            return ["mailbox_domain", "mailbox_imap_user", "mailbox_imap_pass", "mailbox_imap_host", "mailbox_imap_port"]
        if provider_type == "mailbox" and provider_name == "cfworker_admin_api":
            return ["cfworker_api_url", "cfworker_admin_token", "cfworker_domain"]
        return []

    def _clean_value(self, key: str, value: Any) -> Any:
        if value == "***" and is_sensitive_key(key):
            return None
        return value

    def _settings_to_config(self, provider_type: str, provider_name: str, settings: dict[str, Any]) -> dict[str, Any]:
        overrides: dict[str, Any] = {}
        def put(key: str, value: Any) -> None:
            value = self._clean_value(key, value)
            if value is not None:
                overrides[key] = value

        if provider_type == "sms" and provider_name == "herosms_api":
            put("sms_provider", "herosms_api")
            for key in ("sms_api_key", "sms_service", "sms_country", "country_code", "country_name"):
                if key in settings:
                    put(key, settings.get(key))
            max_price = float(settings.get("herosms_max_price") or 0.0999)
            put("herosms_fixed_price", bool(settings.get("herosms_fixed_price", False)))
            put("herosms_max_price", 0.0999 if max_price <= 0 or max_price >= 0.1 else max_price)
            return overrides

        if provider_type == "sms" and provider_name == "smsbower_api":
            put("sms_provider", "smsbower_api")
            for key in ("smsbower_api_key", "sms_service", "sms_country", "country_code", "country_name", "smsbower_provider_ids"):
                if key in settings:
                    put(key, settings.get(key))
            try:
                min_price = float(settings.get("smsbower_min_price") or 0)
                max_price = float(settings.get("smsbower_max_price") or 0)
            except (TypeError, ValueError) as exc:
                raise ValueError("SMSBower 最低/最高单价必须是数字") from exc
            if min_price <= 0 or max_price <= 0 or min_price > max_price:
                raise ValueError("SMSBower 单价必须为正数，且最低单价不能高于最高单价")
            put("smsbower_min_price", min_price)
            put("smsbower_max_price", max_price)
            put("smsbower_reuse_phone_to_max", False)
            return overrides

        if provider_type == "sms" and provider_name == "user_phone_url":
            text = str(settings.get("sms_phone_urls_text") or settings.get("sms_phone_urls") or settings.get("sms_phone_url") or "").strip()
            entries = UserProvidedSmsProvider.parse_entries(text)
            if not entries:
                raise ValueError("请导入至少一行手机号|取码URL")
            put("sms_provider", "user_phone_url")
            put("sms_phone_url", f"{entries[0][0]}|{entries[0][1]}")
            put("sms_phone_urls", "\n".join(f"{phone}|{url}" for phone, url in entries))
            self.resource_pool.import_phone_urls("\n".join(f"{phone}|{url}" for phone, url in entries), provider="user_phone_url")
            return overrides

        if provider_type == "sms" and provider_name == "bind_user_phone_url":
            text = str(settings.get("bind_sms_phone_urls_text") or settings.get("bind_sms_phone_urls") or settings.get("bind_sms_phone_url") or "").strip()
            entries = UserProvidedSmsProvider.parse_entries(text)
            if not entries:
                raise ValueError("请导入至少一行绑定手机号|取码URL")
            put("bind_sms_provider", "bind_user_phone_url")
            put("bind_sms_phone_url", f"{entries[0][0]}|{entries[0][1]}")
            put("bind_sms_phone_urls", "\n".join(f"{phone}|{url}" for phone, url in entries))
            self.resource_pool.import_phone_urls("\n".join(f"{phone}|{url}" for phone, url in entries), provider="bind_user_phone_url")
            return overrides

        if provider_type == "proxy" and provider_name == "lajiao_credentials":
            raw_credentials = self._clean_value("lajiao_proxy_credentials", settings.get("lajiao_proxy_credentials_text") or settings.get("lajiao_proxy_credentials") or "")
            credentials = str(raw_credentials or "").strip()
            rows = [row.strip() for row in credentials.replace("\r", "\n").split("\n") if row.strip()]
            has_file = bool(str(settings.get("lajiao_proxy_credentials_file") or "").strip())
            if rows:
                for row in rows:
                    if "@" not in row or ":" not in row:
                        raise ValueError(f"代理格式错误: {row[:80]}")
                # Collapse sticky session lines into reusable seeds.
                self.resource_pool.import_proxy_seeds(
                    "\n".join(rows),
                    protocol=str(settings.get("lajiao_proxy_credential_protocol") or "socks5"),
                )
            elif not has_file and not (
                self.resource_pool.list_resources("proxy", "proxy_seed")
                or self.resource_pool.list_resources("proxy", "lajiao_credentials")
            ):
                raise ValueError("请配置代理 seed，格式 account:pass@host:port，每行一个")
            put("lajiao_proxy_mode", "credentials")
            put("lajiao_proxy_credential_protocol", settings.get("lajiao_proxy_credential_protocol") or "socks5")
            put("lajiao_proxy_credentials_file", settings.get("lajiao_proxy_credentials_file") or "")
            put("lajiao_proxy_regions", settings.get("lajiao_proxy_regions") or "JP")
            put("lajiao_proxy_timeout", int(settings.get("lajiao_proxy_timeout") or 15))
            return overrides

        if provider_type == "proxy" and provider_name == "lajiao_api":
            put("lajiao_proxy_mode", "api")
            put("lajiao_proxy_api_url", settings.get("lajiao_proxy_api_url") or "")
            put("lajiao_proxy_regions", settings.get("lajiao_proxy_regions") or "JP")
            put("lajiao_proxy_timeout", int(settings.get("lajiao_proxy_timeout") or 15))
            return overrides

        if provider_type == "mailbox" and provider_name == "outlook_token":
            text = str(settings.get("outlook_token_order_text") or "").strip()
            if text:
                rows = self._validate_outlook_token_rows(text)
                self.resource_pool.import_outlook_tokens("\n".join(rows), provider="outlook_token")
                target = Path("data/imports/outlook_token_order.txt")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("\n".join(rows) + "\n", encoding="utf-8")
                put("outlook_token_order_file", str(target))
            elif settings.get("outlook_token_order_file"):
                put("outlook_token_order_file", settings.get("outlook_token_order_file"))
            else:
                raise ValueError("请导入 Outlook token 数据或填写订单文件路径")
            put("mailbox_provider", "outlook_token")
            return overrides

        if provider_type == "mailbox" and provider_name == "icloud_api":
            text = str(settings.get("icloud_api_order_text") or "").strip()
            if text:
                rows = self._validate_link_api_rows(text)
                self.resource_pool.import_link_api_mailboxes("\n".join(rows), provider="icloud_api")
                target = Path("data/imports/icloud_api_order.txt")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("\n".join(rows) + "\n", encoding="utf-8")
                put("icloud_api_order_file", str(target))
            elif settings.get("icloud_api_order_file"):
                put("icloud_api_order_file", settings.get("icloud_api_order_file"))
            else:
                raise ValueError("请导入 iCloud API 邮箱数据或填写订单文件路径")
            put("mailbox_provider", "icloud_api")
            return overrides

        if provider_type == "mailbox" and provider_name == "icloud_privacy":
            text = str(settings.get("icloud_privacy_order_text") or "").strip()
            if text:
                rows = self._validate_icloud_privacy_rows(text)
                self.resource_pool.import_icloud_privacy_mailboxes("\n".join(rows), provider="icloud_privacy")
                target = Path("data/imports/icloud_privacy_order.txt")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("\n".join(rows) + "\n", encoding="utf-8")
                put("icloud_privacy_order_file", str(target))
            elif settings.get("icloud_privacy_order_file"):
                put("icloud_privacy_order_file", settings.get("icloud_privacy_order_file"))
            else:
                raise ValueError("请导入 iCloud 隐私邮箱账号或填写文件路径")
            put("mailbox_provider", "icloud_privacy")
            for key in ("mailbox_imap_user", "mailbox_imap_pass", "mailbox_imap_host", "mailbox_imap_port"):
                if key in settings:
                    put(key, settings.get(key))
            return overrides

        if provider_type == "mailbox" and provider_name == "forwarded_domain":
            put("mailbox_provider", "forwarded_domain")
            for key in ("mailbox_domain", "mailbox_imap_user", "mailbox_imap_pass", "mailbox_imap_host", "mailbox_imap_port"):
                if key in settings:
                    put(key, settings.get(key))
            return overrides

        if provider_type == "mailbox" and provider_name == "cfworker_admin_api":
            put("mailbox_provider", "cfworker_admin_api")
            for key in ("cfworker_api_url", "cfworker_admin_token", "cfworker_domain"):
                if key in settings:
                    put(key, settings.get(key))
            return overrides
        return overrides

    def _validate_outlook_token_rows(self, text: str) -> list[str]:
        rows = [row.strip() for row in text.replace("\r", "\n").split("\n") if row.strip()]
        if not rows:
            raise ValueError("请导入至少一行 Outlook token 数据")
        for row in rows:
            parts = [part.strip() for part in row.split("----")]
            if len(parts) != 4 or "@" not in parts[0] or not parts[2] or not parts[3]:
                raise ValueError(f"Outlook token 行格式错误: {row[:80]}")
        return rows

    def _validate_link_api_rows(self, text: str) -> list[str]:
        rows = [row.strip().lstrip("\ufeff") for row in text.replace("\r", "\n").split("\n") if row.strip()]
        valid: list[str] = []
        for row in rows:
            parts = [part.strip() for part in row.split("----")]
            if len(parts) < 2 or "@" not in parts[0]:
                raise ValueError(f"iCloud API 邮箱行格式错误: {row[:80]}")
            has_link = False
            normalized = [parts[0]]
            for part in parts[1:]:
                label = ""
                link = part
                if ":" in part:
                    prefix, suffix = part.split(":", 1)
                    if prefix.strip().lower() in {"show", "inbox", "mail", "code"}:
                        label = prefix.strip().lower()
                        link = suffix.strip()
                if not link.startswith(("http://", "https://")):
                    continue
                has_link = True
                if label in {"code", "mail"}:
                    normalized.append(f"{label}:{link}")
                else:
                    normalized.append(link)
            if not has_link:
                raise ValueError(f"iCloud API 邮箱行格式错误: {row[:80]}")
            valid.append("----".join(normalized))
        if not valid:
            raise ValueError("请导入至少一行 iCloud API 邮箱数据，格式 email----收信URL----code:验证码API----mail:邮件API")
        return valid

    def _validate_icloud_privacy_rows(self, text: str) -> list[str]:
        rows = [row.strip().lstrip("\ufeff") for row in text.replace("\r", "\n").split("\n") if row.strip()]
        valid: list[str] = []
        for row in rows:
            email = row.split("----", 1)[0].strip().lower()
            if "@" not in email:
                raise ValueError(f"iCloud 隐私邮箱行格式错误: {row[:80]}")
            valid.append(email)
        if not valid:
            raise ValueError("请导入至少一行 iCloud 隐私邮箱账号")
        return valid

    def _test_herosms(self, config: dict[str, Any]) -> dict[str, Any]:
        if not str(config.get("herosms_api_key") or config.get("sms_api_key") or "").strip():
            return {"ok": False, "message": "缺少 HeroSMS API Key", "dry_run": True}
        provider = HeroSmsProvider.from_config(config)
        balance = provider.get_balance()
        return {"ok": True, "message": f"HeroSMS 余额: {balance}", "balance": balance, "recommended_price": 0.0999}

    def _test_smsbower(self, config: dict[str, Any]) -> dict[str, Any]:
        if not str(config.get("smsbower_api_key") or "").strip():
            return {"ok": False, "message": "缺少 SMSBower API Key", "dry_run": True}
        provider = SmsBowerProvider.from_config(config)
        balance = provider.get_balance()
        return {
            "ok": True,
            "message": f"SMSBower 余额: {balance}",
            "balance": balance,
            "service": provider.default_service,
            "country": provider.default_country,
            "provider_ids": provider.provider_ids,
            "min_price": provider.min_price,
            "max_price": provider.max_price,
        }

    def _test_user_phone_url(self, config: dict[str, Any]) -> dict[str, Any]:
        entries = UserProvidedSmsProvider.parse_entries(config.get("sms_phone_url") or config.get("sms_phone_urls") or "")
        if not entries:
            return {"ok": False, "message": "自备接码格式应为 手机号|取码URL", "dry_run": True}
        phone, url = entries[0]
        return {"ok": True, "message": f"自备接码格式有效，已导入 {len(entries)} 条", "phone": phone.strip(), "sms_url_set": bool(url.strip()), "count": len(entries)}

    def _test_lajiao(self, config: dict[str, Any], *, credentials: bool) -> dict[str, Any]:
        if credentials:
            config = dict(config)
            config["lajiao_proxy_mode"] = "credentials"
            if not str(config.get("lajiao_proxy_credentials") or config.get("lajiao_proxy_credentials_file") or "").strip():
                return {"ok": False, "message": "缺少账密代理", "dry_run": True, "browser_bridge": "local_http_to_authenticated_socks5"}
        else:
            if not str(config.get("lajiao_proxy_api_url") or "").strip():
                return {"ok": False, "message": "缺少代理 API URL", "dry_run": True}
        runtime = CredentialProxyRuntime(config)
        try:
            candidates = runtime.credential_candidates() if credentials else runtime.fetch_api_candidates()
            if not candidates:
                return {"ok": False, "message": "未获取到代理候选"}
            ok, exit_ip = runtime.check(candidates[0])
            masked = str(candidates[0]).replace(str(config.get("lajiao_proxy_credentials") or "").split(":", 1)[-1].split("@", 1)[0], "***") if "@" in str(candidates[0]) else str(candidates[0])
            return {"ok": ok, "message": f"代理{'可用' if ok else '不可用'} exit_ip={exit_ip or '?'}", "exit_ip": exit_ip, "candidate": masked}
        finally:
            runtime.cleanup()

    def _test_outlook_token(self, config: dict[str, Any]) -> dict[str, Any]:
        order_file = str(config.get("outlook_token_order_file") or "").strip()
        if not order_file:
            return {"ok": False, "message": "缺少 Outlook token 文件", "dry_run": True}
        path = Path(order_file)
        if not path.exists():
            return {"ok": False, "message": f"Outlook token 文件不存在: {order_file}", "dry_run": True}
        rows = self._validate_outlook_token_rows(path.read_text(encoding="utf-8"))
        return {"ok": True, "message": f"Outlook token 数据有效，共 {len(rows)} 条", "count": len(rows)}

    def _test_cfworker(self, config: dict[str, Any]) -> dict[str, Any]:
        if not str(config.get("cfworker_api_url") or "").strip():
            return {"ok": False, "message": "缺少 CFWorker/Cloud Mail API URL", "dry_run": True}
        if not str(config.get("cfworker_admin_token") or "").strip():
            return {"ok": False, "message": "缺少 CFWorker Admin/Open API Token", "dry_run": True}
        from core.mailbox_providers import CFWorkerMailbox

        mailbox = CFWorkerMailbox.from_config(config)
        account = mailbox.create_account()
        return {"ok": True, "message": f"邮箱 Provider 可用: {account.email}", "email": account.email, "provider": account.extra.get("provider_name") or "cfworker_admin_api"}

    def _test_forwarded_domain(self, config: dict[str, Any]) -> dict[str, Any]:
        if not str(config.get("mailbox_domain") or config.get("forward_mailbox_domain") or "").strip():
            return {"ok": False, "message": "缺少转发邮箱域名 mailbox_domain，例如 @example.invalid", "dry_run": True}
        if not str(config.get("mailbox_imap_user") or config.get("forward_imap_user") or "").strip():
            return {"ok": False, "message": "缺少转发收件箱 IMAP 账号 mailbox_imap_user", "dry_run": True}
        if not str(config.get("mailbox_imap_pass") or config.get("forward_imap_pass") or "").strip():
            return {"ok": False, "message": "缺少转发收件箱 IMAP 授权码 mailbox_imap_pass", "dry_run": True}
        from core.mailbox_providers import ForwardedDomainMailbox

        mailbox = ForwardedDomainMailbox.from_config(config)
        account = mailbox.create_account()
        return {"ok": True, "message": f"转发域名邮箱可用: {account.email}", "email": account.email, "provider": "forwarded_domain"}
