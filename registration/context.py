"""Registration context — data class for a single registration run."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field


@dataclass
class RegistrationRun:
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    mode: str = "phone"            # phone | email
    status: str = "pending"        # pending → running → success / failed

    # inputs
    sms_provider_key: str = "herosms_api"
    sms_country: str = "BR"
    sms_max_price: float = -1
    mailbox_provider_key: str = "icloud_api"
    proxy_mode: str = "credentials"  # api | credentials
    proxy_region: str = "JP"
    headed: bool = False
    skip_precheck: bool = False
    force_signup: bool = False

    # runtime
    phone_number: str = ""
    email: str = ""
    proxy_ip: str = ""
    proxy_url: str = ""

    # outputs
    access_token: str = ""
    refresh_token: str = ""
    id_token: str = ""
    session_token: str = ""
    password: str = ""
    plan_type: str = ""

    # tracking
    steps: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0

    @classmethod
    def from_config(cls, config: dict) -> "RegistrationRun":
        return cls(
            mode=str(config.get("mode") or "phone"),
            sms_provider_key=str(config.get("sms_provider") or config.get("sms_provider_key") or "herosms_api"),
            sms_country=str(config.get("sms_country") or "BR"),
            sms_max_price=float(config.get("herosms_max_price") or config.get("sms_max_price") or -1),
            mailbox_provider_key=str(config.get("mailbox_provider") or config.get("mailbox_provider_key") or "icloud_api"),
            proxy_mode=str(config.get("proxy_mode") or "credentials"),
            proxy_region=str(config.get("proxy_region") or "JP"),
            headed=bool(config.get("headed")),
            skip_precheck=bool(config.get("skip_precheck")),
            force_signup=bool(config.get("force_signup")),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.run_id,
            "mode": self.mode,
            "status": self.status,
            "phone": self.phone_number,
            "email": self.email,
            "sms_provider": self.sms_provider_key,
            "mailbox_provider": self.mailbox_provider_key,
            "proxy_ip": self.proxy_ip,
            "proxy_region": self.proxy_region,
            "plan_type": self.plan_type,
            "access_token_obtained": int(bool(self.access_token)),
            "refresh_token_obtained": int(bool(self.refresh_token)),
            "steps_completed": self.steps,
            "errors": self.errors,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }
