"""DB-backed SMS activation coordination and blacklist state.

The pool stores HeroSMS activation state in resource_pool rows so dashboard
tasks in separate Python subprocesses share one durable source of truth. The
legacy JSON file is still read as a fallback so older blocked activations are
not reused, but new writes go to SQLite.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import time
from typing import Any
from infrastructure.repositories.resource_pool_repository import ResourcePoolRepository


RESOURCE_TYPE = "sms_activation"
DEFAULT_PROVIDER = "herosms_api"


_ACTIVE_STATES = {"reserved", "post_send_pending", "blocked", "release_pending"}
_FINAL_STATES = {"released", "completed", "expired"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _utc_now()).isoformat()


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _digits(value: str) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def default_activation_pool_path() -> Path:
    root = Path(__file__).resolve().parents[2]
    return root / "data" / "sms_activation_pool.json"


@dataclass
class ActivationRecord:
    provider: str
    activation_id: str
    phone_number: str
    status: str = "reserved"
    service: str = ""
    country: str = ""
    proxy_exit_ip: str = ""
    reason: str = ""
    created_at: str = field(default_factory=_iso)
    updated_at: str = field(default_factory=_iso)
    release_after: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActivationRecord":
        return cls(
            provider=str(data.get("provider") or ""),
            activation_id=str(data.get("activation_id") or ""),
            phone_number=str(data.get("phone_number") or ""),
            status=str(data.get("status") or "reserved"),
            service=str(data.get("service") or ""),
            country=str(data.get("country") or ""),
            proxy_exit_ip=str(data.get("proxy_exit_ip") or ""),
            reason=str(data.get("reason") or ""),
            created_at=str(data.get("created_at") or _iso()),
            updated_at=str(data.get("updated_at") or _iso()),
            release_after=str(data.get("release_after") or ""),
            metadata=dict(data.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "activation_id": self.activation_id,
            "phone_number": self.phone_number,
            "status": self.status,
            "service": self.service,
            "country": self.country,
            "proxy_exit_ip": self.proxy_exit_ip,
            "reason": self.reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "release_after": self.release_after,
            "metadata": self.metadata,
        }

    @property
    def phone_digits(self) -> str:
        return _digits(self.phone_number)

    def release_due(self, now: datetime | None = None) -> bool:
        release_after = _parse_ts(self.release_after)
        return bool(release_after and release_after <= (now or _utc_now()))


class _DirectoryLock:
    def __init__(self, path: Path, *, timeout: float = 5.0, poll_interval: float = 0.05):
        self.path = path
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.acquired = False

    def __enter__(self):
        deadline = time.time() + self.timeout
        while True:
            try:
                self.path.mkdir(parents=True)
                self.acquired = True
                return self
            except FileExistsError:
                if time.time() >= deadline:
                    # A stale lock must not permanently break registration. The
                    # write is still atomic; this only sacrifices mutual exclusion.
                    return self
                time.sleep(self.poll_interval)

    def __exit__(self, exc_type, exc, tb):
        if self.acquired:
            shutil.rmtree(self.path, ignore_errors=True)


class LocalActivationPool:
    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        release_grace_seconds: int = 120,
        block_ttl_seconds: int = 24 * 60 * 60,
        enabled: bool = True,
        repo: ResourcePoolRepository | None = None,
        db_path: str | os.PathLike[str] | None = None,
    ):
        self.path = Path(path) if path else default_activation_pool_path()
        self.release_grace_seconds = max(0, int(release_grace_seconds or 0))
        self.block_ttl_seconds = max(60, int(block_ttl_seconds or 0))
        self.enabled = bool(enabled)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.repo = repo or ResourcePoolRepository(db_path)

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "LocalActivationPool":
        config = dict(config or {})
        return cls(
            config.get("sms_activation_pool_file") or None,
            release_grace_seconds=int(config.get("sms_activation_release_grace_seconds") or 120),
            block_ttl_seconds=int(config.get("sms_activation_block_ttl_seconds") or 24 * 60 * 60),
            enabled=str(config.get("sms_activation_pool_enabled", True)).strip().lower() not in {"0", "false", "no", "off", "否"},
            db_path=config.get("db_path") or None,
        )

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": 1, "records": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {"schema_version": 1, "records": []}
        if not isinstance(data, dict):
            return {"schema_version": 1, "records": []}
        records = data.get("records")
        if not isinstance(records, list):
            data["records"] = []
        return data

    def _write_unlocked(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def _mutate(self, callback):
        if not self.enabled:
            return callback({"schema_version": 1, "records": []})
        with _DirectoryLock(self.lock_path):
            data = self._read_unlocked()
            self._expire_old_records(data)
            result = callback(data)
            self._write_unlocked(data)
            return result

    def _records(self, data: dict[str, Any]) -> list[ActivationRecord]:
        return [ActivationRecord.from_dict(item) for item in data.get("records") or [] if isinstance(item, dict)]

    def _replace_records(self, data: dict[str, Any], records: list[ActivationRecord]) -> None:
        data["schema_version"] = 1
        data["records"] = [record.to_dict() for record in records]

    def _expire_old_records(self, data: dict[str, Any]) -> None:
        now = _utc_now()
        changed = False
        records = self._records(data)
        for record in records:
            updated = _parse_ts(record.updated_at) or _parse_ts(record.created_at) or now
            if record.status in _ACTIVE_STATES and now - updated > timedelta(seconds=self.block_ttl_seconds):
                record.status = "expired"
                record.reason = record.reason or "local block ttl expired"
                record.updated_at = _iso(now)
                changed = True
        if changed:
            self._replace_records(data, records)

    def _resource_key(self, activation_id: str, phone_number: str = "") -> str:
        activation_id = str(activation_id or "").strip()
        if activation_id:
            return activation_id
        return f"phone:{_digits(phone_number)}"

    def _record_from_resource(self, item: dict[str, Any]) -> ActivationRecord:
        payload = dict(item.get("payload") or {})
        activation_id = str(payload.get("activation_id") or item.get("resource_key") or "")
        return ActivationRecord(
            provider=str(item.get("provider") or payload.get("provider") or ""),
            activation_id=activation_id,
            phone_number=str(payload.get("phone_number") or ""),
            status=str(item.get("status") or payload.get("status") or "reserved"),
            service=str(payload.get("service") or ""),
            country=str(payload.get("country") or ""),
            proxy_exit_ip=str(payload.get("proxy_exit_ip") or ""),
            reason=str(item.get("last_error") or payload.get("reason") or ""),
            created_at=str(item.get("created_at") or payload.get("created_at") or _iso()),
            updated_at=str(item.get("updated_at") or payload.get("updated_at") or _iso()),
            release_after=str(payload.get("release_after") or ""),
            metadata=dict(payload.get("metadata") or {}),
        )

    def _payload_from_record(self, record: ActivationRecord) -> dict[str, Any]:
        return record.to_dict()

    def _resource_status(self, activation_status: str) -> str:
        if activation_status in _ACTIVE_STATES or activation_status in _FINAL_STATES:
            return activation_status
        return str(activation_status or "reserved")

    def _legacy_snapshot(self) -> list[ActivationRecord]:
        if not self.path.exists():
            return []
        with _DirectoryLock(self.lock_path):
            data = self._read_unlocked()
            self._expire_old_records(data)
            self._write_unlocked(data)
            return self._records(data)

    def _legacy_record(self, activation_id: str, phone_number: str = "") -> ActivationRecord | None:
        phone_digits = _digits(phone_number)
        for record in self._legacy_snapshot():
            if activation_id and record.activation_id == activation_id:
                return record
            if phone_digits and record.phone_digits and record.phone_digits == phone_digits:
                return record
        return None

    def _get_db_record(self, activation_id: str, phone_number: str = "", provider: str = "") -> ActivationRecord | None:
        key = self._resource_key(activation_id, phone_number)
        if activation_id:
            providers = [provider] if provider else [DEFAULT_PROVIDER]
            if provider != DEFAULT_PROVIDER:
                providers.append(DEFAULT_PROVIDER)
            for item_provider in providers:
                item = self.repo.get(RESOURCE_TYPE, item_provider, key)
                if item:
                    return self._record_from_resource(item)
        phone_digits = _digits(phone_number)
        if phone_digits:
            for item in self.repo.list(RESOURCE_TYPE, provider or "", ""):
                record = self._record_from_resource(item)
                if record.phone_digits and record.phone_digits == phone_digits:
                    return record
        return None

    def _save_db_record(self, record: ActivationRecord) -> ActivationRecord:
        record.provider = record.provider or DEFAULT_PROVIDER
        record.updated_at = record.updated_at or _iso()
        self.repo.upsert(
            RESOURCE_TYPE,
            record.provider,
            self._resource_key(record.activation_id, record.phone_number),
            self._payload_from_record(record),
            status=self._resource_status(record.status),
            error=record.reason,
        )
        return record

    def snapshot(self) -> list[ActivationRecord]:
        if not self.enabled:
            return []
        records = [self._record_from_resource(item) for item in self.repo.list(RESOURCE_TYPE, "", "")]
        legacy_keys = {record.activation_id for record in records if record.activation_id}
        for record in self._legacy_snapshot():
            if record.activation_id and record.activation_id in legacy_keys:
                continue
            records.append(record)
        return records

    def reserve(
        self,
        *,
        provider: str,
        activation_id: str,
        phone_number: str,
        service: str = "",
        country: str = "",
        proxy_exit_ip: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> ActivationRecord:
        activation_id = str(activation_id or "").strip()
        phone_number = str(phone_number or "").strip()
        if not activation_id and not phone_number:
            raise ValueError("activation_id or phone_number is required")

        now = _iso()
        record = self._get_db_record(activation_id, phone_number, provider) or self._legacy_record(activation_id, phone_number)
        if record:
            record.provider = provider or record.provider or DEFAULT_PROVIDER
            record.activation_id = activation_id or record.activation_id
            record.phone_number = phone_number or record.phone_number
            record.status = "reserved"
            record.service = service or record.service
            record.country = country or record.country
            record.proxy_exit_ip = proxy_exit_ip or record.proxy_exit_ip
            record.reason = ""
            record.release_after = ""
            record.updated_at = now
            if metadata:
                record.metadata.update(metadata)
            return self._save_db_record(record)

        return self._save_db_record(
            ActivationRecord(
                provider=provider or DEFAULT_PROVIDER,
                activation_id=activation_id,
                phone_number=phone_number,
                status="reserved",
                service=service,
                country=country,
                proxy_exit_ip=proxy_exit_ip,
                metadata=dict(metadata or {}),
                created_at=now,
                updated_at=now,
            )
        )

    def mark_post_send(self, activation_id: str, *, reason: str = "OpenAI requested SMS") -> None:
        self._mark(activation_id, status="post_send_pending", reason=reason, release_after_seconds=self.release_grace_seconds)

    def mark_release_pending(self, activation_id: str, *, reason: str, release_after_seconds: int | None = None) -> None:
        self._mark(
            activation_id,
            status="release_pending",
            reason=reason,
            release_after_seconds=self.release_grace_seconds if release_after_seconds is None else release_after_seconds,
        )

    def block(self, activation_id: str, *, phone_number: str = "", reason: str = "", release_after_seconds: int | None = None) -> None:
        self._mark(
            activation_id,
            status="blocked",
            phone_number=phone_number,
            reason=reason,
            release_after_seconds=self.release_grace_seconds if release_after_seconds is None else release_after_seconds,
        )

    def mark_released(self, activation_id: str, *, reason: str = "released") -> None:
        self._mark(activation_id, status="released", reason=reason, release_after_seconds=None)

    def mark_completed(self, activation_id: str, *, reason: str = "registration succeeded") -> None:
        self._mark(activation_id, status="completed", reason=reason, release_after_seconds=None)

    def _mark(
        self,
        activation_id: str,
        *,
        status: str,
        phone_number: str = "",
        reason: str = "",
        release_after_seconds: int | None = None,
    ) -> None:
        activation_id = str(activation_id or "").strip()
        if not activation_id:
            return

        now = _utc_now()
        release_after = ""
        if release_after_seconds is not None:
            release_after = _iso(now + timedelta(seconds=max(0, int(release_after_seconds))))
        record = self._get_db_record(activation_id, phone_number) or self._legacy_record(activation_id, phone_number)
        if not record:
            record = ActivationRecord(
                provider=DEFAULT_PROVIDER,
                activation_id=activation_id,
                phone_number=phone_number,
                created_at=_iso(now),
            )
        record.status = status
        if phone_number:
            record.phone_number = phone_number
        record.reason = reason
        record.release_after = release_after
        record.updated_at = _iso(now)
        self._save_db_record(record)

    def is_blocked(self, *, activation_id: str = "", phone_number: str = "", provider: str = "") -> tuple[bool, ActivationRecord | None]:
        activation_id = str(activation_id or "").strip()
        phone_digits = _digits(phone_number)
        if not self.enabled or (not activation_id and not phone_digits):
            return False, None
        record = self._get_db_record(activation_id, phone_number, provider) or self._legacy_record(activation_id, phone_number)
        if record and record.status in _ACTIVE_STATES and (not provider or not record.provider or record.provider == provider):
            return True, record
        return False, None

    def blocked_phone_exceptions(self, *, provider: str = "", limit: int = 20) -> list[str]:
        values: list[str] = []
        seen: set[str] = set()
        for record in self.snapshot():
            if record.status not in _ACTIVE_STATES:
                continue
            if provider and record.provider != provider:
                continue
            digits = record.phone_digits
            if len(digits) > 7:
                digits = digits[:7]
            if len(digits) < 4 or digits in seen:
                continue
            seen.add(digits)
            values.append(digits)
            if len(values) >= limit:
                break
        return values

    def releasable(self, *, provider: str = "", limit: int = 20) -> list[ActivationRecord]:
        now = _utc_now()
        records: list[ActivationRecord] = []
        for record in self.snapshot():
            if provider and record.provider and record.provider != provider:
                continue
            if record.status in {"post_send_pending", "release_pending", "blocked"} and record.release_due(now):
                records.append(record)
                if len(records) >= limit:
                    break
        return records
