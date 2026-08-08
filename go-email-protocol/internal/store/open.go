// Package store centralizes main-DB open paths for pure-Go writers.
//
// Backends:
//   - postgres: when env.db / GPT_REGISTER_DB_BACKEND=postgres / postgres URL
//   - sqlite: modernc path via Config.SQLitePath / -db
//
// Production (2026-07-18): project-root env.db flips to Postgres. FromEnv auto-loads
// env.db when process env is empty so bare `go run` still hits PG.
// Explicit postgres without URL is fail-closed (no silent SQLite).
package store

import (
	"bufio"
	"database/sql"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	_ "github.com/jackc/pgx/v5/stdlib"
	_ "modernc.org/sqlite"
)

// Backend names.
const (
	BackendSQLite   = "sqlite"
	BackendPostgres = "postgres"
)

// Config selects the main business DB (accounts / resource_pool).
// Empty Backend + empty URL → SQLite path (only when no env.db postgres).
type Config struct {
	Backend string // sqlite | postgres
	// SQLitePath is used when Backend is sqlite (or empty).
	SQLitePath string
	// URL is postgres DSN when Backend is postgres, or when URL is set
	// and Backend empty (auto postgres).
	URL string
}

var (
	envDBOnce sync.Once
)

// LoadEnvDB loads ../env.db or ../../env.db (relative to cwd / executable search)
// into process env. Existing non-empty vars win. Safe to call repeatedly.
func LoadEnvDB() {
	envDBOnce.Do(func() {
		if strings.TrimSpace(os.Getenv("GPT_REGISTER_SKIP_ENV_DB")) != "" {
			return
		}
		// Already fully set?
		if strings.TrimSpace(os.Getenv("GPT_REGISTER_DB_BACKEND")) != "" &&
			(strings.TrimSpace(os.Getenv("GPT_REGISTER_DATABASE_URL")) != "" ||
				strings.TrimSpace(os.Getenv("DATABASE_URL")) != "") {
			return
		}
		for _, p := range envDBCandidates() {
			if applyEnvFile(p) {
				return
			}
		}
	})
}

func envDBCandidates() []string {
	var out []string
	// cwd and parents (go-email-protocol/ → project root)
	if wd, err := os.Getwd(); err == nil {
		cur := wd
		for i := 0; i < 5; i++ {
			out = append(out, filepath.Join(cur, "env.db"))
			parent := filepath.Dir(cur)
			if parent == cur {
				break
			}
			cur = parent
		}
	}
	// relative to this source tree: go-email-protocol/../env.db
	if exe, err := os.Executable(); err == nil {
		dir := filepath.Dir(exe)
		out = append(out,
			filepath.Join(dir, "env.db"),
			filepath.Join(dir, "..", "env.db"),
			filepath.Join(dir, "..", "..", "env.db"),
		)
	}
	return out
}

func applyEnvFile(path string) bool {
	f, err := os.Open(path)
	if err != nil {
		return false
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	applied := 0
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		i := strings.IndexByte(line, '=')
		if i <= 0 {
			continue
		}
		key := strings.TrimSpace(line[:i])
		val := strings.TrimSpace(line[i+1:])
		val = strings.Trim(val, `"'`)
		if key == "" {
			continue
		}
		if strings.TrimSpace(os.Getenv(key)) == "" {
			_ = os.Setenv(key, val)
			applied++
		}
	}
	// If file existed but sparse, ensure postgres triad when backend says postgres
	if applied > 0 || fileMentionsPostgres(path) {
		if strings.TrimSpace(os.Getenv("GPT_REGISTER_DB_BACKEND")) == "" {
			_ = os.Setenv("GPT_REGISTER_DB_BACKEND", BackendPostgres)
		}
		if strings.EqualFold(strings.TrimSpace(os.Getenv("GPT_REGISTER_DB_BACKEND")), BackendPostgres) ||
			strings.EqualFold(strings.TrimSpace(os.Getenv("GPT_REGISTER_DB_BACKEND")), "pg") {
			if strings.TrimSpace(os.Getenv("GPT_REGISTER_DATABASE_URL")) == "" &&
				strings.TrimSpace(os.Getenv("DATABASE_URL")) == "" {
				_ = os.Setenv("GPT_REGISTER_DATABASE_URL", "postgresql://gpt:gpt@127.0.0.1:5432/gpt_register")
				_ = os.Setenv("DATABASE_URL", "postgresql://gpt:gpt@127.0.0.1:5432/gpt_register")
			}
		}
		return true
	}
	return false
}

func fileMentionsPostgres(path string) bool {
	b, err := os.ReadFile(path)
	if err != nil {
		return false
	}
	s := strings.ToLower(string(b))
	return strings.Contains(s, "postgres")
}

// FromEnv builds Config from env + default sqlite path.
// Auto-loads env.db once when process env is empty.
//
//	GPT_REGISTER_DB_BACKEND=sqlite|postgres
//	GPT_REGISTER_DATABASE_URL / DATABASE_URL
func FromEnv(defaultSQLite string) Config {
	LoadEnvDB()
	backend := strings.ToLower(strings.TrimSpace(os.Getenv("GPT_REGISTER_DB_BACKEND")))
	url := strings.TrimSpace(os.Getenv("GPT_REGISTER_DATABASE_URL"))
	if url == "" {
		url = strings.TrimSpace(os.Getenv("DATABASE_URL"))
	}
	if backend == "" {
		if url != "" && (strings.HasPrefix(url, "postgres://") || strings.HasPrefix(url, "postgresql://")) {
			backend = BackendPostgres
		} else {
			backend = BackendSQLite
		}
	}
	if backend == "pg" || backend == "postgresql" {
		backend = BackendPostgres
	}
	return Config{Backend: backend, SQLitePath: defaultSQLite, URL: url}
}

// OpenMain opens the main business DB (sqlite or postgres).
// Postgres without URL → error (fail-closed).
func OpenMain(cfg Config) (*sql.DB, string, error) {
	backend := strings.ToLower(strings.TrimSpace(cfg.Backend))
	if backend == "" {
		backend = BackendSQLite
	}
	if backend == "pg" || backend == "postgresql" {
		backend = BackendPostgres
	}
	switch backend {
	case BackendSQLite, "sqlite3":
		path := strings.TrimSpace(cfg.SQLitePath)
		if path == "" {
			return nil, "", fmt.Errorf("store: sqlite path required")
		}
		// busy_timeout + WAL: concurrent pure-Go writers need readers/writers overlap.
		dsn := path + "?_pragma=busy_timeout(30000)&_pragma=journal_mode(WAL)&_pragma=synchronous(NORMAL)"
		db, err := sql.Open("sqlite", dsn)
		if err != nil {
			return nil, BackendSQLite, err
		}
		db.SetMaxOpenConns(1) // one writer connection; WAL still helps multi-process
		db.SetConnMaxLifetime(time.Minute)
		_, _ = db.Exec(`PRAGMA busy_timeout=30000`)
		_, _ = db.Exec(`PRAGMA journal_mode=WAL`)
		_, _ = db.Exec(`PRAGMA synchronous=NORMAL`)
		return db, BackendSQLite, nil
	case BackendPostgres:
		url := strings.TrimSpace(cfg.URL)
		if url == "" {
			return nil, BackendPostgres, fmt.Errorf("store: postgres requires DATABASE_URL / GPT_REGISTER_DATABASE_URL (fail-closed; no SQLite fallback)")
		}
		db, err := sql.Open("pgx", url)
		if err != nil {
			return nil, BackendPostgres, err
		}
		db.SetMaxOpenConns(20)
		db.SetMaxIdleConns(5)
		db.SetConnMaxLifetime(5 * time.Minute)
		if err := db.Ping(); err != nil {
			_ = db.Close()
			return nil, BackendPostgres, fmt.Errorf("store: postgres ping: %w", err)
		}
		return db, BackendPostgres, nil
	default:
		return nil, "", fmt.Errorf("store: unknown backend %q", cfg.Backend)
	}
}

// OpenPath opens the main DB for a CLI -db path, honoring env backend override.
//
// Rules:
//   - If env/env.db selects postgres → open PG (path ignored for DSN).
//   - Else open SQLite at path.
func OpenPath(sqlitePath string) (*sql.DB, string, error) {
	cfg := FromEnv(sqlitePath)
	return OpenMain(cfg)
}

// MustSQLite opens SQLite at path only (ignores env postgres). Prefer OpenPath.
// Intended for tests / ledger-adjacent tools — NOT main business writers.
func MustSQLite(dbPath string) (*sql.DB, error) {
	db, _, err := OpenMain(Config{Backend: BackendSQLite, SQLitePath: dbPath})
	return db, err
}

// MustOpen is OpenPath without backend name (call-site convenience).
func MustOpen(sqlitePath string) (*sql.DB, error) {
	db, _, err := OpenPath(sqlitePath)
	return db, err
}

// IsPostgres reports whether backend name is postgres.
func IsPostgres(backend string) bool {
	b := strings.ToLower(strings.TrimSpace(backend))
	return b == BackendPostgres || b == "pg" || b == "postgresql"
}

// Rebind converts ? placeholders to $1,$2… when dialect is postgres.
// No-op for sqlite. Only simple ? markers (not inside quotes).
func Rebind(backend, query string) string {
	if !IsPostgres(backend) {
		return query
	}
	var b strings.Builder
	b.Grow(len(query) + 8)
	n := 0
	inSingle := false
	for i := 0; i < len(query); i++ {
		c := query[i]
		if c == '\'' {
			// toggle on unescaped '; SQLite/PG strings double the quote
			if inSingle && i+1 < len(query) && query[i+1] == '\'' {
				b.WriteByte(c)
				b.WriteByte(query[i+1])
				i++
				continue
			}
			inSingle = !inSingle
			b.WriteByte(c)
			continue
		}
		if c == '?' && !inSingle {
			n++
			b.WriteByte('$')
			b.WriteString(fmt.Sprintf("%d", n))
			continue
		}
		b.WriteByte(c)
	}
	return b.String()
}
