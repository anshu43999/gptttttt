from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class PhoneNumber:
    """租用的手机号"""
    activation_id: str
    number: str              # +55xxxxxxxxx
    country: str             # BR
    provider: str            # herosms
    acquired_at: float = 0.0
    metadata: dict = field(default_factory=dict)


@dataclass
class PhoneLease:
    """号码租约 + 生命周期追踪"""
    phone: PhoneNumber
    attempts: int = 0
    successes: int = 0
    failures: int = 0
    blacklisted: bool = False
    stop_reason: str = ""
    used_codes: set = field(default_factory=set)


class BaseSmsProvider(ABC):
    """接码服务抽象 — 与 core/base_sms.py BaseSmsProvider 接口一致"""

    @abstractmethod
    def acquire(self, *, service: str, country: str, max_price: float = -1) -> PhoneNumber:
        """租用号码 → PhoneNumber"""

    @abstractmethod
    def wait_for_code(self, activation_id: str, *, timeout: int = 120,
                       first_poll_delay: int = 0) -> str:
        """等待并返回 SMS 验证码"""

    @abstractmethod
    def cancel(self, activation_id: str) -> bool:
        """释放号码"""

    def report_success(self, activation_id: str) -> bool: ...
    def report_failure(self, activation_id: str, reason: str = "") -> None: ...
    def request_resend(self, activation_id: str) -> bool: ...
    def mark_send_succeeded(self, activation_id: str) -> None: ...
    def mark_send_failed(self, activation_id: str, reason: str = "") -> None: ...
    def mark_code_failed(self, activation_id: str, reason: str = "") -> None: ...
    def set_resend_callback(self, callback: Callable[[], None] | None) -> None: ...
    def get_reuse_info(self) -> dict: ...
