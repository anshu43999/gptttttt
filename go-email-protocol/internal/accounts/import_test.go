package accounts

import (
	"database/sql"
	"path/filepath"
	"testing"

	_ "modernc.org/sqlite"
)

func TestImportRegisteredRoundTrip(t *testing.T) {
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "t.db")
	db, err := sql.Open("sqlite", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	schema := `
CREATE TABLE accounts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_key TEXT, account_id TEXT, platform TEXT, email TEXT, password TEXT, plan_type TEXT,
  status TEXT, stage TEXT, registration_mode TEXT, registration_status TEXT, registration_task_id TEXT,
  registration_completed_at TEXT, registration_error TEXT, login_identifier TEXT,
  registration_proxy_exit_ip TEXT, registration_proxy_region TEXT,
  created_at TEXT, updated_at TEXT
);
CREATE TABLE account_credentials(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id_ref INTEGER, access_token TEXT, refresh_token TEXT, id_token TEXT,
  chatgpt_access_token_initial TEXT, token_expires_at TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE account_proxy(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id_ref INTEGER, registration_proxy TEXT, registration_exit_ip TEXT, registration_country TEXT,
  created_at TEXT, updated_at TEXT
);
`
	if _, err = db.Exec(schema); err != nil {
		t.Fatal(err)
	}
	_ = db.Close()

	pk, err := ImportRegistered(dbPath, Record{
		Email: "a@test.com", Password: "pw", AccessToken: "tok", AccountID: "acc-1", TaskID: "t1",
		ProxyExitIP: "1.2.3.4", ProxyURL: "socks5://x", ProxyRegion: "JP", Engine: "pure-go",
	})
	if err != nil {
		t.Fatal(err)
	}
	if pk <= 0 {
		t.Fatalf("pk=%d", pk)
	}
	pk2, err := ImportRegistered(dbPath, Record{
		Email: "a@test.com", Password: "pw2", AccessToken: "tok2", AccountID: "acc-1", TaskID: "t2",
	})
	if err != nil {
		t.Fatal(err)
	}
	if pk2 != pk {
		t.Fatalf("expected upsert same pk %d got %d", pk, pk2)
	}

	db, err = sql.Open("sqlite", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	var tok, pw string
	if err := db.QueryRow(`
		SELECT c.access_token, a.password FROM accounts a
		JOIN account_credentials c ON c.account_id_ref=a.id
		WHERE a.id=?
	`, pk).Scan(&tok, &pw); err != nil {
		t.Fatal(err)
	}
	if tok != "tok2" || pw != "pw2" {
		t.Fatalf("tok=%q pw=%q", tok, pw)
	}
}
