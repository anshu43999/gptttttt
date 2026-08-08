#!/usr/bin/env python3
"""Sync GPT Register -> fucccccckgpt, schema-only PG dump, secret scrub, zip.

Usage:
  py -3.13 tools/pack_sanitized_fucccccckgpt.py
  py -3.13 tools/pack_sanitized_fucccccckgpt.py --dst E:/project/fucccccckgpt
  py -3.13 tools/pack_sanitized_fucccccckgpt.py --skip-zip
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import re
import shutil
import zipfile
from pathlib import Path

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None  # type: ignore

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DST = Path(r"E:/project/fucccccckgpt")
DEFAULT_ZIP_DIR = Path(r"E:/project")

SYNC_DIRS = (
    "api",
    "application",
    "core",
    "domain",
    "infrastructure",
    "platforms",
    "providers",
    "registration",
    "services",
    "tests",
    "tools",
    "utils",
    "scripts",
    "frontend",
    "go-email-protocol",
    "docs",
)
SYNC_ROOT_FILES = (
    "__init__.py",
    "main.py",
    "start.py",
    "start.bat",
    "full_pipeline.py",
    "smstome_tool.py",
    "requirements.txt",
    "requirements-dev.txt",
    "env.db.bat",
    "LICENSE",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "THIRD_PARTY_NOTICES.md",
)
SKIP_DIR_NAMES = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "venv",
        "node_modules",
        "dist",
        "build",
        "coverage",
        "htmlcov",
        "test-results",
        ".git",
        ".cache",
        "bin",
        "_fpcheck",
        "_smoke_pg_lease_race",
        "_smoke_pg_store",
        "data",
        "output",
        "tmp",
        "at-file",
    }
)
SKIP_FILE_SUFFIXES = frozenset(
    {
        ".pyc",
        ".pyo",
        ".db",
        ".db-wal",
        ".db-shm",
        ".log",
        ".zip",
        ".tgz",
        ".tar",
        ".gz",
        ".har",
        ".tsbuildinfo",
        ".exe~",
    }
)
BLOCK_REL_PATHS = frozenset(
    {
        "config.yaml",
        "config.local.yaml",
        "env.db",
        "AGENT.md",
        "AI_HANDOFF_SUMMARY.md",
        "REFACTOR_FULLSTACK_PROMPT.md",
        "REFACTOR_PLAN.md",
        "REPAIR_TRACKER.md",
        "STATUS.md",
        "proxies.csv",
        "outlook_accounts.csv",
        "docs/Tips.txt",
        "docs/Tips2.txt",
    }
)
PACKAGE_OWNED = frozenset(
    {
        "README.md",
        "config.example.yaml",
        "env.db.example",
        ".gitignore",
        "docs/INDEX.md",
        "docs/go-protocol-usage.md",
        "docs/operations.md",
        "docs/architecture.md",
        "docs/security-and-data-handling.md",
        "docs/responsible-use.md",
        "docs/release-audit.md",
        "go-email-protocol/README.md",
    }
)
EXCLUDE_TABLES = frozenset({"ping", "x"})
INSERT_RE = re.compile(r"(?im)^\s*INSERT\s+")
SECRET_PATTERNS = [
    # Real JWTs with long segments
    re.compile(r"(?i)eyJ[A-Za-z0-9_-]{30,}\.[A-Za-z0-9_-]{30,}\.[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?i)-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)(sk-|rk-)[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?i)actk_(?!test)[A-Za-z0-9_-]{16,}"),
    re.compile(r"(?i)api_(?!xxx)[A-Za-z0-9]{20,}"),
]
CONFIG_SECRET_ASSIGN = re.compile(
    r"""(?imx)
    ^\s*
    (?P<key>[A-Za-z0-9_.-]*(?:api[_-]?key|secret|password|passwd|token|credentials?))
    \s*[:=]\s*
    (?P<value>[^#
]+)
    """
)
PLACEHOLDER_VALUES = frozenset({
    "", "[]", "{}", "false", "true", "none", "null", "~",
})
BANNED_NAME_FRAGMENTS = (
    "storage_state",
    "network.jsonl",
    "resume_",
    "tokens_",
    "manual_plus_",
)

SANITIZED_CONFIG_EXAMPLE = r"""# GPT Register config template (sanitized)
# Copy to config.yaml and fill your own values. Do not redistribute config.yaml.

output_dir: output

# Main DB is controlled by env.db (start.bat / start.py), not these yaml keys.
# env.db -> GPT_REGISTER_DB_BACKEND=postgres + GPT_REGISTER_DATABASE_URL

headed: false
browser_mode: hybrid
browser_engine: patchright
browser_channel: chromium
browser_profile_mode: per_task
browser_no_viewport: true
email_register_flow: fast
use_camoufox: false
camoufox_geoip: true
camoufox_humanize: true
camoufox_enable_cache: false
locale: ja-JP
timezone_id: Asia/Tokyo
accept_language: ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7
save_session: false
save_tokens: false
max_parallel_tasks: 100
max_register_tasks: 100
max_oauth_tasks: 1
max_plus_verify_workers: 64
save_token_pool: false

sms_provider: herosms_api
sms_phone_url: ""
sms_phone_url_file: ""
sms_api_key: ""
sms_country: "187"
sms_service: "dr"
sms_proxy: "singbox://direct"
sms_code_timeout: 180
phone_retry_limit: 20
country_code: "1"
country_name: "United States"
herosms_fixed_price: false
herosms_max_price: 0.1
herosms_cancel_on_timeout: false
prepare_registration_before_phone: true
precheck_phone_before_sms: true
herosms_presend_cancel_delay: 75

proxy: ""
rotate_proxy_each_attempt: true
lajiao_proxy_api_url: ""
lajiao_proxy_mode: credentials
proxy_seed_styles: bestgo,1024
lajiao_proxy_credential_protocol: socks5
lajiao_proxy_credentials: ""
lajiao_proxy_credentials_file: ""
lajiao_proxy_regions: "JP,US,DE,GB,BR"
lajiao_proxy_num: 1
lajiao_proxy_timeout: 15
lajiao_proxy_max_batches: 10
lajiao_proxy_max_candidates: 60
lajiao_proxy_select_deadline: 300
lajiao_proxy_expected_country: JP
manual_observer_proxy: ""
outlook_graph_max_concurrent: 96

iceaix_api_key: ""
iceaix_base_url: "https://plus.iceaix.com"
iceaix_sms_api: ""
paypal_phone: ""
iceaix_job_timeout: 300
iceaix_poll_interval: 3
iceaix_otp_timeout: 60
iceaix_pplink_retry: 20
iceaix_proxy: ""
iceaix_proxy_jp: ""
iceaix_allow_paid_no_trial: false
iceaix_continue_on_trial_check_error: false
plus_verify_retries: 12
plus_verify_interval: 20

upi_activation_enabled: true
upi_base_url: "https://upi.akkkkk.top"
upi_client_key: ""
upi_client_keys: []
upi_default_channel: "upi"
upi_device_id: "gpt-register"
upi_submit_per_key_per_min: 50
upi_poll_interval_sec: 5
upi_poll_timeout_sec: 1800
upi_auto_verify_plus: true

outlook_email: ""
outlook_password: ""
outlook_web_otp: true
outlook_token_order_file: ""
oauth_client_id: ""
oauth_redirect_uri: "http://localhost:1455/auth/callback"
oauth_callback_mode: cpa
cpa_base_url: ""
cpa_management_key: ""
outlook_failed_retryable_limit: 2
outlook_cooldown_hours: 24
outlook_max_attempts_per_run: 3

sub2api_url: ""
sub2api_admin_key: ""

# Default path: pure-Go + outlook_token (see docs/go-protocol-usage.md)
mailbox_provider: outlook_token
icloud_api_order_file: ""
email_otp_timeout: 120
email_otp_poll_interval: 3
email_protocol_backend: go
go_email_protocol_url: "http://127.0.0.1:18765"
go_email_protocol_timeout_seconds: 900
go_email_protocol_transport: tls
go_email_protocol_mode: pure
mailat_protocol_use_local_bridge: false
codex_protocol_use_local_bridge: false
email_protocol_spawn_mode: inline

icloud_privacy_order_file: ""
mailbox_domain: ""
mailbox_imap_user: ""
mailbox_imap_pass: ""
mailbox_imap_host: ""
mailbox_imap_port: 993
cfworker_api_url: ""
cfworker_admin_token: ""
cfworker_domain: ""

# outlook_token import format:
# email----password----client_id----refresh_token
"""

SANITIZED_GITIGNORE = """# Production/private runtime
config.yaml
config.local.yaml
env.db
output/
data/
tmp/
at-file/
*.db
*.db-*
*.sqlite*
*.log
proxies.csv
outlook_accounts.csv

# Python
__pycache__/
*.py[cod]
*.pyo
.pytest_cache/
.venv/
venv/

# Frontend rebuildables
frontend/node_modules/
frontend/dist/
frontend/*.tsbuildinfo

# Go build scratch
go-email-protocol/bin/
*.exe~

# Sensitive artifacts
resume_*
storage_*
tokens_*
manual_plus_*
**/storage_state.json
**/network.jsonl
**/bodies/

# Local/editor
.env
.env.*
.vscode/
.idea/
*.har
*.zip
"""

PACKAGE_DOCS_INDEX = """# Docs index (fucccccckgpt package)

| Doc | Purpose |
| --- | --- |
| [go-protocol-usage.md](go-protocol-usage.md) | **Required: pure-Go usage** |
| [operations.md](operations.md) | Install / start / ops |
| [architecture.md](architecture.md) | Components and trust boundary |
| [security-and-data-handling.md](security-and-data-handling.md) | Secrets and data |
| [responsible-use.md](responsible-use.md) | Authorization / stop conditions |
| [release-audit.md](release-audit.md) | Re-release audit |
| [DB_POSTGRES_AND_CUTOVER.md](DB_POSTGRES_AND_CUTOVER.md) | PG notes |
| [SUCCESS_RATE_AND_THROUGHPUT_DEV_PLAN.md](SUCCESS_RATE_AND_THROUGHPUT_DEV_PLAN.md) | Success/throughput plan |
| [TRUE_200_CONCURRENCY_DELIVERY.md](TRUE_200_CONCURRENCY_DELIVERY.md) | Concurrency delivery |
| [HIGH_THROUGHPUT_500_PER_10MIN.md](HIGH_THROUGHPUT_500_PER_10MIN.md) | Throughput background |
| [EMAIL_PROTOCOL_GO_PLAN.md](EMAIL_PROTOCOL_GO_PLAN.md) | Go protocol design |
| [LAB_CANARY_VS_MAIN_MERGE_PLAN.md](LAB_CANARY_VS_MAIN_MERGE_PLAN.md) | Lab vs main |

Read first: **go-protocol-usage -> operations -> architecture**.
"""


def log(msg: str) -> None:
    print(msg, flush=True)


def rel_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def should_skip_dir(name: str) -> bool:
    return name in SKIP_DIR_NAMES or name.startswith(".")


def should_skip_file(path: Path, src_root: Path) -> bool:
    rel = rel_posix(path, src_root)
    if rel in BLOCK_REL_PATHS:
        return True
    if path.name in {"config.yaml", "env.db", ".DS_Store"}:
        return True
    if path.suffix.lower() in SKIP_FILE_SUFFIXES:
        return True
    if path.name.lower().endswith(".exe") and path.parent.name == "bin":
        return True
    lower_name = path.name.lower()
    return any(frag in lower_name for frag in BANNED_NAME_FRAGMENTS)


def copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def sync_tree(src_root: Path, dst_root: Path) -> dict[str, int]:
    stats = {"copied": 0, "skipped": 0, "owned_kept": 0, "removed_stale": 0}

    for dirname in SYNC_DIRS:
        sdir = src_root / dirname
        if not sdir.exists():
            log(f"  warn: missing source dir {dirname}")
            continue
        for root, dirs, files in os.walk(sdir):
            dirs[:] = sorted(d for d in dirs if not should_skip_dir(d))
            root_p = Path(root)
            for name in files:
                sp = root_p / name
                if should_skip_file(sp, src_root):
                    stats["skipped"] += 1
                    continue
                rel = rel_posix(sp, src_root)
                if rel in PACKAGE_OWNED:
                    stats["owned_kept"] += 1
                    continue
                dp = dst_root / rel
                if dp.exists() and sp.stat().st_size == dp.stat().st_size:
                    if sp.read_bytes() == dp.read_bytes():
                        stats["skipped"] += 1
                        continue
                copy_file(sp, dp)
                stats["copied"] += 1

    for name in SYNC_ROOT_FILES:
        sp = src_root / name
        if not sp.exists() or name in PACKAGE_OWNED:
            continue
        dp = dst_root / name
        if dp.exists() and sp.read_bytes() == dp.read_bytes():
            stats["skipped"] += 1
            continue
        copy_file(sp, dp)
        stats["copied"] += 1

    for junk in (
        "config.yaml",
        "env.db",
        "proxies.csv",
        "outlook_accounts.csv",
        "AGENT.md",
        "AI_HANDOFF_SUMMARY.md",
        "STATUS.md",
        "REFACTOR_PLAN.md",
        "REFACTOR_FULLSTACK_PROMPT.md",
        "REPAIR_TRACKER.md",
    ):
        p = dst_root / junk
        if p.exists():
            p.unlink()
            stats["removed_stale"] += 1

    for root, dirs, _files in os.walk(dst_root):
        for d in list(dirs):
            if d in SKIP_DIR_NAMES:
                shutil.rmtree(Path(root) / d, ignore_errors=True)
                stats["removed_stale"] += 1
                dirs.remove(d)
    return stats


def ensure_runtime_placeholders(dst: Path) -> None:
    for sub in ("data", "data/imports", "output", "tmp", "at-file"):
        p = dst / sub
        p.mkdir(parents=True, exist_ok=True)
        keep = p / ".gitkeep"
        if not keep.exists():
            keep.write_text("", encoding="utf-8")


def pg_url_from_env(src_root: Path) -> str:
    env_db = src_root / "env.db"
    url = os.environ.get("GPT_REGISTER_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
    if env_db.exists():
        for line in env_db.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() in {"GPT_REGISTER_DATABASE_URL", "DATABASE_URL"} and v.strip():
                url = v.strip()
                break
    return url or "postgresql://gpt:gpt@127.0.0.1:5432/gpt_register"


def _pg_type(data_type: str, udt: str) -> str:
    mapping = {
        "bigint": "bigint",
        "integer": "integer",
        "smallint": "smallint",
        "text": "text",
        "boolean": "boolean",
        "real": "real",
        "double precision": "double precision",
        "numeric": "numeric",
        "json": "json",
        "jsonb": "jsonb",
        "uuid": "uuid",
        "bytea": "bytea",
        "date": "date",
        "timestamp without time zone": "timestamp",
        "timestamp with time zone": "timestamptz",
    }
    return mapping.get(data_type) or udt or data_type


def dump_schema_only(url: str, out_sql: Path, manifest: Path) -> dict:
    if psycopg is None:
        raise RuntimeError("psycopg is required to dump schema")

    with psycopg.connect(url, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.relname
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relkind = 'r'
                ORDER BY 1
                """
            )
            tables = [r[0] for r in cur.fetchall() if r[0] not in EXCLUDE_TABLES]

            row_counts: dict[str, int] = {}
            for t in tables:
                cur.execute(f'SELECT count(*) FROM "{t}"')
                row_counts[t] = int(cur.fetchone()[0])

            cur.execute(
                """
                SELECT table_name, column_name, data_type, udt_name,
                       is_nullable, column_default, is_identity, identity_generation
                FROM information_schema.columns
                WHERE table_schema = 'public'
                ORDER BY table_name, ordinal_position
                """
            )
            cols: dict[str, list] = {}
            for row in cur.fetchall():
                if row[0] in EXCLUDE_TABLES:
                    continue
                cols.setdefault(row[0], []).append(row)

            cur.execute(
                """
                SELECT con.conname, con.contype, rel.relname AS table_name,
                       pg_get_constraintdef(con.oid, true) AS def
                FROM pg_constraint con
                JOIN pg_class rel ON rel.oid = con.conrelid
                JOIN pg_namespace n ON n.oid = rel.relnamespace
                WHERE n.nspname = 'public' AND con.contype IN ('p','u','f')
                ORDER BY rel.relname, con.contype, con.conname
                """
            )
            constraints = [r for r in cur.fetchall() if r[2] not in EXCLUDE_TABLES]

            cur.execute(
                """
                SELECT tablename, indexname, indexdef
                FROM pg_indexes
                WHERE schemaname = 'public'
                ORDER BY tablename, indexname
                """
            )
            constraint_names = {c[0] for c in constraints}
            indexes = []
            for tablename, indexname, indexdef in cur.fetchall():
                if tablename in EXCLUDE_TABLES:
                    continue
                if indexname in constraint_names or indexname.endswith("_pkey"):
                    continue
                indexes.append((tablename, indexname, indexdef))

    lines: list[str] = [
        "-- GPT Register sanitized PostgreSQL database",
        "-- Schema-only, portable (IDENTITY columns; no production sequences/data).",
        "-- Contains NO rows: accounts, tokens, phones, emails, proxies, OTPs, tasks, provider settings.",
        "-- Import:",
        '--   createdb gpt_register',
        '--   psql "postgresql://gpt:gpt@127.0.0.1:5432/gpt_register" -f database/gpt_register_pg_sanitized.sql',
        "-- App start also runs infrastructure/db.py migrations.",
        "",
        "BEGIN;",
        "",
    ]
    for t in reversed(tables):
        lines.append(f'DROP TABLE IF EXISTS "{t}" CASCADE;')
    lines.append("")

    for t in tables:
        col_defs = []
        for row in cols.get(t, []):
            (
                _table,
                column_name,
                data_type,
                udt_name,
                is_nullable,
                column_default,
                is_identity,
                identity_generation,
            ) = row
            typ = _pg_type(data_type, udt_name)
            if is_identity == "YES":
                gen = identity_generation or "BY DEFAULT"
                parts = [f'  "{column_name}" {typ} GENERATED {gen} AS IDENTITY', "NOT NULL"]
            else:
                parts = [f'  "{column_name}" {typ}']
                if column_default is not None and "nextval(" not in str(column_default).lower():
                    parts.append(f"DEFAULT {column_default}")
                if is_nullable == "NO":
                    parts.append("NOT NULL")
            col_defs.append(" ".join(parts))
        lines.append(f'CREATE TABLE "{t}" (')
        lines.append(",\n".join(col_defs))
        lines.append(");")
        lines.append("")

    for conname, _contype, table_name, cdef in constraints:
        lines.append(f'ALTER TABLE ONLY "{table_name}" ADD CONSTRAINT "{conname}" {cdef};')
    if constraints:
        lines.append("")

    for _tablename, _indexname, indexdef in indexes:
        idef = indexdef
        if " IF NOT EXISTS " not in idef.upper():
            idef = idef.replace("CREATE UNIQUE INDEX ", "CREATE UNIQUE INDEX IF NOT EXISTS ", 1)
            idef = idef.replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ", 1)
        lines.append(f"{idef};")
    if indexes:
        lines.append("")

    lines.append("COMMIT;")
    lines.append("")
    sql_text = "\n".join(lines)
    if INSERT_RE.search(sql_text):
        raise RuntimeError("sanitized SQL unexpectedly contains INSERT")
    out_sql.parent.mkdir(parents=True, exist_ok=True)
    out_sql.write_text(sql_text, encoding="utf-8")

    manifest_text = "\n".join(
        [
            "schema-only dump for fucccccckgpt package",
            f"tables={len(tables)}",
            f"excluded_tables={sorted(EXCLUDE_TABLES)}",
            f"source_row_counts_NOT_included_in_sql={row_counts}",
            f"file={out_sql.name}",
            f"bytes={out_sql.stat().st_size}",
            f"sha256={hashlib.sha256(sql_text.encode('utf-8')).hexdigest()}",
            f"generated_at={dt.datetime.now(dt.timezone.utc).isoformat()}",
            "NOTE: production row counts listed only as proof; package SQL has zero rows.",
            "",
        ]
    )
    manifest.write_text(manifest_text, encoding="utf-8")
    return {"tables": tables, "row_counts": row_counts, "bytes": out_sql.stat().st_size}


def copy_worker_exe(src_root: Path, dst_root: Path) -> str:
    src_exe = src_root / "go-email-protocol" / "email-protocol-worker.exe"
    if not src_exe.exists():
        alt = src_root / "email-protocol-worker.exe"
        if alt.exists():
            src_exe = alt
    if not src_exe.exists():
        return "MISSING"
    dst = dst_root / "go-email-protocol" / "email-protocol-worker.exe"
    dst.parent.mkdir(parents=True, exist_ok=True)
    copy_file(src_exe, dst)
    bin_dir = dst_root / "go-email-protocol" / "bin"
    if bin_dir.exists():
        shutil.rmtree(bin_dir, ignore_errors=True)
    for p in (dst_root / "go-email-protocol").glob("*.exe~"):
        p.unlink(missing_ok=True)
    return f"copied {src_exe.stat().st_size} bytes -> {dst}"


def write_package_owned(dst: Path) -> None:
    (dst / "config.example.yaml").write_text(SANITIZED_CONFIG_EXAMPLE, encoding="utf-8")
    (dst / ".gitignore").write_text(SANITIZED_GITIGNORE, encoding="utf-8")
    (dst / "env.db.example").write_text(
        "# Copy to env.db for local Postgres. Adjust credentials before use.\n"
        "# start.bat / start.py load env.db automatically (existing process env wins).\n"
        "GPT_REGISTER_DB_BACKEND=postgres\n"
        "GPT_REGISTER_DATABASE_URL=postgresql://gpt:gpt@127.0.0.1:5432/gpt_register\n"
        "DATABASE_URL=postgresql://gpt:gpt@127.0.0.1:5432/gpt_register\n",
        encoding="utf-8",
    )
    docs = dst / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "INDEX.md").write_text(PACKAGE_DOCS_INDEX, encoding="utf-8")
    readme = dst / "README.md"
    if not readme.exists() or readme.stat().st_size < 200:
        readme.write_text(
            "# GPT Register (fucccccckgpt sanitized package)\n\n"
            "See docs/go-protocol-usage.md and docs/operations.md.\n",
            encoding="utf-8",
        )


def secret_scan(dst: Path) -> list[str]:
    hits: list[str] = []
    text_exts = {
        ".py",
        ".go",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".md",
        ".yaml",
        ".yml",
        ".json",
        ".txt",
        ".sql",
        ".bat",
        ".example",
        ".mod",
        ".sum",
        ".html",
        ".css",
        ".toml",
    }
    for root, dirs, files in os.walk(dst):
        dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES]
        for name in files:
            p = Path(root) / name
            rel = rel_posix(p, dst)
            if name in {"config.yaml", "env.db"}:
                hits.append(f"private file present: {rel}")
                continue
            if p.suffix.lower() in {".db", ".sqlite", ".sqlite3"} and not rel.startswith("database/"):
                hits.append(f"database file: {rel}")
            if p.suffix.lower() not in text_exts and name not in {"LICENSE", "go.mod", "go.sum"}:
                continue
            if p.stat().st_size > 5_000_000:
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if p.suffix.lower() == ".sql" and INSERT_RE.search(text):
                hits.append(f"INSERT in sql: {rel}")
            for pat in SECRET_PATTERNS:
                m = pat.search(text)
                if m:
                    hits.append(f"secret-like {rel}: {m.group(0)[:48]}...")
                    break
            if p.suffix.lower() in {".yaml", ".yml"} or p.name.endswith(".example") or p.name in {"env.db.example"}:
                for m in CONFIG_SECRET_ASSIGN.finditer(text):
                    raw = m.group("value").strip().strip('"').strip("'").strip()
                    low = raw.lower()
                    if low in PLACEHOLDER_VALUES:
                        continue
                    if len(raw) < 6:
                        continue
                    key = m.group("key").lower()
                    if raw.startswith("http://") or raw.startswith("https://") or raw.startswith("singbox://"):
                        # flag only credential-bearing URLs
                        if "@" not in raw and not any(s in key for s in ("token", "password", "secret", "key", "credential")):
                            continue
                        if raw.startswith("https://") and any(s in key for s in ("base_url", "url")) and "@" not in raw:
                            continue
                    if raw in {"actk_...", "api_xxx", "your_key_here", "app_EMoamEEZ73f0CkXaXp7hrann"}:
                        continue
                    # default public oauth client id empty is fine; non-empty public ids still ok if short template
                    hits.append(f"config secret value {rel}: {m.group('key')}={raw[:24]}...")
            if ("lll35844493@" in text or "5445945.xyz" in text) and (
                "config.example" in rel or rel.endswith(".yaml")
            ):
                hits.append(f"personal mailbox residue: {rel}")
    return hits


def make_zip(dst: Path, zip_dir: Path) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_path = zip_dir / f"fucccccckgpt_pg_sanitized_{stamp}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for root, dirs, files in os.walk(dst):
            dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES and d != ".git"]
            for name in files:
                p = Path(root) / name
                if p.suffix.lower() in SKIP_FILE_SUFFIXES:
                    continue
                if name in {"config.yaml", "env.db"}:
                    continue
                arc = Path(dst.name) / p.relative_to(dst)
                zf.write(p, arcname=arc.as_posix())
    return zip_path


def verify_package(dst: Path) -> list[str]:
    problems: list[str] = []
    required = [
        "README.md",
        "start.py",
        "start.bat",
        "config.example.yaml",
        "env.db.example",
        "database/gpt_register_pg_sanitized.sql",
        "database/gpt_register_pg_sanitized_manifest.txt",
        "go-email-protocol/email-protocol-worker.exe",
        "go-email-protocol/cmd/email-protocol-worker/main.go",
        "services/go_registration_batch.py",
        "docs/go-protocol-usage.md",
        "frontend/package.json",
        "requirements.txt",
        "tools/pack_sanitized_fucccccckgpt.py",
    ]
    for r in required:
        if not (dst / r).exists():
            problems.append(f"missing required: {r}")
    for bad in ("config.yaml", "env.db", "data/gpt_register.db", "data/go-email-protocol-ledger.db"):
        if (dst / bad).exists():
            problems.append(f"private present: {bad}")
    sql = (dst / "database/gpt_register_pg_sanitized.sql").read_text(encoding="utf-8", errors="replace")
    if INSERT_RE.search(sql):
        problems.append("sanitized sql has INSERT")
    if "CREATE TABLE" not in sql:
        problems.append("sanitized sql missing CREATE TABLE")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", type=Path, default=ROOT)
    ap.add_argument("--dst", type=Path, default=DEFAULT_DST)
    ap.add_argument("--zip-dir", type=Path, default=DEFAULT_ZIP_DIR)
    ap.add_argument("--skip-zip", action="store_true")
    ap.add_argument("--skip-schema", action="store_true")
    args = ap.parse_args()

    src = args.src.resolve()
    dst = args.dst.resolve()
    log(f"SRC={src}")
    log(f"DST={dst}")
    dst.mkdir(parents=True, exist_ok=True)

    log("== sync code ==")
    stats = sync_tree(src, dst)
    log(f"  {stats}")

    log("== package-owned sanitized templates ==")
    write_package_owned(dst)

    log("== runtime placeholders ==")
    ensure_runtime_placeholders(dst)

    log("== worker exe ==")
    log("  " + copy_worker_exe(src, dst))

    if not args.skip_schema:
        log("== schema-only PG dump ==")
        url = pg_url_from_env(src)
        safe = re.sub(r"://([^:/@]+):([^@]+)@", r"://\1:***@", url)
        log(f"  url={safe}")
        info = dump_schema_only(
            url,
            dst / "database" / "gpt_register_pg_sanitized.sql",
            dst / "database" / "gpt_register_pg_sanitized_manifest.txt",
        )
        log(f"  tables={len(info['tables'])} bytes={info['bytes']}")
        log(f"  row_counts(proof only)={info['row_counts']}")

    log("== secret scan ==")
    hits = secret_scan(dst)
    if hits:
        log(f"  FAIL {len(hits)} hits:")
        for h in hits[:50]:
            log(f"   - {h}")
        return 2
    log("  clean")

    log("== verify ==")
    problems = verify_package(dst)
    if problems:
        for item in problems:
            log(f"  FAIL {item}")
        return 3
    log("  ok")

    if not args.skip_zip:
        log("== zip ==")
        zp = make_zip(dst, args.zip_dir.resolve())
        log(f"  wrote {zp} ({zp.stat().st_size} bytes)")

    log("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
