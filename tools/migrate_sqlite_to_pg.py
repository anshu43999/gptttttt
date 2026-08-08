#!/usr/bin/env python3
"""Export gpt_register.db (SQLite) → Postgres-compatible dump / live import.

Usage:
  # Dry-run: write PG SQL dump (no PG required)
  python tools/migrate_sqlite_to_pg.py export --db data/gpt_register.db --out data/migrations/gpt_register_pg.sql

  # Live import (requires psycopg + DATABASE_URL)
  set DATABASE_URL=postgresql://user:pass@127.0.0.1:5432/gpt_register
  python tools/migrate_sqlite_to_pg.py import --db data/gpt_register.db --url %DATABASE_URL% --drop

  # Schema only
  python tools/migrate_sqlite_to_pg.py export --schema-only --out data/migrations/schema_pg.sql

Notes:
  - Default production remains SQLite until you cut over.
  - Large tables (sms_activations) stream in batches.
  - Does not drop existing PG tables unless --drop.
  - INTEGER columns that actually store text (SQLite affinity) are promoted to TEXT.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT / "data" / "gpt_register.db"
DEFAULT_OUT = PROJECT_ROOT / "data" / "migrations" / "gpt_register_pg.sql"

# Load order: parents before children (FK).
TABLE_ORDER = [
    "accounts",
    "account_credentials",
    "account_proxy",
    "account_artifacts",
    "account_events",
    "sms_activations",
    "tasks",
    "task_events",
    "app_config",
    "provider_settings",
    "resource_pool",
    "email_otp_events",
    "proxies",
    "registration_runs",
]

SKIP_TABLES = {"sqlite_sequence", "sqlite_stat1", "sqlite_stat4"}


def translate_ddl(sql: str) -> str:
    s = sql.strip().rstrip(";")
    if not s:
        return ""
    if s.upper().startswith("PRAGMA"):
        return ""
    s = re.sub(
        r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b",
        "BIGSERIAL PRIMARY KEY",
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(
        r"\bid\s+INTEGER\s+PRIMARY\s+KEY\b(?!\s+AUTOINCREMENT)",
        "id BIGSERIAL PRIMARY KEY",
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(
        r"\bdatetime\s*\(\s*'now'\s*\)",
        "CURRENT_TIMESTAMP",
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(
        r"\bdatetime\s*\(\s*\"now\"\s*\)",
        "CURRENT_TIMESTAMP",
        s,
        flags=re.IGNORECASE,
    )
    return s + ";\n"


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def sql_literal(val: Any) -> str:
    if val is None:
        return "NULL"
    if isinstance(val, (bytes, memoryview)):
        return "E'\\\\x" + bytes(val).hex() + "'"
    if isinstance(val, bool):
        return "TRUE" if val else "FALSE"
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return str(val)
    s = str(val)
    return "'" + s.replace("'", "''") + "'"


def list_user_tables(con: sqlite3.Connection) -> list[str]:
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    names = [r[0] for r in rows if r[0] not in SKIP_TABLES]
    ordered = [t for t in TABLE_ORDER if t in names]
    for n in names:
        if n not in ordered:
            ordered.append(n)
    return ordered


def map_pg_type(con: sqlite3.Connection, table: str, name: str, ctype: str) -> str:
    """Map SQLite declared type → PG; promote INTEGER that holds text to TEXT."""
    ct = (ctype or "TEXT").strip().upper()
    if ct.startswith("INT") or ct in {"INTEGER", "BIGINT", "SMALLINT", "TINYINT"}:
        try:
            kinds = {
                r[0]
                for r in con.execute(
                    f"SELECT DISTINCT typeof({quote_ident(name)}) FROM {quote_ident(table)}"
                )
            }
        except Exception:
            kinds = set()
        # SQLite affinity is loose: production often stores OpenAI user ids in INTEGER cols.
        if kinds - {"integer", "null"}:
            return "TEXT"
        return "BIGINT"
    if ct in {"REAL", "FLOAT", "DOUBLE", "DOUBLE PRECISION"}:
        return "DOUBLE PRECISION"
    if ct in {"BLOB", "BYTEA"}:
        return "BYTEA"
    if ct in {"BOOLEAN", "BOOL"}:
        return "BOOLEAN"
    return "TEXT"


def table_unique_constraints(con: sqlite3.Connection, table: str) -> list[list[str]]:
    """Return inline SQLite UNIQUE constraints omitted from sqlite_master SQL."""
    constraints: list[list[str]] = []
    for row in con.execute(f"PRAGMA index_list({quote_ident(table)})"):
        # Explicit CREATE UNIQUE INDEX statements are emitted by index_ddls().
        # SQLite generates origin='u' auto-indexes for table-level/column UNIQUE.
        if not int(row[2]) or str(row[3]) != "u":
            continue
        index_name = str(row[1])
        columns = [
            str(index_row[2])
            for index_row in con.execute(
                f"PRAGMA index_info({quote_ident(index_name)})"
            )
        ]
        if columns:
            constraints.append(columns)
    return constraints


def table_ddl(con: sqlite3.Connection, table: str) -> str:
    """Build CREATE TABLE from PRAGMA table_info (includes ALTER-added columns)."""
    cols = con.execute(f"PRAGMA table_info({quote_ident(table)})").fetchall()
    if not cols:
        return ""
    parts: list[str] = []
    pk_cols = [c[1] for c in cols if int(c[5] or 0) > 0]
    single_int_pk = len(pk_cols) == 1 and any(
        c[1] == pk_cols[0] and str(c[2] or "").upper().startswith("INT") for c in cols
    )
    for c in cols:
        name, ctype, notnull, dflt, _pk = (
            c[1],
            (c[2] or "TEXT"),
            int(c[3] or 0),
            c[4],
            int(c[5] or 0),
        )
        if single_int_pk and name == pk_cols[0]:
            parts.append(f"  {quote_ident(name)} BIGSERIAL PRIMARY KEY")
            continue
        pg_type = map_pg_type(con, table, name, ctype)
        col = f"  {quote_ident(name)} {pg_type}"
        if notnull and not (single_int_pk and name == pk_cols[0]):
            col += " NOT NULL"
        if len(pk_cols) == 1 and name == pk_cols[0] and not single_int_pk:
            col += " PRIMARY KEY"
        if dflt is not None:
            dflt_s = str(dflt)
            dflt_s = re.sub(
                r"\bdatetime\s*\(\s*'now'\s*\)",
                "CURRENT_TIMESTAMP",
                dflt_s,
                flags=re.IGNORECASE,
            )
            dflt_s = re.sub(
                r"\bdatetime\s*\(\s*\"now\"\s*\)",
                "CURRENT_TIMESTAMP",
                dflt_s,
                flags=re.IGNORECASE,
            )
            col += f" DEFAULT {dflt_s}"
        parts.append(col)
    if len(pk_cols) > 1:
        parts.append(f"  PRIMARY KEY ({', '.join(quote_ident(c) for c in pk_cols)})")
    for unique_cols in table_unique_constraints(con, table):
        parts.append(
            f"  UNIQUE ({', '.join(quote_ident(column) for column in unique_cols)})"
        )
    body = ",\n".join(parts)
    return f"CREATE TABLE IF NOT EXISTS {quote_ident(table)} (\n{body}\n);\n"


def index_ddls(con: sqlite3.Connection, table: str) -> list[str]:
    out: list[str] = []
    for row in con.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
        (table,),
    ):
        sql = translate_ddl(row[0])
        if sql:
            sql = re.sub(
                r"^CREATE\s+INDEX\b",
                "CREATE INDEX IF NOT EXISTS",
                sql,
                count=1,
                flags=re.IGNORECASE,
            )
            sql = re.sub(
                r"^CREATE\s+UNIQUE\s+INDEX\b",
                "CREATE UNIQUE INDEX IF NOT EXISTS",
                sql,
                count=1,
                flags=re.IGNORECASE,
            )
            out.append(sql)
    return out


def iter_rows(
    con: sqlite3.Connection, table: str, batch: int = 500
) -> Iterable[tuple[list[str], list[Any]]]:
    cur = con.execute(f"SELECT * FROM {quote_ident(table)}")
    cols = [d[0] for d in cur.description]
    while True:
        chunk = cur.fetchmany(batch)
        if not chunk:
            break
        yield cols, chunk


def export_sql(
    db_path: Path,
    out_path: Path,
    *,
    schema_only: bool = False,
    tables: Sequence[str] | None = None,
    batch: int = 500,
    drop: bool = False,
) -> dict[str, int]:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    all_tables = list_user_tables(con)
    if tables:
        want = set(tables)
        all_tables = [t for t in all_tables if t in want]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    with out_path.open("w", encoding="utf-8", newline="\n") as f:
        f.write("-- Generated by tools/migrate_sqlite_to_pg.py\n")
        f.write("-- Source: {}\n".format(db_path.as_posix()))
        f.write("BEGIN;\n\n")
        for table in all_tables:
            if drop:
                f.write(f"DROP TABLE IF EXISTS {quote_ident(table)} CASCADE;\n")
            ddl = table_ddl(con, table)
            if ddl:
                f.write(f"-- table {table}\n")
                f.write(ddl if ddl.endswith("\n") else ddl + "\n")
                f.write("\n")
            for idx in index_ddls(con, table):
                f.write(idx)
                f.write("\n")
            if schema_only:
                counts[table] = 0
                continue
            n = 0
            for cols, chunk in iter_rows(con, table, batch=batch):
                col_list = ", ".join(quote_ident(c) for c in cols)
                for row in chunk:
                    vals = ", ".join(sql_literal(row[c]) for c in cols)
                    f.write(
                        f"INSERT INTO {quote_ident(table)} ({col_list}) VALUES ({vals});\n"
                    )
                    n += 1
                if n and n % (batch * 20) == 0:
                    f.write(f"-- progress {table}: {n}\n")
            id_cols = [
                d
                for d in con.execute(f"PRAGMA table_info({quote_ident(table)})")
                if d[1] == "id" and str(d[2] or "").upper().startswith("INT")
            ]
            if n and id_cols:
                f.write(
                    f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                    f"COALESCE((SELECT MAX(id)::bigint FROM {quote_ident(table)}), 1), true);\n"
                )
            counts[table] = n
            f.write(f"-- rows {table}: {n}\n\n")
        f.write("COMMIT;\n")
    con.close()
    return counts


def live_import(
    db_path: Path,
    url: str,
    *,
    tables: Sequence[str] | None = None,
    batch: int = 200,
    drop: bool = False,
    schema_only: bool = False,
) -> dict[str, int]:
    try:
        import psycopg
    except ImportError as e:
        raise SystemExit(
            "psycopg not installed. pip install 'psycopg[binary]>=3.1'\n" + str(e)
        )

    sq = sqlite3.connect(str(db_path))
    sq.row_factory = sqlite3.Row
    all_tables = list_user_tables(sq)
    if tables:
        want = set(tables)
        all_tables = [t for t in all_tables if t in want]

    counts: dict[str, int] = {}
    with psycopg.connect(url) as pg:
        pg.autocommit = False
        with pg.cursor() as cur:
            for table in all_tables:
                if drop:
                    cur.execute(f"DROP TABLE IF EXISTS {quote_ident(table)} CASCADE")
                ddl = table_ddl(sq, table)
                if ddl:
                    cur.execute(ddl if ddl.strip().endswith(";") else ddl + ";")
                for idx in index_ddls(sq, table):
                    try:
                        cur.execute(idx)
                    except Exception as ex:
                        print(f"warn index {table}: {ex}", file=sys.stderr)
                if schema_only:
                    counts[table] = 0
                    pg.commit()
                    continue
                n = 0
                for cols, chunk in iter_rows(sq, table, batch=batch):
                    col_list = ", ".join(quote_ident(c) for c in cols)
                    placeholders = ", ".join(["%s"] * len(cols))
                    sql = (
                        f"INSERT INTO {quote_ident(table)} ({col_list}) "
                        f"VALUES ({placeholders}) ON CONFLICT DO NOTHING"
                    )
                    rows = [tuple(row[c] for c in cols) for row in chunk]
                    cur.executemany(sql, rows)
                    n += len(rows)
                id_meta = [
                    d
                    for d in sq.execute(f"PRAGMA table_info({quote_ident(table)})")
                    if d[1] == "id" and str(d[2] or "").upper().startswith("INT")
                ]
                if n and id_meta:
                    try:
                        cur.execute(
                            "SELECT setval(pg_get_serial_sequence(%s, 'id'), "
                            f"COALESCE((SELECT MAX(id)::bigint FROM {quote_ident(table)}), 1), true)",
                            (table,),
                        )
                    except Exception as ex:
                        print(f"warn setval {table}: {ex}", file=sys.stderr)
                counts[table] = n
                pg.commit()
                print(f"imported {table}: {n}", flush=True)
    sq.close()
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SQLite → Postgres migration helper")
    ap.add_argument("command", choices=["export", "import", "tables"])
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--url", default="", help="Postgres URL for import")
    ap.add_argument("--schema-only", action="store_true")
    ap.add_argument("--drop", action="store_true", help="DROP TABLE before create")
    ap.add_argument("--tables", default="", help="comma-separated table allowlist")
    ap.add_argument("--batch", type=int, default=500)
    args = ap.parse_args(argv)

    tables = [t.strip() for t in args.tables.split(",") if t.strip()] or None

    if args.command == "tables":
        con = sqlite3.connect(str(args.db))
        for t in list_user_tables(con):
            n = con.execute(f"SELECT COUNT(*) FROM {quote_ident(t)}").fetchone()[0]
            print(f"{t}\t{n}")
        con.close()
        return 0

    if args.command == "export":
        counts = export_sql(
            args.db,
            args.out,
            schema_only=args.schema_only,
            tables=tables,
            batch=args.batch,
            drop=args.drop,
        )
        total = sum(counts.values())
        print(f"wrote {args.out} tables={len(counts)} rows={total}")
        for t, n in counts.items():
            print(f"  {t}: {n}")
        return 0

    if args.command == "import":
        url = (args.url or "").strip()
        if not url:
            import os

            url = (
                os.environ.get("GPT_REGISTER_DATABASE_URL")
                or os.environ.get("DATABASE_URL")
                or ""
            ).strip()
        if not url:
            print("need --url or DATABASE_URL", file=sys.stderr)
            return 2
        counts = live_import(
            args.db,
            url,
            tables=tables,
            batch=args.batch,
            drop=args.drop,
            schema_only=args.schema_only,
        )
        print("import done", sum(counts.values()), "rows")
        for t, n in counts.items():
            print(f"  {t}: {n}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
