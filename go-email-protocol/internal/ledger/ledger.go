// Package ledger provides a durable SQLite job ledger for G1.
// Secrets (password, OTP, capabilities) are never stored in plain columns.
package ledger

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	_ "modernc.org/sqlite"
)

// Status values for jobs.
const (
	StatusQueued            = "queued"
	StatusRunning           = "running"
	StatusWaitingForOTP     = "waiting_for_otp"
	StatusCancelling        = "cancelling"
	StatusCancelled         = "cancelled"
	StatusFailed            = "failed"
	StatusSucceeded         = "succeeded"
	StatusReconcileRequired = "reconcile_required"
)

// Terminal reports whether status is terminal.
func Terminal(status string) bool {
	switch status {
	case StatusCancelled, StatusFailed, StatusSucceeded, StatusReconcileRequired:
		return true
	default:
		return false
	}
}

// NonTerminal reports whether status still holds an admission seat.
func NonTerminal(status string) bool {
	return !Terminal(status)
}

// Record is the durable non-secret job state.
type Record struct {
	JobID                       string
	TaskID                      string
	AttemptID                   int
	IdempotencyKeyHash          string
	RequestFingerprint          string
	Status                      string
	StateVersion                int64
	Stage                       string
	CreatedAt                   time.Time
	UpdatedAt                   time.Time
	DeadlineAt                  time.Time
	ProfileID                   string
	EmailResourceKey            string
	ProxyResourceKey            string
	BridgeGeneration            int64
	LeaseFence                  int64
	ExitIPHash                  string
	ChallengeID                 string
	ChallengeIssuedAt           time.Time
	ChallengeDeadline           time.Time
	RetryAfterMS                int
	FailureCode                 string
	Retryable                   bool
	RegistrationMayHaveSucceeded bool
	// CapabilityHash is sha256 of job_capability; never the raw secret.
	CapabilityHash string
	// SecretBlob is encrypted checkpoint bytes (password, capability material, jar, etc.).
	SecretBlob []byte
	// ResultJSON holds non-secret or redacted session document for succeeded jobs.
	// access_token is allowed only in API responses assembled from memory/encrypted blob.
	ResultJSON []byte
	// ProfileJSON is the opaque profile object (no secrets expected).
	ProfileJSON []byte
	// BridgeURL is loopback only; capability never stored plain.
	BridgeURL string
	// Email is required for session document assembly; password is secret-only.
	Email string
	// SkipPhone flag.
	SkipPhone bool
}

// CreateInput is used when inserting a new job.
type CreateInput struct {
	JobID              string
	TaskID             string
	AttemptID          int
	IdempotencyKey     string
	RequestFingerprint string
	Capability         string
	Email              string
	Password           string
	EmailKey           string
	ProxyKey           string
	BridgeURL          string
	BridgeGeneration   int64
	LeaseFence         int64
	ExitIP             string
	ProfileJSON        []byte
	ProfileID          string
	SkipPhone          bool
	DeadlineAt         time.Time
	SecretBlob         []byte
	Status             string
	Stage              string
}

// OTPChallenge is durable challenge metadata (no code).
type OTPChallenge struct {
	ChallengeID string
	IssuedAt    time.Time
	DeadlineAt  time.Time
	Version     int64
}

// Ledger is a SQLite-backed job store.
type Ledger struct {
	db *sql.DB
}

// Open opens or creates a ledger at path (":memory:" allowed).
func Open(path string) (*Ledger, error) {
	dsn := path
	if path != ":memory:" {
		// WAL + busy timeout for concurrent access.
		dsn = path + "?_pragma=busy_timeout(5000)&_pragma=journal_mode(WAL)&_pragma=foreign_keys(1)"
	} else {
		dsn = "file:memdb?mode=memory&cache=shared"
	}
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("open sqlite: %w", err)
	}
	db.SetMaxOpenConns(1) // serialize writers; fine for G1
	l := &Ledger{db: db}
	if err := l.migrate(); err != nil {
		_ = db.Close()
		return nil, err
	}
	return l, nil
}

// Close closes the DB. On file-backed ledgers, switch off WAL first so
// Windows can delete the temp DB after tests (wal/shm handles).
func (l *Ledger) Close() error {
	if l == nil || l.db == nil {
		return nil
	}
	_, _ = l.db.Exec(`PRAGMA wal_checkpoint(TRUNCATE)`)
	_, _ = l.db.Exec(`PRAGMA journal_mode=DELETE`)
	err := l.db.Close()
	l.db = nil
	return err
}

// DB exposes the underlying handle for tests.
func (l *Ledger) DB() *sql.DB { return l.db }

func (l *Ledger) migrate() error {
	const schema = `
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
`
	if _, err := l.db.Exec(schema); err != nil {
		return fmt.Errorf("migrate: %w", err)
	}
	var n int
	if err := l.db.QueryRow(`SELECT COUNT(1) FROM schema_migrations WHERE version = 1`).Scan(&n); err != nil {
		return err
	}
	if n == 0 {
		if _, err := l.db.Exec(`INSERT INTO schema_migrations(version, applied_at) VALUES (1, ?)`, time.Now().UTC().Format(time.RFC3339Nano)); err != nil {
			return err
		}
	}
	return nil
}

// HashSecret returns hex sha256 of s.
func HashSecret(s string) string {
	sum := sha256.Sum256([]byte(s))
	return hex.EncodeToString(sum[:])
}

// HashExitIP hashes exit IP or returns empty.
func HashExitIP(ip string) string {
	ip = strings.TrimSpace(ip)
	if ip == "" {
		return ""
	}
	return HashSecret(ip)
}

// Create inserts a new job. On UNIQUE conflict returns existing via lookup.
func (l *Ledger) Create(ctx context.Context, in CreateInput) (*Record, error) {
	now := time.Now().UTC()
	status := in.Status
	if status == "" {
		status = StatusQueued
	}
	stage := in.Stage
	if stage == "" {
		stage = "admission"
	}
	deadline := in.DeadlineAt
	if deadline.IsZero() {
		deadline = now.Add(15 * time.Minute)
	}
	rec := &Record{
		JobID:              in.JobID,
		TaskID:             in.TaskID,
		AttemptID:          in.AttemptID,
		IdempotencyKeyHash: HashSecret(in.IdempotencyKey),
		RequestFingerprint: in.RequestFingerprint,
		Status:             status,
		StateVersion:       1,
		Stage:              stage,
		CreatedAt:          now,
		UpdatedAt:          now,
		DeadlineAt:         deadline,
		ProfileID:          in.ProfileID,
		EmailResourceKey:   in.EmailKey,
		ProxyResourceKey:   in.ProxyKey,
		BridgeGeneration:   in.BridgeGeneration,
		LeaseFence:         in.LeaseFence,
		ExitIPHash:         HashExitIP(in.ExitIP),
		RetryAfterMS:       1000,
		CapabilityHash:     HashSecret(in.Capability),
		SecretBlob:         in.SecretBlob,
		ProfileJSON:        in.ProfileJSON,
		BridgeURL:          in.BridgeURL,
		Email:              in.Email,
		SkipPhone:          in.SkipPhone,
	}
	_, err := l.db.ExecContext(ctx, `
INSERT INTO jobs (
  job_id, task_id, attempt_id, idempotency_key_hash, request_fingerprint,
  status, state_version, stage, created_at, updated_at, deadline_at,
  profile_id, email_resource_key, proxy_resource_key, bridge_generation,
  lease_fence, exit_ip_hash, challenge_id, challenge_issued_at, challenge_deadline,
  retry_after_ms, failure_code, retryable, registration_may_have_succeeded,
  capability_hash, secret_blob, result_json, profile_json, bridge_url, email, skip_phone
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
		rec.JobID, rec.TaskID, rec.AttemptID, rec.IdempotencyKeyHash, rec.RequestFingerprint,
		rec.Status, rec.StateVersion, rec.Stage, rec.CreatedAt.Format(time.RFC3339Nano), rec.UpdatedAt.Format(time.RFC3339Nano), rec.DeadlineAt.Format(time.RFC3339Nano),
		rec.ProfileID, rec.EmailResourceKey, rec.ProxyResourceKey, rec.BridgeGeneration,
		rec.LeaseFence, rec.ExitIPHash, "", "", "",
		rec.RetryAfterMS, "", 0, 0,
		rec.CapabilityHash, rec.SecretBlob, nil, rec.ProfileJSON, rec.BridgeURL, rec.Email, boolInt(rec.SkipPhone),
	)
	if err != nil {
		return nil, err
	}
	return rec, nil
}

// GetByID loads a job by id.
func (l *Ledger) GetByID(ctx context.Context, jobID string) (*Record, error) {
	return l.scanOne(ctx, `SELECT `+jobColumns+` FROM jobs WHERE job_id = ?`, jobID)
}

// GetByTaskAttempt loads by task+attempt unique key.
func (l *Ledger) GetByTaskAttempt(ctx context.Context, taskID string, attemptID int) (*Record, error) {
	return l.scanOne(ctx, `SELECT `+jobColumns+` FROM jobs WHERE task_id = ? AND attempt_id = ?`, taskID, attemptID)
}

// ListNonTerminal returns jobs that need recovery on restart.
func (l *Ledger) ListNonTerminal(ctx context.Context) ([]*Record, error) {
	rows, err := l.db.QueryContext(ctx, `SELECT `+jobColumns+` FROM jobs WHERE status NOT IN (?,?,?,?)`,
		StatusCancelled, StatusFailed, StatusSucceeded, StatusReconcileRequired)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*Record
	for rows.Next() {
		r, err := scanRow(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// CountActive returns number of non-terminal jobs.
func (l *Ledger) CountActive(ctx context.Context) (int, error) {
	var n int
	err := l.db.QueryRowContext(ctx, `SELECT COUNT(1) FROM jobs WHERE status NOT IN (?,?,?,?)`,
		StatusCancelled, StatusFailed, StatusSucceeded, StatusReconcileRequired).Scan(&n)
	return n, err
}

// VerifyCapability checks capability hash.
func (l *Ledger) VerifyCapability(rec *Record, capability string) bool {
	if rec == nil || capability == "" {
		return false
	}
	return rec.CapabilityHash == HashSecret(capability)
}

// Transition updates status/stage/version atomically with optimistic concurrency.
// expectedVersion must match current state_version; new version is expectedVersion+1.
func (l *Ledger) Transition(ctx context.Context, jobID string, expectedVersion int64, mut func(*Record) error) (*Record, error) {
	tx, err := l.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer func() { _ = tx.Rollback() }()

	row := tx.QueryRowContext(ctx, `SELECT `+jobColumns+` FROM jobs WHERE job_id = ?`, jobID)
	rec, err := scanRow(row)
	if err != nil {
		return nil, err
	}
	if rec.StateVersion != expectedVersion {
		return nil, fmt.Errorf("%w: have %d want %d", ErrVersionConflict, rec.StateVersion, expectedVersion)
	}
	if err := mut(rec); err != nil {
		return nil, err
	}
	rec.StateVersion = expectedVersion + 1
	rec.UpdatedAt = time.Now().UTC()
	if err := updateRow(ctx, tx, rec); err != nil {
		return nil, err
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return rec, nil
}

// Update unconditional write of full record (caller holds versioning discipline).
func (l *Ledger) Update(ctx context.Context, rec *Record) error {
	rec.UpdatedAt = time.Now().UTC()
	return updateRow(ctx, l.db, rec)
}

// BumpVersion increments version and applies mut under lock.
func (l *Ledger) BumpVersion(ctx context.Context, jobID string, mut func(*Record) error) (*Record, error) {
	if l == nil || l.db == nil {
		return nil, errors.New("ledger closed")
	}
	tx, err := l.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer func() { _ = tx.Rollback() }()
	row := tx.QueryRowContext(ctx, `SELECT `+jobColumns+` FROM jobs WHERE job_id = ?`, jobID)
	rec, err := scanRow(row)
	if err != nil {
		return nil, err
	}
	if err := mut(rec); err != nil {
		return nil, err
	}
	rec.StateVersion++
	rec.UpdatedAt = time.Now().UTC()
	if err := updateRow(ctx, tx, rec); err != nil {
		return nil, err
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return rec, nil
}

// ErrVersionConflict is returned on optimistic concurrency failure.
var ErrVersionConflict = errors.New("state_version conflict")

// ErrNotFound is returned when job is missing.
var ErrNotFound = errors.New("job not found")

// IsUniqueViolation reports SQLite unique constraint errors.
func IsUniqueViolation(err error) bool {
	if err == nil {
		return false
	}
	msg := strings.ToLower(err.Error())
	return strings.Contains(msg, "unique") || strings.Contains(msg, "constraint")
}

const jobColumns = `job_id, task_id, attempt_id, idempotency_key_hash, request_fingerprint,
  status, state_version, stage, created_at, updated_at, deadline_at,
  profile_id, email_resource_key, proxy_resource_key, bridge_generation,
  lease_fence, exit_ip_hash, challenge_id, challenge_issued_at, challenge_deadline,
  retry_after_ms, failure_code, retryable, registration_may_have_succeeded,
  capability_hash, secret_blob, result_json, profile_json, bridge_url, email, skip_phone`

type rowScanner interface {
	Scan(dest ...any) error
}

func (l *Ledger) scanOne(ctx context.Context, q string, args ...any) (*Record, error) {
	if l == nil || l.db == nil {
		return nil, errors.New("ledger closed")
	}
	row := l.db.QueryRowContext(ctx, q, args...)
	rec, err := scanRow(row)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrNotFound
	}
	return rec, err
}

func scanRow(row rowScanner) (*Record, error) {
	var r Record
	var created, updated, deadline string
	var challengeIssued, challengeDeadline string
	var retryable, regMay, skip int
	var secret, result, profile []byte
	err := row.Scan(
		&r.JobID, &r.TaskID, &r.AttemptID, &r.IdempotencyKeyHash, &r.RequestFingerprint,
		&r.Status, &r.StateVersion, &r.Stage, &created, &updated, &deadline,
		&r.ProfileID, &r.EmailResourceKey, &r.ProxyResourceKey, &r.BridgeGeneration,
		&r.LeaseFence, &r.ExitIPHash, &r.ChallengeID, &challengeIssued, &challengeDeadline,
		&r.RetryAfterMS, &r.FailureCode, &retryable, &regMay,
		&r.CapabilityHash, &secret, &result, &profile, &r.BridgeURL, &r.Email, &skip,
	)
	if err != nil {
		return nil, err
	}
	r.CreatedAt = parseTime(created)
	r.UpdatedAt = parseTime(updated)
	r.DeadlineAt = parseTime(deadline)
	r.ChallengeIssuedAt = parseTime(challengeIssued)
	r.ChallengeDeadline = parseTime(challengeDeadline)
	r.Retryable = retryable != 0
	r.RegistrationMayHaveSucceeded = regMay != 0
	r.SkipPhone = skip != 0
	r.SecretBlob = secret
	r.ResultJSON = result
	r.ProfileJSON = profile
	return &r, nil
}

type execer interface {
	ExecContext(ctx context.Context, query string, args ...any) (sql.Result, error)
}

func updateRow(ctx context.Context, db execer, r *Record) error {
	_, err := db.ExecContext(ctx, `
UPDATE jobs SET
  status=?, state_version=?, stage=?, updated_at=?, deadline_at=?,
  profile_id=?, email_resource_key=?, proxy_resource_key=?, bridge_generation=?,
  lease_fence=?, exit_ip_hash=?, challenge_id=?, challenge_issued_at=?, challenge_deadline=?,
  retry_after_ms=?, failure_code=?, retryable=?, registration_may_have_succeeded=?,
  capability_hash=?, secret_blob=?, result_json=?, profile_json=?, bridge_url=?, email=?, skip_phone=?
WHERE job_id=?`,
		r.Status, r.StateVersion, r.Stage, r.UpdatedAt.Format(time.RFC3339Nano), r.DeadlineAt.Format(time.RFC3339Nano),
		r.ProfileID, r.EmailResourceKey, r.ProxyResourceKey, r.BridgeGeneration,
		r.LeaseFence, r.ExitIPHash, r.ChallengeID, formatTime(r.ChallengeIssuedAt), formatTime(r.ChallengeDeadline),
		r.RetryAfterMS, r.FailureCode, boolInt(r.Retryable), boolInt(r.RegistrationMayHaveSucceeded),
		r.CapabilityHash, r.SecretBlob, r.ResultJSON, r.ProfileJSON, r.BridgeURL, r.Email, boolInt(r.SkipPhone),
		r.JobID,
	)
	return err
}

func parseTime(s string) time.Time {
	if s == "" {
		return time.Time{}
	}
	t, err := time.Parse(time.RFC3339Nano, s)
	if err != nil {
		t, _ = time.Parse(time.RFC3339, s)
	}
	return t
}

func formatTime(t time.Time) string {
	if t.IsZero() {
		return ""
	}
	return t.UTC().Format(time.RFC3339Nano)
}

func boolInt(b bool) int {
	if b {
		return 1
	}
	return 0
}

// MarshalResult stores a result payload.
func MarshalResult(v any) ([]byte, error) {
	return json.Marshal(v)
}
