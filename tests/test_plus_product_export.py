from __future__ import annotations

from pathlib import Path

from application.accounts_service import AccountsService
from infrastructure.repositories.accounts_repository import AccountsRepository
from infrastructure.repositories.resource_pool_repository import ResourcePoolRepository
from infrastructure import db


def test_export_plus_products_txt_formats_outlook_and_icloud(tmp_path: Path) -> None:
    db_path = tmp_path / "plus-export.db"
    accounts = AccountsRepository(db_path)
    resources = ResourcePoolRepository(db_path)

    accounts.upsert({
        "account_key": "alice@outlook.com",
        "email": "alice@outlook.com",
        "password": "ChatGptPass1",
        "plus_status": "verified_plus",
        "plan_type": "plus",
    })
    accounts.upsert({
        "account_key": "bob@icloud.com",
        "email": "bob@icloud.com",
        "password": "ChatGptPass2",
        "plus_status": "verified_plus",
        "plan_type": "plus",
    })
    accounts.upsert({
        "account_key": "free@icloud.com",
        "email": "free@icloud.com",
        "password": "ChatGptPass3",
        "plus_status": "free",
        "plan_type": "free",
    })

    resources.upsert(
        "email",
        "outlook_token",
        "alice@outlook.com",
        {
            "email": "alice@outlook.com",
            "password": "outlook-login-pass",
            "client_id": "client-123",
            "refresh_token": "refresh-token-xyz",
        },
        status="used",
    )
    resources.upsert(
        "email",
        "icloud_api",
        "bob@icloud.com",
        {
            "email": "bob@icloud.com",
            "inbox_url": "https://mail.example/show/bob",
            "code_url": "https://mail.example/api/code/bob",
            "mail_url": "https://mail.example/api/mail/bob",
        },
        status="used",
    )

    svc = AccountsService(repo=accounts)
    result = svc.export_plus_products_txt(
        ["alice@outlook.com", "bob@icloud.com", "free@icloud.com"],
        only_verified=True,
    )

    assert result["count"] == 2
    assert result["skipped_count"] == 1
    assert result["kind_counts"] == {"outlook": 1, "icloud_api": 1}
    lines = [line for line in result["text"].splitlines() if line.strip()]
    assert lines == [
        "alice@outlook.com----outlook-login-pass----client-123----refresh-token-xyz",
        "bob@icloud.com----https://mail.example/show/bob----code:https://mail.example/api/code/bob----mail:https://mail.example/api/mail/bob",
    ]
    alice = accounts.get("alice@outlook.com").to_dict()
    bob = accounts.get("bob@icloud.com").to_dict()
    free = accounts.get("free@icloud.com").to_dict()
    assert alice.get("export_status") == "plus_exported"
    assert alice.get("export_kind") == "plus"
    assert bob.get("export_status") == "plus_exported"
    assert free.get("export_status") in {"", None}


def test_export_plus_products_txt_archive_excludes_next_full_export(tmp_path: Path) -> None:
    db_path = tmp_path / "plus-export-archive.db"
    accounts = AccountsRepository(db_path)
    resources = ResourcePoolRepository(db_path)

    for email in ("first@outlook.com", "second@outlook.com"):
        accounts.upsert({
            "account_key": email,
            "email": email,
            "plus_status": "verified_plus",
            "plan_type": "plus",
        })
        resources.upsert(
            "email",
            "outlook_token",
            email,
            {
                "email": email,
                "password": f"pass-{email}",
                "client_id": "client-123",
                "refresh_token": f"refresh-{email}",
            },
            status="used",
        )

    svc = AccountsService(repo=accounts)

    first = svc.export_plus_products_txt(["first@outlook.com"], only_verified=True, archive_after_export=True)
    assert first["count"] == 1
    assert first["archived"] == 1
    second = svc.export_plus_products_txt(["first@outlook.com", "second@outlook.com"], only_verified=True)

    lines = [line for line in second["text"].splitlines() if line.strip()]
    assert second["count"] == 1
    assert lines == ["second@outlook.com----pass-second@outlook.com----client-123----refresh-second@outlook.com"]
    assert second["skipped_count"] == 1
    assert second["skipped"][0]["reason"] == "账号已归档"


def test_export_at_products_txt_appends_access_token_no_plus_check(tmp_path: Path) -> None:
    db_path = tmp_path / "at-export.db"
    accounts = AccountsRepository(db_path)
    resources = ResourcePoolRepository(db_path)

    accounts.upsert({
        "account_key": "free@outlook.com",
        "email": "free@outlook.com",
        "password": "ChatGptPassFree",
        "plus_status": "free",
        "plan_type": "free",
        "access_token": "eyJhbGciOi.free-at.sig",
    })
    accounts.upsert({
        "account_key": "noat@outlook.com",
        "email": "noat@outlook.com",
        "password": "ChatGptPassNoAt",
        "plus_status": "free",
        "plan_type": "free",
    })
    accounts.upsert({
        "account_key": "bob@icloud.com",
        "email": "bob@icloud.com",
        "password": "ChatGptPassBob",
        "plus_status": "free",
        "plan_type": "free",
        "access_token": "eyJhbGciOi.bob-at.sig",
    })

    resources.upsert(
        "email",
        "outlook_token",
        "free@outlook.com",
        {
            "email": "free@outlook.com",
            "password": "outlook-login-pass",
            "client_id": "client-123",
            "refresh_token": "refresh-token-xyz",
        },
        status="used",
    )
    resources.upsert(
        "email",
        "outlook_token",
        "noat@outlook.com",
        {
            "email": "noat@outlook.com",
            "password": "outlook-login-pass",
            "client_id": "client-123",
            "refresh_token": "refresh-token-noat",
        },
        status="used",
    )
    resources.upsert(
        "email",
        "icloud_api",
        "bob@icloud.com",
        {
            "email": "bob@icloud.com",
            "inbox_url": "https://mail.example/show/bob",
            "code_url": "https://mail.example/api/code/bob",
            "mail_url": "https://mail.example/api/mail/bob",
        },
        status="used",
    )

    svc = AccountsService(repo=accounts)
    result = svc.export_at_products_txt(
        ["free@outlook.com", "noat@outlook.com", "bob@icloud.com"],
    )

    assert result["count"] == 2
    assert result["skipped_count"] == 1
    assert result["kind_counts"] == {"outlook": 1, "icloud_api": 1}
    lines = [line for line in result["text"].splitlines() if line.strip()]
    assert lines == [
        "free@outlook.com----outlook-login-pass----client-123----refresh-token-xyz----eyJhbGciOi.free-at.sig",
        "bob@icloud.com----https://mail.example/show/bob----code:https://mail.example/api/code/bob----mail:https://mail.example/api/mail/bob----eyJhbGciOi.bob-at.sig",
    ]
    free = accounts.get("free@outlook.com").to_dict()
    noat = accounts.get("noat@outlook.com").to_dict()
    assert free.get("export_status") == "at_exported"
    assert free.get("export_kind") == "at"
    assert noat.get("export_status") in {"", None}
    skipped_reasons = {item["email"]: item["reason"] for item in result["skipped"]}
    assert "缺少 access_token" in skipped_reasons.get("noat@outlook.com", "")


def test_export_at_products_txt_supports_custom_domain_without_resource(tmp_path: Path, monkeypatch) -> None:
    """Forwarded/custom domains must still export email----password----access_token."""
    from infrastructure import db_backend

    monkeypatch.setenv("GPT_REGISTER_SKIP_ENV_DB", "1")
    monkeypatch.setenv("GPT_REGISTER_DB_BACKEND", "sqlite")
    monkeypatch.setenv("GPT_REGISTER_DATABASE_URL", "")
    monkeypatch.setenv("DATABASE_URL", "")
    db_backend.reset_backend_cache()

    db_path = tmp_path / "at-export-custom-domain.db"
    accounts = AccountsRepository(db_path)
    accounts.upsert({
        "account_key": "user@5445945.xyz",
        "email": "user@5445945.xyz",
        "password": "ChatGptPassCustom",
        "plus_status": "needs_plus",
        "plan_type": "free",
        "access_token": "eyJhbGciOi.custom-domain-at.sig",
    })

    svc = AccountsService(repo=accounts)
    result = svc.export_at_products_txt(["user@5445945.xyz"])

    assert result["count"] == 1
    assert result["skipped_count"] == 0
    assert result["kind_counts"].get("account") == 1
    lines = [line for line in result["text"].splitlines() if line.strip()]
    assert lines == [
        "user@5445945.xyz----ChatGptPassCustom----eyJhbGciOi.custom-domain-at.sig",
    ]
    row = accounts.get("user@5445945.xyz").to_dict()
    assert row.get("export_status") == "at_exported"
    assert row.get("export_kind") == "at"
