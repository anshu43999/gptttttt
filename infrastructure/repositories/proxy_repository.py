from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from infrastructure import db


@dataclass
class ProxyEntry:
    id: int = 0
    url: str = ""
    exit_ip: str = ""
    region: str = ""
    success_count: int = 0
    fail_count: int = 0
    consecutive_fails: int = 0
    is_active: bool = True
    last_checked: str = ""
    source: str = "manual"
    created_at: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProxyEntry":
        return cls(
            id=int(data.get("id") or 0),
            url=str(data.get("url") or ""),
            exit_ip=str(data.get("exit_ip") or ""),
            region=str(data.get("region") or ""),
            success_count=int(data.get("success_count") or 0),
            fail_count=int(data.get("fail_count") or 0),
            consecutive_fails=int(data.get("consecutive_fails") or 0),
            is_active=bool(data.get("is_active", 1)),
            last_checked=str(data.get("last_checked") or ""),
            source=str(data.get("source") or "manual"),
            created_at=str(data.get("created_at") or ""),
        )

    @property
    def success_rate(self) -> float:
        total = self.success_count + self.fail_count
        return self.success_count / total if total > 0 else 0.0


@dataclass
class ProxyStats:
    total: int = 0
    active: int = 0
    by_region: dict[str, int] = field(default_factory=dict)
    avg_success_rate: float = 0.0


class ProxyRepository:
    def __init__(self, db_path: str | Path | None = None):
        self.db_path = db_path
        db.init_db(db_path)

    def list_active(self, region: str = "") -> list[ProxyEntry]:
        items = db.list_proxies(active_only=True, region=region, path=self.db_path)
        return [ProxyEntry.from_dict(item) for item in items]

    def list_all(self) -> list[ProxyEntry]:
        items = db.list_proxies(path=self.db_path)
        return [ProxyEntry.from_dict(item) for item in items]

    def get(self, url: str) -> ProxyEntry:
        return ProxyEntry.from_dict(db.get_proxy(url, path=self.db_path))

    def save(self, entry: ProxyEntry) -> None:
        db.upsert_proxy(
            entry.url,
            exit_ip=entry.exit_ip,
            region=entry.region,
            source=entry.source,
            path=self.db_path,
        )

    def increment_success(self, url: str) -> None:
        db.increment_proxy_success(url, path=self.db_path)

    def increment_fail(self, url: str) -> None:
        db.increment_proxy_fail(url, path=self.db_path)

    def stats(self) -> ProxyStats:
        all_items = self.list_all()
        active = [e for e in all_items if e.is_active]
        regions: dict[str, int] = {}
        for e in active:
            regions[e.region] = regions.get(e.region, 0) + 1
        avg = sum(e.success_rate for e in active) / len(active) if active else 0.0
        return ProxyStats(
            total=len(all_items),
            active=len(active),
            by_region=regions,
            avg_success_rate=round(avg, 4),
        )
