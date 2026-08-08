from __future__ import annotations

from typing import Any

from core.config_loader import load_config
from infrastructure.repositories.config_repository import ConfigRepository


_SECRET_FRAGMENTS = ("key", "token", "password", "pass", "secret", "credential")
_NON_SENSITIVE_KEYS = frozenset({
    "upi_activation_enabled",
    "upi_submit_per_key_per_min",
    "upi_poll_interval_sec",
    "upi_poll_timeout_sec",
    "upi_auto_verify_plus",
})


def is_sensitive_key(key: str) -> bool:
    lowered = str(key or "").lower()
    if lowered in _NON_SENSITIVE_KEYS:
        return False
    return any(fragment in lowered for fragment in _SECRET_FRAGMENTS)


def mask_value(key: str, value: Any) -> Any:
    # This dashboard is an operator-only local tool. Source configuration
    # values must remain inspectable instead of being replaced by placeholders.
    return value


class ConfigService:
    def __init__(self, repo: ConfigRepository | None = None, base_config: str = "config.yaml"):
        self.repo = repo or ConfigRepository()
        self.base_config = base_config

    def file_config(self) -> dict[str, Any]:
        return load_config(self.base_config)

    def db_config(self) -> dict[str, Any]:
        return self.repo.get_all()

    def merged_config(self) -> dict[str, Any]:
        config = self.file_config()
        config.update(self.db_config())
        return config

    def safe_file_config(self) -> dict[str, Any]:
        return {key: mask_value(key, value) for key, value in self.file_config().items()}

    def safe_db_config(self) -> dict[str, Any]:
        return {key: mask_value(key, value) for key, value in self.db_config().items()}

    def save_overrides(self, values: dict[str, Any]) -> dict[str, Any]:
        return self.repo.set_many(values)
