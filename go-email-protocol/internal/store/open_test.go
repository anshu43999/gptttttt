package store

import (
	"strings"
	"testing"
)

func TestOpenMainSQLite(t *testing.T) {
	dir := t.TempDir()
	p := dir + "/t.db"
	// path join portable
	p = strings.ReplaceAll(p, "\\", "/")
	db, backend, err := OpenMain(Config{Backend: BackendSQLite, SQLitePath: p})
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if backend != BackendSQLite {
		t.Fatalf("backend %s", backend)
	}
	if _, err := db.Exec(`CREATE TABLE t(id INTEGER PRIMARY KEY)`); err != nil {
		t.Fatal(err)
	}
}

func TestOpenMainPostgresOptional(t *testing.T) {
	url := "postgresql://gpt:gpt@127.0.0.1:5432/gpt_register"
	db, backend, err := OpenMain(Config{Backend: BackendPostgres, URL: url})
	if err != nil {
		// PG may be down in CI; skip rather than fail-closed forever.
		t.Skipf("postgres unavailable: %v", err)
	}
	defer db.Close()
	if backend != BackendPostgres {
		t.Fatalf("backend %s", backend)
	}
	var one int
	if err := db.QueryRow(`SELECT 1`).Scan(&one); err != nil {
		t.Fatal(err)
	}
	if one != 1 {
		t.Fatalf("got %d", one)
	}
}

func TestOpenMainPostgresWithoutURLFailsClosed(t *testing.T) {
	db, backend, err := OpenMain(Config{Backend: BackendPostgres})
	if db != nil {
		db.Close()
		t.Fatal("expected no database handle")
	}
	if backend != BackendPostgres {
		t.Fatalf("backend %q", backend)
	}
	if err == nil || !strings.Contains(err.Error(), "no SQLite fallback") {
		t.Fatalf("expected fail-closed error, got %v", err)
	}
}

func TestFromEnvDefaultsSQLite(t *testing.T) {
	t.Setenv("GPT_REGISTER_SKIP_ENV_DB", "1")
	t.Setenv("GPT_REGISTER_DB_BACKEND", "")
	t.Setenv("GPT_REGISTER_DATABASE_URL", "")
	t.Setenv("DATABASE_URL", "")
	cfg := FromEnv("data/gpt_register.db")
	if cfg.Backend != BackendSQLite {
		t.Fatalf("got %q", cfg.Backend)
	}
	if cfg.SQLitePath != "data/gpt_register.db" {
		t.Fatalf("path %q", cfg.SQLitePath)
	}
}

func TestFromEnvPostgresURL(t *testing.T) {
	t.Setenv("GPT_REGISTER_DB_BACKEND", "")
	t.Setenv("DATABASE_URL", "postgresql://u:p@h/db")
	cfg := FromEnv("x.db")
	if cfg.Backend != BackendPostgres {
		t.Fatalf("got %q", cfg.Backend)
	}
	if cfg.URL != "postgresql://u:p@h/db" {
		t.Fatalf("url %q", cfg.URL)
	}
}

func TestRebindPostgres(t *testing.T) {
	q := `SELECT id FROM t WHERE a=? AND b=? AND c='x?'`
	got := Rebind(BackendPostgres, q)
	want := `SELECT id FROM t WHERE a=$1 AND b=$2 AND c='x?'`
	if got != want {
		t.Fatalf("got %q want %q", got, want)
	}
	if Rebind(BackendSQLite, q) != q {
		t.Fatal("sqlite rebind should no-op")
	}
}

func TestRebindEscapedQuote(t *testing.T) {
	q := `SELECT 1 WHERE x='it''s' AND y=?`
	got := Rebind(BackendPostgres, q)
	want := `SELECT 1 WHERE x='it''s' AND y=$1`
	if got != want {
		t.Fatalf("got %q want %q", got, want)
	}
}
