from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class MailboxAccount:
    email: str
    account_id: str
    provider: str = ""
    metadata: dict = field(default_factory=dict)


class BaseMailboxProvider(ABC):
    @abstractmethod
    def create(self) -> MailboxAccount:
        """创建/分配一个邮箱"""

    @abstractmethod
    def wait_for_code(self, account: MailboxAccount, *, timeout: int = 180,
                       before_ids: set | None = None,
                       code_pattern: str | None = None) -> str:
        """等待邮箱验证码"""

    @classmethod
    @abstractmethod
    def from_config(cls, config: dict) -> "BaseMailboxProvider":
        """从配置字典创建实例"""
