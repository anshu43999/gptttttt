from __future__ import annotations

from pathlib import Path


def test_normalize_spawn_mode_aliases() -> None:
    from services.task_runtime import normalize_spawn_mode

    assert normalize_spawn_mode("inline") == "inline"
    assert normalize_spawn_mode("thread_pool") == "inline"
    assert normalize_spawn_mode("process") == "process"
    assert normalize_spawn_mode("") == "process"
    assert normalize_spawn_mode(None) == "process"


def test_email_protocol_spawn_mode_defaults_inline(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GPT_REGISTER_SKIP_ENV_DB", "1")
    monkeypatch.setenv("GPT_REGISTER_DB_BACKEND", "sqlite")
    monkeypatch.setenv("GPT_REGISTER_DATABASE_URL", "")
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.delenv("EMAIL_PROTOCOL_SPAWN_MODE", raising=False)

    from application.config_service import ConfigService
    from application.tasks_service import TasksService
    from infrastructure.repositories.config_repository import ConfigRepository
    from infrastructure.repositories.tasks_repository import TasksRepository

    db_path = tmp_path / "spawn.db"
    cfg = ConfigRepository(db_path)
    svc = TasksService(repo=TasksRepository(db_path), config_service=ConfigService(cfg))
    assert svc._email_protocol_spawn_mode() == "inline"

    cfg.set_many({"email_protocol_spawn_mode": "process"})
    svc.reload_limits()
    assert svc._email_protocol_spawn_mode() == "process"


def test_make_managed_uses_inline_for_email_protocol(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GPT_REGISTER_SKIP_ENV_DB", "1")
    monkeypatch.setenv("GPT_REGISTER_DB_BACKEND", "sqlite")
    monkeypatch.setenv("GPT_REGISTER_DATABASE_URL", "")
    monkeypatch.setenv("DATABASE_URL", "")

    from application.config_service import ConfigService
    from application.tasks_service import TasksService
    from infrastructure.repositories.config_repository import ConfigRepository
    from infrastructure.repositories.tasks_repository import TasksRepository

    db_path = tmp_path / "managed.db"
    repo = TasksRepository(db_path)
    cfg = ConfigRepository(db_path)
    cfg.set_many({"email_protocol_spawn_mode": "inline", "max_parallel_tasks": 8, "max_register_tasks": 8})
    svc = TasksService(repo=repo, config_service=ConfigService(cfg))

    config_path = tmp_path / "task_config.yaml"
    config_path.write_text("email_protocol_backend: go\n", encoding="utf-8")
    log_file = tmp_path / "t.log"
    repo.create(
        {
            "id": "inline-task-1",
            "type": "email-protocol-register-token",
            "status": "starting",
            "command": ["python", "-c", "pass"],
            "log_file": str(log_file),
            "params": {"config_path": str(config_path), "overrides": {}},
        }
    )
    managed = svc._make_managed("inline-task-1", ["python", "-c", "pass"], str(log_file))
    assert managed.spawn_mode == "inline"
    assert managed.config_path == str(config_path)


def test_inline_run_calls_mailat_task_without_subprocess(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GPT_REGISTER_SKIP_ENV_DB", "1")
    monkeypatch.setenv("GPT_REGISTER_DB_BACKEND", "sqlite")
    monkeypatch.setenv("GPT_REGISTER_DATABASE_URL", "")
    monkeypatch.setenv("DATABASE_URL", "")

    import time

    from infrastructure.repositories.tasks_repository import TasksRepository
    from services.task_runtime import ManagedTask

    db_path = tmp_path / "inline-run.db"
    repo = TasksRepository(db_path)
    config_path = tmp_path / "cfg.yaml"
    config_path.write_text("x: 1\n", encoding="utf-8")
    log_file = tmp_path / "inline.log"
    repo.create(
        {
            "id": "inline-run-1",
            "type": "email-protocol-register-token",
            "status": "starting",
            "started_at": "2026-07-23T12:00:00",
            "command": ["python", "-m", "services.mailat_email_protocol_task"],
            "log_file": str(log_file),
            "params": {"config_path": str(config_path)},
            "result": {},
        }
    )

    called: dict[str, str] = {}

    def fake_run(config_path_arg: str, *, task_id: str = "") -> dict:
        called["config"] = config_path_arg
        called["task_id"] = task_id
        print("fake inline ok")
        return {"ok": True}

    monkeypatch.setattr("services.mailat_email_protocol_task.run", fake_run)

    done = {"code": None}

    def on_finish(code: int) -> None:
        done["code"] = code

    managed = ManagedTask(
        "inline-run-1",
        ["python", "-c", "pass"],
        str(log_file),
        repo,
        spawn_mode="inline",
        config_path=str(config_path),
        inline_pool_size=4,
    )
    managed.start(on_finish=on_finish)

    deadline = time.time() + 5
    while time.time() < deadline and done["code"] is None:
        time.sleep(0.05)

    assert done["code"] == 0
    assert called.get("task_id") == "inline-run-1"
    assert called.get("config") == str(config_path)
    row = repo.get("inline-run-1").to_dict()
    assert row.get("status") == "succeeded"
    result = row.get("result") if isinstance(row.get("result"), dict) else {}
    assert result.get("spawn_mode") == "inline" or result.get("inline") in {1, True, "1"}
