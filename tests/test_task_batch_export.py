from __future__ import annotations

from pathlib import Path


def test_export_batch_accounts_marks_export_status(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GPT_REGISTER_SKIP_ENV_DB", "1")
    monkeypatch.setenv("GPT_REGISTER_DB_BACKEND", "sqlite")
    monkeypatch.setenv("GPT_REGISTER_DATABASE_URL", "")
    monkeypatch.setenv("DATABASE_URL", "")

    from application.config_service import ConfigService
    from application.tasks_service import TasksService
    from infrastructure.repositories.accounts_repository import AccountsRepository
    from infrastructure.repositories.config_repository import ConfigRepository
    from infrastructure.repositories.tasks_repository import TasksRepository

    db_path = tmp_path / "task-batch-export.db"
    tasks = TasksRepository(db_path)
    accounts = AccountsRepository(db_path)
    cfg = ConfigRepository(db_path)
    svc = TasksService(repo=tasks, config_service=ConfigService(cfg))

    for index, status in enumerate(("succeeded", "succeeded", "failed")):
        task_id = f"task_export_{index}"
        tasks.create(
            {
                "id": task_id,
                "type": "email-protocol-register-token",
                "status": status,
                "created_at": f"2026-07-23T10:00:0{index}",
                "command": ["python", "-c", "pass"],
                "log_file": str(tmp_path / f"{task_id}.log"),
                "params": {"overrides": {"batch_id": "batch_export_test"}},
            }
        )
        if status == "succeeded":
            accounts.upsert(
                {
                    "account_key": f"user{index}@outlook.com",
                    "email": f"user{index}@outlook.com",
                    "password": f"Pass{index}!",
                    "registration_task_id": task_id,
                    "registration_status": "registered",
                    "platform": "chatgpt",
                }
            )

    result = svc.export_batch_accounts(
        ["batch_export_test"],
        fields=["email", "password", "account_key", "registration_task_id"],
        only_succeeded=True,
        archive_after_export=False,
    )

    assert result["ok"] is True
    assert result["count"] == 2
    assert set(result["exported_keys"]) == {"user0@outlook.com", "user1@outlook.com"}
    emails = {str(item.get("email") or "") for item in result["products"]}
    assert emails == {"user0@outlook.com", "user1@outlook.com"}

    for key in ("user0@outlook.com", "user1@outlook.com"):
        row = accounts.get(key).to_dict()
        assert row.get("export_status") == "bulk_exported"
        assert row.get("export_kind") == "bulk"
        assert str(row.get("exported_at") or "").strip()

    empty = svc.export_batch_accounts([], fields=["email"])
    assert empty["ok"] is False
