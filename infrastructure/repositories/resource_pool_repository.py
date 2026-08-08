from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from infrastructure import db


@dataclass
class ResourceLease:
    id: int = 0
    resource_type: str = ""
    provider: str = ""
    resource_key: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    status: str = ""
    lease_id: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResourceLease":
        return cls(
            id=int(data.get("id") or 0),
            resource_type=str(data.get("resource_type") or ""),
            provider=str(data.get("provider") or ""),
            resource_key=str(data.get("resource_key") or ""),
            payload=dict(data.get("payload") or {}),
            status=str(data.get("status") or ""),
            lease_id=str(data.get("lease_id") or ""),
        )


class ResourcePoolRepository:
    def __init__(self, db_path: str | Path | None = None):
        self.db_path = db_path
        db.init_db(db_path)

    def import_many(self, resource_type: str, provider: str, rows: list[tuple[str, dict[str, Any]]]) -> int:
        # Preserve skip-existing semantics, but load keys once and write in one txn.
        pending: list[tuple[str, dict[str, Any]]] = []
        seen: set[str] = set()
        for resource_key, payload in rows:
            key = str(resource_key or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            pending.append((key, payload if isinstance(payload, dict) else {}))
        if not pending:
            return 0
        existing = db.existing_resource_keys(resource_type, provider, path=self.db_path)
        fresh = [(key, payload) for key, payload in pending if key not in existing]
        if not fresh:
            return 0
        return db.upsert_resources_many(resource_type, provider, fresh, path=self.db_path)

    def list(self, resource_type: str = "", provider: str = "", status: str = "") -> list[dict[str, Any]]:
        return db.list_resources(resource_type=resource_type, provider=provider, status=status, path=self.db_path)
    def list_ids(self, resource_type: str = "", provider: str = "", status: str = "") -> list[int]:
        return [int(item.get("id") or 0) for item in self.list(resource_type, provider, status) if int(item.get("id") or 0)]


    def get(self, resource_type: str, provider: str, resource_key: str) -> dict[str, Any]:
        return db.get_resource(resource_type, provider, resource_key, path=self.db_path)

    def upsert(self, resource_type: str, provider: str, resource_key: str, payload: dict[str, Any], *, status: str = "available", error: str = "") -> None:
        db.upsert_resource(resource_type, provider, resource_key, payload, status=status, error=error, path=self.db_path)


    def recover_stale(self, *, lease_ttl_seconds: int = 1800) -> int:
        return db.recover_stale_resources(lease_ttl_seconds=lease_ttl_seconds, path=self.db_path)
    def lease(self, resource_type: str, provider: str, lease_id: str, *, region: str = "") -> ResourceLease:
        return ResourceLease.from_dict(db.lease_resource(resource_type, provider, lease_id, region=region, path=self.db_path))


    def set_status(self, resource_id: int, *, status: str, cooldown_until: str = "", error: str = "") -> None:
        db.update_resource_status(resource_id, status=status, cooldown_until=cooldown_until, error=error, path=self.db_path)
    def set_status_many(self, resource_ids: list[int], *, status: str, cooldown_until: str = "", error: str = "") -> int:
        count = 0
        for resource_id in resource_ids:
            if resource_id <= 0:
                continue
            db.update_resource_status(resource_id, status=status, cooldown_until=cooldown_until, error=error, path=self.db_path)
            count += 1
        return count
    def delete_many(self, resource_ids: list[int]) -> int:
        ids = [int(resource_id) for resource_id in resource_ids if int(resource_id) > 0]
        if not ids:
            return 0
        return db.delete_resources(ids, path=self.db_path)
    def report(self, lease_id: str, resource_key: str, *, success: bool, cooldown_until: str = "", error: str = "") -> None:
        db.report_resource(lease_id, resource_key, success=success, cooldown_until=cooldown_until, error=error, path=self.db_path)
