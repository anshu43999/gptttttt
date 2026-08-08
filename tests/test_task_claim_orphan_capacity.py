from __future__ import annotations

from pathlib import Path


def test_claim_sets_starting_and_requeues_aged_zombies(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GPT_REGISTER_SKIP_ENV_DB", "1")
    monkeypatch.setenv("GPT_REGISTER_DB_BACKEND", "sqlite")
    monkeypatch.setenv("GPT_REGISTER_DATABASE_URL", "")
    monkeypatch.setenv("DATABASE_URL", "")

    from infrastructure.repositories.tasks_repository import TasksRepository

    db_path = tmp_path / "claim-orphan.db"
    repo = TasksRepository(db_path)

    # Aged starting + aged nopid running used to fill max_parallel forever.
    for i in range(2):
        repo.create(
            {
                "id": f"zombie-{i}",
                "type": "email-protocol-register-token",
                "status": "starting" if i == 0 else "running",
                "started_at": "2020-01-01T00:00:00",
                "updated_at": "2020-01-01T00:00:00",
                "command": ["python", "-c", "pass"],
                "log_file": str(tmp_path / f"zombie-{i}.log"),
                "result": {},
            }
        )
    repo.create(
        {
            "id": "queued-real",
            "type": "email-protocol-register-token",
            "status": "queued",
            "created_at": "2026-07-23T12:00:00",
            "command": ["python", "-c", "pass"],
            "log_file": str(tmp_path / "queued-real.log"),
        }
    )

    claimed = repo.claim_next_queued_task(
        max_parallel=2,
        bucket_limits={"register": 2},
        bucket_for_type=lambda t: "register",
        orphan_grace_seconds=45,
    )
    assert claimed is not None
    assert claimed.id == "queued-real"
    assert claimed.status == "starting"
    assert repo.get("zombie-0").status == "queued"
    assert repo.get("zombie-1").status == "queued"


def test_claim_counts_pid_running_and_fresh_starting(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GPT_REGISTER_SKIP_ENV_DB", "1")
    monkeypatch.setenv("GPT_REGISTER_DB_BACKEND", "sqlite")
    monkeypatch.setenv("GPT_REGISTER_DATABASE_URL", "")
    monkeypatch.setenv("DATABASE_URL", "")

    from infrastructure import db
    from infrastructure.repositories.tasks_repository import TasksRepository

    db_path = tmp_path / "claim-live.db"
    repo = TasksRepository(db_path)
    now = db.now_iso()

    # Live pid seat + fresh starting fill limit=2.
    repo.create(
        {
            "id": "with-pid",
            "type": "email-protocol-register-token",
            "status": "running",
            "started_at": now,
            "command": ["python", "-c", "pass"],
            "log_file": str(tmp_path / "with-pid.log"),
            "result": {"pid": 12345},
        }
    )
    repo.create(
        {
            "id": "fresh-starting",
            "type": "email-protocol-register-token",
            "status": "starting",
            "started_at": now,
            "command": ["python", "-c", "pass"],
            "log_file": str(tmp_path / "fresh-starting.log"),
            "result": {},
        }
    )
    repo.create(
        {
            "id": "queued-blocked",
            "type": "email-protocol-register-token",
            "status": "queued",
            "command": ["python", "-c", "pass"],
            "log_file": str(tmp_path / "queued-blocked.log"),
        }
    )

    claimed = repo.claim_next_queued_task(
        max_parallel=2,
        bucket_limits={"register": 2},
        bucket_for_type=lambda t: "register",
        orphan_grace_seconds=120,
    )
    assert claimed is None
    assert repo.get("queued-blocked").status == "queued"
    assert repo.get("fresh-starting").status == "starting"


def test_managed_task_promotes_starting_to_running_with_pid(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GPT_REGISTER_SKIP_ENV_DB", "1")
    monkeypatch.setenv("GPT_REGISTER_DB_BACKEND", "sqlite")
    monkeypatch.setenv("GPT_REGISTER_DATABASE_URL", "")
    monkeypatch.setenv("DATABASE_URL", "")

    import subprocess
    import time

    from infrastructure.repositories.tasks_repository import TasksRepository
    from services.task_runtime import ManagedTask

    db_path = tmp_path / "promote.db"
    repo = TasksRepository(db_path)
    log_file = tmp_path / "promote.log"
    repo.create(
        {
            "id": "promo-1",
            "type": "email-protocol-register-token",
            "status": "starting",
            "started_at": "2026-07-23T12:00:00",
            "command": ["python", "-c", "import time; time.sleep(0.2)"],
            "log_file": str(log_file),
            "result": {},
        }
    )

    done = {"code": None}

    def on_finish(code: int) -> None:
        done["code"] = code

    managed = ManagedTask("promo-1", ["python", "-c", "import time; time.sleep(0.2)"], str(log_file), repo)
    managed.start(on_finish=on_finish)
    deadline = time.time() + 5
    while time.time() < deadline:
        row = repo.get("promo-1").to_dict()
        if str(row.get("status") or "") == "running":
            result = row.get("result") if isinstance(row.get("result"), dict) else {}
            assert int(result.get("pid") or 0) > 0
            break
        time.sleep(0.05)
    else:
        raise AssertionError("task never promoted to running with pid")

    while done["code"] is None and time.time() < deadline + 3:
        time.sleep(0.05)
    assert done["code"] == 0
    assert repo.get("promo-1").status == "succeeded"
