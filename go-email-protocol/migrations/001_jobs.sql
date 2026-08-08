-- G1 durable job ledger (also applied embedded from ledger.Open).
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  attempt_id INTEGER NOT NULL,
  idempotency_key_hash TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL,
  status TEXT NOT NULL,
  state_version INTEGER NOT NULL DEFAULT 1,
  stage TEXT NOT NULL DEFAULT 'admission',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  deadline_at TEXT NOT NULL,
  profile_id TEXT NOT NULL DEFAULT '',
  email_resource_key TEXT NOT NULL DEFAULT '',
  proxy_resource_key TEXT NOT NULL DEFAULT '',
  bridge_generation INTEGER NOT NULL DEFAULT 0,
  lease_fence INTEGER NOT NULL DEFAULT 0,
  exit_ip_hash TEXT NOT NULL DEFAULT '',
  challenge_id TEXT NOT NULL DEFAULT '',
  challenge_issued_at TEXT NOT NULL DEFAULT '',
  challenge_deadline TEXT NOT NULL DEFAULT '',
  retry_after_ms INTEGER NOT NULL DEFAULT 1000,
  failure_code TEXT NOT NULL DEFAULT '',
  retryable INTEGER NOT NULL DEFAULT 0,
  registration_may_have_succeeded INTEGER NOT NULL DEFAULT 0,
  capability_hash TEXT NOT NULL,
  secret_blob BLOB,
  result_json BLOB,
  profile_json BLOB,
  bridge_url TEXT NOT NULL DEFAULT '',
  email TEXT NOT NULL DEFAULT '',
  skip_phone INTEGER NOT NULL DEFAULT 1,
  UNIQUE(task_id, attempt_id)
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_fingerprint ON jobs(task_id, attempt_id, request_fingerprint);
