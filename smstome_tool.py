"""SMSToMe tool stub.

Full implementation exists in any-auto-register/smstome_tool.py (38KB).
This stub provides the minimal interface needed by phone_service.py and oauth_client.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional


@dataclass
class PhoneEntry:
    """Phone number entry."""
    phone_number: str = ""
    activation_id: str = ""
    country: str = ""
    status: str = ""
    url: str = ""


def parse_country_slugs(raw) -> list[str]:
    """Parse country slug configuration."""
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if x]
    if isinstance(raw, str):
        return [s.strip() for s in raw.replace(";", ",").split(",") if s.strip()]
    return []


def update_global_phone_list(
    cookie_header: str,
    countries: Optional[list[str]] = None,
    output_path: Optional[Path] = None,
    max_pages_per_country: int = 5,
) -> int:
    """Sync phone list from SMSToMe. Stub always returns 0."""
    return 0


def get_unused_phone(
    task_name: str,
    country_slug: Optional[list[str]] = None,
    global_file: Optional[Path] = None,
    used_numbers_dir: Optional[Path] = None,
    exclude_prefixes: Optional[Iterable[str]] = None,
) -> Optional[PhoneEntry]:
    """Get an unused phone number. Stub — use HeroSMS instead."""
    raise NotImplementedError(
        "SMSToMe is not configured. Use HeroSMS instead: "
        "set sms_provider=herosms_api in config.yaml"
    )


def mark_phone_blacklisted(
    task_name: str,
    phone: str,
    used_numbers_dir: Optional[Path] = None,
) -> None:
    """Mark a phone as blacklisted. Stub no-op."""
    pass


def wait_for_otp(
    entry: PhoneEntry,
    cookie_header: str,
    timeout: int = 45,
    poll_interval: int = 5,
    trace: Optional[Callable[[str], None]] = None,
    raise_on_timeout: bool = False,
) -> Optional[str]:
    """Wait for OTP. Stub — use HeroSMS instead."""
    raise NotImplementedError(
        "SMSToMe is not configured. Use HeroSMS instead."
    )


class SMSToMePhoneService:
    """Stub for SMSToMe phone verification service."""

    def __init__(self, config: Optional[dict] = None, log_fn: Optional[Callable[[str], None]] = None):
        self.config = config or {}
        self.log_fn = log_fn or (lambda _msg: None)

    def get_number(self, country: str = "US") -> dict:
        raise NotImplementedError(
            "SMSToMe is not configured. Use HeroSMS instead."
        )

    def get_status(self, activation_id: str) -> dict:
        raise NotImplementedError(
            "SMSToMe is not configured. Use HeroSMS instead."
        )

    def wait_for_code(self, entry: PhoneEntry, *, timeout: Optional[int] = None) -> Optional[str]:
        raise NotImplementedError(
            "SMSToMe is not configured. Use HeroSMS instead."
        )
