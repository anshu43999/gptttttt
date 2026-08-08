"""DB backend selection (SQLite default, Postgres opt-in)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from infrastructure import db_backend as backend


@pytest.fixture(autouse=True)
def _reset_backend(monkeypatch):
    backend.reset_backend_cache()
    monkeypatch.delenv("GPT_REGISTER_DB_BACKEND", raising=False)
    monkeypatch.delenv("DB_BACKEND", raising=False)
    monkeypatch.delenv("GPT_REGISTER_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("GPT_REGISTER_SKIP_ENV_DB", "1")
    yield
    backend.reset_backend_cache()


def test_default_backend_is_sqlite():
    assert backend.resolve_backend() == "sqlite"


def test_postgres_without_url_is_fail_closed(monkeypatch):
    monkeypatch.setenv("GPT_REGISTER_DB_BACKEND", "postgres")
    backend.reset_backend_cache()
    assert backend.resolve_backend() == "postgres"
    with pytest.raises(RuntimeError, match="no SQLite fallback"):
        with backend.open_connection():
            pass


def test_postgres_with_url(monkeypatch):
    monkeypatch.setenv("GPT_REGISTER_DB_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@127.0.0.1:5432/gpt")
    backend.reset_backend_cache()
    assert backend.resolve_backend() == "postgres"


def test_database_url_implies_postgres(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@localhost/db")
    backend.reset_backend_cache()
    assert backend.resolve_backend() == "postgres"


def test_translate_autoincrement():
    sql = "CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)"
    out = backend.translate_sqlite_ddl_to_pg(sql)
    assert "SERIAL PRIMARY KEY" in out
    assert "AUTOINCREMENT" not in out.upper()


def test_placeholder_convert():
    conn = backend._PGCompatConnection(raw=None, driver="psycopg")  # type: ignore[arg-type]
    assert conn._convert("SELECT * FROM t WHERE a=? AND b=?") == "SELECT * FROM t WHERE a=%s AND b=%s"
    assert conn._convert("SELECT '?' as q") == "SELECT '?' as q"

def test_json_extract_convert():
    conn = backend._PGCompatConnection(raw=None, driver="psycopg")  # type: ignore[arg-type]
    sql = "SELECT json_extract(payload_json, '$.region') WHERE json_extract(busy.payload_json, '$.exit_ip')=?"
    assert conn._convert(sql) == (
        "SELECT (payload_json::jsonb ->> 'region') WHERE "
        "(busy.payload_json::jsonb ->> 'exit_ip')=%s"
    )


def test_sqlite_connect_still_works(tmp_path: Path):
    db_path = tmp_path / "t.db"
    with backend.open_connection(db_path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS ping (id INTEGER PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO ping(v) VALUES (?)", ("ok",))
        row = conn.execute("SELECT v FROM ping").fetchone()
        assert row["v"] == "ok" or row[0] == "ok"


def test_db_connect_wrapper(tmp_path: Path, monkeypatch):
    from infrastructure import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "wrap.db")
    backend.reset_backend_cache()
    with db.connect(tmp_path / "wrap.db") as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS x (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO x DEFAULT VALUES")
