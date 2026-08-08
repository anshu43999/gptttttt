"""Dry-run export for SQLite→PG migration tool (no Postgres required)."""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
TOOL = PROJECT / "tools" / "migrate_sqlite_to_pg.py"


def test_export_schema_and_sample(tmp_path: Path):
    db = tmp_path / "src.db"
    con = sqlite3.connect(str(db))
    con.executescript(
        """
        CREATE TABLE accounts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          account_key TEXT NOT NULL UNIQUE,
          email TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE app_config (
          key TEXT PRIMARY KEY,
          value_json TEXT,
          updated_at TEXT NOT NULL
        );
        INSERT INTO accounts(account_key, email, created_at, updated_at)
        VALUES ('k1', 'a@b.c', 't0', 't1');
        INSERT INTO app_config(key, value_json, updated_at) VALUES ('x', '1', 't');
        """
    )
    con.commit()
    con.close()

    out = tmp_path / "out.sql"
    r = subprocess.run(
        [
            sys.executable,
            str(TOOL),
            "export",
            "--db",
            str(db),
            "--out",
            str(out),
            "--tables",
            "accounts,app_config",
        ],
        cwd=str(PROJECT),
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    text = out.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS" in text
    assert "SERIAL PRIMARY KEY" in text
    assert "INSERT INTO \"accounts\"" in text
    assert "a@b.c" in text
    assert 'UNIQUE ("account_key")' in text
    assert "AUTOINCREMENT" not in text.upper()


def test_tables_command_on_real_db():
    db = PROJECT / "data" / "gpt_register.db"
    if not db.exists():
        return
    r = subprocess.run(
        [sys.executable, str(TOOL), "tables", "--db", str(db)],
        cwd=str(PROJECT),
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    assert "accounts" in r.stdout
