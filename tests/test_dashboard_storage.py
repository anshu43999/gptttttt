from __future__ import annotations

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from core.account_store import product_export
from infrastructure import db


def test_sqlite_account_task_flow(tmp_path: Path) -> None:
    db_path = tmp_path / "gpt_register.db"
    if db_path.exists():
        db_path.unlink()
    account_pk = db.upsert_account(
        {
            "account_key": "auth0_test",
            "account_id": "auth0|test",
            "platform": "chatgpt",
            "phone_number": "+15551234567",
            "email": "user@example.com",
            "password": "pass",
            "plan_type": "plus",
            "stage": "complete",
            "status": "complete",
            "paths": {"resume": "output/resume_test.json", "storage_state": "output/storage_test.json"},
            "proxy": {"registration_proxy": "socks5://127.0.0.1:1080", "registration_exit_ip": "1.2.3.4"},
            "raw_tokens": {"access_token": "at", "refresh_token": "rt", "id_token": "id"},
            "activation_id": "activation-internal",
        },
        path=db_path,
    )
    assert account_pk > 0
    account = db.get_account("auth0_test", path=db_path)
    assert account["account_id"] == "auth0|test"
    assert account["paths"]["resume"] == "output/resume_test.json"
    assert account["tokens"]["refresh_token"] == "rt"

    task = db.create_task(
        {
            "id": "task_test",
            "type": "register-token",
            "status": "pending",
            "params": {"phone_country": "BR"},
            "command": ["python", "full_pipeline.py"],
        },
        path=db_path,
    )
    assert task["status"] == "pending"
    db.add_task_event("task_test", "info", "step", "started", {"step": 1}, path=db_path)
    events = db.list_task_events("task_test", path=db_path)
    assert any(event["message"] == "started" for event in events)
    updated = db.update_task("task_test", status="succeeded", result={"exit_code": 0}, path=db_path)
    assert updated["status"] == "succeeded"


def test_product_export_excludes_internal_fields() -> None:
    product = product_export(
        {
            "phone_number": "+15551234567",
            "email": "user@example.com",
            "password": "pass",
            "account_id": "auth0|test",
            "plan_type": "plus",
            "activation_id": "must-not-export",
            "paths": {"storage_state": "secret-storage.json"},
        }
    )
    assert product["email"] == "user@example.com"
    assert "activation_id" not in product
    assert "paths" not in product
    assert "browser_storage_state_path" not in product


if __name__ == "__main__":
    test_sqlite_account_task_flow(Path("tmp/test_dashboard_storage"))
    test_product_export_excludes_internal_fields()
    print("dashboard storage tests passed")
