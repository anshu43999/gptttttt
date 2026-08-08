from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from application.accounts_service import AccountsService
from domain.accounts import AccountQuery
from infrastructure.repositories.accounts_repository import AccountsRepository
from infrastructure.repositories.tasks_repository import TasksRepository
from platforms.chatgpt.pipeline_adapter import ChatGptPipelineAdapter
from services.pipeline_runner import PipelineRunner


def test_accounts_service_filters_and_exports(tmp_path: Path) -> None:
    db_path = tmp_path / "services.db"
    repo = AccountsRepository(db_path)
    svc = AccountsService(repo)
    repo.upsert({
        "account_key": "a1",
        "account_id": "auth0|a1",
        "email": "a@example.com",
        "password": "pw",
        "phone_number": "+15550000001",
        "stage": "manual_plus_required",
        "status": "manual_plus_required",
        "plan_type": "free",
    })
    repo.upsert({
        "account_key": "a2",
        "account_id": "auth0|a2",
        "email": "b@example.com",
        "password": "pw2",
        "phone_number": "+15550000002",
        "stage": "complete",
        "status": "complete",
        "plan_type": "plus",
        "activation_id": "internal-only",
    })

    pending = svc.list_accounts(AccountQuery(stage="manual_plus_required"))
    assert len(pending) == 1
    assert pending[0]["account_key"] == "a1"
    marked = svc.mark_plus("a1")
    assert marked["stage"] == "manual_plus_confirmed"
    assert marked["plan_type"] == "plus"
    product = svc.export_product("a2")
    assert product["email"] == "b@example.com"
    assert "activation_id" not in product
    assert svc.archive("a2") is True
    assert svc.get_account("a2")["stage"] == "archived"


def test_tasks_repository_and_pipeline_adapter(tmp_path: Path) -> None:
    db_path = tmp_path / "tasks.db"
    repo = TasksRepository(db_path)
    task = repo.create({"id": "t1", "type": "register-token", "status": "pending", "command": ["python", "full_pipeline.py"]})
    assert task.id == "t1"
    assert task.task_type == "register-token"
    repo.add_event("t1", "info", "log", "hello")
    events = repo.events("t1")
    assert len(events) >= 1
    assert events[-1].message == "hello"
    updated = repo.update("t1", status="succeeded", result={"exit_code": 0})
    assert updated.status == "succeeded"

    adapter = ChatGptPipelineAdapter(PipelineRunner(PROJECT_ROOT))
    reg_cmd = adapter.register_token_command("config.yaml", headed=True)
    assert reg_cmd[-1] == "--headed"
    assert "register-token" in reg_cmd
    email_cmd = adapter.email_register_token_command("config.yaml", headed=True)
    assert "-m" in email_cmd
    assert "services.modular_runner" in email_cmd
    assert "--task-type" in email_cmd
    assert "email-register-token" in email_cmd
    assert "full_pipeline.py" not in email_cmd
    assert email_cmd[-1] == "--headed"
    protocol_cmd = adapter.protocol_register_token_command("config.yaml", headed=False, task_id="task-protocol")
    assert "services.codex_protocol_runner" in protocol_cmd
    assert "--task-id" in protocol_cmd
    assert "task-protocol" in protocol_cmd
    assert "full_pipeline.py" not in protocol_cmd
    email_protocol_cmd = adapter.email_protocol_register_command("config.yaml", task_id="task-email-protocol")
    assert "services.mailat_email_protocol_task" in email_protocol_cmd
    assert "--task-id" in email_protocol_cmd
    assert "task-email-protocol" in email_protocol_cmd
    resume_cmd = adapter.resume_oauth_command("config.yaml", "output/resume.json", headed=False)
    assert "resume-oauth" in resume_cmd
    assert "--headed" not in resume_cmd



def test_tasks_service_merges_db_config_into_temp_config(tmp_path: Path) -> None:
    from application.config_service import ConfigService
    from application.tasks_service import TasksService
    from infrastructure.repositories.config_repository import ConfigRepository

    db_path = tmp_path / "config.db"
    base_config = tmp_path / "config.yaml"
    base_config.write_text("sms_api_key: file-key\nmailbox_domain: example.com\n", encoding="utf-8")
    config_repo = ConfigRepository(db_path)
    config_repo.set_many({"sms_api_key": "db-key", "lajiao_proxy_mode": "credentials"})
    svc = TasksService(repo=TasksRepository(tmp_path / "tasks.db"), config_service=ConfigService(config_repo, base_config=str(base_config)))

    config_path = svc._write_task_config("task_test", str(base_config), {"sms_country": "73"})
    content = Path(config_path).read_text(encoding="utf-8")
    assert "sms_api_key: db-key" in content
    assert "mailbox_domain: example.com" in content
    assert "lajiao_proxy_mode: credentials" in content
    assert "sms_country: '73'" in content or "sms_country: 73" in content


def test_config_service_loads_yaml_without_full_pipeline_import(tmp_path: Path, monkeypatch) -> None:
    from application.config_service import ConfigService
    from infrastructure.repositories.config_repository import ConfigRepository

    import full_pipeline

    def fail_register_pipeline(*args, **kwargs):
        raise AssertionError("ConfigService must not instantiate full_pipeline.RegisterPipeline")

    monkeypatch.setattr(full_pipeline, "RegisterPipeline", fail_register_pipeline)
    base_config = tmp_path / "config.yaml"
    base_config.write_text("mailbox_provider: outlook_token\ncustom_value: ok\n", encoding="utf-8")
    svc = ConfigService(ConfigRepository(tmp_path / "config.db"), base_config=str(base_config))
    assert svc.file_config()["mailbox_provider"] == "outlook_token"
    assert svc.file_config()["custom_value"] == "ok"


def test_tasks_service_bucket_limits(tmp_path: Path) -> None:
    from application.config_service import ConfigService
    from application.tasks_service import TasksService
    from infrastructure.repositories.config_repository import ConfigRepository

    db_path = tmp_path / "bucket.db"
    repo = TasksRepository(db_path)
    config_repo = ConfigRepository(db_path)
    config_repo.set_many({"max_parallel_tasks": 3, "max_register_tasks": 1, "max_oauth_tasks": 1})
    svc = TasksService(repo=repo, config_service=ConfigService(config_repo))
    repo.create({"id": "reg-running", "type": "register-token", "status": "running", "command": ["python"]})
    repo.create({"id": "oauth-running", "type": "resume-oauth", "status": "running", "command": ["python"]})
    svc.running["reg-running"] = object()  # type: ignore[assignment]
    svc.running["oauth-running"] = object()  # type: ignore[assignment]

    can_register, register_reason = svc._can_start_locked("register-token")
    can_oauth, oauth_reason = svc._can_start_locked("resume-oauth")
    can_maintenance, _ = svc._can_start_locked("proxy-check")

    assert can_register is False
    assert "bucket=register" in register_reason
    assert can_oauth is False
    assert "bucket=oauth" in oauth_reason
    assert can_maintenance is True

def test_tasks_service_reload_limits_and_resume_retry_keeps_overrides(tmp_path: Path) -> None:
    from application.config_service import ConfigService
    from application.tasks_service import TasksService
    from infrastructure.repositories.config_repository import ConfigRepository

    db_path = tmp_path / "retry.db"
    repo = TasksRepository(db_path)
    config_repo = ConfigRepository(db_path)
    config_repo.set_many({"max_parallel_tasks": 2, "max_register_tasks": 1, "max_oauth_tasks": 1})
    svc = TasksService(repo=repo, config_service=ConfigService(config_repo))
    assert svc.max_parallel == 2
    assert svc.bucket_limits["oauth"] == 1

    config_repo.set_many({"max_parallel_tasks": 20, "max_register_tasks": 20, "max_oauth_tasks": 8})
    svc.reload_limits()
    assert svc.max_parallel == 20
    assert svc.bucket_limits["register"] == 20
    assert svc.bucket_limits["oauth"] == 8

    repo.create({
        "id": "resume-original",
        "type": "resume-oauth",
        "status": "failed",
        "command": ["python"],
        "params": {
            "resume_file": "output/resume.json",
            "headed": False,
            "overrides": {
                "oauth_callback_mode": "cpa",
                "cpa_base_url": "https://cpa.local",
                "cpa_management_key": "key",
                "sms_provider": "user_phone_url",
                "sms_country": "73",
                "sms_service": "dr",
            },
        },
    })
    captured: dict[str, object] = {}
    svc.start_resume = lambda data: captured.setdefault("data", data) or {"id": "new"}  # type: ignore[method-assign]

    svc.retry("resume-original")

    assert captured["data"] == {
        "resume_file": "output/resume.json",
        "headed": False,
        "oauth_callback_mode": "cpa",
        "cpa_base_url": "https://cpa.local",
        "cpa_management_key": "key",
        "sms_provider": "user_phone_url",
        "sms_country": "73",
        "sms_service": "dr",
    }


def test_resume_task_writes_skip_plus_check_override(tmp_path: Path) -> None:
    from application.config_service import ConfigService
    from application.tasks_service import TasksService
    from infrastructure.repositories.config_repository import ConfigRepository

    db_path = tmp_path / "skip-plus.db"
    repo = TasksRepository(db_path)
    svc = TasksService(repo=repo, config_service=ConfigService(ConfigRepository(db_path)))
    svc._schedule_or_queue = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

    task = svc.start_resume({"resume_file": "output/resume.json", "headed": False})
    config_text = Path(task["params"]["config_path"]).read_text(encoding="utf-8")

    assert "skip_plus_check_for_binding: true" in config_text
    assert task["params"]["overrides"]["skip_plus_check_for_binding"] is True



def test_email_register_task_does_not_lease_phone_pools(tmp_path: Path) -> None:
    from application.config_service import ConfigService
    from application.resource_pool_service import ResourcePoolService
    from application.tasks_service import TasksService
    from infrastructure.repositories.config_repository import ConfigRepository
    from infrastructure.repositories.resource_pool_repository import ResourcePoolRepository

    db_path = tmp_path / "email-no-phone.db"
    config_repo = ConfigRepository(db_path)
    config_repo.set_many({
        "bind_sms_provider": "bind_user_phone_url",
        "sms_provider": "user_phone_url",
        "lajiao_proxy_mode": "",
        "mailbox_provider": "icloud_api",
    })
    resource_pool = ResourcePoolService(ResourcePoolRepository(db_path))
    resource_pool.import_phone_urls("17078027183|https://sms.local/bind", provider="bind_user_phone_url")
    resource_pool.import_phone_urls("1904329600|https://sms.local/register", provider="user_phone_url")
    resource_pool.import_link_api_mailboxes("icloud@example.com----https://mail.local/inbox")
    svc = TasksService(repo=TasksRepository(db_path), config_service=ConfigService(config_repo), resource_pool=resource_pool)

    task = svc.start_email_register({"headed": False}, {})
    config_path = Path(task["params"]["config_path"])
    config_text = config_path.read_text(encoding="utf-8")
    import yaml
    config = yaml.safe_load(config_text) or {}
    leases = config.get("resource_leases") or []
    assert all(item.get("provider") not in {"bind_user_phone_url", "user_phone_url"} for item in leases)
    assert any(item.get("provider") == "icloud_api" for item in leases)
    assert config.get("bind_sms_phone_url") == ""
    assert config.get("sms_phone_url") == ""
    assert resource_pool.list_resources("phone", "bind_user_phone_url")[0]["status"] == "available"
    assert resource_pool.list_resources("phone", "user_phone_url")[0]["status"] == "available"


def test_email_protocol_register_task_reuses_project_pools(tmp_path: Path) -> None:
    from application.config_service import ConfigService
    from application.resource_pool_service import ResourcePoolService
    from application.tasks_service import TasksService
    from infrastructure.repositories.config_repository import ConfigRepository
    from infrastructure.repositories.resource_pool_repository import ResourcePoolRepository

    db_path = tmp_path / "email-protocol-pools.db"
    config_repo = ConfigRepository(db_path)
    config_repo.set_many({
        "bind_sms_provider": "bind_user_phone_url",
        "sms_provider": "user_phone_url",
        "lajiao_proxy_mode": "credentials",
        "mailbox_provider": "outlook_token",
    })
    resource_pool = ResourcePoolService(ResourcePoolRepository(db_path))
    resource_pool.import_phone_urls("17078027183|https://sms.local/bind", provider="bind_user_phone_url")
    resource_pool.import_phone_urls("1904329600|https://sms.local/register", provider="user_phone_url")
    resource_pool.import_lajiao_credentials("user:pass@proxy.local:1080", region="JP", protocol="socks5")
    resource_pool.import_outlook_tokens("mail@example.com----pass----client-id----refresh-token")
    svc = TasksService(repo=TasksRepository(db_path), config_service=ConfigService(config_repo), resource_pool=resource_pool)

    task = svc.start_email_protocol_register({"headed": False}, {})
    config_path = Path(task["params"]["config_path"])
    config_text = config_path.read_text(encoding="utf-8")
    import yaml
    config = yaml.safe_load(config_text) or {}
    leases = config.get("resource_leases") or []
    assert any(item.get("provider") == "lajiao_credentials" for item in leases)
    assert any(item.get("provider") == "outlook_token" for item in leases)
    assert all(item.get("provider") not in {"bind_user_phone_url", "user_phone_url"} for item in leases)
    assert config.get("outlook_email") == "mail@example.com"
    assert config.get("bind_sms_phone_url") == ""
    assert config.get("sms_phone_url") == ""
    assert resource_pool.list_resources("phone", "bind_user_phone_url")[0]["status"] == "available"
    assert resource_pool.list_resources("phone", "user_phone_url")[0]["status"] == "available"



def test_email_protocol_bulk_queue_defers_resource_lease(tmp_path: Path) -> None:
    from application.config_service import ConfigService
    from application.resource_pool_service import ResourcePoolService
    from application.tasks_service import TasksService
    from infrastructure.repositories.config_repository import ConfigRepository
    from infrastructure.repositories.resource_pool_repository import ResourcePoolRepository
    import yaml

    db_path = tmp_path / "email-protocol-defer.db"
    config_repo = ConfigRepository(db_path)
    config_repo.set_many({
        "lajiao_proxy_mode": "credentials",
        "mailbox_provider": "outlook_token",
    })
    resource_pool = ResourcePoolService(ResourcePoolRepository(db_path))
    resource_pool.import_lajiao_credentials("user:pass@proxy.local:1080", region="JP", protocol="socks5")
    resource_pool.import_outlook_tokens("mail@example.com----pass----client-id----refresh-token")
    svc = TasksService(repo=TasksRepository(db_path), config_service=ConfigService(config_repo), resource_pool=resource_pool)

    task = svc.start_email_protocol_register({"headed": False}, {}, defer_start=True)
    assert task["status"] == "queued"
    config = yaml.safe_load(Path(task["params"]["config_path"]).read_text(encoding="utf-8")) or {}
    assert not config.get("_resources_prepared")
    assert not (config.get("resource_leases") or [])
    proxies = resource_pool.list_resources("proxy", "proxy_seed") or resource_pool.list_resources("proxy", "lajiao_credentials")
    assert proxies and proxies[0]["status"] == "available"
    assert resource_pool.list_resources("email", "outlook_token")[0]["status"] == "available"

    # Real start still leases when the job is prepared.
    assert svc._prepare_task_resources(task["id"]) is True
    config = yaml.safe_load(Path(task["params"]["config_path"]).read_text(encoding="utf-8")) or {}
    assert config.get("_resources_prepared") is True
    leases = config.get("resource_leases") or []
    assert any(item.get("provider") == "outlook_token" for item in leases)
    # Proxy seed pool is preferred over legacy lajiao_credentials provider name.
    assert any(item.get("provider") in {"proxy_seed", "lajiao_credentials"} for item in leases)

def test_email_protocol_register_fails_when_outlook_pool_empty(tmp_path: Path) -> None:
    from application.config_service import ConfigService
    from application.resource_pool_service import ResourcePoolService
    from application.tasks_service import TasksService
    from infrastructure.repositories.config_repository import ConfigRepository
    from infrastructure.repositories.resource_pool_repository import ResourcePoolRepository

    db_path = tmp_path / "email-protocol-empty-outlook.db"
    config_repo = ConfigRepository(db_path)
    config_repo.set_many({
        "lajiao_proxy_mode": "credentials",
        "mailbox_provider": "outlook_token",
        "outlook_token_order_file": str(tmp_path / "shared_order.txt"),
    })
    (tmp_path / "shared_order.txt").write_text(
        "shared@example.com----pass----client-id----refresh-token\n",
        encoding="utf-8",
    )
    resource_pool = ResourcePoolService(ResourcePoolRepository(db_path))
    resource_pool.import_lajiao_credentials("user:pass@proxy.local:1080", region="JP", protocol="socks5")
    # No outlook_token resources imported -> pool empty.
    svc = TasksService(repo=TasksRepository(db_path), config_service=ConfigService(config_repo), resource_pool=resource_pool)

    try:
        svc.start_email_protocol_register({"headed": False}, {})
        raise AssertionError("expected empty outlook pool to fail fast")
    except RuntimeError as exc:
        assert "邮箱池已耗尽" in str(exc) or "Outlook token" in str(exc)
    # Proxy lease must not remain stuck after the failed create path.
    proxies = resource_pool.list_resources("proxy", "proxy_seed") or resource_pool.list_resources("proxy", "lajiao_credentials")
    assert proxies
    assert proxies[0]["status"] == "available"


def test_protocol_register_leases_single_reusable_binding_email(tmp_path: Path) -> None:
    from application.resource_pool_service import ResourcePoolService
    from infrastructure.repositories.resource_pool_repository import ResourcePoolRepository

    pool = ResourcePoolService(ResourcePoolRepository(tmp_path / "protocol-email.db"))
    overrides, leases = pool.lease_for_task("task-protocol-email", {
        "registration_engine": "protocol",
        "mailbox_provider": "forwarded_domain",
        "mailbox_domain": "example.com",
        "lajiao_proxy_mode": "",
    })

    assert overrides["codex_bind_email"].endswith("@example.com")
    assert overrides["billing_email"] == overrides["codex_bind_email"]
    assert overrides["codex_email"] == overrides["codex_bind_email"]
    assert any(lease.resource_type == "email" and lease.provider == "forwarded_domain" for lease in leases)
    assert pool.list_resources("email", "forwarded_domain", "leased")[0]["resource_key"] == overrides["codex_bind_email"]

def test_tasks_service_marks_stale_running_and_cancels_queued(tmp_path: Path) -> None:
    from application.config_service import ConfigService
    from application.tasks_service import TasksService
    from infrastructure.repositories.config_repository import ConfigRepository

    db_path = tmp_path / "stale.db"
    repo = TasksRepository(db_path)
    svc = TasksService(repo=repo, config_service=ConfigService(ConfigRepository(db_path)))
    repo.create({
        "id": "stale-running",
        "type": "register-token",
        "status": "running",
        "command": ["python"],
        "result": {"pid": 999_999_999},
    })
    repo.create({"id": "queued-task", "type": "register-token", "status": "queued", "command": ["python"]})

    tasks = {item["id"]: item for item in svc.list_tasks(reconcile_stale=True, drain_queue=False)}
    assert tasks["stale-running"]["status"] == "interrupted"
    assert tasks["stale-running"]["retryable"] is True

    assert svc.stop("queued-task") is True
    assert repo.get("queued-task").status == "cancelled"


def test_reconcile_orphan_running_requeues_nopid_seats(tmp_path: Path) -> None:
    """claim→running without pid must not permanently fill max_parallel after restart."""
    from application.config_service import ConfigService
    from application.tasks_service import TasksService
    from infrastructure.repositories.config_repository import ConfigRepository

    db_path = tmp_path / "orphan-running.db"
    repo = TasksRepository(db_path)
    config_repo = ConfigRepository(db_path)
    config_repo.set_many({"max_parallel_tasks": 2, "max_register_tasks": 2, "max_oauth_tasks": 1})
    svc = TasksService(repo=repo, config_service=ConfigService(config_repo))

    # Abandoned claim seat: running, no pid, older than grace.
    repo.create({
        "id": "orphan-nopid",
        "type": "email-protocol-register-token",
        "status": "running",
        "started_at": "2020-01-01T00:00:00",
        "updated_at": "2020-01-01T00:00:00",
        "command": ["python", "-c", "pass"],
        "log_file": str(tmp_path / "orphan-nopid.log"),
        "result": {},
    })
    # Dead pid seat.
    repo.create({
        "id": "orphan-deadpid",
        "type": "email-protocol-register-token",
        "status": "running",
        "started_at": "2020-01-01T00:00:00",
        "command": ["python", "-c", "pass"],
        "log_file": str(tmp_path / "orphan-deadpid.log"),
        "result": {"pid": 999_999_999},
    })

    stats = svc.reconcile_orphan_running_tasks(grace_seconds=15)
    assert stats["requeued"] >= 1
    assert stats["interrupted"] >= 1
    assert repo.get("orphan-nopid").status == "queued"
    assert repo.get("orphan-deadpid").status == "interrupted"


def test_tasks_service_stop_all_cancels_waiting_tasks(tmp_path: Path) -> None:
    from application.config_service import ConfigService
    from application.tasks_service import TasksService
    from infrastructure.repositories.config_repository import ConfigRepository

    db_path = tmp_path / "stop-all.db"
    repo = TasksRepository(db_path)
    svc = TasksService(repo=repo, config_service=ConfigService(ConfigRepository(db_path)))
    repo.create({"id": "pending-task", "type": "register-token", "status": "pending", "command": ["python"]})
    repo.create({"id": "queued-task", "type": "register-token", "status": "queued", "command": ["python"]})
    repo.create({"id": "done-task", "type": "register-token", "status": "succeeded", "command": ["python"]})

    result = svc.stop_all()

    assert result == {"requested": 2, "stopped": 2, "failed": 0}
    assert repo.get("pending-task").status == "cancelled"
    assert repo.get("queued-task").status == "cancelled"
    assert repo.get("done-task").status == "succeeded"

def test_drain_queue_finds_queued_beyond_recent_finished_window(tmp_path: Path) -> None:
    """Regression: unfiltered list() only returns newest 50; queued must not disappear after ~50 finished."""
    from application.config_service import ConfigService
    from application.tasks_service import TasksService
    from infrastructure.repositories.config_repository import ConfigRepository
    from services.task_runtime import ManagedTask

    db_path = tmp_path / "drain-window.db"
    repo = TasksRepository(db_path)
    config_repo = ConfigRepository(db_path)
    config_repo.set_many({"max_parallel_tasks": 2, "max_register_tasks": 2, "max_oauth_tasks": 1})
    svc = TasksService(repo=repo, config_service=ConfigService(config_repo))

    started: list[str] = []
    original_start = ManagedTask.start

    def fake_start(self, on_finish=None):  # type: ignore[no-untyped-def]
        started.append(self.task_id)
        repo.update(self.task_id, status="running")

    ManagedTask.start = fake_start  # type: ignore[method-assign]

    try:
        # 55 finished tasks that are newer than the queued ones (DESC window fills with these).
        for i in range(55):
            repo.create({
                "id": f"done-{i:03d}",
                "type": "register-token",
                "status": "succeeded",
                "created_at": f"2026-07-15T15:{i // 60:02d}:{i % 60:02d}",
                "command": ["python", "-c", "pass"],
                "log_file": str(tmp_path / f"done-{i:03d}.log"),
            })
        # Older queued tasks that fall outside the default newest-50 window.
        for i in range(3):
            repo.create({
                "id": f"queued-old-{i}",
                "type": "register-token",
                "status": "queued",
                "created_at": f"2026-07-15T14:00:0{i}",
                "command": ["python", "-c", "pass"],
                "log_file": str(tmp_path / f"queued-old-{i}.log"),
            })

        # Prove the buggy window: unfiltered newest-50 has zero queued.
        recent = [item.to_dict() for item in repo.list(limit=50)]
        assert all(item.get("status") != "queued" for item in recent)

        svc._drain_queue()

        assert len(started) == 2  # max_parallel=2
        assert set(started) == {"queued-old-0", "queued-old-1"}  # FIFO oldest first
        assert "queued-old-0" in svc.running
        assert "queued-old-1" in svc.running
        assert repo.get("queued-old-2").status == "queued"
    finally:
        ManagedTask.start = original_start  # type: ignore[method-assign]






def test_drain_queue_claims_starting_status_immediately(tmp_path: Path) -> None:
    """Dequeued tasks leave status=starting before process spawn writes running+pid."""
    from application.config_service import ConfigService
    from application.tasks_service import TasksService
    from infrastructure.repositories.config_repository import ConfigRepository
    from services.task_runtime import ManagedTask

    db_path = tmp_path / "drain-claim.db"
    repo = TasksRepository(db_path)
    config_repo = ConfigRepository(db_path)
    config_repo.set_many({"max_parallel_tasks": 1, "max_register_tasks": 1, "max_oauth_tasks": 1})
    svc = TasksService(repo=repo, config_service=ConfigService(config_repo))

    started: list[str] = []
    original_start = ManagedTask.start

    def fake_start(self, on_finish=None):  # type: ignore[no-untyped-def]
        # Claim already flipped queued -> starting; real path promotes to running after Popen.
        started.append(self.task_id)
        assert repo.get(self.task_id).status == "starting"

    ManagedTask.start = fake_start  # type: ignore[method-assign]
    try:
        for i in range(2):
            repo.create({
                "id": f"claim-{i}",
                "type": "email-protocol-register-token",
                "status": "queued",
                "created_at": f"2026-07-16T05:00:0{i}",
                "command": ["python", "-c", "pass"],
                "log_file": str(tmp_path / f"claim-{i}.log"),
            })
        svc._drain_queue()
        assert started == ["claim-0"]
        assert repo.get("claim-0").status == "starting"
        assert repo.get("claim-1").status == "queued"
        assert "claim-0" in svc.running
    finally:
        ManagedTask.start = original_start  # type: ignore[method-assign]


def test_drain_queue_claim_is_global_across_services(tmp_path: Path) -> None:
    """Two dashboard schedulers must never over-claim one shared task database."""
    import threading

    from application.config_service import ConfigService
    from application.tasks_service import TasksService
    from infrastructure.repositories.config_repository import ConfigRepository
    from services.task_runtime import ManagedTask

    db_path = tmp_path / "global-claim.db"
    config_repo = ConfigRepository(db_path)
    config_repo.set_many({"max_parallel_tasks": 100, "max_register_tasks": 100, "max_oauth_tasks": 100})
    repo_a = TasksRepository(db_path)
    repo_b = TasksRepository(db_path)
    svc_a = TasksService(repo=repo_a, config_service=ConfigService(config_repo))
    svc_b = TasksService(repo=repo_b, config_service=ConfigService(config_repo))
    # Production caps claim burst to avoid nopid zombies; this test needs full fan-out.
    svc_a._claim_burst = 100
    svc_b._claim_burst = 100
    svc_a._start_workers = 32
    svc_b._start_workers = 32
    for i in range(101):
        repo_a.create({
            "id": f"global-claim-{i:03d}",
            "type": "email-protocol-register-token",
            "status": "queued",
            "created_at": f"2026-07-16T06:{i // 60:02d}:{i % 60:02d}",
            "command": ["python", "-c", "pass"],
            "log_file": str(tmp_path / f"global-claim-{i:03d}.log"),
        })

    started: list[str] = []
    started_lock = threading.Lock()
    original_start = ManagedTask.start

    def fake_start(self, on_finish=None):  # type: ignore[no-untyped-def]
        with started_lock:
            started.append(self.task_id)

    ManagedTask.start = fake_start  # type: ignore[method-assign]
    barrier = threading.Barrier(3)
    failures: list[BaseException] = []

    def drain(service: TasksService) -> None:
        try:
            barrier.wait(timeout=5)
            service._drain_queue()
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=drain, args=(svc,)) for svc in (svc_a, svc_b)]
    try:
        for thread in threads:
            thread.start()
        barrier.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=20)
        assert not any(thread.is_alive() for thread in threads)
        assert failures == []
        # Claim now yields starting (not running) until pid is written.
        starting = [task for task in repo_a.list(status="starting", limit=200, order="asc") if str(task.id).startswith("global-claim-")]
        assert [task.id for task in starting] == [f"global-claim-{i:03d}" for i in range(100)]
        assert repo_a.get("global-claim-100").status == "queued"
        assert len([tid for tid in started if str(tid).startswith("global-claim-")]) == 100
        assert set(svc_a.running).isdisjoint(set(svc_b.running))
        assert len(svc_a.running) + len(svc_b.running) >= 100
    finally:
        ManagedTask.start = original_start  # type: ignore[method-assign]


def test_tasks_service_extracts_verified_binding_phone(tmp_path: Path) -> None:
    from application.config_service import ConfigService
    from application.tasks_service import TasksService
    from infrastructure.repositories.config_repository import ConfigRepository

    svc = TasksService(repo=TasksRepository(tmp_path / "tasks.db"), config_service=ConfigService(ConfigRepository(tmp_path / "config.db")))
    rented_only = "已成功租到号码(新号码): +17078027164 (activation_id=https://smscloud.sbs/api/system/get_sms/x)\n手机号提交后未进入验证码页"
    assert svc._extract_binding_phone_report(rented_only) == ("+17078027164", False)

    verified = rented_only + "\n短信验证成功，已标记号码完成使用: activation_id=https://smscloud.sbs/api/system/get_sms/x"
    assert svc._extract_binding_phone_report(verified) == ("+17078027164", True)
    assert svc._extract_successful_binding_phone(verified) == "+17078027164"


def test_add_phone_wait_retries_submit_when_still_on_add_phone(monkeypatch) -> None:
    import platforms.chatgpt.browser_register as browser_register

    calls = {"wait": 0, "retry": 0}

    def fake_wait(page, *, timeout: int = 30):
        calls["wait"] += 1
        if calls["wait"] == 1:
            return {"url": "https://auth.openai.com/add-phone"}
        return {"url": "https://auth.openai.com/phone-verification", "phoneVerificationReady": True}

    def fake_retry(page, log):
        calls["retry"] += 1
        return "button[type=submit]"

    monkeypatch.setattr(browser_register, "_wait_for_phone_verification_ready", fake_wait)
    monkeypatch.setattr(browser_register, "_force_submit_add_phone_form", fake_retry)

    status = browser_register._wait_add_phone_after_submit(object(), lambda _msg: None, timeout=1)

    assert status["phoneVerificationReady"] is True
    assert status["submit_retry"] == "button[type=submit]"
    assert calls == {"wait": 2, "retry": 1}


def test_add_phone_required_error_uses_trusted_ui_retry(monkeypatch) -> None:
    import platforms.chatgpt.browser_register as browser_register

    calls = {"trusted": 0, "wait": 0, "marked": []}

    class Callback:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self) -> str:
            self.calls += 1
            return "+13527216080" if self.calls == 1 else "123456"

        def mark_send_failed(self, text: str) -> None:
            calls["marked"].append(text)

    def fake_wait(page, log=None, *, timeout: int = 30):
        calls["wait"] += 1
        if calls["wait"] == 1:
            return {"url": "https://auth.openai.com/add-phone", "addPhoneError": "電話番号が必要です"}
        return {"url": "https://auth.openai.com/phone-verification", "phoneVerificationReady": True}

    def fake_trusted(page, **kwargs):
        calls["trusted"] += 1
        assert kwargs["phone_number"] == "+13527216080"
        return "button[type=submit]"

    monkeypatch.setattr(browser_register, "_submit_add_phone_dom", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(browser_register, "_wait_add_phone_after_submit", fake_wait)
    monkeypatch.setattr(browser_register, "_select_phone_country_ui", lambda *args, **kwargs: True)
    monkeypatch.setattr(browser_register, "_submit_add_phone_via_trusted_ui", fake_trusted)
    monkeypatch.setattr(browser_register, "_wait_for_phone_verification_ready", lambda page, *, timeout=12: {"phoneVerificationReady": True})
    monkeypatch.setattr(browser_register, "_submit_phone_otp_dom", lambda *args, **kwargs: {"ok": True, "status": 200, "data": {}})
    monkeypatch.setattr(browser_register, "_extract_flow_state", lambda data, current_url="": {"page_type": "callback"})

    state = browser_register._do_add_phone_attempt(
        SimpleNamespace(url="https://auth.openai.com/add-phone"),
        Callback(),
        device_id="device",
        user_agent="Mozilla/5.0 Chrome/136.0.0.0",
        log=lambda _msg: None,
    )

    assert state == {"page_type": "callback"}
    assert calls == {"trusted": 1, "wait": 2, "marked": []}


def test_browser_session_open_falls_back_saved_pool_direct(monkeypatch, tmp_path: Path) -> None:
    import application.browser_session_service as browser_service
    from application.browser_session_service import BrowserSessionHandle, BrowserSessionService

    monkeypatch.setattr(browser_service.db, "add_account_event", lambda *args, **kwargs: None)
    storage = tmp_path / "storage.json"
    storage.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
    handle = BrowserSessionHandle(
        id="session-test",
        account_key="acct-test",
        account_label="acct@example.com",
        storage_path=storage,
        target_url="https://chatgpt.com/",
        proxy="socks5://saved.example:3000",
        engine="camoufox",
        headed=True,
        save_on_close=False,
    )
    svc = BrowserSessionService()
    attempts: list[str] = []

    monkeypatch.setattr(svc, "_check_browser_proxy", lambda _handle, _proxy, _label: "203.0.113.10")
    monkeypatch.setattr(svc, "_select_fresh_browser_proxy", lambda _handle: ("socks5://pool.example:3000", "203.0.113.20"))

    class FakePage:
        def goto(self, _url: str, **_kwargs):
            return None

        def title(self) -> str:
            return "ChatGPT"

    def fake_launch_once(_handle, proxy: str, _geoip_ip: str = ""):
        attempts.append(proxy or "direct")
        if proxy:
            raise RuntimeError("proxy failed")
        return object(), object(), object(), FakePage(), None

    monkeypatch.setattr(svc, "_launch_once", fake_launch_once)

    result = svc._launch(handle)

    assert len(result) == 7
    assert attempts == ["socks5://saved.example:3000", "socks5://pool.example:3000", "direct"]
    assert handle.proxy == ""
if __name__ == "__main__":
    root = Path("tmp/test_modular_services")
    root.mkdir(parents=True, exist_ok=True)
    test_accounts_service_filters_and_exports(root)
    test_tasks_repository_and_pipeline_adapter(root)
    test_tasks_service_merges_db_config_into_temp_config(root)
    test_tasks_service_bucket_limits(root)
    print("modular service tests passed")
