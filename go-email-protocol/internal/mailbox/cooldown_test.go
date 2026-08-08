package mailbox

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/gpt-register/go-email-protocol/internal/store"
)

func forceSQLite(t *testing.T) {
	t.Helper()
	// OpenPath honors env.db → postgres in this workspace. Pin SQLite for unit tests.
	_ = os.Setenv("GPT_REGISTER_SKIP_ENV_DB", "1")
	_ = os.Setenv("GPT_REGISTER_DB_BACKEND", "sqlite")
	_ = os.Unsetenv("GPT_REGISTER_DATABASE_URL")
	_ = os.Unsetenv("DATABASE_URL")
}

func TestCooldownDurationForOTPEmpty(t *testing.T) {
	d := cooldownDurationFor("mailbox: outlook OTP timeout for a@x after 50s: last=graph_no_openai_code")
	if d != OTPEmptyEmailCooldown {
		t.Fatalf("got %v want %v", d, OTPEmptyEmailCooldown)
	}
}

func TestCooldownDurationForSession(t *testing.T) {
	d := cooldownDurationFor(`protocol: S10 status 409 body=no longer valid invalid_state`)
	if d != SessionEmailCooldown {
		t.Fatalf("got %v want %v", d, SessionEmailCooldown)
	}
}

func TestMarkUsedCooldownWritesUntil(t *testing.T) {
	forceSQLite(t)
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "pool.db")
	db, err := store.MustSQLite(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	_, err = db.Exec(`
CREATE TABLE resource_pool (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  resource_type TEXT, provider TEXT, resource_key TEXT,
  payload_json TEXT, status TEXT, lease_id TEXT, leased_at TEXT,
  cooldown_until TEXT, success_count INTEGER DEFAULT 0, fail_count INTEGER DEFAULT 0,
  last_error TEXT, created_at TEXT, updated_at TEXT
)`)
	if err != nil {
		t.Fatal(err)
	}
	res, err := db.Exec(`INSERT INTO resource_pool(resource_type,provider,resource_key,payload_json,status,lease_id,fail_count,last_error,created_at,updated_at,cooldown_until)
VALUES('email','outlook_token','a@x.com','{"email":"a@x.com","client_id":"c","refresh_token":"r"}','reserved','t1',0,'','2020-01-01T00:00:00Z','2020-01-01T00:00:00Z','')`)
	if err != nil {
		t.Fatal(err)
	}
	id, _ := res.LastInsertId()
	_ = db.Close()

	if err := MarkUsed(dbPath, id, "cooldown", "mailbox: outlook OTP timeout after 50s: last=graph_no_openai_code"); err != nil {
		t.Fatal(err)
	}

	db2, err := store.MustSQLite(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer db2.Close()
	var status, until, last string
	var fails int
	if err := db2.QueryRow(`SELECT status, COALESCE(cooldown_until,''), COALESCE(last_error,''), COALESCE(fail_count,0) FROM resource_pool WHERE id=?`, id).
		Scan(&status, &until, &last, &fails); err != nil {
		t.Fatal(err)
	}
	if status != "cooldown" {
		t.Fatalf("status=%s", status)
	}
	if until == "" {
		t.Fatal("cooldown_until empty")
	}
	exp, err := time.Parse(time.RFC3339, until)
	if err != nil {
		exp, err = time.Parse(time.RFC3339Nano, until)
		if err != nil {
			t.Fatalf("parse until %q: %v", until, err)
		}
	}
	if d := time.Until(exp); d < 5*time.Hour || d > 7*time.Hour {
		t.Fatalf("until delta %v not ~6h", d)
	}
	if fails != 1 {
		t.Fatalf("fail_count=%d", fails)
	}
	if !strings.Contains(last, "graph_no_openai") {
		t.Fatalf("last_error=%s", last)
	}
}

func TestReclaimExpiredEmptyUntilCooldown(t *testing.T) {
	forceSQLite(t)
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "pool.db")
	db, err := store.MustSQLite(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	_, err = db.Exec(`
CREATE TABLE resource_pool (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  resource_type TEXT, provider TEXT, resource_key TEXT,
  payload_json TEXT, status TEXT, lease_id TEXT, leased_at TEXT,
  cooldown_until TEXT, success_count INTEGER DEFAULT 0, fail_count INTEGER DEFAULT 0,
  last_error TEXT, created_at TEXT, updated_at TEXT
)`)
	if err != nil {
		t.Fatal(err)
	}
	old := time.Now().UTC().Add(-7 * time.Hour).Format(time.RFC3339)
	payloadOld := `{"email":"old@x.com","client_id":"c","refresh_token":"r"}`
	payloadFresh := `{"email":"fresh@x.com","client_id":"c","refresh_token":"r"}`
	_, err = db.Exec(`INSERT INTO resource_pool(resource_type,provider,resource_key,payload_json,status,lease_id,fail_count,last_error,created_at,updated_at,cooldown_until)
VALUES('email','outlook_token','old@x.com',?,'cooldown','',2,'otp timeout graph_no_openai_code',?,?, '')`, payloadOld, old, old)
	if err != nil {
		t.Fatal(err)
	}
	_, err = db.Exec(`INSERT INTO resource_pool(resource_type,provider,resource_key,payload_json,status,lease_id,fail_count,last_error,created_at,updated_at,cooldown_until)
VALUES('email','outlook_token','fresh@x.com',?,'available','',0,'',?,?, '')`, payloadFresh, old, old)
	if err != nil {
		t.Fatal(err)
	}
	_ = db.Close()

	acc, err := LeaseFromDBProvider(dbPath, "task1", ProviderOutlookToken)
	if err != nil {
		t.Fatal(err)
	}
	if acc.Email != "fresh@x.com" {
		t.Fatalf("prefer fresh fail0, got %s id=%d", acc.Email, acc.ID)
	}

	acc2, err := LeaseFromDBProvider(dbPath, "task2", ProviderOutlookToken)
	if err != nil {
		t.Fatal(err)
	}
	if acc2.Email != "old@x.com" {
		t.Fatalf("want reclaimed old, got %s", acc2.Email)
	}
}
