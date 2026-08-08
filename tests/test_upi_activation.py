from __future__ import annotations

from pathlib import Path
import asyncio
import hashlib
import threading
import time

import pytest
from api import accounts as accounts_api

from infrastructure.repositories.accounts_repository import AccountsRepository
from infrastructure import db, db_backend
from infrastructure.repositories.resource_pool_repository import ResourcePoolRepository
from platforms.chatgpt.upi_activation_client import (
    TokenBucket,
    UpiActivationClient,
    UpiActivationError,
    UpiTask,
    normalize_channel,
    normalize_idempotency_key,
)
from services.upi_activation_service import UpiActivationService
from application.accounts_service import AccountsService
from application.config_service import ConfigService

from infrastructure.repositories.plus_activation_repository import PlusActivationRepository
from services.plus_activation_batch_service import PlusActivationBatchService

@pytest.fixture(autouse=True)
def _use_sqlite_test_backend(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GPT_REGISTER_SKIP_ENV_DB", "1")
    monkeypatch.delenv("GPT_REGISTER_DB_BACKEND", raising=False)
    monkeypatch.delenv("DB_BACKEND", raising=False)
    monkeypatch.delenv("GPT_REGISTER_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    db_backend.reset_backend_cache()
    yield
    db_backend.reset_backend_cache()


def test_normalize_channel_and_idempotency() -> None:
    assert normalize_channel("UPI") == "upi"
    assert normalize_channel("PIX") == "pix"
    assert normalize_channel("weird") == "upi"
    assert normalize_channel("kakao") == "kakao"
    assert normalize_channel("ideal") == "ideal"
    key = normalize_idempotency_key("upi-alice@outlook.com-a1")
    assert key.startswith("upi-")
    assert len(key) >= 8


def test_v1_task_payload_prefers_state_and_charged_flag() -> None:
    task = UpiActivationClient._task_from_payload({
        "ok": True,
        "requestId": "req_unit",
        "data": {
            "id": "t_v1_success",
            "state": "succeeded",
            "status": "internal_done",
            "channel": "kakao",
            "charged": True,
            "cdkConsumed": 0,
            "canRelease": False,
            "display": {"description": "Plus 已核验成功", "action": "done"},
        },
    })

    assert task.id == "t_v1_success"
    assert task.status == "succeeded"
    assert task.success is True
    assert task.done is True
    assert task.channel == "kakao"
    assert task.cdk_consumed == 1
    assert task.display_description == "Plus 已核验成功"


def test_v1_client_uses_customer_paths_and_error_envelope() -> None:
    class Response:
        def __init__(self, status_code: int, payload: dict[str, object], headers: dict[str, str] | None = None):
            self.status_code = status_code
            self._payload = payload
            self.headers = headers or {}
            self.content = b"{}"

        def json(self):
            return self._payload

    calls: list[tuple[str, str]] = []
    client = UpiActivationClient("actk_test_local", base_url="https://upi.example")

    def request(method, url, **_kwargs):
        calls.append((method, url))
        return Response(200, {"ok": True, "data": {"id": "t_v1", "state": "processing"}})

    client.session.request = request  # type: ignore[method-assign]
    assert client.submit_task("access", idempotency_key="order-v1-0001").id == "t_v1"
    assert client.get_task("t_v1").status == "processing"
    client.release_task("t_v1")
    assert [url for _method, url in calls] == [
        "https://upi.example/api/v1/customer/activation/tasks",
        "https://upi.example/api/v1/customer/activation/tasks/t_v1",
        "https://upi.example/api/v1/customer/activation/tasks/t_v1/release",
    ]

    def error_request(method, url, **_kwargs):
        return Response(422, {"ok": False, "error": {"code": "channel_disabled", "message": "渠道 IDEAL 已关闭", "retryable": False}})

    client.session.request = error_request  # type: ignore[method-assign]
    with pytest.raises(UpiActivationError) as caught:
        client.submit_task("access", channel="ideal", idempotency_key="order-v1-0002")
    assert str(caught.value) == "渠道 IDEAL 已关闭"
    assert caught.value.payload["error"]["code"] == "channel_disabled"


def test_v1_error_retryable_flag_overrides_http_status(tmp_path: Path) -> None:
    _repo, _accounts, service = _service_for(tmp_path)
    retryable = UpiActivationError("temporarily busy", status_code=503, payload={"error": {"code": "submit_temporarily_busy", "retryable": True}})
    disabled = UpiActivationError("渠道 IDEAL 已关闭", status_code=503, payload={"error": {"code": "channel_disabled", "retryable": False}})

    assert service._is_retryable(retryable) is True
    assert service._is_retryable(disabled) is False


def test_token_bucket_rate() -> None:
    bucket = TokenBucket(rate_per_minute=5, capacity=1)
    assert bucket.try_acquire() == 0.0
    wait = bucket.try_acquire()
    assert wait > 0


def test_enqueue_without_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "upi.db"
    accounts = AccountsRepository(db_path)
    accounts.upsert(
        {
            "account_key": "alice@example.com",
            "email": "alice@example.com",
            "plus_status": "needs_plus",
            "access_token": "tok-1",
        }
    )
    cfg = ConfigService()
    monkeypatch.setattr(
        cfg,
        "merged_config",
        lambda: {"upi_activation_enabled": True, "upi_client_key": "", "upi_client_keys": []},
    )
    # force empty key via db overrides empty
    svc = UpiActivationService(accounts=AccountsService(repo=accounts, config_service=cfg), config_service=cfg)
    monkeypatch.setattr(svc, "ensure_worker", lambda: None)
    monkeypatch.setattr(svc, "wake", lambda: None)
    result = svc.enqueue_accounts(["alice@example.com"], channel="pix")
    assert result["queued"] == 0
    assert result["failed"] >= 1
    assert "未配置" in str(result.get("message") or "")


def test_enqueue_marks_queued_with_fake_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "upi2.db"
    accounts_repo = AccountsRepository(db_path)
    accounts_repo.upsert(
        {
            "account_key": "bob@example.com",
            "email": "bob@example.com",
            "plus_status": "free",
            "access_token": "access-token-bob",
        }
    )

    class FakeConfig(ConfigService):
        def merged_config(self):  # type: ignore[override]
            return {
                "upi_activation_enabled": True,
                "upi_client_key": "actk_test_key_for_unit",
                "upi_default_channel": "upi",
                "upi_base_url": "https://upi.akkkkk.top",
                "upi_device_id": "gpt-register",
                "upi_submit_per_key_per_min": 5,
                "upi_poll_interval_sec": 5,
                "upi_poll_timeout_sec": 1800,
                "upi_auto_verify_plus": False,
            }

    cfg = FakeConfig()
    accounts = AccountsService(repo=accounts_repo, config_service=cfg)
    svc = UpiActivationService(accounts=accounts, config_service=cfg)
    # prevent background threads doing real HTTP in unit test process noise
    monkeypatch.setattr(svc, "ensure_worker", lambda: None)
    monkeypatch.setattr(svc, "wake", lambda: None)

    result = svc.enqueue_accounts(["bob@example.com"], channel="ideal")
    assert result["queued"] == 1
    assert result["channel"] == "ideal"
    saved = accounts_repo.get("bob@example.com").to_dict()
    assert saved.get("activation_status") == "queued"
    assert saved.get("activation_channel") == "ideal"
    assert saved.get("activation_provider") == "upi"
    assert str(saved.get("activation_idempotency_key") or "").startswith("upi-")


class _ActivationConfig(ConfigService):
    def __init__(self, **overrides):
        self.values = {
            "upi_activation_enabled": True,
            "upi_client_key": "actk_test_one",
            "upi_client_keys": ["actk_test_one", "actk_test_two"],
            "upi_default_channel": "upi",
            "upi_base_url": "http://unit.test",
            "upi_device_id": "pytest",
            "upi_submit_per_key_per_min": 5,
            "upi_poll_interval_sec": 1,
            "upi_poll_timeout_sec": 2,
            "upi_auto_verify_plus": False,
        }
        self.values.update(overrides)

    def merged_config(self):  # type: ignore[override]
        return dict(self.values)


def _service_for(tmp_path: Path, cfg: ConfigService | None = None):
    repo = AccountsRepository(tmp_path / "activation.db")
    config = cfg or _ActivationConfig()
    accounts = AccountsService(repo=repo, config_service=config)
    service = UpiActivationService(accounts=accounts, config_service=config)
    service.ensure_worker = lambda: None
    service.wake = lambda: None
    return repo, accounts, service


def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def test_fresh_schema_has_auxiliary_timestamps_and_activation_key_hash(tmp_path: Path) -> None:
    repo = AccountsRepository(tmp_path / "fresh.db")
    with db.connect(repo.db_path) as conn:  # type: ignore[arg-type]
        for table in ("account_proxy", "account_artifacts", "sms_activations"):
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            assert {"created_at", "updated_at"} <= columns
        account_columns = {row["name"] for row in conn.execute("PRAGMA table_info(accounts)")}
        assert "activation_client_key_hash" in account_columns
        assert "activation_submission_claim" in account_columns


def test_activation_save_preserves_email_fields(tmp_path: Path) -> None:
    repo, _accounts, service = _service_for(tmp_path)
    repo.upsert({
        "account_key": "email-preserve@example.com",
        "email": "login@example.com",
        "billing_email": "billing@example.com",
        "codex_email": "codex@example.com",
        "access_token": "access-preserve",
    })
    service._save_account({
        "account_key": "email-preserve@example.com",
        "activation_provider": "upi",
        "activation_status": "queued",
    })
    account = repo.get("email-preserve@example.com").to_dict()
    assert account["billing_email"] == "billing@example.com"
    assert account["codex_email"] == "codex@example.com"


def test_enqueue_disabled_is_explicitly_rejected(tmp_path: Path) -> None:
    cfg = _ActivationConfig(upi_activation_enabled="false")
    repo, _accounts, service = _service_for(tmp_path, cfg)
    repo.upsert({"account_key": "disabled@example.com", "access_token": "access-disabled"})
    result = service.enqueue_accounts(["disabled@example.com"])
    assert result["ok"] is False
    assert result["queued"] == 0
    assert "禁用" in result["message"]


@pytest.mark.parametrize("activation_status", ["active", "success", "verified", "replace_account", "verified_plus"])
def test_force_cannot_reenqueue_dangerous_statuses(tmp_path: Path, activation_status: str) -> None:
    repo, _accounts, service = _service_for(tmp_path)
    key = f"danger-{activation_status}@example.com"
    repo.upsert({"account_key": key, "access_token": "access-danger", "activation_status": activation_status})
    result = service.enqueue_accounts([key], force=True)
    assert result["queued"] == 0
    assert result["failed"] == 1
    assert "禁止" in result["results"][0]["message"]


def test_remote_success_is_terminal_without_local_plus_verification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _ActivationConfig(upi_auto_verify_plus=True)
    repo, accounts, service = _service_for(tmp_path, cfg)
    key = "state-machine@example.com"
    repo.upsert({"account_key": key, "access_token": "access-state"})
    assert service.enqueue_accounts([key])["queued"] == 1

    class FakeClient:
        def __init__(self, _key):
            self.calls = 0

        def submit_task(self, _token, **kwargs):
            return UpiTask(id="remote-state", status="submitted", channel=kwargs["channel"], cdk_consumed=0)

        def get_task(self, _task_id):
            self.calls += 1
            if self.calls == 1:
                return UpiTask(id="remote-state", status="processing", cdk_consumed=0)
            return UpiTask(id="remote-state", status="success", cdk_consumed=1)

    clients: dict[str, FakeClient] = {}

    def make_client(client_key, _cfg):
        clients.setdefault(client_key, FakeClient(client_key))
        return clients[client_key]

    def fail_verify(*_args, **_kwargs):
        raise AssertionError("remote success must not trigger local Plus verification")

    monkeypatch.setattr(service, "_client", make_client)
    monkeypatch.setattr(accounts, "verify_plus_batch", fail_verify)
    monkeypatch.setattr(accounts, "verify_plus", fail_verify)
    service._submit_once()
    _wait_until(lambda: repo.get(key).to_dict()["activation_status"] == "submitted")
    service._poll_once()
    assert repo.get(key).to_dict()["activation_status"] == "processing"
    service._poll_once()
    saved = repo.get(key).to_dict()
    assert saved["activation_status"] == "success"
    assert saved["plan_type"] == "plus"
    assert saved["plus_status"] == "verified_plus"
    assert saved["plus_check_source"] == "upi_activation"
    assert saved["stage"] == "plus_verified_needs_oauth"
    assert saved["binding_status"] == "pending"
    assert service._verify_once() is False


def test_async_enqueue_is_visible_before_response_returns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, _accounts, service = _service_for(tmp_path)
    wake_calls = 0

    def wake():
        nonlocal wake_calls
        wake_calls += 1

    monkeypatch.setattr(service, "wake", wake)
    keys = []
    for index in range(3):
        key = f"bulk-visible-{index}@example.com"
        keys.append(key)
        repo.upsert({"account_key": key, "access_token": f"access-{index}"})

    result = service.enqueue_accounts_async(keys, channel="upi")
    assert result["async"] is True
    assert result["queued"] == 3
    assert result["failed"] == 0
    assert wake_calls == 1
    for key in keys:
        saved = repo.get(key).to_dict()
        assert saved["activation_status"] == "queued"
        assert saved["activation_channel"] == "upi"
        assert str(saved.get("activation_idempotency_key") or "").startswith("upi-")



def test_transient_submit_marks_unknown_then_recovers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Single key so multi-key 404 probing does not inflate idempotency call counts.
    cfg = _ActivationConfig(upi_client_keys=["actk_test_one"], upi_client_key="actk_test_one")
    repo, _accounts, service = _service_for(tmp_path, cfg)
    key = "unknown-recover@example.com"
    repo.upsert({"account_key": key, "access_token": "access-unknown"})
    assert service.enqueue_accounts([key])["queued"] == 1
    idem = str(repo.get(key).to_dict().get("activation_idempotency_key") or "")
    assert idem

    class FlakyClient:
        def __init__(self, _key: str):
            self.submit_calls = 0
            self.idem_calls = 0

        def submit_task(self, _token, **_kwargs):
            self.submit_calls += 1
            raise UpiActivationError("submit temporarily busy", status_code=503, retry_after=2.0)

        def get_task_by_idempotency(self, idempotency_key: str):
            self.idem_calls += 1
            assert idempotency_key == idem
            # First recover attempt (POST aftermath): not created yet.
            if self.idem_calls <= 1:
                raise UpiActivationError("not ready", status_code=404)
            return UpiTask(id="recovered-task", status="processing", channel="pix", cdk_consumed=0)

        def get_task(self, task_id: str):
            return UpiTask(id=task_id, status="success", cdk_consumed=1)

    client = FlakyClient("actk_test_one")
    monkeypatch.setattr(service, "_client", lambda _client_key, _cfg: client)

    # POST 503 + idempotency 404 → submit_unknown (not business failure).
    service._submit_once()
    _wait_until(lambda: repo.get(key).to_dict()["activation_status"] == "submit_unknown")
    account = repo.get(key).to_dict()
    assert not account.get("activation_task_id")
    assert account["activation_idempotency_key"] == idem
    assert "待确认" in str(account.get("activation_display") or "")

    # Next loop: recover finds the remote task and resumes normal flow.
    service._submit_once()
    _wait_until(lambda: bool(repo.get(key).to_dict().get("activation_task_id")))
    account = repo.get(key).to_dict()
    assert client.submit_calls == 1
    assert client.idem_calls >= 2


def test_closed_channel_submit_fails_without_unknown_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _ActivationConfig(upi_client_keys=["actk_test_one"], upi_client_key="actk_test_one", upi_submit_per_key_per_min=50)
    repo, _accounts, service = _service_for(tmp_path, cfg)
    key = "closed-channel@example.com"
    repo.upsert({
        "account_key": key,
        "access_token": "access-closed-channel",
        "activation_provider": "upi",
        "activation_status": "queued",
        "activation_channel": "ideal",
        "activation_idempotency_key": "upi-closed-channel-a1",
    })

    class ClosedChannelClient:
        def submit_task(self, *_args, **_kwargs):
            raise UpiActivationError("渠道 IDEAL 已关闭，暂时不可提交。请稍后再试或联系售后。", status_code=503)

        def get_task_by_idempotency(self, *_args, **_kwargs):
            raise AssertionError("closed channel failures must not enter idempotency recovery")

    monkeypatch.setattr(service, "_client", lambda _client_key, _cfg: ClosedChannelClient())

    service._submit_once()
    _wait_until(lambda: repo.get(key).to_dict()["activation_status"] == "failed")
    account = repo.get(key).to_dict()
    assert account["activation_error"] == "渠道 IDEAL 已关闭，暂时不可提交。请稍后再试或联系售后。"
    assert account["activation_display"] == account["activation_error"]
    assert account["activation_finished_at"]

def test_submit_dispatches_many_concurrent_posts_per_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from services.upi_activation_service import MAX_SUBMISSIONS_PER_KEY

    cfg = _ActivationConfig(
        upi_client_key="actk_test_one",
        upi_client_keys=["actk_test_one"],
        upi_submit_per_key_per_min=50,
    )
    repo, _accounts, service = _service_for(tmp_path, cfg)
    # One extra queued row proves the per-key in-flight ceiling is enforced.
    keys = [f"parallel-{index}@example.com" for index in range(MAX_SUBMISSIONS_PER_KEY + 1)]
    for index, key in enumerate(keys):
        repo.upsert({"account_key": key, "access_token": f"access-{index}"})
        assert service.enqueue_accounts([key])["queued"] == 1

    entered_cap = threading.Event()
    release_posts = threading.Event()
    counts_lock = threading.Lock()
    active = 0
    max_active = 0

    class DelayedClient:
        def submit_task(self, token: str, **kwargs):
            nonlocal active, max_active
            with counts_lock:
                active += 1
                max_active = max(max_active, active)
                if active >= MAX_SUBMISSIONS_PER_KEY:
                    entered_cap.set()
            assert release_posts.wait(timeout=3.0)
            with counts_lock:
                active -= 1
            return UpiTask(id=f"task-{token}", status="submitted", channel=kwargs["channel"], cdk_consumed=0)

    monkeypatch.setattr(service, "_client", lambda _client_key, _cfg: DelayedClient())
    service._submit_once()
    assert entered_cap.wait(timeout=2.0)
    with counts_lock:
        assert active >= max(2, MAX_SUBMISSIONS_PER_KEY - 2)
        assert max_active == MAX_SUBMISSIONS_PER_KEY
    release_posts.set()
    _wait_until(
        lambda: sum(repo.get(key).to_dict()["activation_status"] == "submitted" for key in keys) == MAX_SUBMISSIONS_PER_KEY
    )
    service._submit_once()
    _wait_until(lambda: all(repo.get(key).to_dict()["activation_status"] == "submitted" for key in keys))
    with counts_lock:
        assert max_active == MAX_SUBMISSIONS_PER_KEY


def test_persisted_submitting_recovers_by_idempotency_without_post(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, _accounts, service = _service_for(tmp_path)
    key = "restart-submitting@example.com"
    idem = "upi-restart-submitting-a1"
    repo.upsert(
        {
            "account_key": key,
            "access_token": "access-restart-submitting",
            "activation_provider": "upi",
            "activation_status": "submitting",
            "activation_submission_claim": "persisted-claim",
            "activation_idempotency_key": idem,
        }
    )

    class RecoveringClient:
        def __init__(self):
            self.lookup_calls = 0
            self.post_calls = 0

        def get_task_by_idempotency(self, idempotency_key: str):
            self.lookup_calls += 1
            assert idempotency_key == idem
            return UpiTask(id="recovered-submitting-task", status="submitted", channel="upi", cdk_consumed=0)

        def submit_task(self, *_args, **_kwargs):
            self.post_calls += 1
            raise AssertionError("persisted submitting must recover before POST")

    client = RecoveringClient()
    monkeypatch.setattr(service, "_client", lambda _client_key, _cfg: client)
    service._submit_once()
    account = repo.get(key).to_dict()
    assert account["activation_status"] == "submitted"
    assert account["activation_task_id"] == "recovered-submitting-task"
    assert client.lookup_calls == 1
    assert client.post_calls == 0


def test_atomic_queued_submission_claim_has_one_winner(tmp_path: Path) -> None:
    repo = AccountsRepository(tmp_path / "claim-race.db")
    key = "claim-race@example.com"
    repo.upsert({"account_key": key, "activation_provider": "upi", "activation_status": "queued"})
    barrier = threading.Barrier(3)
    winners: list[bool] = []
    winners_lock = threading.Lock()

    def claim(index: int) -> None:
        barrier.wait()
        claimed = repo.claim_queued_activation_submission(key, f"claim-{index}", "test-key-hash")
        with winners_lock:
            winners.append(bool(claimed))

    workers = [threading.Thread(target=claim, args=(index,)) for index in range(2)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(timeout=2.0)
        assert not worker.is_alive()
    assert winners.count(True) == 1
    assert repo.get(key).to_dict()["activation_status"] == "submitting"


def test_poll_loop_keeps_configured_interval_when_progressing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _repo, _accounts, service = _service_for(tmp_path, _ActivationConfig(upi_poll_interval_sec=7))
    waits: list[float] = []

    class StopAfterOneWait:
        def __init__(self) -> None:
            self.stopped = False

        def is_set(self) -> bool:
            return self.stopped

        def wait(self, timeout: float) -> bool:
            waits.append(timeout)
            self.stopped = True
            return True

    service._stop = StopAfterOneWait()  # type: ignore[assignment]
    monkeypatch.setattr(service, "_poll_once", lambda: True)
    monkeypatch.setattr(service, "_verify_once", lambda: False)
    service._poll_loop()
    assert waits == [7.0]

def test_success_ignores_cdk_consumed_and_finishes_locally(tmp_path: Path) -> None:
    cfg = _ActivationConfig(upi_auto_verify_plus=True)
    repo, _accounts, service = _service_for(tmp_path, cfg)
    account = {
        "account_key": "consumed-zero@example.com",
        "activation_status": "processing",
        "activation_task_id": "task-zero",
    }
    state = service._apply_task_result(
        account,
        UpiTask(id="task-zero", status="success", cdk_consumed=0),
        cfg=service._runtime_config(),
    )
    assert state == "success"
    assert account["activation_status"] == "success"
    assert account["activation_error"] == ""
    assert account["plan_type"] == "plus"
    assert account["plus_status"] == "verified_plus"
    assert account["plus_check_source"] == "upi_activation"
    assert account["stage"] == "plus_verified_needs_oauth"
    assert account["binding_status"] == "pending"


def test_plus_verify_proxy_candidates_use_seed_sid_sessions(tmp_path: Path) -> None:
    db_path = tmp_path / "seed-proxy.db"
    resources = ResourcePoolRepository(db_path)
    resources.upsert(
        "proxy",
        "proxy_seed",
        "seeduser:seedpass@as.proxy001.com:7878",
        {
            "kind": "proxy_seed",
            "account": "seeduser",
            "password": "seedpass",
            "host": "as.proxy001.com",
            "port": 7878,
            "style": "kookeey",
            "protocol": "socks5",
        },
    )
    service = AccountsService(AccountsRepository(db_path), ConfigService())

    first = service._pool_proxy_candidates(proxy_region="JP", limit=1)[0]
    second = service._pool_proxy_candidates(proxy_region="JP", limit=1)[0]

    assert first.startswith("socks5://seeduser_custom_zone_JP_sid_")
    assert "_time_30:seedpass@as.proxy001.com:7878" in first
    assert second.startswith("socks5://seeduser_custom_zone_JP_sid_")
    assert first != second


def test_replace_account_auto_releases_when_can_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, _accounts, service = _service_for(tmp_path)
    account = {
        "account_key": "replace-auto@example.com",
        "activation_status": "processing",
        "activation_task_id": "task-replace",
        "activation_client_key_hash": "",
    }

    class FakeClient:
        def __init__(self, _key: str):
            self.released = False

        def release_task(self, task_id: str):
            self.released = True
            assert task_id == "task-replace"
            return {"ok": True}

    client = FakeClient("actk_test_one")
    monkeypatch.setattr(service, "_client", lambda _client_key, _cfg: client)
    state = service._apply_task_result(
        account,
        UpiTask(
            id="task-replace",
            status="failed",
            display_action="replace_account",
            display_description="支付风控拒绝",
            can_release=True,
            cdk_consumed=0,
        ),
        cfg=service._runtime_config(),
    )
    assert client.released is True
    assert state == "released"
    assert account["activation_status"] == "released"
    assert account["activation_can_release"] == 0
    assert account.get("activation_display") in ("", None)


def test_cancel_submit_unknown_without_task_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, _accounts, service = _service_for(tmp_path)
    key = "cancel-unknown@example.com"
    repo.upsert({
        "account_key": key,
        "access_token": "access-cancel-unknown",
        "activation_provider": "upi",
        "activation_status": "submit_unknown",
        "activation_task_id": "",
        "activation_error": "UPI 请求失败: ('Connection aborted.', ConnectionResetError(10054))",
    })
    monkeypatch.setattr(
        service,
        "_recover_by_idempotency",
        lambda _account, _cfg: (None, "actk_test_one", "幂等查询未找到任务", True),
    )
    payload, status_code = service.release_account(key)
    assert status_code == 200
    assert payload["ok"] is True
    account = repo.get(key).to_dict()
    assert account["activation_status"] == "cancelled"
    assert "取消" in str(account.get("activation_error") or "")


def test_network_error_message_is_sanitized() -> None:
    from platforms.chatgpt.upi_activation_client import _network_error_message
    from services.upi_activation_service import _safe_message

    raw = "('Connection aborted.', ConnectionResetError(10054, '远程主机强迫关闭了一个现有的连接。', None, 10054, None))"
    msg = _network_error_message(Exception(raw))
    assert "10054" not in msg
    assert "Connection aborted" not in msg
    assert "重置" in msg or "重试" in msg
    cleaned = _safe_message(f"UPI 请求失败: {raw}")
    assert "10054" not in cleaned
    assert "重置" in cleaned or "重试" in cleaned


def test_explicit_release_cancels_local_queued_task(tmp_path: Path) -> None:
    repo, _accounts, service = _service_for(tmp_path)
    key = "cancel-queued@example.com"
    repo.upsert({"account_key": key, "access_token": "access-cancel", "activation_provider": "upi", "activation_status": "queued"})
    payload, status_code = service.release_account(key)
    assert status_code == 200
    assert payload["ok"] is True
    assert repo.get(key).to_dict()["activation_status"] == "cancelled"

def test_release_accounts_batch_cancels_local_queued(tmp_path: Path) -> None:
    repo, _accounts, service = _service_for(tmp_path)
    keys = ["batch-a@example.com", "batch-b@example.com", "batch-skip@example.com"]
    for key in keys[:2]:
        repo.upsert({
            "account_key": key,
            "access_token": f"access-{key}",
            "activation_provider": "upi",
            "activation_status": "queued",
        })
    # No activation status — should fail per-key, not abort the batch.
    repo.upsert({"account_key": keys[2], "access_token": "access-skip"})
    result = service.release_accounts(keys)
    assert result["released"] == 2
    assert result["failed"] == 1
    assert result["ok"] is False  # partial
    assert repo.get(keys[0]).to_dict()["activation_status"] == "cancelled"
    assert repo.get(keys[1]).to_dict()["activation_status"] == "cancelled"



def test_multi_key_poll_prefers_saved_hash_and_probes_401(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _ActivationConfig()
    repo, _accounts, service = _service_for(tmp_path, cfg)
    key = "multi-key@example.com"
    saved_hash = hashlib.sha256(b"actk_missing").hexdigest()
    repo.upsert({
        "account_key": key,
        "access_token": "access-multi",
        "activation_provider": "upi",
        "activation_status": "processing",
        "activation_task_id": "task-multi",
        "activation_client_key_hash": saved_hash,
    })
    calls: list[str] = []

    class FakeClient:
        def __init__(self, client_key):
            self.client_key = client_key

        def get_task(self, _task_id):
            calls.append(self.client_key)
            if self.client_key == "actk_test_one":
                raise UpiActivationError("wrong key", status_code=401)
            return UpiTask(id="task-multi", status="success", cdk_consumed=1)

    monkeypatch.setattr(service, "_client", lambda client_key, _cfg: FakeClient(client_key))
    service._poll_once()
    assert calls[:2] == ["actk_test_one", "actk_test_two"]
    assert repo.get(key).to_dict()["activation_status"] == "success"
    assert repo.get(key).to_dict()["activation_client_key_hash"] == hashlib.sha256(b"actk_test_two").hexdigest()


def test_verifying_queue_recovers_after_new_service_instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _ActivationConfig(upi_auto_verify_plus=True)
    repo, accounts, service = _service_for(tmp_path, cfg)
    key = "restart-verifying@example.com"
    repo.upsert({"account_key": key, "access_token": "access-restart", "activation_provider": "upi", "activation_status": "verifying"})
    restarted = UpiActivationService(accounts=accounts, config_service=cfg)
    restarted.ensure_worker = lambda: None
    monkeypatch.setattr(accounts, "verify_plus", lambda _key, **_kwargs: ({"ok": True, "paid": True}, 200))
    monkeypatch.setattr(
        accounts,
        "verify_plus_batch",
        lambda keys, **_kwargs: {
            "ok": True,
            "results": [{"key": value, "ok": True, "paid": True, "status_code": 200} for value in keys],
        },
    )
    assert restarted._verify_once() is True
    assert repo.get(key).to_dict()["activation_status"] == "verified"


def test_config_returns_upi_keys_in_plain() -> None:
    from application.config_service import mask_value

    assert mask_value("upi_submit_per_key_per_min", 5) == 5
    assert mask_value("upi_client_key", "actk_test") == "actk_test"



def test_activation_public_config_does_not_expose_client_keys(tmp_path: Path) -> None:
    _repo, _accounts, service = _service_for(tmp_path)

    public_status = service.config_status()
    stats = service.queue_stats()
    runtime_status = service._runtime_config()

    assert public_status["has_key"] is True
    assert public_status["key_count"] == 2
    assert public_status["key_prefixes"] == ["actk_tes…", "actk_tes…"]
    assert "client_keys" not in public_status
    assert "client_keys" not in stats["config"]
    assert runtime_status["client_keys"] == ["actk_test_one", "actk_test_two"]
    assert runtime_status["keys"] == ["actk_test_one", "actk_test_two"]


def test_activate_plus_request_defaults_to_upi_channel() -> None:
    assert accounts_api.ActivatePlusBatchRequest().channel == "upi"
    assert accounts_api.ActivationTasksRetryRequest().channel == "upi"

def test_list_activation_tasks_returns_public_rows_and_stats(tmp_path: Path) -> None:
    repo, _accounts, service = _service_for(tmp_path)
    repo.upsert({
        "account_key": "progress@example.com",
        "email": "progress@example.com",
        "password": "secret-password",
        "access_token": "access-progress",
        "activation_provider": "upi",
        "activation_status": "processing",
        "activation_task_id": "task-progress",
    })
    repo.upsert({"account_key": "idle@example.com", "activation_status": "idle"})

    result = service.list_activation_tasks(statuses=["processing"])

    assert result["ok"] is True
    assert result["total"] == 1
    assert result["items"][0]["account_key"] == "progress@example.com"
    assert result["items"][0]["activation_task_id"] == "task-progress"
    assert "password" not in result["items"][0]
    assert "access_token" not in result["items"][0]
    assert result["stats"]["active"] == 1


def test_refresh_activation_tasks_polls_and_updates_public_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, _accounts, service = _service_for(tmp_path)
    repo.upsert({
        "account_key": "refresh@example.com",
        "access_token": "access-refresh",
        "activation_provider": "upi",
        "activation_status": "processing",
        "activation_task_id": "task-refresh",
    })

    monkeypatch.setattr(
        service,
        "_poll_remote",
        lambda _snapshot, _cfg: (UpiTask(id="task-refresh", status="success", cdk_consumed=1), "actk_test_one", "", False),
    )
    result = service.refresh_activation_tasks(["refresh@example.com"])

    assert result["ok"] is True
    assert result["checked"] == 1
    assert result["updated"] == 1
    assert result["items"][0]["activation_status"] == "success"
    assert "access_token" not in result["items"][0]
    assert repo.get("refresh@example.com").to_dict()["activation_status"] == "success"


def test_retry_activation_tasks_requeues_terminal_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, _accounts, service = _service_for(tmp_path)
    repo.upsert({
        "account_key": "retry-progress@example.com",
        "access_token": "access-retry-progress",
        "activation_provider": "upi",
        "activation_status": "failed",
        "activation_error": "old failure",
    })
    monkeypatch.setattr(service, "ensure_worker", lambda: None)
    monkeypatch.setattr(service, "wake", lambda: None)

    result = service.retry_activation_tasks(statuses=["failed"], channel="upi")

    assert result["ok"] is True
    assert result["accepted"] == 1
    assert repo.get("retry-progress@example.com").to_dict()["activation_status"] == "queued"


def test_account_list_summarizes_tokens_but_detail_returns_raw_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    account = {
        "account_key": "token-detail@example.com",
        "password": "secret-password",
        "cpa_auth_file_json": '{"access_token":"should-not-list"}',
        "tokens": {"access_token": "access-detail", "refresh_token": "", "id_token": "id-detail"},
    }
    listed = accounts_api._account_list_item(account)
    assert listed["tokens"] == {"access_token": True, "refresh_token": False, "id_token": True}
    assert listed["has_password"] is True
    assert "password" not in listed
    assert "cpa_auth_file_json" not in listed

    monkeypatch.setattr(accounts_api.account_store, "get_account", lambda _key: account)
    detailed = asyncio.run(accounts_api.reveal_tokens("token-detail@example.com"))
    assert detailed["tokens"] == account["tokens"]
    assert detailed["tokens"] is not account["tokens"]


def test_recover_by_idempotency_probes_all_keys_after_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _ActivationConfig()
    repo, _accounts, service = _service_for(tmp_path, cfg)
    key = "fallback-recover@example.com"
    idem = "upi-fallback-recover-a1"
    original = "actk_test_one"
    fallback = "actk_test_two"
    repo.upsert(
        {
            "account_key": key,
            "access_token": "access-fallback",
            "activation_provider": "upi",
            "activation_status": "submitting",
            "activation_submission_claim": "claim-fallback",
            "activation_idempotency_key": idem,
            "activation_client_key_hash": hashlib.sha256(original.encode("utf-8")).hexdigest(),
        }
    )
    lookups: list[str] = []

    class MultiKeyClient:
        def __init__(self, client_key: str):
            self.client_key = client_key

        def get_task_by_idempotency(self, idempotency_key: str):
            lookups.append(self.client_key)
            assert idempotency_key == idem
            if self.client_key == original:
                raise UpiActivationError("not found", status_code=404)
            return UpiTask(id="task-on-fallback", status="submitted", channel="upi")

        def submit_task(self, *_args, **_kwargs):
            raise AssertionError("must recover via multi-key lookup, not POST")

    monkeypatch.setattr(service, "_client", lambda client_key, _cfg: MultiKeyClient(client_key))
    service._submit_once()
    account = repo.get(key).to_dict()
    assert account["activation_status"] == "submitted"
    assert account["activation_task_id"] == "task-on-fallback"
    assert original in lookups and fallback in lookups
    assert lookups.index(original) < lookups.index(fallback)


def test_release_account_blocks_cancel_while_submitting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, _accounts, service = _service_for(tmp_path)
    key = "cancel-submitting@example.com"
    repo.upsert(
        {
            "account_key": key,
            "access_token": "access-cancel-submitting",
            "activation_provider": "upi",
            "activation_status": "submitting",
            "activation_submission_claim": "claim-live",
            "activation_idempotency_key": "upi-cancel-submitting-a1",
        }
    )

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("submitting cancel must not probe idempotency for terminal cancel")

    monkeypatch.setattr(service, "_recover_by_idempotency", fail_if_called)
    payload, status_code = service.release_account(key)
    assert status_code == 409
    assert payload["ok"] is False
    assert repo.get(key).to_dict()["activation_status"] == "submitting"


def test_submit_honors_429_retry_after_hold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _ActivationConfig(upi_client_keys=["actk_test_one"], upi_client_key="actk_test_one", upi_submit_per_key_per_min=50)
    repo, _accounts, service = _service_for(tmp_path, cfg)
    key = "retry-after@example.com"
    repo.upsert(
        {
            "account_key": key,
            "access_token": "access-retry-after",
            "activation_provider": "upi",
            "activation_status": "queued",
            "activation_idempotency_key": "upi-retry-after-a1",
        }
    )
    post_calls = {"n": 0}

    class RateLimitedClient:
        def submit_task(self, *_args, **_kwargs):
            post_calls["n"] += 1
            raise UpiActivationError("rate limited", status_code=429, retry_after=30.0)

        def get_task_by_idempotency(self, *_args, **_kwargs):
            raise UpiActivationError("not found", status_code=404)

    monkeypatch.setattr(service, "_client", lambda _client_key, _cfg: RateLimitedClient())
    service._submit_once()
    _wait_until(lambda: repo.get(key).to_dict()["activation_status"] in {"submit_unknown", "queued", "submitting"})
    first_posts = post_calls["n"]
    assert first_posts >= 1
    # Hold is process-local; reserve must refuse further POST tokens for this key.
    assert service._key_on_hold("actk_test_one") is True
    assert service._reserve_submission_dispatch("actk_test_one", 50) is None
    # Requeue path still cannot burn another POST while hold is active.
    if repo.get(key).to_dict()["activation_status"] == "submit_unknown":
        # Force requeue then attempt another dispatch cycle.
        row = repo.get(key).to_dict()
        row["activation_status"] = "queued"
        row["activation_submission_claim"] = ""
        repo.upsert(row)
    service._submit_once()
    assert post_calls["n"] == first_posts


def test_postgres_init_applies_full_activation_column_migration() -> None:
    """Source contract: non-sqlite init must ADD every activation column, not only claim."""
    import inspect
    from infrastructure import db as db_mod

    source = inspect.getsource(db_mod.init_db)
    assert "if backend != \"sqlite\":" in source or "if backend != 'sqlite':" in source
    for column in (
        "activation_provider",
        "activation_status",
        "activation_task_id",
        "activation_idempotency_key",
        "activation_submission_claim",
        "activation_client_key_hash",
        "activation_channel",
        "activation_error",
        "activation_display",
        "activation_can_release",
        "activation_cdk_consumed",
        "activation_submitted_at",
        "activation_finished_at",
        "activation_updated_at",
    ):
        assert column in source
    assert "ADD COLUMN IF NOT EXISTS" in source



def test_poll_timeout_marks_failed(tmp_path: Path) -> None:
    from datetime import datetime, timedelta, timezone

    cfg = _ActivationConfig(upi_poll_timeout_sec=1)
    repo, _accounts, service = _service_for(tmp_path, cfg)
    key = "timeout@example.com"
    submitted_at = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    repo.upsert(
        {
            "account_key": key,
            "access_token": "access-timeout",
            "activation_provider": "upi",
            "activation_status": "processing",
            "activation_task_id": "task-timeout",
            "activation_submitted_at": submitted_at,
        }
    )
    assert service._poll_once() is True
    account = repo.get(key).to_dict()
    assert account["activation_status"] == "failed"
    assert "轮询超过" in str(account.get("activation_error") or "")

def test_account_list_includes_public_plus_batch_markers() -> None:
    listed = accounts_api._account_list_item({
        "account_key": "batch-marker@example.com",
        "active_plus_batch_id": 12,
        "active_plus_batch_key": "plus_batch_unit",
        "active_plus_item_id": 34,
        "plus_batch_status": "queued",
        "plus_archived_at": "2026-07-22T00:00:00",
        "plus_export_key": "plus_export_unit",
        "access_token": "must-not-leak",
    })
    assert listed["active_plus_batch_id"] == 12
    assert listed["active_plus_batch_key"] == "plus_batch_unit"
    assert listed["active_plus_item_id"] == 34
    assert listed["plus_batch_status"] == "queued"
    assert listed["plus_archived_at"] == "2026-07-22T00:00:00"
    assert listed["plus_export_key"] == "plus_export_unit"
    assert "access_token" not in listed



def test_plus_activation_batch_schema_and_precheck_blocks_occupied(tmp_path: Path) -> None:
    db_path = tmp_path / "plus-batch.db"
    accounts = AccountsRepository(db_path)
    accounts.upsert({"account_key": "ready@example.com", "email": "ready@example.com", "access_token": "access-ready", "plus_status": "free"})
    accounts.upsert({"account_key": "plus@example.com", "email": "plus@example.com", "access_token": "access-plus", "plus_status": "verified_plus"})

    repo = PlusActivationRepository(db_path)
    with db.connect(db_path) as conn:
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        account_columns = {row["name"] for row in conn.execute("PRAGMA table_info(accounts)")}
    assert {"plus_activation_batches", "plus_activation_batch_items", "plus_activation_exports"} <= tables
    assert {"active_plus_batch_key", "active_plus_item_id", "plus_archived_at", "plus_export_key"} <= account_columns

    result = repo.create_batch_with_items(["ready@example.com", "ready@example.com", "plus@example.com", "missing@example.com"], dry_run=True)
    assert result.batch is None
    assert result.accepted_keys == ["ready@example.com"]
    assert result.skip_counts == {"duplicate_input": 1, "already_plus": 1, "not_found": 1}


def test_plus_activation_batch_create_reserves_account_and_blocks_duplicate_batch(tmp_path: Path) -> None:
    db_path = tmp_path / "plus-batch-create.db"
    accounts = AccountsRepository(db_path)
    accounts.upsert({"account_key": "batch-ready@example.com", "email": "batch-ready@example.com", "access_token": "access-ready", "plus_status": "free"})
    repo = PlusActivationRepository(db_path)

    created = repo.create_batch_with_items(["batch-ready@example.com"], name="unit batch")
    assert created.batch is not None
    assert created.batch["total_count"] == 1
    with db.connect(db_path) as conn:
        saved = dict(conn.execute("SELECT active_plus_batch_key, plus_batch_status FROM accounts WHERE account_key='batch-ready@example.com'").fetchone())
    assert saved["active_plus_batch_key"] == created.batch["batch_key"]
    assert saved["plus_batch_status"] == "queued"

    duplicate = repo.create_batch_with_items(["batch-ready@example.com"], dry_run=True)
    assert duplicate.accepted_keys == []
    assert duplicate.skip_counts == {"already_in_batch": 1}



def test_plus_activation_batch_refresh_polls_remote_active_items(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "plus-batch-refresh.db"
    accounts = AccountsRepository(db_path)
    accounts.upsert({
        "account_key": "refresh-ready@example.com",
        "email": "refresh-ready@example.com",
        "access_token": "access-refresh",
        "plus_status": "free",
    })
    repo = PlusActivationRepository(db_path)
    created = repo.create_batch_with_items(["refresh-ready@example.com"], name="refresh batch")
    batch_key = str(created.batch["batch_key"])
    saved = accounts.get("refresh-ready@example.com").to_dict()
    saved.update({
        "activation_provider": "upi",
        "activation_status": "processing",
        "activation_task_id": "task-refresh-stale",
        "activation_idempotency_key": "upi-refresh-a1",
        "activation_submitted_at": "2026-07-22T14:00:00",
    })
    accounts.upsert(saved)
    repo.sync_items_from_accounts(batch_key)

    class FakeUpiService:
        def refresh_activation_tasks(self, keys, statuses=None):
            assert keys == ["refresh-ready@example.com"]
            assert statuses == ["submitted", "processing"]
            latest = accounts.get("refresh-ready@example.com").to_dict()
            latest.update({
                "activation_status": "success",
                "plus_status": "verified_plus",
                "activation_finished_at": "2026-07-22T14:05:00",
                "activation_updated_at": "2026-07-22T14:05:00",
            })
            accounts.upsert(latest)
            return {"ok": True, "checked": 1, "updated": 1}

    import services.upi_activation_service as upi_service_mod

    monkeypatch.setattr(upi_service_mod, "get_upi_activation_service", lambda: FakeUpiService())
    result = PlusActivationBatchService(repo=repo).refresh(batch_key)

    assert result["ok"] is True
    assert result["remote_refresh"] == {"checked": 1, "updated": 1}
    assert result["batch"]["progress_percent"] == 100
    item = repo.list_items(batch_key, status="verified")["items"][0]
    assert item["remote_task_id"] == "task-refresh-stale"

def test_plus_activation_batch_show_accounts_clears_list_hiding_marker(tmp_path: Path) -> None:
    db_path = tmp_path / "plus-batch-show.db"
    accounts = AccountsRepository(db_path)
    accounts.upsert({"account_key": "show-ready@example.com", "email": "show-ready@example.com", "access_token": "access-show", "plus_status": "free"})
    repo = PlusActivationRepository(db_path)
    created = repo.create_batch_with_items(["show-ready@example.com"], name="show batch")
    batch_key = str(created.batch["batch_key"])

    result = PlusActivationBatchService(repo=repo).show_accounts_in_account_list(batch_key)

    assert result["ok"] is True
    assert result["visible"] == 1
    with db.connect(db_path) as conn:
        account = dict(conn.execute("SELECT active_plus_batch_key, active_plus_item_id, plus_batch_status FROM accounts WHERE account_key='show-ready@example.com'").fetchone())
        item = dict(conn.execute("SELECT status FROM plus_activation_batch_items WHERE batch_key=?", (batch_key,)).fetchone())
    assert account == {"active_plus_batch_key": "", "active_plus_item_id": None, "plus_batch_status": ""}
    assert item["status"] == "queued"


def test_plus_activation_batch_show_then_create_skips_active_items(tmp_path: Path) -> None:
    """Account-list show clears active_plus_batch_* but item unique index still holds.

    Creating another batch must skip those accounts instead of 500 UniqueViolation.
    """
    db_path = tmp_path / "plus-batch-show-recreate.db"
    accounts = AccountsRepository(db_path)
    accounts.upsert({
        "account_key": "show-recreate@example.com",
        "email": "show-recreate@example.com",
        "access_token": "access-show-recreate",
        "plus_status": "free",
    })
    accounts.upsert({
        "account_key": "fresh-recreate@example.com",
        "email": "fresh-recreate@example.com",
        "access_token": "access-fresh-recreate",
        "plus_status": "free",
    })
    repo = PlusActivationRepository(db_path)
    first = repo.create_batch_with_items(["show-recreate@example.com"], name="first batch")
    assert first.batch is not None
    first_key = str(first.batch["batch_key"])

    shown = PlusActivationBatchService(repo=repo).show_accounts_in_account_list(first_key)
    assert shown["ok"] is True
    assert shown["visible"] == 1
    with db.connect(db_path) as conn:
        cleared = dict(conn.execute(
            "SELECT active_plus_batch_key, plus_batch_status FROM accounts WHERE account_key='show-recreate@example.com'"
        ).fetchone())
        item = dict(conn.execute(
            "SELECT status FROM plus_activation_batch_items WHERE batch_key=?",
            (first_key,),
        ).fetchone())
    assert cleared["active_plus_batch_key"] == ""
    assert item["status"] == "queued"

    second = repo.create_batch_with_items(
        ["show-recreate@example.com", "fresh-recreate@example.com"],
        name="second batch",
    )
    assert second.accepted_keys == ["fresh-recreate@example.com"]
    assert second.skip_counts.get("already_in_batch") == 1
    assert any(item.get("key") == "show-recreate@example.com" and item.get("reason") == "already_in_batch" for item in second.skipped)
    assert second.batch is not None
    assert second.batch["total_count"] == 1

    # dry-run must also report the occupied account without creating a batch
    dry = repo.create_batch_with_items(["show-recreate@example.com"], dry_run=True)
    assert dry.accepted_keys == []
    assert dry.skip_counts == {"already_in_batch": 1}


def test_plus_activation_batch_export_archives_verified_accounts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "plus-batch-export.db"
    accounts = AccountsRepository(db_path)
    accounts.upsert({
        "account_key": "export-ready@outlook.com",
        "email": "export-ready@outlook.com",
        "password": "ChatGptPass!",
        "access_token": "access-export",
        "plus_status": "free",
    })
    resources = ResourcePoolRepository(db_path)
    resources.upsert(
        resource_type="email",
        provider="outlook_token",
        resource_key="export-ready@outlook.com",
        payload={
            "email": "export-ready@outlook.com",
            "password": "OutlookPass1",
            "client_id": "cid-export-1",
            "refresh_token": "rt-export-1",
        },
        status="available",
    )
    repo = PlusActivationRepository(db_path)
    created = repo.create_batch_with_items(["export-ready@outlook.com"], name="export batch")
    batch_key = str(created.batch["batch_key"])
    with db.connect(db_path) as conn:
        conn.execute("UPDATE plus_activation_batch_items SET status='verified' WHERE batch_key=?", (batch_key,))
        conn.execute("UPDATE accounts SET plus_status='verified_plus' WHERE account_key='export-ready@outlook.com'")
    service = PlusActivationBatchService(repo=repo)
    import services.plus_activation_batch_service as batch_service_mod

    monkeypatch.setattr(batch_service_mod, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(batch_service_mod, "EXPORT_ROOT", tmp_path / "plus_exports")

    result = service.export_plus(batch_key, fmt="txt", archive_after_export=True)
    assert result["ok"] is True
    assert result["count"] == 1
    expected = "export-ready@outlook.com----OutlookPass1----cid-export-1----rt-export-1"
    assert result.get("text", "").strip() == expected
    exported_file = tmp_path / str(result["export"]["file_path"])
    assert exported_file.read_text(encoding="utf-8").strip() == expected
    with db.connect(db_path) as conn:
        saved = dict(conn.execute("SELECT plus_archived_at, active_plus_batch_key FROM accounts WHERE account_key='export-ready@outlook.com'").fetchone())
    assert saved["plus_archived_at"]
    assert saved["active_plus_batch_key"] == ""
    items = repo.list_items(batch_key, status="archived")
    assert items["total"] == 1


def test_plus_activation_batch_export_can_reexport_archived_accounts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "plus-batch-reexport.db"
    accounts = AccountsRepository(db_path)
    accounts.upsert({
        "account_key": "already-exported@outlook.com",
        "email": "already-exported@outlook.com",
        "password": "ChatGptPass!",
        "access_token": "access-reexport",
        "plus_status": "free",
    })
    resources = ResourcePoolRepository(db_path)
    resources.upsert(
        resource_type="email",
        provider="outlook_token",
        resource_key="already-exported@outlook.com",
        payload={
            "email": "already-exported@outlook.com",
            "password": "OutlookPass2",
            "client_id": "cid-reexport-2",
            "refresh_token": "rt-reexport-2",
        },
        status="available",
    )
    repo = PlusActivationRepository(db_path)
    created = repo.create_batch_with_items(["already-exported@outlook.com"], name="reexport batch")
    batch_key = str(created.batch["batch_key"])
    with db.connect(db_path) as conn:
        conn.execute("UPDATE plus_activation_batch_items SET status='archived', archived_at='2026-07-22T00:00:00+00:00' WHERE batch_key=?", (batch_key,))
        conn.execute("UPDATE accounts SET plus_status='verified_plus', plus_batch_status='archived', plus_archived_at='2026-07-22T00:00:00+00:00' WHERE account_key='already-exported@outlook.com'")
    repo.refresh_batch_summary(batch_key)
    service = PlusActivationBatchService(repo=repo)
    import services.plus_activation_batch_service as batch_service_mod

    monkeypatch.setattr(batch_service_mod, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(batch_service_mod, "EXPORT_ROOT", tmp_path / "plus_exports")

    without_archived = service.export_plus(batch_key, fmt="txt", include_already_exported=False)
    assert without_archived["ok"] is False
    assert without_archived["count"] == 0

    result = service.export_plus(batch_key, fmt="txt", include_already_exported=True, archive_after_export=True)
    assert result["ok"] is True
    assert result["count"] == 1
    expected = "already-exported@outlook.com----OutlookPass2----cid-reexport-2----rt-reexport-2"
    exported_file = tmp_path / str(result["export"]["file_path"])
    assert result.get("text", "").strip() == expected
    assert exported_file.read_text(encoding="utf-8").strip() == expected
