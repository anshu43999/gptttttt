from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ProviderFieldDefinition:
    key: str
    label: str
    help: str = ""
    placeholder: str = ""
    secret: bool = False
    multiline: bool = False
    required: bool = False

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "key": self.key,
            "label": self.label,
        }
        if self.help:
            data["help"] = self.help
        if self.placeholder:
            data["placeholder"] = self.placeholder
        if self.secret:
            data["secret"] = True
        if self.multiline:
            data["multiline"] = True
        if self.required:
            data["required"] = True
        return data


@dataclass(frozen=True)
class ProviderDefinition:
    provider_type: str
    provider_name: str
    label: str
    help: str = ""
    fields: tuple[ProviderFieldDefinition, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "provider_type": self.provider_type,
            "provider_name": self.provider_name,
            "label": self.label,
            "fields": [field.to_dict() for field in self.fields],
        }
        if self.help:
            data["help"] = self.help
        return data


@dataclass
class ProviderSetting:
    provider_type: str
    provider_name: str
    enabled: bool = True
    settings: dict[str, Any] = field(default_factory=dict)
    definition: ProviderDefinition | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "provider_type": self.provider_type,
            "provider_name": self.provider_name,
            "enabled": self.enabled,
            "settings": dict(self.settings),
        }
        if self.definition is not None:
            data["definition"] = self.definition.to_dict()
        return data
