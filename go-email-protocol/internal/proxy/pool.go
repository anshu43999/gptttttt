// Package proxy leases SOCKS proxies from the project resource_pool.
package proxy

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/gpt-register/go-email-protocol/internal/store"
)

// Lease is a reserved proxy row from resource_pool.
type Lease struct {
	ID              int64
	ResourceKey     string
	URL             string // full socks5://user:pass@host:port
	RawURL          string // payload url as stored
	Region          string
	Protocol        string
	ExitIP          string
	ExpectedCountry string
}

// LeaseFromDB reserves one available proxy (default provider lajiao_credentials).
// Concurrent-safe via tx + CAS (SQLite) or FOR UPDATE SKIP LOCKED (Postgres).
func LeaseFromDB(dbPath, taskID string) (*Lease, error) {
	return LeaseFromDBProvider(dbPath, taskID, "lajiao_credentials")
}

// LeaseFromDBProvider leases from a specific proxy provider.
func LeaseFromDBProvider(dbPath, taskID, provider string) (*Lease, error) {
	if strings.TrimSpace(provider) == "" {
		provider = "lajiao_credentials"
	}
	db, backend, err := store.OpenPath(dbPath)
	if err != nil {
		return nil, err
	}
	defer db.Close()

	const maxAttempts = 40
	var last error
	for attempt := 0; attempt < maxAttempts; attempt++ {
		lease, err := tryLeaseOnce(db, backend, taskID, provider)
		if err == nil {
			return lease, nil
		}
		last = err
		time.Sleep(time.Duration(15+attempt*5) * time.Millisecond)
	}
	if last == nil {
		last = fmt.Errorf("proxy: lease exhausted")
	}
	return nil, last
}

func tryLeaseOnce(db *sql.DB, backend, taskID, provider string) (*Lease, error) {
	tx, err := db.Begin()
	if err != nil {
		return nil, err
	}
	committed := false
	defer func() {
		if !committed {
			_ = tx.Rollback()
		}
	}()

	var id int64
	var key, payload string
	if store.IsPostgres(backend) {
		err = tx.QueryRow(`
			SELECT id, resource_key, payload_json
			FROM resource_pool
			WHERE resource_type='proxy' AND provider=$1 AND status='available'
			ORDER BY random()
			FOR UPDATE SKIP LOCKED
			LIMIT 1
		`, provider).Scan(&id, &key, &payload)
	} else {
		err = tx.QueryRow(`
			SELECT id, resource_key, payload_json
			FROM resource_pool
			WHERE resource_type='proxy' AND provider=? AND status='available'
			ORDER BY RANDOM()
			LIMIT 1
		`, provider).Scan(&id, &key, &payload)
	}
	if err != nil {
		return nil, fmt.Errorf("proxy: no available %s proxy: %w", provider, err)
	}

	now := time.Now().UTC().Format(time.RFC3339Nano)
	upd := store.Rebind(backend, `
		UPDATE resource_pool
		SET status='reserved', lease_id=?, leased_at=?, updated_at=?
		WHERE id=? AND status='available'
	`)
	res, err := tx.Exec(upd, taskID, now, now, id)
	if err != nil {
		return nil, err
	}
	n, _ := res.RowsAffected()
	if n != 1 {
		return nil, fmt.Errorf("proxy: lease race on id=%d", id)
	}

	lease, err := parsePayload(id, key, payload)
	if err != nil {
		return nil, err
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	committed = true
	return lease, nil
}

// Release marks a leased proxy back to available (or cooldown/used).
func Release(dbPath string, id int64, status, errMsg string) error {
	if id <= 0 {
		return nil
	}
	if status == "" {
		status = "available"
	}
	db, backend, err := store.OpenPath(dbPath)
	if err != nil {
		return err
	}
	defer db.Close()
	q := store.Rebind(backend, `
		UPDATE resource_pool
		SET status=?, last_error=?, updated_at=?, lease_id=NULL
		WHERE id=?
	`)
	_, err = db.Exec(q, status, errMsg, time.Now().UTC().Format(time.RFC3339Nano), id)
	return err
}

// MarkSuccess releases proxy as available after a successful registration
// (sticky proxies are reusable; do not burn the pool).
func MarkSuccess(dbPath string, id int64) error {
	return Release(dbPath, id, "available", "")
}

// MarkCooldown puts proxy on cooldown after transport failures.
func MarkCooldown(dbPath string, id int64, errMsg string) error {
	return Release(dbPath, id, "cooldown", errMsg)
}

func parsePayload(id int64, key, payload string) (*Lease, error) {
	var m map[string]any
	if err := json.Unmarshal([]byte(payload), &m); err != nil {
		return nil, fmt.Errorf("proxy: bad payload id=%d: %w", id, err)
	}
	get := func(k string) string {
		v, _ := m[k].(string)
		return strings.TrimSpace(v)
	}
	raw := get("url")
	if raw == "" {
		raw = strings.TrimSpace(key)
	}
	if raw == "" {
		return nil, fmt.Errorf("proxy: empty url id=%d", id)
	}
	proto := strings.ToLower(get("protocol"))
	if proto == "" {
		proto = "socks5"
	}
	full := NormalizeURL(raw, proto)
	region := get("region")
	return &Lease{
		ID:              id,
		ResourceKey:     key,
		URL:             full,
		RawURL:          raw,
		Region:          region,
		Protocol:        proto,
		ExitIP:          get("exit_ip"),
		ExpectedCountry: region,
	}, nil
}

// NormalizeURL ensures scheme is present for socks dialers.
func NormalizeURL(raw, protocol string) string {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return raw
	}
	if strings.Contains(raw, "://") {
		return raw
	}
	scheme := strings.ToLower(strings.TrimSpace(protocol))
	if scheme == "" || scheme == "socks" {
		scheme = "socks5"
	}
	if scheme == "http" || scheme == "https" {
		// resource pool is SOCKS-oriented; force socks5 for DirectSOCKS path
		scheme = "socks5"
	}
	return scheme + "://" + raw
}
