"""Database backend selection: SQLite or Postgres.

Production (2026-07-18 formal flip):
  Project-root env.db selects Postgres. Auto-loaded on first resolve if env unset.
  Explicit backend=postgres without URL is FAIL-CLOSED (no silent SQLite fallback).
  SQLite remains available only when backend is sqlite / no env.db postgres.

Env:
  GPT_REGISTER_DB_BACKEND=sqlite|postgres
  GPT_REGISTER_DATABASE_URL / DATABASE_URL
  GPT_REGISTER_SKIP_ENV_DB=1  — do not auto-load env.db (tests)
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = PROJECT_ROOT / "data"
DEFAULT_SQLITE_PATH = DATA_ROOT / "gpt_register.db"
ENV_DB_FILE = PROJECT_ROOT / "env.db"
DEFAULT_PG_URL = "postgresql://gpt:gpt@127.0.0.1:5432/gpt_register"

_BACKEND_LOCK = threading.Lock()
_RESOLVED_BACKEND: str | None = None
_ENV_DB_LOADED = False


def _env(name: str, default: str = "") -> str:
    return str(os.environ.get(name) or default).strip()


def load_env_db(*, force: bool = False) -> dict[str, str]:
    """Load project-root env.db into os.environ (non-empty existing env wins).

    Returns key→value actually present after load (from file or prior env).
    """
    global _ENV_DB_LOADED
    if _ENV_DB_LOADED and not force:
        out: dict[str, str] = {}
        for key in ("GPT_REGISTER_DB_BACKEND", "GPT_REGISTER_DATABASE_URL", "DATABASE_URL"):
            cur = _env(key)
            if cur:
                out[key] = cur
        return out

    if _env("GPT_REGISTER_SKIP_ENV_DB") in {"1", "true", "yes", "on"}:
        _ENV_DB_LOADED = True
        return {}

    applied: dict[str, str] = {}
    if ENV_DB_FILE.is_file():
        defaults: dict[str, str] = {}
        for raw in ENV_DB_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key:
                defaults[key] = val
        defaults.setdefault("GPT_REGISTER_DB_BACKEND", "postgres")
        defaults.setdefault("GPT_REGISTER_DATABASE_URL", DEFAULT_PG_URL)
        defaults.setdefault("DATABASE_URL", defaults.get("GPT_REGISTER_DATABASE_URL", DEFAULT_PG_URL))
        for key, val in defaults.items():
            cur = _env(key)
            if cur:
                applied[key] = cur
                continue
            os.environ[key] = val
            applied[key] = val
    else:
        for key in ("GPT_REGISTER_DB_BACKEND", "GPT_REGISTER_DATABASE_URL", "DATABASE_URL"):
            cur = _env(key)
            if cur:
                applied[key] = cur

    _ENV_DB_LOADED = True
    return applied


def resolve_backend() -> str:
    """Return 'sqlite' or 'postgres'. Fail-closed: postgres without URL raises on open, not silent sqlite."""
    global _RESOLVED_BACKEND
    with _BACKEND_LOCK:
        if _RESOLVED_BACKEND is not None:
            return _RESOLVED_BACKEND
        load_env_db()
        raw = _env("GPT_REGISTER_DB_BACKEND") or _env("DB_BACKEND")
        url = database_url()
        if raw:
            b = raw.lower()
            if b in {"pg", "postgres", "postgresql"}:
                b = "postgres"
            if b not in {"sqlite", "postgres"}:
                b = "sqlite"
            # Hard cut: explicit postgres stays postgres even if URL empty
            # (open_connection will raise). No silent SQLite fallback.
            _RESOLVED_BACKEND = b
            return b
        if url.startswith("postgres"):
            _RESOLVED_BACKEND = "postgres"
            return "postgres"
        _RESOLVED_BACKEND = "sqlite"
        return "sqlite"


def database_url() -> str:
    load_env_db()
    return _env("GPT_REGISTER_DATABASE_URL") or _env("DATABASE_URL")


def reset_backend_cache() -> None:
    """Test helper — also allows re-load of env.db on next resolve."""
    global _RESOLVED_BACKEND, _ENV_DB_LOADED
    with _BACKEND_LOCK:
        _RESOLVED_BACKEND = None
        _ENV_DB_LOADED = False


def postgres_available() -> bool:
    try:
        import psycopg  # noqa: F401
        return True
    except Exception:
        try:
            import psycopg2  # noqa: F401
            return True
        except Exception:
            return False


class _DictRow(dict):
    """Mapping row with sqlite3.Row-style integer indexing."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def _psycopg_dict_row():
    from psycopg.rows import dict_row

    return dict_row


class _PGCompatConnection:
    """SQLite-shaped Postgres connection used by the existing Python services.

    It preserves dict/index row access and translates placeholder / harmless
    SQLite dialect differences at the DB boundary. Schema migration itself is
    done before flip; executescript is only retained for isolated test/setup use.
    """

    def __init__(self, raw: Any, driver: str) -> None:
        self._raw = raw
        self._driver = driver
        self.row_factory = None

    @staticmethod
    def _convert(sql: str, params: Any = None) -> str:
        """Convert SQLite placeholders and a small set of safe SQL idioms."""
        import re

        out: list[str] = []
        in_str = False
        quote = ""
        i = 0
        while i < len(sql):
            ch = sql[i]
            if in_str:
                out.append(ch)
                if ch == quote:
                    if quote == "'" and i + 1 < len(sql) and sql[i + 1] == "'":
                        out.append(sql[i + 1])
                        i += 2
                        continue
                    in_str = False
                i += 1
                continue
            if ch in {"'", '"'}:
                in_str = True
                quote = ch
                out.append(ch)
            elif ch == "?":
                out.append("%s")
            else:
                out.append(ch)
            i += 1
        converted = "".join(out)
        # Existing Python path uses these SQLite-only idioms in normal writes.
        converted = re.sub(r"\bBEGIN\s+IMMEDIATE\b", "BEGIN", converted, flags=re.IGNORECASE)
        converted = re.sub(r"\blast_insert_rowid\s*\(\s*\)", "lastval()", converted, flags=re.IGNORECASE)
        # Resource leasing reads JSON stored as TEXT. SQLite's json_extract()
        # becomes a PostgreSQL jsonb text projection; payloads are normalized JSON.
        converted = re.sub(
            r"\bjson_extract\s*\(\s*([A-Za-z_][A-Za-z0-9_.]*)\s*,\s*'\$\.([A-Za-z_][A-Za-z0-9_]*)'\s*\)",
            r"(\1::jsonb ->> '\2')",
            converted,
            flags=re.IGNORECASE,
        )
        if isinstance(params, dict):
            # SQLite :field binding → psycopg %(field)s binding; not inside text
            parts = re.split(r"('(?:''|[^'])*')", converted)
            for index in range(0, len(parts), 2):
                parts[index] = re.sub(r":([A-Za-z_][A-Za-z0-9_]*)", r"%(\1)s", parts[index])
            converted = "".join(parts)
        return converted

    def _cursor(self):
        if self._driver == "psycopg":
            return self._raw.cursor(row_factory=_psycopg_dict_row())
        import psycopg2.extras

        return self._raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def execute(self, sql: str, params: Any = None):
        cur = self._cursor()
        sql2 = self._convert(sql, params)
        if params is None:
            cur.execute(sql2)
        else:
            cur.execute(sql2, params)
        return _PGCompatCursor(cur)

    def cursor(self):
        return _PGCompatCursor(self._cursor())

    def executemany(self, sql: str, seq_of_params):
        items = list(seq_of_params)
        cur = self._cursor()
        sample = items[0] if items else None
        cur.executemany(self._convert(sql, sample), items)
        return _PGCompatCursor(cur)

    def executescript(self, script: str) -> None:
        # init_db short-circuits for PG; retain only test/setup compatibility.
        statement: list[str] = []
        for line in script.splitlines():
            if line.strip().upper().startswith("PRAGMA"):
                continue
            statement.append(line)
            if line.rstrip().endswith(";"):
                sql = translate_sqlite_ddl_to_pg("\n".join(statement)).strip()
                if sql:
                    self.execute(sql)
                statement = []
        tail = translate_sqlite_ddl_to_pg("\n".join(statement)).strip()
        if tail:
            self.execute(tail)

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def close(self) -> None:
        self._raw.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class _PGCompatCursor:
    def __init__(self, cur: Any) -> None:
        self._cur = cur

    def execute(self, sql: str, params: Any = None):
        sql2 = _PGCompatConnection._convert(sql, params)
        if params is None:
            self._cur.execute(sql2)
        else:
            self._cur.execute(sql2, params)
        return self

    def fetchone(self):
        return _as_row(self._cur.fetchone())

    def fetchall(self):
        return [_as_row(row) for row in self._cur.fetchall()]

    def fetchmany(self, size: int | None = None):
        rows = self._cur.fetchmany() if size is None else self._cur.fetchmany(size)
        return [_as_row(row) for row in rows]

    @property
    def lastrowid(self):
        return getattr(self._cur, "lastrowid", None)

    @property
    def rowcount(self):
        return self._cur.rowcount

    def close(self) -> None:
        self._cur.close()

    def __iter__(self):
        for row in self._cur:
            yield _as_row(row)


def _as_row(row: Any):
    if row is None:
        return None
    return _DictRow(row if isinstance(row, dict) else dict(row))


def translate_sqlite_ddl_to_pg(stmt: str) -> str:
    """Best-effort SQLite → PG DDL for init_db scripts."""
    import re

    s = stmt
    s = re.sub(
        r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b",
        "SERIAL PRIMARY KEY",
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(r"\bINSERT\s+OR\s+IGNORE\b", "INSERT", s, flags=re.IGNORECASE)
    return s


@contextmanager
def open_connection(path: str | Path | None = None) -> Iterator[Any]:
    """Open SQLite or Postgres connection based on resolve_backend().

    path is only used for SQLite (file path). PG uses DATABASE_URL.
    Postgres selection is fail-closed (no silent SQLite fallback).
    """
    backend = resolve_backend()
    if backend == "postgres":
        url = database_url()
        if not url:
            raise RuntimeError(
                "postgres backend selected but DATABASE_URL/GPT_REGISTER_DATABASE_URL empty "
                "(hard-cut: no SQLite fallback). Fix env.db or set URL."
            )
        if not postgres_available():
            raise RuntimeError(
                "postgres backend requires psycopg: pip install 'psycopg[binary]>=3.1'"
            )
        last_err: Exception | None = None
        try:
            import psycopg

            raw = psycopg.connect(url, autocommit=True)
            conn = _PGCompatConnection(raw, "psycopg")
        except Exception as e1:
            last_err = e1
            try:
                import psycopg2

                raw = psycopg2.connect(url)
                raw.autocommit = True
                conn = _PGCompatConnection(raw, "psycopg2")
                last_err = None
            except Exception as e2:
                raise RuntimeError(
                    f"postgres connect failed (fail-closed, no SQLite fallback): {e2}"
                ) from e2
        if last_err is not None and "conn" not in dir():
            raise RuntimeError(
                f"postgres connect failed (fail-closed, no SQLite fallback): {last_err}"
            ) from last_err
        try:
            yield conn
            try:
                conn.commit()
            except Exception:
                pass
        finally:
            conn.close()
        return

    # SQLite only when backend is sqlite
    target = Path(path or DEFAULT_SQLITE_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-64000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
