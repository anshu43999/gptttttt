from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = PROJECT_ROOT / "data"
DB_PATH = DATA_ROOT / "gpt_register.db"

_INIT_DB_LOCK = threading.Lock()
_INIT_DB_DONE: set[str] = set()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(value: str | bytes | None, default: Any = None) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)  # type: ignore[arg-type]
    except Exception:
        return default

def normalize_ignored_codes(value: Any, *, limit: int = 20) -> list[str]:
    if isinstance(value, str):
        parsed = loads(value, [])
        value = parsed if isinstance(parsed, list) else [value]
    if not isinstance(value, list):
        return []
    codes = [str(item) for item in value if str(item or "").strip()]
    return codes[-max(1, limit):]



@contextmanager
def connect(path: str | Path | None = None):
    """Open DB connection. Default SQLite; Postgres when GPT_REGISTER_DB_BACKEND=postgres + DATABASE_URL."""
    from infrastructure.db_backend import open_connection
    with open_connection(path or DB_PATH) as conn:
        yield conn


def init_db(path: str | Path | None = None) -> None:
    """Create schema once per process. Backfill UPDATEs run only on first open.

    Previously every list/get/export call re-ran multi-second full-table UPDATEs,
    which under concurrent tasks locked the whole UI (export stuck on 150 accounts).

    Postgres: schema via translated DDL; SQLite-only PRAGMA backfills are skipped.
    """
    from infrastructure.db_backend import resolve_backend
    backend = resolve_backend()
    target = f"pg:{__import__('os').environ.get('DATABASE_URL') or __import__('os').environ.get('GPT_REGISTER_DATABASE_URL') or 'default'}" if backend == "postgres" else str(Path(path or DB_PATH).resolve())
    with _INIT_DB_LOCK:
        if target in _INIT_DB_DONE:
            return
        with connect(path) as conn:
            conn.executescript(
                """
            CREATE TABLE IF NOT EXISTS accounts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              account_key TEXT NOT NULL UNIQUE,
              account_id TEXT,
              platform TEXT NOT NULL DEFAULT 'chatgpt',
              login_identifier TEXT DEFAULT '',
              phone_number TEXT,
              email TEXT,
              billing_email TEXT DEFAULT '',
              codex_email TEXT DEFAULT '',
              password TEXT,
              plan_type TEXT,
              status TEXT,
              stage TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              activation_client_key_hash TEXT DEFAULT '',
              activation_submission_claim TEXT DEFAULT '',
              registration_mode TEXT DEFAULT '',
              registration_status TEXT DEFAULT '',
              registration_task_id TEXT DEFAULT '',
              registration_started_at TEXT DEFAULT '',
              registration_completed_at TEXT DEFAULT '',
              registration_error TEXT DEFAULT '',
              display_name TEXT DEFAULT '',
              plus_status TEXT DEFAULT '',
              plus_verified_at TEXT DEFAULT '',
              plus_check_source TEXT DEFAULT '',
              plus_check_error TEXT DEFAULT '',
              binding_status TEXT DEFAULT '',
              binding_task_id TEXT DEFAULT '',
              binding_provider TEXT DEFAULT '',
              binding_phone_number TEXT DEFAULT '',
              binding_started_at TEXT DEFAULT '',
              binding_completed_at TEXT DEFAULT '',
              binding_error TEXT DEFAULT '',
              oauth_callback_mode TEXT DEFAULT '',
              cpa_base_url TEXT DEFAULT '',
              cpa_submitted_at TEXT DEFAULT '',
              cpa_submit_status TEXT DEFAULT '',
              cpa_submit_error TEXT DEFAULT '',
              cpa_auth_file_name TEXT DEFAULT '',
              cpa_auth_file_json TEXT DEFAULT '',
              cpa_synced_at TEXT DEFAULT '',
              cpa_sync_error TEXT DEFAULT '',
              registration_phone_resource_id INTEGER DEFAULT 0,
              binding_phone_resource_id INTEGER DEFAULT 0,
              email_resource_id INTEGER DEFAULT 0,
              proxy_resource_id INTEGER DEFAULT 0,
              registration_proxy_exit_ip TEXT DEFAULT '',
              registration_proxy_region TEXT DEFAULT '',
              resume_file TEXT DEFAULT '',
              storage_file TEXT DEFAULT '',
              account_file TEXT DEFAULT '',
              account_health_status TEXT DEFAULT '',
              account_health_checked_at TEXT DEFAULT '',
              account_health_source TEXT DEFAULT '',
              account_health_error TEXT DEFAULT '',
              account_health_detail_json TEXT DEFAULT '',
              last_error TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_accounts_stage ON accounts(stage);
            CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status);
            CREATE INDEX IF NOT EXISTS idx_accounts_email ON accounts(email);
            CREATE INDEX IF NOT EXISTS idx_accounts_phone ON accounts(phone_number);

            CREATE TABLE IF NOT EXISTS account_credentials (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              account_id_ref INTEGER NOT NULL UNIQUE,
              access_token TEXT DEFAULT '',
              refresh_token TEXT DEFAULT '',
              id_token TEXT DEFAULT '',
              chatgpt_access_token_initial TEXT DEFAULT '',
              token_expires_at TEXT DEFAULT '',
              created_at TEXT DEFAULT '',
              updated_at TEXT DEFAULT '',
              FOREIGN KEY(account_id_ref) REFERENCES accounts(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS account_proxy (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              account_id_ref INTEGER NOT NULL UNIQUE,
              registration_proxy TEXT DEFAULT '',
              registration_exit_ip TEXT DEFAULT '',
              registration_country TEXT DEFAULT '',
              subscription_check_proxy TEXT DEFAULT '',
              subscription_check_source TEXT DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(account_id_ref) REFERENCES accounts(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS account_artifacts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              account_id_ref INTEGER NOT NULL,
              artifact_type TEXT NOT NULL,
              path TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(account_id_ref, artifact_type, path),
              FOREIGN KEY(account_id_ref) REFERENCES accounts(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_account_artifacts_ref ON account_artifacts(account_id_ref);

            CREATE TABLE IF NOT EXISTS account_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              account_key TEXT NOT NULL,
              task_id TEXT DEFAULT '',
              event_type TEXT NOT NULL,
              status TEXT DEFAULT '',
              message TEXT DEFAULT '',
              payload_json TEXT DEFAULT '{}',
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_account_events_key ON account_events(account_key, created_at DESC);

            CREATE TABLE IF NOT EXISTS sms_activations (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              account_id_ref INTEGER,
              provider TEXT DEFAULT '',
              activation_id TEXT DEFAULT '',
              phone_number TEXT DEFAULT '',
              sms_url TEXT DEFAULT '',
              country TEXT DEFAULT '',
              status TEXT DEFAULT '',
              last_code TEXT DEFAULT '',
              ignored_codes TEXT DEFAULT '[]',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(account_id_ref) REFERENCES accounts(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS tasks (
              id TEXT PRIMARY KEY,
              task_type TEXT NOT NULL,
              status TEXT NOT NULL,
              account_id_ref INTEGER,
              params_json TEXT DEFAULT '{}',
              result_json TEXT DEFAULT '{}',
              created_at TEXT NOT NULL,
              started_at TEXT DEFAULT '',
              finished_at TEXT DEFAULT '',
              updated_at TEXT NOT NULL,
              error TEXT DEFAULT '',
              retryable INTEGER DEFAULT 0,
              command_json TEXT DEFAULT '[]',
              log_file TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
            CREATE INDEX IF NOT EXISTS idx_tasks_type ON tasks(task_type);
            CREATE INDEX IF NOT EXISTS idx_tasks_status_created ON tasks(status, created_at, id);

            CREATE TABLE IF NOT EXISTS task_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              task_id TEXT NOT NULL,
              timestamp TEXT NOT NULL,
              level TEXT DEFAULT 'info',
              event_type TEXT DEFAULT '',
              message TEXT DEFAULT '',
              data_json TEXT DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id, id);

            CREATE TABLE IF NOT EXISTS app_config (
              key TEXT PRIMARY KEY,
              value_json TEXT,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS provider_settings (
              provider_type TEXT NOT NULL,
              provider_name TEXT NOT NULL,
              enabled INTEGER DEFAULT 1,
              settings_json TEXT DEFAULT '{}',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(provider_type, provider_name)
            );

            CREATE TABLE IF NOT EXISTS resource_pool (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              resource_type TEXT NOT NULL,
              provider TEXT NOT NULL,
              resource_key TEXT NOT NULL,
              payload_json TEXT DEFAULT '{}',
              status TEXT NOT NULL DEFAULT 'available',
              lease_id TEXT DEFAULT '',
              leased_at TEXT DEFAULT '',
              cooldown_until TEXT DEFAULT '',
              success_count INTEGER DEFAULT 0,
              fail_count INTEGER DEFAULT 0,
              last_error TEXT DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(resource_type, provider, resource_key)
            );
            CREATE INDEX IF NOT EXISTS idx_resource_pool_status ON resource_pool(resource_type, provider, status);

            CREATE TABLE IF NOT EXISTS email_otp_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              email TEXT NOT NULL,
              code TEXT DEFAULT '',
              subject TEXT DEFAULT '',
              body TEXT DEFAULT '',
              received_at TEXT DEFAULT '',
              consumed INTEGER DEFAULT 0,
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_email_otp_email ON email_otp_events(email, consumed, id DESC);

            CREATE TABLE IF NOT EXISTS proxies (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              url TEXT NOT NULL UNIQUE,
              exit_ip TEXT DEFAULT '',
              region TEXT DEFAULT '',
              source TEXT DEFAULT 'manual',
              is_active INTEGER DEFAULT 1,
              last_checked TEXT DEFAULT '',
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS registration_runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              run_id TEXT UNIQUE,
              status TEXT DEFAULT '',
              stage TEXT DEFAULT '',
              failure_code TEXT DEFAULT '',
              account_email TEXT DEFAULT '',
              account_id TEXT DEFAULT '',
              plan_type TEXT DEFAULT '',
              access_token_obtained INTEGER DEFAULT 0,
              refresh_token_obtained INTEGER DEFAULT 0,
              steps_completed TEXT DEFAULT '[]',
              errors TEXT DEFAULT '[]',
              started_at TEXT,
              finished_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_reg_runs_status ON registration_runs(status, started_at);

            CREATE TABLE IF NOT EXISTS plus_activation_batches (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              batch_key TEXT NOT NULL UNIQUE,
              name TEXT NOT NULL DEFAULT '',
              provider TEXT NOT NULL DEFAULT 'upi',
              channel TEXT NOT NULL DEFAULT 'upi',
              status TEXT NOT NULL DEFAULT 'queued',
              requested_count INTEGER NOT NULL DEFAULT 0,
              accepted_count INTEGER NOT NULL DEFAULT 0,
              skipped_count INTEGER NOT NULL DEFAULT 0,
              total_count INTEGER NOT NULL DEFAULT 0,
              reserved_count INTEGER NOT NULL DEFAULT 0,
              queued_count INTEGER NOT NULL DEFAULT 0,
              submitting_count INTEGER NOT NULL DEFAULT 0,
              submit_unknown_count INTEGER NOT NULL DEFAULT 0,
              submitted_count INTEGER NOT NULL DEFAULT 0,
              processing_count INTEGER NOT NULL DEFAULT 0,
              verifying_count INTEGER NOT NULL DEFAULT 0,
              verified_count INTEGER NOT NULL DEFAULT 0,
              failed_count INTEGER NOT NULL DEFAULT 0,
              releasable_count INTEGER NOT NULL DEFAULT 0,
              released_count INTEGER NOT NULL DEFAULT 0,
              exported_count INTEGER NOT NULL DEFAULT 0,
              archived_count INTEGER NOT NULL DEFAULT 0,
              cdk_consumed_count INTEGER NOT NULL DEFAULT 0,
              submit_rate_per_min INTEGER NOT NULL DEFAULT 0,
              max_in_flight INTEGER NOT NULL DEFAULT 0,
              progress_percent INTEGER NOT NULL DEFAULT 0,
              success_rate_percent INTEGER NOT NULL DEFAULT 0,
              last_error TEXT NOT NULL DEFAULT '',
              last_error_code TEXT NOT NULL DEFAULT '',
              error_summary_json TEXT NOT NULL DEFAULT '{}',
              created_by TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              started_at TEXT NOT NULL DEFAULT '',
              finished_at TEXT NOT NULL DEFAULT '',
              updated_at TEXT NOT NULL,
              archived_at TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_plus_batches_status_updated ON plus_activation_batches(status, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_plus_batches_created ON plus_activation_batches(created_at DESC);

            CREATE TABLE IF NOT EXISTS plus_activation_batch_items (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              batch_id INTEGER NOT NULL,
              batch_key TEXT NOT NULL,
              item_key TEXT NOT NULL UNIQUE,
              account_id_ref INTEGER NOT NULL,
              account_key TEXT NOT NULL,
              email TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'queued',
              provider TEXT NOT NULL DEFAULT 'upi',
              channel TEXT NOT NULL DEFAULT 'upi',
              remote_task_id TEXT NOT NULL DEFAULT '',
              idempotency_key TEXT NOT NULL DEFAULT '',
              client_key_hash TEXT NOT NULL DEFAULT '',
              activation_attempt INTEGER NOT NULL DEFAULT 0,
              retry_count INTEGER NOT NULL DEFAULT 0,
              activation_error TEXT NOT NULL DEFAULT '',
              activation_error_code TEXT NOT NULL DEFAULT '',
              activation_display TEXT NOT NULL DEFAULT '',
              can_release INTEGER NOT NULL DEFAULT 0,
              cdk_consumed INTEGER NOT NULL DEFAULT 0,
              exported_at TEXT NOT NULL DEFAULT '',
              export_key TEXT NOT NULL DEFAULT '',
              archived_at TEXT NOT NULL DEFAULT '',
              submitted_at TEXT NOT NULL DEFAULT '',
              finished_at TEXT NOT NULL DEFAULT '',
              released_at TEXT NOT NULL DEFAULT '',
              last_polled_at TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(batch_id) REFERENCES plus_activation_batches(id) ON DELETE CASCADE,
              FOREIGN KEY(account_id_ref) REFERENCES accounts(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_plus_items_batch_status_updated ON plus_activation_batch_items(batch_id, status, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_plus_items_batch_account ON plus_activation_batch_items(batch_id, account_key);
            CREATE INDEX IF NOT EXISTS idx_plus_items_status_updated ON plus_activation_batch_items(status, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_plus_items_remote_task ON plus_activation_batch_items(remote_task_id);
            CREATE INDEX IF NOT EXISTS idx_plus_items_idempotency ON plus_activation_batch_items(idempotency_key);
            CREATE UNIQUE INDEX IF NOT EXISTS uq_plus_items_one_active_per_account
              ON plus_activation_batch_items(account_id_ref)
              WHERE status IN ('reserved','queued','submitting','submit_unknown','submitted','processing','verifying','verified','failed','releasable','exported');

            CREATE TABLE IF NOT EXISTS plus_activation_exports (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              export_key TEXT NOT NULL UNIQUE,
              batch_id INTEGER NOT NULL,
              batch_key TEXT NOT NULL,
              kind TEXT NOT NULL DEFAULT 'plus_verified',
              format TEXT NOT NULL DEFAULT 'txt',
              file_path TEXT NOT NULL DEFAULT '',
              file_name TEXT NOT NULL DEFAULT '',
              count INTEGER NOT NULL DEFAULT 0,
              checksum TEXT NOT NULL DEFAULT '',
              include_already_exported INTEGER NOT NULL DEFAULT 0,
              archive_after_export INTEGER NOT NULL DEFAULT 1,
              created_by TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              FOREIGN KEY(batch_id) REFERENCES plus_activation_batches(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_plus_exports_batch_created ON plus_activation_exports(batch_id, created_at DESC);
                """
            )
            # Shared account-column migrations for both backends. Fresh Postgres
            # CREATE TABLE above historically omitted activation_* fields; claim/
            # state-machine SQL references them immediately.
            account_column_defs = {
                "login_identifier": "TEXT DEFAULT ''",
                "registration_mode": "TEXT DEFAULT ''",
                "registration_status": "TEXT DEFAULT ''",
                "registration_task_id": "TEXT DEFAULT ''",
                "registration_started_at": "TEXT DEFAULT ''",
                "registration_completed_at": "TEXT DEFAULT ''",
                "registration_error": "TEXT DEFAULT ''",
                "display_name": "TEXT DEFAULT ''",
                "plus_status": "TEXT DEFAULT ''",
                "plus_verified_at": "TEXT DEFAULT ''",
                "plus_check_source": "TEXT DEFAULT ''",
                "plus_check_error": "TEXT DEFAULT ''",
                "binding_status": "TEXT DEFAULT ''",
                "binding_task_id": "TEXT DEFAULT ''",
                "binding_provider": "TEXT DEFAULT ''",
                "binding_phone_number": "TEXT DEFAULT ''",
                "binding_started_at": "TEXT DEFAULT ''",
                "binding_completed_at": "TEXT DEFAULT ''",
                "binding_error": "TEXT DEFAULT ''",
                "oauth_callback_mode": "TEXT DEFAULT ''",
                "cpa_base_url": "TEXT DEFAULT ''",
                "cpa_submitted_at": "TEXT DEFAULT ''",
                "cpa_submit_status": "TEXT DEFAULT ''",
                "cpa_submit_error": "TEXT DEFAULT ''",
                "cpa_auth_file_name": "TEXT DEFAULT ''",
                "cpa_auth_file_json": "TEXT DEFAULT ''",
                "cpa_synced_at": "TEXT DEFAULT ''",
                "cpa_sync_error": "TEXT DEFAULT ''",
                "registration_phone_resource_id": "INTEGER DEFAULT 0",
                "binding_phone_resource_id": "INTEGER DEFAULT 0",
                "email_resource_id": "INTEGER DEFAULT 0",
                "billing_email": "TEXT DEFAULT ''",
                "codex_email": "TEXT DEFAULT ''",
                "proxy_resource_id": "INTEGER DEFAULT 0",
                "registration_proxy_exit_ip": "TEXT DEFAULT ''",
                "registration_proxy_region": "TEXT DEFAULT ''",
                "resume_file": "TEXT DEFAULT ''",
                "storage_file": "TEXT DEFAULT ''",
                "account_file": "TEXT DEFAULT ''",
                "account_health_status": "TEXT DEFAULT ''",
                "account_health_checked_at": "TEXT DEFAULT ''",
                "account_health_source": "TEXT DEFAULT ''",
                "account_health_error": "TEXT DEFAULT ''",
                "account_health_detail_json": "TEXT DEFAULT ''",
                "export_status": "TEXT DEFAULT ''",
                "export_kind": "TEXT DEFAULT ''",
                "exported_at": "TEXT DEFAULT ''",
                "activation_provider": "TEXT DEFAULT ''",
                "activation_client_key_hash": "TEXT DEFAULT ''",
                "activation_submission_claim": "TEXT DEFAULT ''",
                "activation_status": "TEXT DEFAULT ''",
                "activation_channel": "TEXT DEFAULT ''",
                "activation_task_id": "TEXT DEFAULT ''",
                "activation_idempotency_key": "TEXT DEFAULT ''",
                "activation_attempt": "INTEGER DEFAULT 0",
                "activation_error": "TEXT DEFAULT ''",
                "activation_display": "TEXT DEFAULT ''",
                "activation_can_release": "INTEGER DEFAULT 0",
                "activation_cdk_consumed": "INTEGER DEFAULT 0",
                "activation_submitted_at": "TEXT DEFAULT ''",
                "activation_finished_at": "TEXT DEFAULT ''",
                "activation_updated_at": "TEXT DEFAULT ''",
                "active_plus_batch_id": "INTEGER",
                "active_plus_batch_key": "TEXT DEFAULT ''",
                "active_plus_item_id": "INTEGER",
                "plus_batch_status": "TEXT DEFAULT ''",
                "plus_reserved_at": "TEXT DEFAULT ''",
                "plus_archived_at": "TEXT DEFAULT ''",
                "plus_export_batch_key": "TEXT DEFAULT ''",
                "plus_export_key": "TEXT DEFAULT ''",
                "archive_batch_key": "TEXT DEFAULT ''",
            }
            if backend != "sqlite":
                for column, definition in account_column_defs.items():
                    conn.execute(
                        f"ALTER TABLE accounts ADD COLUMN IF NOT EXISTS {column} {definition}"
                    )
                for column, definition in {
                    "created_at": "TEXT DEFAULT ''",
                    "updated_at": "TEXT DEFAULT ''",
                }.items():
                    conn.execute(
                        f"ALTER TABLE account_credentials ADD COLUMN IF NOT EXISTS {column} {definition}"
                    )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_active_plus_batch ON accounts(active_plus_batch_id, plus_batch_status)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_plus_archived ON accounts(plus_archived_at)")
                # Postgres SERIAL-compatible archive batch tables (same SQL as sqlite path).
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS archive_batches (
                      id SERIAL PRIMARY KEY,
                      batch_key TEXT NOT NULL UNIQUE,
                      name TEXT NOT NULL DEFAULT '',
                      reason TEXT NOT NULL DEFAULT '',
                      total_count INTEGER NOT NULL DEFAULT 0,
                      product_count INTEGER NOT NULL DEFAULT 0,
                      plus_count INTEGER NOT NULL DEFAULT 0,
                      free_count INTEGER NOT NULL DEFAULT 0,
                      other_count INTEGER NOT NULL DEFAULT 0,
                      restored_count INTEGER NOT NULL DEFAULT 0,
                      active_count INTEGER NOT NULL DEFAULT 0,
                      cutoff_at TEXT NOT NULL DEFAULT '',
                      created_at TEXT NOT NULL,
                      updated_at TEXT NOT NULL DEFAULT '',
                      notes TEXT NOT NULL DEFAULT ''
                    )
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_archive_batches_created ON archive_batches(created_at DESC)")
                conn.execute("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS archive_batch_key TEXT DEFAULT ''")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_archive_batch ON accounts(archive_batch_key)")
                _INIT_DB_DONE.add(target)
                return
            cred_cols = {row[1] for row in conn.execute("PRAGMA table_info(account_credentials)").fetchall()}
            for column, definition in {
                "created_at": "TEXT DEFAULT ''",
                "updated_at": "TEXT DEFAULT ''",
            }.items():
                if column not in cred_cols:
                    conn.execute(f"ALTER TABLE account_credentials ADD COLUMN {column} {definition}")
            # Older databases predate timestamps on these auxiliary tables.
            for table, definitions in {
                "account_proxy": {"created_at": "TEXT DEFAULT ''", "updated_at": "TEXT DEFAULT ''"},
                "account_artifacts": {"created_at": "TEXT DEFAULT ''", "updated_at": "TEXT DEFAULT ''"},
                "sms_activations": {"created_at": "TEXT DEFAULT ''", "updated_at": "TEXT DEFAULT ''"},
            }.items():
                table_cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                for column, definition in definitions.items():
                    if column not in table_cols:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(accounts)").fetchall()}
            for column, definition in account_column_defs.items():
                if column not in existing_cols:
                    conn.execute(f"ALTER TABLE accounts ADD COLUMN {column} {definition}")
            # Re-read after ALTERs so newly introduced activation metadata is
            # present even on databases created by older process versions.
            refreshed_account_cols = {row[1] for row in conn.execute("PRAGMA table_info(accounts)").fetchall()}
            for column, definition in {
                "billing_email": "TEXT DEFAULT ''",
                "codex_email": "TEXT DEFAULT ''",
                "activation_client_key_hash": "TEXT DEFAULT ''",
            }.items():
                if column not in refreshed_account_cols:
                    conn.execute(f"ALTER TABLE accounts ADD COLUMN {column} {definition}")
                    refreshed_account_cols.add(column)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_active_plus_batch ON accounts(active_plus_batch_id, plus_batch_status)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS archive_batches (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  batch_key TEXT NOT NULL UNIQUE,
                  name TEXT NOT NULL DEFAULT '',
                  reason TEXT NOT NULL DEFAULT '',
                  total_count INTEGER NOT NULL DEFAULT 0,
                  product_count INTEGER NOT NULL DEFAULT 0,
                  plus_count INTEGER NOT NULL DEFAULT 0,
                  free_count INTEGER NOT NULL DEFAULT 0,
                  other_count INTEGER NOT NULL DEFAULT 0,
                  restored_count INTEGER NOT NULL DEFAULT 0,
                  active_count INTEGER NOT NULL DEFAULT 0,
                  cutoff_at TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL DEFAULT '',
                  notes TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_archive_batches_created ON archive_batches(created_at DESC)")
            if "archive_batch_key" not in existing_cols and "archive_batch_key" not in refreshed_account_cols:
                try:
                    conn.execute("ALTER TABLE accounts ADD COLUMN archive_batch_key TEXT DEFAULT ''")
                    refreshed_account_cols.add("archive_batch_key")
                except Exception:
                    pass
            conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_archive_batch ON accounts(archive_batch_key)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_plus_archived ON accounts(plus_archived_at)")
            # One-shot backfills. Safe to re-run only when schema is first opened in-process.
            conn.execute("UPDATE accounts SET registration_mode='phone' WHERE COALESCE(registration_mode, '')='' AND COALESCE(phone_number, '')<>''")
            conn.execute("UPDATE accounts SET registration_mode='email' WHERE COALESCE(registration_mode, '')='' AND COALESCE(email, '')<>''")
            conn.execute("UPDATE accounts SET login_identifier=COALESCE(NULLIF(email,''), NULLIF(phone_number,''), account_key) WHERE COALESCE(login_identifier, '')=''")
            conn.execute("UPDATE accounts SET registration_status=CASE WHEN COALESCE(stage,status) IN ('failed','error') THEN 'failed' WHEN COALESCE(stage,status)='archived' THEN 'archived' WHEN COALESCE(stage,status) IN ('registered','email_registered','manual_plus_required','manual_plus_confirmed','plus_verified_needs_oauth','cpa_bound','complete','resume_manual') THEN 'registered' ELSE 'unknown' END WHERE COALESCE(registration_status,'')=''")
            conn.execute("UPDATE accounts SET registration_error=COALESCE(NULLIF(last_error,''), registration_error) WHERE registration_status='failed' AND COALESCE(registration_error,'')=''")
            conn.execute("UPDATE accounts SET plus_status=CASE WHEN COALESCE(stage,status) IN ('manual_plus_confirmed') THEN 'manual_confirmed' WHEN COALESCE(stage,status) IN ('plus_verified_needs_oauth','cpa_bound','complete') OR lower(COALESCE(plan_type,'')) IN ('plus','pro','team','business','enterprise','paid') THEN 'verified_plus' WHEN COALESCE(stage,status) IN ('manual_plus_required','email_registered','registered') THEN 'needs_plus' WHEN lower(COALESCE(plan_type,''))='free' THEN 'free' WHEN COALESCE(stage,status) IN ('failed','error') THEN 'check_failed' ELSE 'unverified' END WHERE COALESCE(plus_status,'')=''")
            conn.execute("UPDATE accounts SET binding_status=CASE WHEN COALESCE(stage,status)='complete' THEN 'bound' WHEN COALESCE(stage,status)='cpa_bound' THEN 'cpa_submitted' WHEN COALESCE(stage,status) IN ('plus_verified_needs_oauth','manual_plus_confirmed') THEN 'pending' WHEN COALESCE(stage,status) IN ('failed','error') THEN 'failed' ELSE 'not_ready' END WHERE COALESCE(binding_status,'')=''")


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    return dict(row) if row else {}


def upsert_account(record: dict[str, Any], *, path: str | Path | None = None) -> int:
    init_db(path)
    now = now_iso()
    key = str(record.get("account_key") or record.get("account_id") or record.get("email") or record.get("phone_number") or "account")
    with connect(path) as conn:
        existing = conn.execute("SELECT * FROM accounts WHERE account_key=?", (key,)).fetchone()
        existing_values = dict(existing) if existing else {}

        def _text(name: str, *fallbacks: Any, default: str = "") -> str:
            if name in record and record.get(name) is not None:
                return str(record.get(name) or "")
            if name in existing_values and existing_values.get(name) not in (None, ""):
                return str(existing_values.get(name) or "")
            for fallback in fallbacks:
                if fallback not in (None, ""):
                    return str(fallback)
            return str(existing_values.get(name) or default or "")

        def _int(name: str, default: int = 0) -> int:
            raw = record.get(name) if name in record else existing_values.get(name, default)
            try:
                return int(raw or 0)
            except (TypeError, ValueError):
                return int(default)

        paths = record.get("paths") if isinstance(record.get("paths"), dict) else {}
        created_at = _text("created_at", default=now) or now
        fields = {
            "account_key": key,
            "account_id": _text("account_id"),
            "platform": _text("platform", "chatgpt", default="chatgpt"),
            "login_identifier": _text("login_identifier", record.get("email"), record.get("phone_number"), key),
            "phone_number": _text("phone_number"),
            "email": _text("email"),
            "billing_email": _text("billing_email", record.get("email")),
            "codex_email": _text("codex_email", record.get("billing_email"), record.get("email")),
            "password": _text("password"),
            "plan_type": _text("plan_type"),
            "status": _text("status", record.get("stage")),
            "stage": _text("stage", record.get("status")),
            "registration_mode": _text("registration_mode"),
            "registration_status": _text("registration_status"),
            "registration_task_id": _text("registration_task_id"),
            "registration_started_at": _text("registration_started_at"),
            "registration_completed_at": _text("registration_completed_at"),
            "registration_error": _text("registration_error"),
            "display_name": _text("display_name", record.get("nickname")),
            "created_at": created_at,
            "plus_status": _text("plus_status"),
            "plus_verified_at": _text("plus_verified_at"),
            "plus_check_source": _text("plus_check_source"),
            "plus_check_error": _text("plus_check_error"),
            "binding_status": _text("binding_status"),
            "binding_task_id": _text("binding_task_id"),
            "binding_provider": _text("binding_provider"),
            "binding_phone_number": _text("binding_phone_number"),
            "binding_started_at": _text("binding_started_at"),
            "binding_completed_at": _text("binding_completed_at"),
            "binding_error": _text("binding_error"),
            "oauth_callback_mode": _text("oauth_callback_mode"),
            "cpa_base_url": _text("cpa_base_url"),
            "cpa_submitted_at": _text("cpa_submitted_at"),
            "cpa_submit_status": _text("cpa_submit_status"),
            "cpa_submit_error": _text("cpa_submit_error"),
            "cpa_auth_file_name": _text("cpa_auth_file_name"),
            "cpa_auth_file_json": _text("cpa_auth_file_json"),
            "cpa_synced_at": _text("cpa_synced_at"),
            "cpa_sync_error": _text("cpa_sync_error"),
            "registration_phone_resource_id": _int("registration_phone_resource_id"),
            "binding_phone_resource_id": _int("binding_phone_resource_id"),
            "email_resource_id": _int("email_resource_id"),
            "proxy_resource_id": _int("proxy_resource_id"),
            "registration_proxy_exit_ip": _text("registration_proxy_exit_ip"),
            "registration_proxy_region": _text("registration_proxy_region"),
            "resume_file": _text("resume_file", paths.get("resume")),
            "storage_file": _text("storage_file", paths.get("storage_state")),
            "account_file": _text("account_file", record.get("source_file"), paths.get("source")),
            "account_health_status": _text("account_health_status"),
            "account_health_checked_at": _text("account_health_checked_at"),
            "account_health_source": _text("account_health_source"),
            "account_health_error": _text("account_health_error"),
            "account_health_detail_json": _text("account_health_detail_json"),
            "export_status": _text("export_status"),
            "export_kind": _text("export_kind"),
            "exported_at": _text("exported_at"),
            "activation_provider": _text("activation_provider"),
            "activation_client_key_hash": _text("activation_client_key_hash"),
            "activation_submission_claim": _text("activation_submission_claim"),
            "activation_status": _text("activation_status"),
            "activation_channel": _text("activation_channel"),
            "activation_task_id": _text("activation_task_id"),
            "activation_idempotency_key": _text("activation_idempotency_key"),
            "activation_attempt": _int("activation_attempt"),
            "activation_error": _text("activation_error"),
            "activation_display": _text("activation_display"),
            "activation_can_release": _int("activation_can_release"),
            "activation_cdk_consumed": _int("activation_cdk_consumed"),
            "activation_submitted_at": _text("activation_submitted_at"),
            "activation_finished_at": _text("activation_finished_at"),
            "activation_updated_at": _text("activation_updated_at"),
            "updated_at": now,
            "last_error": _text("last_error", (record.get("failure") or {}).get("reason") if isinstance(record.get("failure"), dict) else ""),
        }
        if existing:
            assignments = ", ".join(f"{column}=:{column}" for column in fields if column not in {"account_key", "created_at"})
            conn.execute(f"UPDATE accounts SET {assignments} WHERE account_key=:account_key", fields)
            account_pk = int(existing["id"])
        else:
            columns = ", ".join(fields.keys())
            placeholders = ", ".join(f":{column}" for column in fields)
            conn.execute(f"INSERT INTO accounts({columns}) VALUES({placeholders})", fields)
            account_pk = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

        tokens: dict[str, str] = {}
        for source_name in ("raw_tokens", "tokens"):
            source_tokens = record.get(source_name)
            if isinstance(source_tokens, dict):
                for name, value in source_tokens.items():
                    if isinstance(value, str) and value:
                        tokens[str(name)] = value
        for name in ("access_token", "refresh_token", "id_token", "chatgpt_access_token_initial", "token_expires_at"):
            value = record.get(name)
            if isinstance(value, str) and value:
                tokens[name] = value
        if tokens:
            old_cred = conn.execute("SELECT access_token, refresh_token, id_token, chatgpt_access_token_initial, token_expires_at FROM account_credentials WHERE account_id_ref=?", (account_pk,)).fetchone()
            old_tokens = dict(old_cred) if old_cred else {}
            effective_tokens = {name: tokens.get(name) or str(old_tokens.get(name) or "") for name in ("access_token", "refresh_token", "id_token", "chatgpt_access_token_initial", "token_expires_at")}
            conn.execute(
                """
                INSERT INTO account_credentials(account_id_ref, access_token, refresh_token, id_token,
                  chatgpt_access_token_initial, token_expires_at, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id_ref) DO UPDATE SET
                  access_token=excluded.access_token,
                  refresh_token=excluded.refresh_token,
                  id_token=excluded.id_token,
                  chatgpt_access_token_initial=excluded.chatgpt_access_token_initial,
                  token_expires_at=excluded.token_expires_at,
                  updated_at=excluded.updated_at
                """,
                (
                    account_pk,
                    str(effective_tokens.get("access_token") or ""),
                    str(effective_tokens.get("refresh_token") or ""),
                    str(effective_tokens.get("id_token") or ""),
                    str(effective_tokens.get("chatgpt_access_token_initial") or ""),
                    str(effective_tokens.get("token_expires_at") or ""),
                    now,
                    now,
                ),
            )

        paths = record.get("paths") if isinstance(record.get("paths"), dict) else {}
        for artifact_type, artifact_path in paths.items():
            if artifact_path:
                conn.execute(
                "INSERT INTO account_artifacts(account_id_ref, artifact_type, path, created_at, updated_at) VALUES(?, ?, ?, ?, ?) ON CONFLICT(account_id_ref, artifact_type, path) DO NOTHING",
                    (account_pk, str(artifact_type), str(artifact_path), now, now),
                )

        proxy = record.get("proxy") if isinstance(record.get("proxy"), dict) else {}
        if proxy:
            conn.execute(
                """
                INSERT INTO account_proxy(account_id_ref, registration_proxy, registration_exit_ip, registration_country,
                  subscription_check_proxy, subscription_check_source, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id_ref) DO UPDATE SET
                  registration_proxy=excluded.registration_proxy,
                  registration_exit_ip=excluded.registration_exit_ip,
                  registration_country=excluded.registration_country,
                  subscription_check_proxy=excluded.subscription_check_proxy,
                  subscription_check_source=excluded.subscription_check_source,
                  updated_at=excluded.updated_at
                """,
                (
                    account_pk,
                    str(proxy.get("registration_proxy") or ""),
                    str(proxy.get("registration_exit_ip") or ""),
                    str(proxy.get("registration_country") or ""),
                    str(proxy.get("subscription_check_proxy") or ""),
                    str(proxy.get("subscription_check_source") or ""),
                    now,
                    now,
                ),
            )

        sms = record.get("sms") if isinstance(record.get("sms"), dict) else {}
        activation_id = str(record.get("activation_id") or sms.get("activation_id") or "")
        if activation_id or sms:
            conn.execute(
                """
                INSERT INTO sms_activations(account_id_ref, provider, activation_id, phone_number, sms_url, country,
                  status, last_code, ignored_codes, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_pk,
                    str(sms.get("provider") or ""),
                    activation_id,
                    str(record.get("phone_number") or sms.get("phone_number") or ""),
                    str(sms.get("sms_url") or ""),
                    str(sms.get("country") or ""),
                    str(sms.get("status") or ""),
                    str(sms.get("last_code") or ""),
                    dumps(normalize_ignored_codes(sms.get("ignored_codes"))),
                    now,
                    now,
                ),
            )
        return account_pk


def claim_queued_activation_submission(
    account_key: str,
    claim_id: str,
    client_key_hash: str,
    *,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Atomically transition one queued UPI activation to a durable claim."""
    init_db(path)
    key = str(account_key or "").strip()
    claim = str(claim_id or "").strip()
    if not key or not claim:
        return {}
    now = now_iso()
    with connect(path) as conn:
        result = conn.execute(
            """
            UPDATE accounts
            SET activation_status='submitting',
                activation_submission_claim=?,
                activation_client_key_hash=?,
                activation_error='',
                activation_display='正在提交远端激活请求',
                activation_updated_at=?,
                updated_at=?
            WHERE account_key=?
              AND COALESCE(activation_provider, '') IN ('', 'upi')
              AND COALESCE(activation_status, '')='queued'
            """,
            (claim, str(client_key_hash or ""), now, now, key),
        )
        if result.rowcount != 1:
            return {}
        conn.execute(
            """
            INSERT INTO account_events(account_key, task_id, event_type, status, message, payload_json, created_at)
            VALUES(?, '', 'activation_submitting', 'submitting', '正在提交远端激活请求', '{}', ?)
            """,
            (key, now),
        )
    return get_account(key, path=path)


def persist_claimed_activation_submission(
    account_key: str,
    claim_id: str,
    updates: dict[str, Any],
    *,
    event: str = "",
    message: str = "",
    clear_claim: bool = True,
    path: str | Path | None = None,
) -> bool:
    """Persist a submit result only while its durable ``submitting`` claim owns the row."""
    init_db(path)
    key = str(account_key or "").strip()
    claim = str(claim_id or "")
    if not key:
        return False
    allowed = {
        "activation_provider",
        "activation_client_key_hash",
        "activation_status",
        "activation_channel",
        "activation_task_id",
        "activation_idempotency_key",
        "activation_attempt",
        "activation_error",
        "activation_display",
        "activation_can_release",
        "activation_cdk_consumed",
        "activation_submitted_at",
        "activation_finished_at",
        "activation_updated_at",
    }
    fields = {name: value for name, value in updates.items() if name in allowed}
    if clear_claim:
        fields["activation_submission_claim"] = ""
    fields.setdefault("activation_updated_at", now_iso())
    fields["updated_at"] = now_iso()
    assignments = ", ".join(f"{name}=?" for name in fields)
    values = [fields[name] for name in fields]
    with connect(path) as conn:
        result = conn.execute(
            f"""
            UPDATE accounts SET {assignments}
            WHERE account_key=?
              AND COALESCE(activation_status, '')='submitting'
              AND COALESCE(activation_submission_claim, '')=?
            """,
            (*values, key, claim),
        )
        if result.rowcount != 1:
            return False
        if event:
            conn.execute(
                """
                INSERT INTO account_events(account_key, task_id, event_type, status, message, payload_json, created_at)
                VALUES(?, ?, ?, ?, ?, '{}', ?)
                """,
                (
                    key,
                    str(fields.get("activation_task_id") or ""),
                    str(event),
                    str(fields.get("activation_status") or ""),
                    str(message or event),
                    now_iso(),
                ),
            )
    return True


def add_account_event(account_key: str, event_type: str, *, task_id: str = "", status: str = "", message: str = "", payload: dict[str, Any] | None = None, path: str | Path | None = None) -> int:
    init_db(path)
    with connect(path) as conn:
        conn.execute(
            """
            INSERT INTO account_events(account_key, task_id, event_type, status, message, payload_json, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (str(account_key or ""), str(task_id or ""), str(event_type or ""), str(status or ""), str(message or ""), dumps(payload or {}), now_iso()),
        )
        return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def list_account_events(account_key: str, *, path: str | Path | None = None) -> list[dict[str, Any]]:
    init_db(path)
    with connect(path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM account_events WHERE account_key=? OR task_id=? ORDER BY created_at DESC, id DESC LIMIT 500
            """,
            (account_key, account_key),
        ).fetchall()
        return [dict(row) for row in rows]


def list_accounts(*, path: str | Path | None = None, include_token_values: bool = False) -> list[dict[str, Any]]:
    """List accounts for UI/export metadata.

    By default only returns presence flags for credentials (has_access_token etc.).
    Full token text is huge (tens of MB) and must not ride the accounts list path —
    use get_account / export joins when values are required.
    """
    init_db(path)
    with connect(path) as conn:
        if include_token_values:
            token_select = """
                   c.access_token, c.refresh_token, c.id_token, c.chatgpt_access_token_initial
            """
        else:
            # Presence only — avoids shipping multi-MB JWT blobs on every list.
            token_select = """
                   CASE WHEN length(coalesce(c.access_token, '')) > 0 THEN 1 ELSE 0 END AS has_access_token,
                   CASE WHEN length(coalesce(c.refresh_token, '')) > 0 THEN 1 ELSE 0 END AS has_refresh_token,
                   CASE WHEN length(coalesce(c.id_token, '')) > 0 THEN 1 ELSE 0 END AS has_id_token,
                   CASE WHEN length(coalesce(c.chatgpt_access_token_initial, '')) > 0 THEN 1 ELSE 0 END AS has_initial_access_token
            """
        rows = conn.execute(
            f"""
            SELECT a.*, p.registration_proxy, p.registration_exit_ip, p.registration_country,
                   p.subscription_check_proxy, p.subscription_check_source,
                   {token_select}
            FROM accounts a
            LEFT JOIN account_proxy p ON p.account_id_ref=a.id
            LEFT JOIN account_credentials c ON c.account_id_ref=a.id
            ORDER BY a.updated_at DESC, a.id DESC
            """
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            if include_token_values:
                access_token = str(item.pop("access_token", "") or "")
                refresh_token = str(item.pop("refresh_token", "") or "")
                id_token = str(item.pop("id_token", "") or "")
                chatgpt_access_token_initial = str(item.pop("chatgpt_access_token_initial", "") or "")
                item["tokens"] = {
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "id_token": id_token,
                    "chatgpt_access_token_initial": chatgpt_access_token_initial,
                    "has_access_token": bool(access_token),
                    "has_refresh_token": bool(refresh_token),
                    "has_id_token": bool(id_token),
                    "has_initial_access_token": bool(chatgpt_access_token_initial),
                }
            else:
                has_at = bool(int(item.pop("has_access_token", 0) or 0))
                has_rt = bool(int(item.pop("has_refresh_token", 0) or 0))
                has_id = bool(int(item.pop("has_id_token", 0) or 0))
                has_init = bool(int(item.pop("has_initial_access_token", 0) or 0))
                item["tokens"] = {
                    "access_token": has_at,
                    "refresh_token": has_rt,
                    "id_token": has_id,
                    "chatgpt_access_token_initial": has_init,
                    "has_access_token": has_at,
                    "has_refresh_token": has_rt,
                    "has_id_token": has_id,
                    "has_initial_access_token": has_init,
                }
            item["proxy"] = {
                "registration_proxy": item.pop("registration_proxy", "") or "",
                "registration_exit_ip": item.pop("registration_exit_ip", "") or "",
                "registration_country": item.pop("registration_country", "") or "",
                "subscription_check_proxy": item.pop("subscription_check_proxy", "") or "",
                "subscription_check_source": item.pop("subscription_check_source", "") or "",
            }
            result.append(item)
        return result


def archive_account(key: str, *, path: str | Path | None = None) -> bool:
    init_db(path)
    now = now_iso()
    with connect(path) as conn:
        row = conn.execute(
            "SELECT id, account_key FROM accounts WHERE account_key=? OR phone_number=? OR account_id=? OR CAST(id AS TEXT)=?",
            (key, key, key, key),
        ).fetchone()
        if not row:
            return False
        conn.execute(
            "UPDATE accounts SET stage='archived', status='archived', binding_status='archived', registration_status='archived', updated_at=? WHERE id=?",
            (now, int(row["id"])),
        )
        conn.execute(
            """
            INSERT INTO account_events(account_key, task_id, event_type, status, message, payload_json, created_at)
            VALUES(?, '', 'account_archived', 'archived', ?, '{}', ?)
            """,
            (str(row["account_key"] or key), "账号已归档", now),
        )
        return True


def get_account(key: str, *, path: str | Path | None = None) -> dict[str, Any]:
    init_db(path)
    with connect(path) as conn:
        row = conn.execute("SELECT * FROM accounts WHERE account_key=? OR phone_number=? OR account_id=? OR CAST(id AS TEXT)=?", (key, key, key, key)).fetchone()
        if not row:
            return {}
        account = dict(row)
        account["paths"] = artifacts_for_account(int(account["id"]), path=path)
        cred = conn.execute("SELECT * FROM account_credentials WHERE account_id_ref=?", (account["id"],)).fetchone()
        if cred:
            c = dict(cred)
            account["tokens"] = {k: c.get(k, "") for k in ("access_token", "refresh_token", "id_token", "chatgpt_access_token_initial", "token_expires_at")}
        proxy = conn.execute("SELECT * FROM account_proxy WHERE account_id_ref=?", (account["id"],)).fetchone()
        account["proxy"] = _row_to_dict(proxy)
        sms = conn.execute(
            "SELECT provider, activation_id, phone_number, sms_url, country, status, last_code, ignored_codes FROM sms_activations WHERE account_id_ref=? ORDER BY id DESC LIMIT 1",
            (account["id"],),
        ).fetchone()
        account["sms"] = _row_to_dict(sms)
        if account["sms"]:
            account["activation_id"] = str(account["sms"].get("activation_id") or "")
        return account


def _is_plus_account_row(plan_type: str, plus_status: str) -> bool:
    plan = str(plan_type or "").strip().lower()
    status = str(plus_status or "").strip().lower()
    if status in {"verified_plus", "manual_confirmed"}:
        return True
    return plan in {"plus", "pro", "premium", "paid", "team", "business", "enterprise"}


def _is_free_account_row(plan_type: str, plus_status: str) -> bool:
    if _is_plus_account_row(plan_type, plus_status):
        return False
    plan = str(plan_type or "").strip().lower()
    status = str(plus_status or "").strip().lower()
    return plan in {"", "free"} or status in {"", "free", "unverified", "needs_plus"}


def uuid_hex_short() -> str:
    import secrets
    return secrets.token_hex(3)


def create_archive_batch(
    *,
    name: str = "",
    reason: str = "",
    cutoff_at: str = "",
    notes: str = "",
    path: str | Path | None = None,
) -> dict[str, Any]:
    init_db(path)
    now = now_iso()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    batch_key = f"archive_{stamp}_{uuid_hex_short()}"
    with connect(path) as conn:
        conn.execute(
            """
            INSERT INTO archive_batches(
              batch_key, name, reason, total_count, product_count, plus_count, free_count,
              other_count, restored_count, active_count, cutoff_at, created_at, updated_at, notes
            ) VALUES (?, ?, ?, 0, 0, 0, 0, 0, 0, 0, ?, ?, ?, ?)
            """,
            (
                batch_key,
                str(name or f"归档批次 {stamp}"),
                str(reason or ""),
                str(cutoff_at or ""),
                now,
                now,
                str(notes or ""),
            ),
        )
    return get_archive_batch(batch_key, path=path) or {"batch_key": batch_key}


def get_archive_batch(batch_key: str, *, path: str | Path | None = None) -> dict[str, Any] | None:
    init_db(path)
    with connect(path) as conn:
        row = conn.execute(
            "SELECT * FROM archive_batches WHERE batch_key=?",
            (str(batch_key or "").strip(),),
        ).fetchone()
        return dict(row) if row else None


def list_archive_batches(*, limit: int = 100, path: str | Path | None = None) -> list[dict[str, Any]]:
    init_db(path)
    limit = max(1, min(int(limit or 100), 500))
    with connect(path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM archive_batches
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def refresh_archive_batch_counts(batch_key: str, *, path: str | Path | None = None) -> dict[str, Any] | None:
    """Recompute stats for accounts still tagged with batch_key (not yet restored)."""
    init_db(path)
    key = str(batch_key or "").strip()
    if not key:
        return None
    now = now_iso()
    with connect(path) as conn:
        rows = conn.execute(
            """
            SELECT stage, status, registration_status, plan_type, plus_status,
                   CASE WHEN EXISTS (
                     SELECT 1 FROM account_credentials c
                     WHERE c.account_id_ref = a.id AND length(coalesce(c.access_token,'')) > 0
                   ) THEN 1 ELSE 0 END AS has_at
            FROM accounts a
            WHERE archive_batch_key=?
            """,
            (key,),
        ).fetchall()
        still = len(rows)
        product = plus = free = other = 0
        for row in rows:
            item = dict(row)
            if int(item.get("has_at") or 0):
                product += 1
            if _is_plus_account_row(str(item.get("plan_type") or ""), str(item.get("plus_status") or "")):
                plus += 1
            elif _is_free_account_row(str(item.get("plan_type") or ""), str(item.get("plus_status") or "")):
                free += 1
            else:
                other += 1
        conn.execute(
            """
            UPDATE archive_batches
            SET active_count=?, product_count=?, plus_count=?, free_count=?, other_count=?, updated_at=?
            WHERE batch_key=?
            """,
            (still, product, plus, free, other, now, key),
        )
    return get_archive_batch(key, path=path)


def archive_accounts_older_than(
    *,
    days: int = 3,
    reason: str = "older_than_days",
    name: str = "",
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Bulk-archive non-archived accounts older than N days into one archive batch."""
    init_db(path)
    days = max(1, int(days or 3))
    cutoff_dt = datetime.now() - timedelta(days=days)
    cutoff = cutoff_dt.isoformat(timespec="seconds")
    now = now_iso()
    batch = create_archive_batch(
        name=name or f"归档 {days} 天前 · {cutoff_dt.strftime('%Y-%m-%d')}",
        reason=reason or f"older_than_{days}d",
        cutoff_at=cutoff,
        notes=f"auto archive accounts with created_at/updated_at before {cutoff}",
        path=path,
    )
    batch_key = str(batch.get("batch_key") or "")

    product = plus = free = other = 0
    archived_ids: list[int] = []

    def _parse_ts(raw: str) -> datetime | None:
        text = str(raw or "").strip()
        if not text:
            return None
        try:
            if text.replace(".", "", 1).isdigit() and float(text) > 1_000_000_000:
                return datetime.fromtimestamp(float(text))
        except Exception:
            pass
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00").split("+")[0])
        except Exception:
            return None

    with connect(path) as conn:
        rows = conn.execute(
            """
            SELECT a.id, a.account_key, a.created_at, a.updated_at, a.plan_type, a.plus_status,
                   a.stage, a.status, a.registration_status,
                   CASE WHEN length(coalesce(c.access_token,'')) > 0 THEN 1 ELSE 0 END AS has_at
            FROM accounts a
            LEFT JOIN account_credentials c ON c.account_id_ref=a.id
            WHERE coalesce(a.stage,'') <> 'archived'
              AND coalesce(a.status,'') <> 'archived'
              AND coalesce(a.registration_status,'') <> 'archived'
            """
        ).fetchall()
        for row in rows:
            item = dict(row)
            ts = _parse_ts(str(item.get("created_at") or "")) or _parse_ts(str(item.get("updated_at") or ""))
            if ts is None or ts > cutoff_dt:
                continue
            aid = int(item.get("id") or 0)
            if not aid:
                continue
            archived_ids.append(aid)
            if int(item.get("has_at") or 0):
                product += 1
            if _is_plus_account_row(str(item.get("plan_type") or ""), str(item.get("plus_status") or "")):
                plus += 1
            elif _is_free_account_row(str(item.get("plan_type") or ""), str(item.get("plus_status") or "")):
                free += 1
            else:
                other += 1

        chunk = 400
        for offset in range(0, len(archived_ids), chunk):
            part = archived_ids[offset: offset + chunk]
            placeholders = ",".join("?" for _ in part)
            conn.execute(
                f"""
                UPDATE accounts
                SET stage='archived', status='archived', binding_status='archived',
                    registration_status='archived', archive_batch_key=?, updated_at=?
                WHERE id IN ({placeholders})
                """,
                (batch_key, now, *part),
            )

        total = len(archived_ids)
        conn.execute(
            """
            UPDATE archive_batches
            SET total_count=?, product_count=?, plus_count=?, free_count=?, other_count=?,
                active_count=?, restored_count=0, updated_at=?
            WHERE batch_key=?
            """,
            (total, product, plus, free, other, total, now, batch_key),
        )

    return {
        "ok": True,
        "batch": get_archive_batch(batch_key, path=path),
        "archived": len(archived_ids),
        "product_count": product,
        "plus_count": plus,
        "free_count": free,
        "other_count": other,
        "cutoff_at": cutoff,
        "days": days,
    }


def restore_archive_batch(
    batch_key: str,
    *,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Restore all still-archived accounts in a batch back to the active list."""
    init_db(path)
    key = str(batch_key or "").strip()
    batch = get_archive_batch(key, path=path)
    if not batch:
        return {"ok": False, "message": "归档批次不存在", "restored": 0}
    now = now_iso()
    with connect(path) as conn:
        cur = conn.execute(
            """
            UPDATE accounts
            SET stage=CASE WHEN stage='archived' THEN 'manual_plus_required' ELSE stage END,
                status=CASE WHEN status='archived' THEN 'registered' ELSE status END,
                registration_status=CASE WHEN registration_status='archived' THEN 'registered' ELSE registration_status END,
                binding_status=CASE WHEN binding_status='archived' THEN 'not_ready' ELSE binding_status END,
                archive_batch_key='',
                updated_at=?
            WHERE archive_batch_key=?
              AND (
                stage='archived' OR status='archived' OR registration_status='archived'
              )
            """,
            (now, key),
        )
        restored = int(cur.rowcount or 0)
        prev_restored = int(batch.get("restored_count") or 0)
        active = max(0, int(batch.get("active_count") or batch.get("total_count") or 0) - restored)
        conn.execute(
            """
            UPDATE archive_batches
            SET restored_count=?, active_count=?, updated_at=?
            WHERE batch_key=?
            """,
            (prev_restored + restored, active, now, key),
        )
    return {
        "ok": True,
        "restored": restored,
        "batch": get_archive_batch(key, path=path),
    }


def artifacts_for_account(account_pk: int, *, path: str | Path | None = None) -> dict[str, str]:
    with connect(path) as conn:
        rows = conn.execute("SELECT artifact_type, path FROM account_artifacts WHERE account_id_ref=? ORDER BY id", (account_pk,)).fetchall()
        result: dict[str, str] = {}
        for row in rows:
            result[str(row["artifact_type"])] = str(row["path"])
        return result


def create_task(task: dict[str, Any], *, path: str | Path | None = None) -> dict[str, Any]:
    init_db(path)
    now = now_iso()
    task_id = str(task.get("id") or task.get("task_id") or "")
    with connect(path) as conn:
        conn.execute(
            """
            INSERT INTO tasks(id, task_type, status, account_id_ref, params_json, result_json, created_at,
              started_at, finished_at, updated_at, error, retryable, command_json, log_file)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET status=excluded.status, result_json=excluded.result_json,
              started_at=excluded.started_at, finished_at=excluded.finished_at, updated_at=excluded.updated_at,
              error=excluded.error, retryable=excluded.retryable, command_json=excluded.command_json,
              log_file=excluded.log_file
            """,
            (
                task_id,
                str(task.get("task_type") or task.get("type") or ""),
                str(task.get("status") or "pending"),
                task.get("account_id_ref"),
                dumps(task.get("params") or task.get("overrides") or {}),
                dumps(task.get("result") or {}),
                str(task.get("created_at") or now),
                str(task.get("started_at") or ""),
                str(task.get("finished_at") or ""),
                now,
                str(task.get("error") or ""),
                1 if task.get("retryable") else 0,
                dumps(task.get("command") or []),
                str(task.get("log_file") or ""),
            ),
        )
    add_task_event(task_id, "info", "task_saved", f"任务已保存: {task.get('status') or 'pending'}", task, path=path)
    return get_task(task_id, path=path)


def create_tasks_bulk(tasks: list[dict[str, Any]], *, path: str | Path | None = None) -> int:
    """Insert many queued tasks in one transaction for high-count batch launches."""
    init_db(path)
    if not tasks:
        return 0
    now = now_iso()
    task_rows = []
    event_rows = []
    account_event_rows = []
    for task in tasks:
        task_id = str(task.get("id") or task.get("task_id") or "")
        if not task_id:
            continue
        status = str(task.get("status") or "pending")
        task_rows.append(
            (
                task_id,
                str(task.get("task_type") or task.get("type") or ""),
                status,
                task.get("account_id_ref"),
                dumps(task.get("params") or task.get("overrides") or {}),
                dumps(task.get("result") or {}),
                str(task.get("created_at") or now),
                str(task.get("started_at") or ""),
                str(task.get("finished_at") or ""),
                now,
                str(task.get("error") or ""),
                1 if task.get("retryable") else 0,
                dumps(task.get("command") or []),
                str(task.get("log_file") or ""),
            )
        )
        event_rows.append((task_id, now, "info", "task_saved", f"任务已保存: {status}", dumps(task)))
        if str(task.get("task_type") or task.get("type") or "") == "email-protocol-register-token":
            account_event_rows.append((task_id, task_id, "registration_started", "queued", "邮箱协议注册任务已创建", dumps({}), now))
    if not task_rows:
        return 0
    with connect(path) as conn:
        conn.executemany(
            """
            INSERT INTO tasks(id, task_type, status, account_id_ref, params_json, result_json, created_at,
              started_at, finished_at, updated_at, error, retryable, command_json, log_file)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET status=excluded.status, result_json=excluded.result_json,
              started_at=excluded.started_at, finished_at=excluded.finished_at, updated_at=excluded.updated_at,
              error=excluded.error, retryable=excluded.retryable, command_json=excluded.command_json,
              log_file=excluded.log_file
            """,
            task_rows,
        )
        conn.executemany(
            "INSERT INTO task_events(task_id, timestamp, level, event_type, message, data_json) VALUES(?, ?, ?, ?, ?, ?)",
            event_rows,
        )
        if account_event_rows:
            conn.executemany(
                """
                INSERT INTO account_events(account_key, task_id, event_type, status, message, payload_json, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                account_event_rows,
            )
    return len(task_rows)


def update_task(task_id: str, *, path: str | Path | None = None, **patch: Any) -> dict[str, Any]:
    task = get_task(task_id, path=path)
    if not task:
        return {}
    task.update(patch)
    return create_task(task, path=path)


def _task_from_row(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["type"] = item.get("task_type")
    item["params"] = loads(item.get("params_json"), {})
    item["result"] = loads(item.get("result_json"), {})
    item["command"] = loads(item.get("command_json"), [])
    item["retryable"] = bool(item.get("retryable"))
    return item

def get_task(task_id: str, *, path: str | Path | None = None) -> dict[str, Any]:
    init_db(path)
    with connect(path) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            return {}
        return _task_from_row(row)


def list_tasks(*, status: str = "", limit: int = 50, offset: int = 0, order: str = "desc", path: str | Path | None = None) -> list[dict[str, Any]]:
    init_db(path)
    parsed_limit = max(1, min(int(limit or 50), 500))
    parsed_offset = max(0, int(offset or 0))
    order_sql = "ASC" if str(order or "").strip().lower() in {"asc", "ascending", "oldest", "fifo"} else "DESC"
    with connect(path) as conn:
        if status:
            rows = conn.execute(
                f"SELECT * FROM tasks WHERE status=? ORDER BY created_at {order_sql}, id {order_sql} LIMIT ? OFFSET ?",
                (status, parsed_limit, parsed_offset),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT * FROM tasks ORDER BY created_at {order_sql}, id {order_sql} LIMIT ? OFFSET ?",
                (parsed_limit, parsed_offset),
            ).fetchall()
        return [_task_from_row(row) for row in rows]


def add_task_event(task_id: str, level: str, event_type: str, message: str, data: Any = None, *, path: str | Path | None = None) -> None:
    init_db(path)
    with connect(path) as conn:
        conn.execute(
            "INSERT INTO task_events(task_id, timestamp, level, event_type, message, data_json) VALUES(?, ?, ?, ?, ?, ?)",
            (task_id, now_iso(), level, event_type, message, dumps(data or {})),
        )


def list_task_events(task_id: str, *, since_id: int = 0, path: str | Path | None = None) -> list[dict[str, Any]]:
    init_db(path)
    with connect(path) as conn:
        rows = conn.execute("SELECT * FROM task_events WHERE task_id=? AND id>? ORDER BY id", (task_id, since_id)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["data"] = loads(item.pop("data_json", "{}"), {})
            items.append(item)
        return items


def set_config(key: str, value: Any, *, path: str | Path | None = None) -> None:
    init_db(path)
    with connect(path) as conn:
        conn.execute(
            "INSERT INTO app_config(key, value_json, updated_at) VALUES(?, ?, ?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
            (key, dumps(value), now_iso()),
        )


def get_config(*, path: str | Path | None = None) -> dict[str, Any]:
    init_db(path)
    with connect(path) as conn:
        rows = conn.execute("SELECT key, value_json FROM app_config ORDER BY key").fetchall()
        return {str(row["key"]): loads(row["value_json"], None) for row in rows}


def upsert_provider(provider_type: str, provider_name: str, settings: dict[str, Any], enabled: bool = True, *, path: str | Path | None = None) -> None:
    init_db(path)
    now = now_iso()
    with connect(path) as conn:
        conn.execute(
            """
            INSERT INTO provider_settings(provider_type, provider_name, enabled, settings_json, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider_type, provider_name) DO UPDATE SET enabled=excluded.enabled, settings_json=excluded.settings_json, updated_at=excluded.updated_at
            """,
            (provider_type, provider_name, 1 if enabled else 0, dumps(settings), now, now),
        )


def list_providers(*, path: str | Path | None = None) -> list[dict[str, Any]]:
    init_db(path)
    with connect(path) as conn:
        rows = conn.execute("SELECT * FROM provider_settings ORDER BY provider_type, provider_name").fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["enabled"] = bool(item.get("enabled"))
            item["settings"] = loads(item.pop("settings_json", "{}"), {})
            items.append(item)
        return items


def summary(*, path: str | Path | None = None) -> dict[str, Any]:
    accounts = list_accounts(path=path)
    tasks = list_tasks(path=path)
    stages: dict[str, int] = {}
    plans: dict[str, int] = {}
    task_status: dict[str, int] = {}
    for item in accounts:
        stages[str(item.get("stage") or "unknown")] = stages.get(str(item.get("stage") or "unknown"), 0) + 1
        plans[str(item.get("plan_type") or "unknown")] = plans.get(str(item.get("plan_type") or "unknown"), 0) + 1
    for item in tasks:
        task_status[str(item.get("status") or "unknown")] = task_status.get(str(item.get("status") or "unknown"), 0) + 1
    return {"total": len(accounts), "stages": stages, "plans": plans, "tasks": task_status, "updated_at": now_iso()}

# ─────────────────────────────────────────────────────────
# 新增: 代理池 CRUD
# ─────────────────────────────────────────────────────────

def upsert_proxy(url: str, *, exit_ip: str = "", region: str = "", source: str = "manual",
                 path: str | Path | None = None) -> None:
    init_db(path)
    with connect(path) as conn:
        conn.execute(
            """INSERT INTO proxies(url, exit_ip, region, source, is_active, created_at)
               VALUES(?, ?, ?, ?, 1, ?)
               ON CONFLICT(url) DO UPDATE SET exit_ip=excluded.exit_ip, region=excluded.region,
               is_active=1, last_checked=?""",
            (url, exit_ip, region, source, now_iso(), now_iso()),
        )

def list_proxies(*, active_only: bool = False, region: str = "",
                 path: str | Path | None = None) -> list[dict[str, Any]]:
    init_db(path)
    with connect(path) as conn:
        sql = "SELECT * FROM proxies WHERE 1=1"
        params: list[Any] = []
        if active_only:
            sql += " AND is_active=1"
        if region:
            sql += " AND region=?"
            params.append(region)
        sql += " ORDER BY success_count DESC"
        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

def get_proxy(url: str, *, path: str | Path | None = None) -> dict[str, Any]:
    init_db(path)
    with connect(path) as conn:
        row = conn.execute("SELECT * FROM proxies WHERE url=?", (url,)).fetchone()
        return dict(row) if row else {}

# ─────────────────────────────────────────────────────────
# 通用资源池 CRUD / lease
# ─────────────────────────────────────────────────────────


def _lease_expired_cutoff(seconds: int) -> str:
    return (datetime.now() - timedelta(seconds=max(1, seconds))).isoformat(timespec="seconds")


def recover_stale_resources(*, lease_ttl_seconds: int = 1800, path: str | Path | None = None) -> int:
    init_db(path)
    cutoff = _lease_expired_cutoff(lease_ttl_seconds)
    with connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            """
            UPDATE resource_pool
            SET status='available', lease_id='', leased_at='', updated_at=?, last_error='stale lease recovered'
            WHERE status='leased' AND leased_at!='' AND leased_at<?
            """,
            (now_iso(), cutoff),
        )
        return int(cur.rowcount or 0)

def upsert_resource(resource_type: str, provider: str, resource_key: str, payload: dict[str, Any], *,
                    status: str = "available", error: str = "", path: str | Path | None = None) -> None:
    init_db(path)
    now = now_iso()
    with connect(path) as conn:
        conn.execute(
            """
            INSERT INTO resource_pool(resource_type, provider, resource_key, payload_json, status, last_error, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(resource_type, provider, resource_key) DO UPDATE SET
              payload_json=excluded.payload_json,
              status=CASE WHEN resource_pool.status='leased' THEN resource_pool.status ELSE excluded.status END,
              last_error=excluded.last_error,
              updated_at=excluded.updated_at
            """,
            (resource_type, provider, resource_key, dumps(payload), status, error, now, now),
        )


def upsert_resources_many(
    resource_type: str,
    provider: str,
    rows: list[tuple[str, dict[str, Any]]],
    *,
    status: str = "available",
    error: str = "",
    path: str | Path | None = None,
) -> int:
    """Insert/update many resource_pool rows in one connection/transaction.

    Per-row upsert_resource opens a connection each time (~30ms+ on Windows),
    so 3000 Outlook tokens took ~2 minutes. Bulk path is O(batch).
    """
    if not rows:
        return 0
    init_db(path)
    now = now_iso()
    params: list[tuple[Any, ...]] = []
    seen: set[str] = set()
    for resource_key, payload in rows:
        key = str(resource_key or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        params.append(
            (
                resource_type,
                provider,
                key,
                dumps(payload if isinstance(payload, dict) else {}),
                status,
                error,
                now,
                now,
            )
        )
    if not params:
        return 0
    sql = """
        INSERT INTO resource_pool(resource_type, provider, resource_key, payload_json, status, last_error, created_at, updated_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(resource_type, provider, resource_key) DO UPDATE SET
          payload_json=excluded.payload_json,
          status=CASE WHEN resource_pool.status='leased' THEN resource_pool.status ELSE excluded.status END,
          last_error=excluded.last_error,
          updated_at=excluded.updated_at
    """
    with connect(path) as conn:
        # One explicit transaction so SQLite does not fsync per row.
        try:
            conn.execute("BEGIN")
        except Exception:
            pass
        conn.executemany(sql, params)
        try:
            conn.execute("COMMIT")
        except Exception:
            pass
    return len(params)


def existing_resource_keys(
    resource_type: str,
    provider: str,
    *,
    path: str | Path | None = None,
) -> set[str]:
    """Return all resource_key values for a type/provider in one query."""
    init_db(path)
    with connect(path) as conn:
        rows = conn.execute(
            """
            SELECT resource_key FROM resource_pool
            WHERE resource_type=? AND provider=?
            """,
            (resource_type, provider),
        ).fetchall()
    return {str(row[0] if not hasattr(row, "keys") else row["resource_key"]) for row in rows if row}


def list_resources(*, resource_type: str = "", provider: str = "", status: str = "", path: str | Path | None = None) -> list[dict[str, Any]]:
    init_db(path)
    with connect(path) as conn:
        sql = "SELECT * FROM resource_pool WHERE 1=1"
        params: list[Any] = []
        if resource_type:
            sql += " AND resource_type=?"
            params.append(resource_type)
        if provider:
            sql += " AND provider=?"
            params.append(provider)
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY updated_at DESC, id DESC"
        rows = conn.execute(sql, params).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["payload"] = loads(item.pop("payload_json", "{}"), {})
            items.append(item)
        return items

def delete_resources(resource_ids: Iterable[int], *, path: str | Path | None = None) -> int:
    init_db(path)
    ids = [int(resource_id) for resource_id in resource_ids if int(resource_id) > 0]
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    with connect(path) as conn:
        cur = conn.execute(f"DELETE FROM resource_pool WHERE id IN ({placeholders})", ids)
        return int(cur.rowcount or 0)


def get_resource(resource_type: str, provider: str, resource_key: str, *, path: str | Path | None = None) -> dict[str, Any]:
    init_db(path)
    with connect(path) as conn:
        row = conn.execute(
            """
            SELECT * FROM resource_pool
            WHERE resource_type=? AND provider=? AND resource_key=?
            """,
            (resource_type, provider, resource_key),
        ).fetchone()
        item = dict(row) if row else {}
        if item:
            item["payload"] = loads(item.pop("payload_json", "{}"), {})
        return item



def _is_db_locked_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return isinstance(exc, sqlite3.OperationalError) and (
        "database is locked" in msg or "database is busy" in msg or "locked" in msg
    )


def _with_db_retry(op_name: str, fn, *, attempts: int = 8, base_sleep: float = 0.05):
    """Retry write-ish SQLite ops under concurrent BEGIN IMMEDIATE contention."""
    import time
    import random
    last: BaseException | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            last = exc
            if not _is_db_locked_error(exc) or attempt + 1 >= attempts:
                raise
            # jittered backoff; total roughly < ~4s beyond busy_timeout waits inside sqlite
            time.sleep(base_sleep * (2 ** min(attempt, 5)) + random.random() * 0.05)
    assert last is not None
    raise last


def lease_resource(resource_type: str, provider: str, lease_id: str, *, region: str = "", lease_ttl_seconds: int = 1800, path: str | Path | None = None) -> dict[str, Any]:
    init_db(path)

    def _once() -> dict[str, Any]:
        return _lease_resource_once(resource_type, provider, lease_id, region=region, lease_ttl_seconds=lease_ttl_seconds, path=path)

    return _with_db_retry("lease_resource", _once)


def _lease_resource_once(resource_type: str, provider: str, lease_id: str, *, region: str = "", lease_ttl_seconds: int = 1800, path: str | Path | None = None) -> dict[str, Any]:
    now = now_iso()
    cutoff = _lease_expired_cutoff(lease_ttl_seconds)
    from infrastructure.db_backend import resolve_backend

    backend = resolve_backend()
    with connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE" if backend != "postgres" else "BEGIN")
        conn.execute(
            """
            UPDATE resource_pool
            SET status='available', lease_id='', leased_at='', updated_at=?, last_error='stale lease recovered'
            WHERE status='leased' AND leased_at!='' AND leased_at<?
            """,
            (now, cutoff),
        )
        conn.execute(
            """
            UPDATE resource_pool
            SET status='available', lease_id='', leased_at='', cooldown_until='', updated_at=?, last_error='cooldown expired'
            WHERE status='cooldown' AND cooldown_until!='' AND cooldown_until<=?
            """,
            (now, now),
        )
        sql = """
            SELECT id FROM resource_pool
            WHERE resource_type=? AND provider=? AND status='available'
              AND (cooldown_until='' OR cooldown_until IS NULL OR cooldown_until<=?)
        """
        params: list[Any] = [resource_type, provider, now]
        if resource_type == "proxy":
            sql += """
              AND NOT EXISTS (
                SELECT 1 FROM resource_pool busy
                WHERE busy.resource_type=resource_pool.resource_type
                  AND busy.provider=resource_pool.provider
                  AND busy.id!=resource_pool.id
                  AND COALESCE(json_extract(busy.payload_json, '$.exit_ip'), '')!=''
                  AND COALESCE(json_extract(busy.payload_json, '$.exit_ip'), '')=COALESCE(json_extract(resource_pool.payload_json, '$.exit_ip'), '')
                  AND (busy.status='leased' OR (busy.status='cooldown' AND (busy.cooldown_until='' OR busy.cooldown_until IS NULL OR busy.cooldown_until>?)))
              )
            """
            params.append(now)
        if region:
            sql += " AND (json_extract(payload_json, '$.region')=? OR json_extract(payload_json, '$.regions') LIKE ?)"
            params.extend([region, f"%{region}%"])
        sql += " ORDER BY CASE WHEN resource_type='proxy' THEN success_count ELSE -success_count END ASC, fail_count ASC, updated_at ASC, id ASC LIMIT 1"
        # Postgres: claim in one hop with SKIP LOCKED so concurrent leasers never block
        # each other on the same available row (no process-wide Python lock needed).
        if backend == "postgres":
            sql += " FOR UPDATE SKIP LOCKED"
        row = conn.execute(sql, params).fetchone()
        if not row:
            return {}
        cur = conn.execute(
            "UPDATE resource_pool SET status='leased', lease_id=?, leased_at=?, updated_at=? WHERE id=? AND status='available'",
            (lease_id, now, now, row["id"]),
        )
        if int(cur.rowcount or 0) != 1:
            return {}
        locked = conn.execute("SELECT * FROM resource_pool WHERE id=?", (row["id"],)).fetchone()
        item = dict(locked) if locked else {}
        if not item:
            return {}
        item["payload"] = loads(item.pop("payload_json", "{}"), {})
        return item



def report_resource(lease_id: str, resource_key: str, *, success: bool, cooldown_until: str = "", error: str = "", path: str | Path | None = None) -> None:
    init_db(path)
    now = now_iso()
    with connect(path) as conn:
        row = conn.execute(
            "SELECT id, provider, success_count FROM resource_pool WHERE resource_key=? AND (lease_id=? OR lease_id='') ORDER BY CASE WHEN lease_id=? THEN 0 ELSE 1 END, id LIMIT 1",
            (resource_key, lease_id, lease_id),
        ).fetchone()
        next_success_count = (int(row["success_count"] or 0) + 1) if success and row else 0
        if success and row and str(row["provider"] or "") == "bind_user_phone_url" and next_success_count < 3:
            status = "available"
        elif success and cooldown_until:
            status = "cooldown"
        else:
            status = "used" if success else ("cooldown" if cooldown_until else "available")
        conn.execute(
            """
            UPDATE resource_pool SET status=?, lease_id='', leased_at='', cooldown_until=?,
              success_count=success_count+?, fail_count=fail_count+?, last_error=?, updated_at=?
            WHERE resource_key=? AND (lease_id=? OR lease_id='')
            """,
            (status, cooldown_until, 1 if success else 0, 0 if success else 1, error, now, resource_key, lease_id),
        )

def increment_proxy_success(url: str, *, path: str | Path | None = None) -> None:
    init_db(path)
    with connect(path) as conn:
        conn.execute(
            "UPDATE proxies SET success_count=success_count+1, consecutive_fails=0, last_checked=? WHERE url=?",
            (now_iso(), url),
        )

def increment_proxy_fail(url: str, *, path: str | Path | None = None) -> None:
    init_db(path)
    with connect(path) as conn:
        conn.execute(
            "UPDATE proxies SET fail_count=fail_count+1, consecutive_fails=consecutive_fails+1, last_checked=? WHERE url=?",
            (now_iso(), url),
        )
        conn.execute(
            "UPDATE proxies SET is_active=0 WHERE url=? AND consecutive_fails>=3", (url,),
        )


def update_resource_status(resource_id: int, *, status: str, lease_id: str = "", cooldown_until: str = "", error: str = "", path: str | Path | None = None) -> None:
    init_db(path)
    now = now_iso()
    with connect(path) as conn:
        conn.execute(
            """
            UPDATE resource_pool
            SET status=?, lease_id=?, leased_at=CASE WHEN ?='leased' THEN COALESCE(NULLIF(leased_at, ''), ?) ELSE '' END,
                cooldown_until=?, last_error=?, updated_at=?
            WHERE id=?
            """,
            (status, lease_id, status, now, cooldown_until, error, now, resource_id),
        )

# ─────────────────────────────────────────────────────────
# 新增: 邮箱 OTP 事件
# ─────────────────────────────────────────────────────────

def insert_email_otp(email: str, code: str, *, subject: str = "", body: str = "",
                      path: str | Path | None = None) -> None:
    init_db(path)
    with connect(path) as conn:
        conn.execute(
            "INSERT INTO email_otp_events(email, code, raw_subject, raw_body) VALUES(?, ?, ?, ?)",
            (email.strip().lower(), code, subject, body),
        )

def get_latest_email_otp(email: str, *, path: str | Path | None = None) -> dict[str, Any]:
    init_db(path)
    with connect(path) as conn:
        row = conn.execute(
            "SELECT * FROM email_otp_events WHERE email=? AND consumed=0 ORDER BY id DESC LIMIT 1",
            (email.strip().lower(),),
        ).fetchone()
        return dict(row) if row else {}

def consume_email_otp(email: str, code: str, *, consumed_by: str = "",
                       path: str | Path | None = None) -> None:
    init_db(path)
    with connect(path) as conn:
        conn.execute(
            "UPDATE email_otp_events SET consumed=1, consumed_by=? WHERE email=? AND code=? AND consumed=0",
            (consumed_by, email.strip().lower(), code),
        )

# ─────────────────────────────────────────────────────────
# 新增: 注册运行记录
# ─────────────────────────────────────────────────────────

def create_registration_run(run: dict[str, Any], *, path=None) -> None:
    init_db(path)
    with connect(path) as conn:
        conn.execute(
            """INSERT INTO registration_runs(id, task_id, mode, status, phone, email,
               sms_provider, mailbox_provider, proxy_ip, proxy_region, plan_type,
               steps_completed, errors, started_at)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', '[]', ?)""",
            (run["id"], run.get("task_id", ""), run.get("mode", "phone"),
             run.get("status", "pending"), run.get("phone", ""), run.get("email", ""),
             run.get("sms_provider", ""), run.get("mailbox_provider", ""),
             run.get("proxy_ip", ""), run.get("proxy_region", ""),
             run.get("plan_type", ""), now_iso()),
        )

def update_registration_run(run_id: str, path=None, **patch: Any) -> None:
    init_db(path)
    allowed = {"status", "phone", "email", "proxy_ip", "proxy_region", "plan_type",
               "access_token_obtained", "refresh_token_obtained", "steps_completed",
               "errors", "finished_at", "proxy_ip", "proxy_region"}
    valid = {k: v for k, v in patch.items() if k in allowed}
    if not valid:
        return
    set_clause = ", ".join(f"{k}=?" for k in valid)
    values = list(valid.values()) + [run_id]
    with connect(path) as conn:
        conn.execute(f"UPDATE registration_runs SET {set_clause} WHERE id=?", values)

def get_registration_run(run_id: str, *, path=None) -> dict[str, Any]:
    init_db(path)
    with connect(path) as conn:
        row = conn.execute("SELECT * FROM registration_runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else {}

def list_registration_runs(*, status: str = "", limit: int = 50,
                            path: str | Path | None = None) -> list[dict[str, Any]]:
    init_db(path)
    sql = "SELECT * FROM registration_runs"
    params: list[Any] = []
    if status:
        sql += " WHERE status=?"
        params.append(status)
    sql += " ORDER BY started_at DESC LIMIT ?"
    params.append(limit)
    with connect(path) as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
