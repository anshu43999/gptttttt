"""
Test: Register API endpoints via FastAPI TestClient (no proxy).
"""
import pytest
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient


class FakeTasksService:
    def __init__(self):
        self.task = {
            "id": "task_20260618_000000_fake123",
            "type": "register-token",
            "task_type": "register-token",
            "status": "pending",
        }

    def start_register(self, data, overrides):
        self.data = data
        self.overrides = overrides
        return self.task

    def start_email_register(self, data, overrides):
        self.data = data
        self.overrides = overrides
        task = dict(self.task)
        task["id"] = "task_20260618_000000_email12"
        task["type"] = "email-register-token"
        task["task_type"] = "email-register-token"
        return task

    def start_protocol_register(self, data, overrides):
        self.data = data
        self.overrides = overrides
        task = dict(self.task)
        task["id"] = "task_20260618_000000_proto1"
        task["type"] = "protocol-register-token"
        task["task_type"] = "protocol-register-token"
        return task

    def start_email_protocol_register(self, data, overrides):
        self.data = data
        self.overrides = overrides
        task = dict(self.task)
        task["id"] = "task_20260618_000000_eproto1"
        task["type"] = "email-protocol-register-token"
        task["task_type"] = "email-protocol-register-token"
        return task

    def get_task(self, task_id):
        return self.task if task_id == self.task["id"] else {}

    def task_events(self, task_id, since_id=0):
        return []

    def list_tasks(self):
        return [self.task]

    def stop(self, task_id):
        return task_id == self.task["id"]

    def stop_all(self):
        return {"requested": 1, "stopped": 1, "failed": 0}




class DeferredTasksService:
    def __init__(self):
        self.max_parallel = 1
        self.bucket_limits = {}
        self.defer_flags: list[bool] = []
        self.drained = 0

    def set_max_parallel(self, value):
        self.max_parallel = value

    def start_email_register(self, data, overrides, *, defer_start=False):
        self.defer_flags.append(bool(defer_start))
        index = len(self.defer_flags)
        return {"id": f"task_20260618_000000_defer{index:02d}", "type": "email-register-token", "status": "queued"}

    def drain_queue_async(self):
        self.drained += 1



class FastBulkEmailProtocolService(DeferredTasksService):
    def __init__(self):
        super().__init__()
        self.bulk_calls: list[int] = []

    def start_email_protocol_register(self, data, overrides, *, defer_start=False):
        self.defer_flags.append(bool(defer_start))
        return {"id": "task_20260618_000000_first1", "type": "email-protocol-register-token", "status": "queued"}

    def start_email_protocol_register_many(self, data, overrides, count):
        self.bulk_calls.append(int(count))
        return int(count)

def test_register_creation_defers_queue_drain() -> None:
    from api.register import RegisterRequest, start_register

    svc = DeferredTasksService()
    response = start_register(RegisterRequest(mode="email", register_count=5, register_threads=5), svc)

    assert response["ok"] is True
    assert response["count"] == 5
    assert response["threads"] == 5
    assert svc.defer_flags == [True, True, True, True, True]
    assert svc.drained == 1
    assert all(task["status"] == "queued" for task in response["tasks"])


def test_register_creation_accepts_high_concurrency_and_count() -> None:
    from api.register import RegisterRequest, start_register

    svc = DeferredTasksService()
    response = start_register(RegisterRequest(mode="email", register_count=120, register_threads=20), svc)

    assert response["ok"] is True
    assert response["count"] == 120
    assert response["threads"] == 20
    assert svc.max_parallel == 20
    assert svc.bucket_limits["register"] == 20
    assert svc.drained == 1


def test_register_creation_accepts_2000_count_and_200_threads() -> None:
    from api.register import RegisterRequest, start_register

    svc = DeferredTasksService()
    response = start_register(
        RegisterRequest(mode="email", register_count=2000, register_threads=200),
        svc,
    )

    assert response["ok"] is True
    assert response["count"] == 2000
    assert response["threads"] == 200
    assert svc.max_parallel == 200
    assert svc.bucket_limits["register"] == 200


def test_email_protocol_bulk_creation_uses_fast_path() -> None:
    from api.register import RegisterRequest, start_register

    svc = FastBulkEmailProtocolService()
    response = start_register(
        RegisterRequest(
            mode="email_protocol",
            register_count=300,
            register_threads=100,
            email_protocol_backend="go",
            mailbox_provider="outlook_token",
        ),
        svc,
    )

    assert response["ok"] is True
    assert response["count"] == 300
    assert response["accepted"] == 300
    # Whole batch is Go-managed in one shot — no Python first-task shell.
    assert response["created"] == 300
    assert response["creating"] == 0
    assert response["async_create"] is False
    assert response.get("go_managed") is True
    assert svc.defer_flags == []
    assert svc.bulk_calls == [300]
    assert svc.drained == 0

def test_tasks_list_route_kicks_async_drain_without_blocking() -> None:
    from api.tasks import list_tasks

    class SlowDrainService:
        def __init__(self):
            self.list_kwargs = None
            self.drained = 0
            self.async_drains = 0

        def list_tasks(self, **kwargs):
            self.list_kwargs = kwargs
            return [{"id": "task-a", "status": "queued"}]

        def drain_queue(self):
            self.drained += 1

        def drain_queue_async(self):
            self.async_drains += 1

    svc = SlowDrainService()

    response = asyncio.run(list_tasks(limit=10, svc=svc))

    assert response["items"] == [{"id": "task-a", "status": "queued"}]
    assert svc.list_kwargs["drain_queue"] is False
    # List must not block on drain, but must kick a background drain.
    assert svc.async_drains == 1
    assert svc.drained == 0

def override_tasks_service():
    return FakeTasksService()



@pytest.fixture(scope="module")
def client():
    from main import app
    from api.deps import get_tasks_service

    app.dependency_overrides[get_tasks_service] = override_tasks_service
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_tasks_service, None)


def test_health_endpoint(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_providers_list(client):
    resp = client.get("/api/providers")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert len(data["items"]) > 0
    herosms = next(item for item in data["items"] if item["provider_type"] == "sms" and item["provider_name"] == "herosms_api")
    assert herosms["definition"]["label"] == "HeroSMS 接码"
    assert any(field["key"] == "sms_api_key" for field in herosms["definition"]["fields"])


def test_stats_overview(client):
    resp = client.get("/api/stats/overview")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_accounts" in data


def test_config_get(client):
    resp = client.get("/api/config")
    assert resp.status_code == 200
    data = resp.json()
    assert "config" in data


def test_register_start_returns_run_id(client):
    resp = client.post("/api/register", json={
        "mode": "phone",
        "sms_provider": "herosms_api",
        "sms_country": "BR",
        "mailbox_provider": "forwarded_domain",
        "proxy_mode": "credentials",
        "proxy_region": "JP",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["ok"] is True
    run_id = data.get("run_id", "")
    assert len(run_id) > 20


def test_email_register_start_uses_email_task(client):
    resp = client.post("/api/register", json={
        "mode": "email",
        "sms_provider": "user_phone_url",
        "mailbox_provider": "outlook_token",
        "proxy_mode": "credentials",
        "proxy_region": "JP",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["ok"] is True
    assert data["task"]["type"] == "email-register-token"


def test_protocol_register_start_uses_protocol_task(client):
    resp = client.post("/api/register", json={
        "mode": "phone",
        "registration_engine": "protocol",
        "sms_provider": "herosms_api",
        "sms_country": "BR",
        "mailbox_provider": "forwarded_domain",
        "proxy_mode": "credentials",
        "proxy_region": "JP",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["task"]["type"] == "protocol-register-token"


def test_email_protocol_register_start_uses_separate_task(client):
    resp = client.post("/api/register", json={
        "mode": "email_protocol",
        "mailbox_provider": "outlook_token",
        "proxy_mode": "credentials",
        "proxy_region": "JP",
        "email_protocol_backend": "go",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["task"]["type"] == "email-protocol-register-token"
    assert client.app.dependency_overrides  # service injected


def test_email_protocol_backend_override_forwarded(client):
    from api.deps import get_tasks_service

    svc = client.app.dependency_overrides[get_tasks_service]()
    resp = client.post("/api/register", json={
        "mode": "email_protocol",
        "mailbox_provider": "outlook_token",
        "email_protocol_backend": "golang",
        "go_email_protocol_url": "http://127.0.0.1:19001",
    })
    assert resp.status_code == 201
    assert svc.overrides["email_protocol_backend"] == "go"
    assert svc.overrides["go_email_protocol_url"] == "http://127.0.0.1:19001"

def test_register_status_not_found(client):
    resp = client.get("/api/register/nonexistent_run/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "not_found"


def test_email_otp_not_found(client):
    resp = client.get("/api/email-otp/nonexistent@5445945.xyz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False


def test_tasks_list(client):
    resp = client.get("/api/tasks")
    assert resp.status_code == 200
    assert "items" in resp.json()


def test_tasks_stop_all(client):
    resp = client.post("/api/tasks/stop-all")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "requested": 1, "stopped": 1, "failed": 0}


def test_frontend_serves_html(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "<!doctype html>" in resp.text.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
