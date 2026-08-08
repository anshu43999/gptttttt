"""账户轮换模块 — 代理 + 邮箱配对管理。"""

from __future__ import annotations

import csv
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class AccountSlot:
    """一个完整的注册槽位：代理 + 邮箱。"""
    email: str
    email_password: str
    proxy_url: str


class AccountRotator:
    """线程安全的账户轮换器，自动配对代理和邮箱。"""

    def __init__(self, proxy_list: list[str], emails: list[tuple[str, str]]):
        self._proxies = list(proxy_list)
        self._emails = list(emails)  # [(email, password), ...]
        self._index = 0
        self._lock = threading.Lock()

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, min(len(self._proxies), len(self._emails)) - self._index)

    def next(self) -> Optional[AccountSlot]:
        """获取下一个注册槽位。"""
        with self._lock:
            limit = min(len(self._proxies), len(self._emails))
            if self._index >= limit:
                return None
            proxy = self._proxies[self._index]
            email, pwd = self._emails[self._index]
            self._index += 1
            return AccountSlot(email=email, email_password=pwd, proxy_url=proxy)

    def mark_current_failed(self):
        """当前槽位已由 next() 消费；失败标记不再额外跳过下一个槽位。"""
        return None


def load_emails_from_csv(csv_path: str) -> list[tuple[str, str]]:
    """从 CSV 加载邮箱列表。格式: email,password"""
    emails = []
    path = Path(csv_path)
    if not path.exists():
        return emails
    with open(path, "r", encoding="utf-8") as f:
        for row in csv.reader(f):
            if len(row) >= 2 and "@" in row[0]:
                emails.append((row[0].strip(), row[1].strip()))
    return emails


def create_account_rotator(
    proxy_csv: str = "proxies.csv",
    email_csv: str = "outlook_accounts.csv",
) -> AccountRotator:
    """便捷函数：从 CSV 创建账户轮换器。"""
    from core.proxy_rotator import load_proxy_list

    proxies = load_proxy_list(proxy_csv)
    emails = load_emails_from_csv(email_csv)
    return AccountRotator(proxies, emails)
