"""代理轮换模块 — 从 CSV 文件读取代理列表，自动轮换。"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional


def load_proxy_list(csv_path: str) -> list[str]:
    """
    从 CSV 文件加载代理列表。
    格式: host:port:username:password
    输出: ["socks5://user:pass@host:port", ...]
    """
    proxies = []
    path = Path(csv_path)
    if not path.exists():
        return proxies
    
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(":")
            if len(parts) >= 4:
                host = parts[0]
                port = parts[1]
                user = parts[2]
                pwd = parts[3]
                proxy_url = f"socks5://{user}:{pwd}@{host}:{port}"
                proxies.append(proxy_url)
    
    return proxies


class ProxyRotator:
    """线程安全的代理轮换器。"""

    def __init__(self, proxy_list: list[str]):
        self._proxies = list(proxy_list)
        self._index = 0
        self._last_index: Optional[int] = None
        self._lock = threading.Lock()

    @property
    def count(self) -> int:
        return len(self._proxies)

    @property
    def current_index(self) -> int:
        with self._lock:
            return self._index

    def next(self) -> Optional[str]:
        """获取下一个代理 URL。"""
        with self._lock:
            if not self._proxies:
                return None
            idx = self._index % len(self._proxies)
            proxy = self._proxies[idx]
            self._last_index = idx
            self._index = (idx + 1) % len(self._proxies)
            return proxy

    def current(self) -> Optional[str]:
        """获取当前代理 URL（不轮换）。"""
        with self._lock:
            if not self._proxies:
                return None
            return self._proxies[self._index % len(self._proxies)]

    def reset(self):
        """重置到第一个代理。"""
        with self._lock:
            self._index = 0
            self._last_index = None

    def remove_current(self):
        """移除当前代理（例如被封了）。"""
        with self._lock:
            if not self._proxies:
                return
            idx = self._last_index if self._last_index is not None else self._index % len(self._proxies)
            self._proxies.pop(idx)
            if self._proxies:
                if self._index > idx:
                    self._index -= 1
                self._index = self._index % len(self._proxies)
            self._last_index = None


def create_rotator_from_csv(csv_path: str = "proxies.csv") -> ProxyRotator:
    """便捷函数：从 CSV 创建代理轮换器。"""
    proxies = load_proxy_list(csv_path)
    return ProxyRotator(proxies)
