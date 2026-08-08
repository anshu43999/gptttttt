// Package accounts writes pure-Go registration results into the main business DB
// using the same tables the Python dashboard reads (accounts + account_credentials).
package accounts

import (
	"database/sql"
	"fmt"
	"strings"
	"time"

	"github.com/gpt-register/go-email-protocol/internal/store"
)

// Record is a minimal registered-account payload.
type Record struct {
	Email       string
	Password    string
	AccessToken string
	AccountID   string // OpenAI chatgpt account id when known
	DeviceID    string
	UserAgent   string
	Browser     string
	TaskID      string
	ProxyExitIP string
	ProxyURL    string
	ProxyRegion string
	Engine      string
	PlanType    string
}

// ImportRegistered upserts into accounts + account_credentials.
// Safe to call multiple times for the same email (updates tokens/password).
// dbPath is used only when SQLite is explicitly selected. env.db / backend=postgres
// ignores it and opens Postgres; a selected PostgreSQL URL failure does not fall back.
func ImportRegistered(dbPath string, rec Record) (accountPK int64, err error) {
	email := strings.TrimSpace(rec.Email)
	if email == "" {
		return 0, fmt.Errorf("accounts: email required")
	}
	if strings.TrimSpace(rec.AccessToken) == "" {
		return 0, fmt.Errorf("accounts: access_token required")
	}
	engine := rec.Engine
	if engine == "" {
		engine = "pure-go"
	}
	_ = engine
	plan := rec.PlanType
	if plan == "" {
		plan = "free"
	}
	now := time.Now().UTC().Format(time.RFC3339Nano)
	db, backend, err := store.OpenPath(dbPath)
	if err != nil {
		return 0, err
	}
	defer db.Close()

	// Retry a few times on SQLITE_BUSY / transient PG serialization.
	const maxAttempts = 8
	var last error
	for attempt := 0; attempt < maxAttempts; attempt++ {
		accountPK, last = importOnce(db, backend, rec, email, plan, now)
		if last == nil {
			return accountPK, nil
		}
		msg := strings.ToLower(last.Error())
		if !strings.Contains(msg, "busy") &&
			!strings.Contains(msg, "locked") &&
			!strings.Contains(msg, "serialization") &&
			!strings.Contains(msg, "deadlock") {
			return 0, last
		}
		time.Sleep(time.Duration(30+attempt*20) * time.Millisecond)
	}
	return 0, last
}

func importOnce(db *sql.DB, backend string, rec Record, email, plan, now string) (accountPK int64, err error) {
	tx, err := db.Begin()
	if err != nil {
		return 0, err
	}
	defer func() {
		if err != nil {
			_ = tx.Rollback()
		}
	}()

	var existingID int64
	qSel := store.Rebind(backend, `SELECT id FROM accounts WHERE email=? OR account_key=? LIMIT 1`)
	qerr := tx.QueryRow(qSel, email, email).Scan(&existingID)
	if qerr != nil && qerr != sql.ErrNoRows {
		err = qerr
		return 0, err
	}

	accountID := strings.TrimSpace(rec.AccountID)
	status := "registered"
	stage := "registered"
	regMode := "email_protocol"
	regStatus := "registered"

	if existingID > 0 {
		qUpd := store.Rebind(backend, `
			UPDATE accounts SET
				account_id=CASE WHEN ?!='' THEN ? ELSE account_id END,
				password=CASE WHEN ?!='' THEN ? ELSE password END,
				plan_type=?,
				status=?,
				stage=?,
				registration_mode=?,
				registration_status=?,
				registration_task_id=CASE WHEN ?!='' THEN ? ELSE registration_task_id END,
				registration_completed_at=?,
				registration_error='',
				login_identifier=?,
				registration_proxy_exit_ip=CASE WHEN ?!='' THEN ? ELSE registration_proxy_exit_ip END,
				registration_proxy_region=CASE WHEN ?!='' THEN ? ELSE registration_proxy_region END,
				updated_at=?
			WHERE id=?
		`)
		_, err = tx.Exec(qUpd, accountID, accountID,
			rec.Password, rec.Password,
			plan, status, stage, regMode, regStatus,
			rec.TaskID, rec.TaskID,
			now, email,
			rec.ProxyExitIP, rec.ProxyExitIP,
			rec.ProxyRegion, rec.ProxyRegion,
			now, existingID)
		if err != nil {
			return 0, err
		}
		accountPK = existingID
	} else {
		qIns := store.Rebind(backend, `
			INSERT INTO accounts(
				account_key, account_id, platform, email, password, plan_type,
				status, stage, registration_mode, registration_status, registration_task_id,
				registration_completed_at, login_identifier,
				registration_proxy_exit_ip, registration_proxy_region,
				created_at, updated_at
			) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
		`)
		res, e := tx.Exec(qIns, email, accountID, "chatgpt", email, rec.Password, plan,
			status, stage, regMode, regStatus, rec.TaskID,
			now, email,
			rec.ProxyExitIP, rec.ProxyRegion,
			now, now)
		if e != nil {
			err = e
			return 0, err
		}
		if store.IsPostgres(backend) {
			// LastInsertId not reliable on pgx; re-select by email.
			if e := tx.QueryRow(store.Rebind(backend, `SELECT id FROM accounts WHERE email=? LIMIT 1`), email).Scan(&accountPK); e != nil {
				err = e
				return 0, err
			}
		} else {
			accountPK, _ = res.LastInsertId()
		}
	}

	var credID int64
	cerr := tx.QueryRow(store.Rebind(backend, `SELECT id FROM account_credentials WHERE account_id_ref=? LIMIT 1`), accountPK).Scan(&credID)
	if cerr != nil && cerr != sql.ErrNoRows {
		err = cerr
		return 0, err
	}
	if credID > 0 {
		_, err = tx.Exec(store.Rebind(backend, `
			UPDATE account_credentials SET
				access_token=?,
				chatgpt_access_token_initial=CASE WHEN chatgpt_access_token_initial IS NULL OR chatgpt_access_token_initial='' THEN ? ELSE chatgpt_access_token_initial END,
				updated_at=?
			WHERE id=?
		`), rec.AccessToken, rec.AccessToken, now, credID)
	} else {
		_, err = tx.Exec(store.Rebind(backend, `
			INSERT INTO account_credentials(account_id_ref, access_token, chatgpt_access_token_initial, created_at, updated_at)
			VALUES (?,?,?,?,?)
		`), accountPK, rec.AccessToken, rec.AccessToken, now, now)
	}
	if err != nil {
		return 0, err
	}

	var proxyRow int64
	_ = tx.QueryRow(store.Rebind(backend, `SELECT id FROM account_proxy WHERE account_id_ref=? LIMIT 1`), accountPK).Scan(&proxyRow)
	if proxyRow > 0 {
		_, _ = tx.Exec(store.Rebind(backend, `
			UPDATE account_proxy SET registration_proxy=?, registration_exit_ip=?, registration_country=?, updated_at=?
			WHERE id=?
		`), rec.ProxyURL, rec.ProxyExitIP, rec.ProxyRegion, now, proxyRow)
	} else if strings.TrimSpace(rec.ProxyURL) != "" || strings.TrimSpace(rec.ProxyExitIP) != "" {
		_, _ = tx.Exec(store.Rebind(backend, `
			INSERT INTO account_proxy(account_id_ref, registration_proxy, registration_exit_ip, registration_country, created_at, updated_at)
			VALUES (?,?,?,?,?,?)
		`), accountPK, rec.ProxyURL, rec.ProxyExitIP, rec.ProxyRegion, now, now)
	}

	if err = tx.Commit(); err != nil {
		return 0, err
	}
	return accountPK, nil
}
