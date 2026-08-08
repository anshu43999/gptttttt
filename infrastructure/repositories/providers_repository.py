from __future__ import annotations

from pathlib import Path
from typing import Any

from domain.providers import ProviderSetting
from infrastructure import db


class ProvidersRepository:
    def __init__(self, db_path: str | Path | None = None):
        self.db_path = db_path
        db.init_db(db_path)

    def list(self) -> list[ProviderSetting]:
        return [
            ProviderSetting(
                provider_type=str(item.get("provider_type") or ""),
                provider_name=str(item.get("provider_name") or ""),
                enabled=bool(item.get("enabled")),
                settings=item.get("settings") if isinstance(item.get("settings"), dict) else {},
            )
            for item in db.list_providers(path=self.db_path)
        ]

    def upsert(self, provider: ProviderSetting) -> ProviderSetting:
        db.upsert_provider(provider.provider_type, provider.provider_name, provider.settings, provider.enabled, path=self.db_path)
        for item in self.list():
            if item.provider_type == provider.provider_type and item.provider_name == provider.provider_name:
                return item
        return provider
