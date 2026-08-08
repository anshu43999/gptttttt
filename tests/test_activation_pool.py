from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path
from unittest.mock import patch
import json

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.base_sms import SmsActivation
from core.sms.activation_pool import LocalActivationPool
from infrastructure.repositories.resource_pool_repository import ResourcePoolRepository
from full_pipeline import RegisterPipeline


class PoolRentSms:
    def __init__(self, activations):
        self.activations = list(activations)
        self.phone_exceptions = []
        self.cancelled = []
        self.failed = []

    def get_balance(self):
        return 1.0

    def get_active_activations(self, limit=20):
        return []

    def _normalize_phone_exceptions(self, values):
        result = []
        for value in values:
            digits = "".join(ch for ch in str(value or "") if ch.isdigit())
            if digits and digits not in result:
                result.append(digits[:7] if len(digits) > 7 else digits)
        return result

    def get_number(self, *, service, country=""):
        if not self.activations:
            raise RuntimeError("NO_NUMBERS")
        return self.activations.pop(0)

    def cancel_activation(self, activation_id):
        self.cancelled.append(activation_id)
        return True

    def mark_send_failed(self, activation_id, reason):
        self.failed.append((activation_id, reason))


class TimeoutSms:
    def __init__(self):
        self.cancelled = []
        self.stopped = []

    def wait_for_code(self, activation_id, timeout=180, poll_interval=3):
        return None

    def _stop_reuse(self, reason):
        self.stopped.append(reason)

    def cancel_activation(self, activation_id):
        self.cancelled.append(activation_id)
        return True


def activation_resource(pool: LocalActivationPool, activation_id: str) -> dict:
    return pool.repo.get("sms_activation", "herosms_api", activation_id)


def test_activation_pool_blocks_reserved_old_activation(tmp_path):
    pool_path = tmp_path / "pool.json"
    db_path = tmp_path / "pool.db"
    pool = LocalActivationPool(pool_path, db_path=db_path)
    pool.reserve(provider="herosms_api", activation_id="act-old", phone_number="+551111111111", service="dr", country="73")

    pipeline = RegisterPipeline({
        "sms_api_key": "key",
        "sms_provider": "herosms_api",
        "sms_service": "dr",
        "sms_country": "73",
        "prepare_registration_before_phone": False,
        "precheck_phone_before_sms": False,
        "sms_activation_pool_file": str(pool_path),
        "db_path": str(db_path),
    })
    rented_sms = PoolRentSms([
        SmsActivation(activation_id="act-old", phone_number="+551111111111", country="73"),
        SmsActivation(activation_id="act-new", phone_number="+552222222222", country="73"),
    ])

    with patch("core.base_sms.HeroSmsProvider.from_config", lambda config: rented_sms), \
         patch("core.base_sms.hero_sms_cache_file", lambda: tmp_path / "cache.json"):
        phone = pipeline.step_get_phone_number()

    assert phone == "+552222222222"
    assert pipeline.result["activation_id"] == "act-new"
    blocked, record = pool.is_blocked(activation_id="act-new", phone_number="+552222222222")
    assert blocked is True
    assert record is not None
    assert record.status == "reserved"
    resource = activation_resource(pool, "act-new")
    assert resource["resource_type"] == "sms_activation"
    assert resource["provider"] == "herosms_api"
    assert resource["status"] == "reserved"
    assert resource["payload"]["phone_number"] == "+552222222222"


def test_sms_timeout_marks_post_send_pending_without_cancel(tmp_path):
    pool_path = tmp_path / "pool.json"
    db_path = tmp_path / "pool.db"
    pipeline = RegisterPipeline({
        "sms_activation_pool_file": str(pool_path),
        "sms_activation_release_grace_seconds": 120,
        "herosms_cancel_on_timeout": False,
        "db_path": str(db_path),
    })
    pipeline.sms_provider = TimeoutSms()
    pipeline.result["activation_id"] = "act-timeout"
    pipeline.result["phone_number"] = "+553333333333"

    assert pipeline._wait_for_sms_code("act-timeout", timeout=1, poll_interval=1) == ""

    pool = LocalActivationPool(pool_path, db_path=db_path)
    records = pool.snapshot()
    record = next(item for item in records if item.activation_id == "act-timeout")
    assert record.status == "post_send_pending"
    assert record.release_after
    resource = activation_resource(pool, "act-timeout")
    assert resource["status"] == "post_send_pending"
    assert resource["payload"]["release_after"]
    assert pipeline.sms_provider.cancelled == []
    assert pipeline.sms_provider.stopped == ["sms timeout after password submitted"]


def test_activation_pool_returns_releasable_after_grace(tmp_path):
    pool_path = tmp_path / "pool.json"
    db_path = tmp_path / "pool.db"
    pool = LocalActivationPool(pool_path, release_grace_seconds=120, db_path=db_path)
    pool.reserve(provider="herosms_api", activation_id="act-release", phone_number="+554444444444")
    pool.mark_post_send("act-release", reason="OpenAI requested SMS")

    resource = activation_resource(pool, "act-release")
    payload = dict(resource["payload"])
    payload["release_after"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    pool.repo.upsert("sms_activation", "herosms_api", "act-release", payload, status="post_send_pending", error="OpenAI requested SMS")

    due = pool.releasable(provider="herosms_api")
    assert [record.activation_id for record in due] == ["act-release"]
    pool.mark_released("act-release", reason="delayed release")
    released = activation_resource(pool, "act-release")
    assert released["status"] == "released"
    assert released["payload"]["status"] == "released"

    pool.mark_completed("act-done", reason="registration succeeded")
    completed = activation_resource(pool, "act-done")
    assert completed["status"] == "completed"


def test_activation_pool_reads_legacy_json_but_writes_db(tmp_path):
    pool_path = tmp_path / "pool.json"
    pool_path.write_text(json.dumps({"schema_version": 1, "records": [{"provider": "herosms_api", "activation_id": "act-legacy", "phone_number": "+555000000000", "status": "reserved"}]}), encoding="utf-8")

    pool = LocalActivationPool(pool_path, db_path=tmp_path / "pool.db")
    blocked, record = pool.is_blocked(activation_id="act-legacy", phone_number="+555000000000", provider="herosms_api")
    assert blocked is True
    assert record is not None
    assert record.status == "reserved"

    pool.block("act-blocked", phone_number="+555111111111", reason="旧号阻止复用")
    blocked_resource = activation_resource(pool, "act-blocked")
    assert blocked_resource["status"] == "blocked"
    assert blocked_resource["last_error"] == "旧号阻止复用"
    assert blocked_resource["payload"]["phone_number"] == "+555111111111"
    assert not any(item.activation_id == "act-blocked" for item in pool._legacy_snapshot())
