package mailbox

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/gpt-register/go-email-protocol/internal/store"
	xproxy "golang.org/x/net/proxy"
	"golang.org/x/sync/singleflight"
)

const (
	ProviderICloudAPI    = "icloud_api"
	ProviderOutlookToken = "outlook_token"
)

var (
	// These MSA refresh tokens reject explicit Mail.Read on /common (AADSTS70000).
	// /consumers + Mail.Read matches Python OutlookTokenMailbox; .default is fallback.
	graphTokenURL    = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
	graphMessagesURL = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages"
	graphMailScope   = "https://graph.microsoft.com/.default offline_access"
	graphMailScopeAlt = "https://graph.microsoft.com/Mail.Read offline_access"
	// Well-known folders to scan for OpenAI OTP. Junk catches filtered verification mail.
	graphOTPFolders = []string{"inbox", "junkemail"}
)

const (
	defaultGraphMaxConcurrent = 64
	maxCachedGraphClients     = 128
)

type graphTokenCacheEntry struct {
	value     string
	expiresAt time.Time
}

type graphClientCacheEntry struct {
	client   *http.Client
	lastUsed time.Time
}

var (
	graphConfigMu      sync.RWMutex
	graphRequestSem    = make(chan struct{}, defaultGraphMaxConcurrent)
	graphTokenCacheMu  sync.RWMutex
	graphTokenCache    = make(map[string]graphTokenCacheEntry)
	graphTokenRefresh  singleflight.Group
	graphClientCacheMu sync.Mutex
	graphClientCache   = make(map[string]graphClientCacheEntry)
)

// SetGraphMaxConcurrent configures bounded concurrent Graph HTTP requests.
// It must be called during worker startup, before serving jobs.
func SetGraphMaxConcurrent(n int) {
	if n <= 0 {
		n = defaultGraphMaxConcurrent
	}
	graphConfigMu.Lock()
	graphRequestSem = make(chan struct{}, n)
	graphConfigMu.Unlock()
}

func graphRequest(ctx context.Context, client *http.Client, req *http.Request) (*http.Response, error) {
	graphConfigMu.RLock()
	sem := graphRequestSem
	graphConfigMu.RUnlock()
	select {
	case sem <- struct{}{}:
		defer func() { <-sem }()
	case <-ctx.Done():
		return nil, ctx.Err()
	}
	return client.Do(req)
}

func graphCacheKey(parts ...string) string {
	h := sha256.New()
	for _, part := range parts {
		_, _ = h.Write([]byte(part))
		_, _ = h.Write([]byte{0})
	}
	return fmt.Sprintf("%x", h.Sum(nil))
}

func graphCachedAccessToken(ctx context.Context, client *http.Client, clientID, refreshToken string) (string, error) {
	key := graphCacheKey(clientID, refreshToken)
	if token, ok := graphTokenFromCache(key); ok {
		return token, nil
	}
	result := graphTokenRefresh.DoChan(key, func() (any, error) {
		if token, ok := graphTokenFromCache(key); ok {
			return token, nil
		}
		// Keep singleflight window wide enough for up to 3 transport retries.
		refreshCtx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
		defer cancel()
		token, expiresAt, err := refreshGraphAccessTokenWithExpiry(refreshCtx, client, clientID, refreshToken)
		if err != nil {
			return "", err
		}
		graphTokenCacheMu.Lock()
		graphTokenCache[key] = graphTokenCacheEntry{value: token, expiresAt: expiresAt}
		graphTokenCacheMu.Unlock()
		return token, nil
	})
	select {
	case <-ctx.Done():
		return "", ctx.Err()
	case res := <-result:
		if res.Err != nil {
			return "", res.Err
		}
		token, _ := res.Val.(string)
		if token == "" {
			return "", fmt.Errorf("mailbox: graph token cache returned empty token")
		}
		return token, nil
	}
}

// graphCachedAccessTokenViaProxy tries sticky proxy first, then Python-proven
// socks5h → http → direct fallbacks on transport errors only.
func graphCachedAccessTokenViaProxy(ctx context.Context, proxyURL, clientID, refreshToken string) (string, *http.Client, error) {
	var last error
	for _, cand := range graphProxyCandidates(proxyURL) {
		client, err := graphHTTPClient(cand)
		if err != nil {
			last = err
			continue
		}
		token, err := graphCachedAccessToken(ctx, client, clientID, refreshToken)
		if err == nil {
			return token, client, nil
		}
		last = err
		if !isTransientGraphNetErr(err) {
			return "", nil, err
		}
		// Drop bad idle conns for this route before next candidate.
		client.CloseIdleConnections()
	}
	if last == nil {
		last = fmt.Errorf("mailbox: outlook token refresh failed")
	}
	return "", nil, last
}

func graphProxyCandidates(proxyURL string) []string {
	raw := strings.TrimSpace(proxyURL)
	if raw == "" {
		return []string{""}
	}
	if !strings.Contains(raw, "://") {
		raw = "socks5://" + raw
	}
	u, err := url.Parse(raw)
	if err != nil || u.Host == "" {
		return []string{raw, ""}
	}
	hostport := u.Host
	userinfo := ""
	if u.User != nil {
		if pass, ok := u.User.Password(); ok {
			userinfo = url.UserPassword(u.User.Username(), pass).String() + "@"
		} else {
			userinfo = url.User(u.User.Username()).String() + "@"
		}
	}
	out := make([]string, 0, 4)
	add := func(s string) {
		for _, existing := range out {
			if existing == s {
				return
			}
		}
		out = append(out, s)
	}
	// Prefer remote DNS for SOCKS (matches Python: socks5h then http then direct).
	add("socks5h://" + userinfo + hostport)
	add("socks5://" + userinfo + hostport)
	add("http://" + userinfo + hostport)
	add(raw)
	add("") // last resort: host direct
	return out
}

func isTransientGraphNetErr(err error) bool {
	if err == nil {
		return false
	}
	s := strings.ToLower(err.Error())
	// Permanent auth failures must not rotate proxy forever.
	if strings.Contains(s, "invalid_grant") ||
		strings.Contains(s, "aadsts") ||
		strings.Contains(s, "unauthorized_client") ||
		strings.Contains(s, "interaction_required") ||
		strings.Contains(s, "http 400") ||
		strings.Contains(s, "http 401") {
		return false
	}
	return strings.Contains(s, "eof") ||
		strings.Contains(s, "timeout") ||
		strings.Contains(s, "deadline exceeded") ||
		strings.Contains(s, "connection reset") ||
		strings.Contains(s, "connection refused") ||
		strings.Contains(s, "broken pipe") ||
		strings.Contains(s, "tls:") ||
		strings.Contains(s, "i/o timeout") ||
		strings.Contains(s, "use of closed network connection") ||
		strings.Contains(s, "socks") ||
		strings.Contains(s, "proxy")
}

func graphTokenFromCache(key string) (string, bool) {
	graphTokenCacheMu.RLock()
	entry, ok := graphTokenCache[key]
	graphTokenCacheMu.RUnlock()
	if !ok || entry.value == "" || time.Until(entry.expiresAt) <= 0 {
		return "", false
	}
	return entry.value, true
}

func graphInvalidateAccessToken(clientID, refreshToken string) {
	key := graphCacheKey(clientID, refreshToken)
	graphTokenCacheMu.Lock()
	delete(graphTokenCache, key)
	graphTokenCacheMu.Unlock()
}

var outlookOTPPoll = 5 * time.Second

func outlookOTPPollInterval() time.Duration {
	if outlookOTPPoll <= 0 {
		return 5 * time.Second
	}
	return outlookOTPPoll
}

// LeaseFromDB leases one available icloud_api email (legacy pure-go default).
func LeaseFromDB(dbPath, taskID string) (*Account, error) {
	return LeaseFromDBProvider(dbPath, taskID, ProviderICloudAPI)
}

// LeaseFromDBProvider leases one available email from resource_pool by provider.
// Concurrent-safe: CAS on SQLite; FOR UPDATE SKIP LOCKED on Postgres.
func LeaseFromDBProvider(dbPath, taskID, provider string) (*Account, error) {
	provider = normalizeMailboxProvider(provider)
	db, backend, err := store.OpenPath(dbPath)
	if err != nil {
		return nil, err
	}
	defer db.Close()

	const maxAttempts = 40
	var last error
	for attempt := 0; attempt < maxAttempts; attempt++ {
		acc, err := tryLeaseProviderOnce(db, backend, taskID, provider)
		if err == nil {
			return acc, nil
		}
		last = err
		time.Sleep(time.Duration(20+attempt*5) * time.Millisecond)
	}
	if last == nil {
		last = fmt.Errorf("mailbox: lease exhausted")
	}
	return nil, last
}

func normalizeMailboxProvider(provider string) string {
	p := strings.ToLower(strings.TrimSpace(provider))
	switch p {
	case "", "icloud", "icloud_api", "hme", "hide_my_email":
		return ProviderICloudAPI
	case "outlook", "outlook_token", "hotmail", "graph":
		return ProviderOutlookToken
	default:
		return p
	}
}

func tryLeaseProviderOnce(db *sql.DB, backend, taskID, provider string) (*Account, error) {
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

	// Revive expired cooldowns before selecting. Empty cooldown_until is treated as
	// expired after OTPEmptyEmailCooldown from updated_at so historical soft-burns return.
	if err := reclaimExpiredEmailCooldowns(tx, backend); err != nil {
		return nil, err
	}

	var id int64
	var key, payload string
	switch provider {
	case ProviderOutlookToken:
		if store.IsPostgres(backend) {
			err = tx.QueryRow(`
				SELECT id, resource_key, payload_json
				FROM resource_pool
				WHERE resource_type='email' AND provider='outlook_token' AND status='available'
				ORDER BY COALESCE(fail_count, 0) ASC, updated_at ASC, random()
				FOR UPDATE SKIP LOCKED
				LIMIT 1
			`).Scan(&id, &key, &payload)
		} else {
			err = tx.QueryRow(`
				SELECT id, resource_key, payload_json
				FROM resource_pool
				WHERE resource_type='email' AND provider='outlook_token' AND status='available'
				ORDER BY COALESCE(fail_count, 0) ASC, updated_at ASC, RANDOM()
				LIMIT 1
			`).Scan(&id, &key, &payload)
		}
	default: // icloud_api
		if store.IsPostgres(backend) {
			err = tx.QueryRow(`
				SELECT id, resource_key, payload_json
				FROM resource_pool
				WHERE resource_type='email' AND provider='icloud_api' AND status='available'
				ORDER BY
				  CASE WHEN position('"code_url":"http' in payload_json) > 0 THEN 0 ELSE 1 END,
				  COALESCE(fail_count, 0) ASC,
				  updated_at ASC,
				  random()
				FOR UPDATE SKIP LOCKED
				LIMIT 1
			`).Scan(&id, &key, &payload)
		} else {
			err = tx.QueryRow(`
				SELECT id, resource_key, payload_json
				FROM resource_pool
				WHERE resource_type='email' AND provider='icloud_api' AND status='available'
				ORDER BY
				  CASE WHEN instr(payload_json, '"code_url":"http') > 0 THEN 0 ELSE 1 END,
				  COALESCE(fail_count, 0) ASC,
				  updated_at ASC,
				  RANDOM()
				LIMIT 1
			`).Scan(&id, &key, &payload)
		}
	}
	if err != nil {
		return nil, fmt.Errorf("mailbox: no available %s email: %w", provider, err)
	}

	now := time.Now().UTC().Format(time.RFC3339Nano)
	upd := store.Rebind(backend, `
		UPDATE resource_pool
		SET status='reserved', lease_id=?, leased_at=?, updated_at=?, cooldown_until=''
		WHERE id=? AND status='available'
	`)
	res, err := tx.Exec(upd, taskID, now, now, id)
	if err != nil {
		return nil, err
	}
	n, _ := res.RowsAffected()
	if n != 1 {
		return nil, fmt.Errorf("mailbox: lease race on id=%d", id)
	}
	acc, err := parseProviderPayload(provider, key, payload)
	if err != nil {
		return nil, err
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	committed = true
	acc.ID = id
	return acc, nil
}

// reclaimExpiredEmailCooldowns moves cooled emails back to available when due.
// Prefer available pool first via ORDER BY fail_count; revived rows keep higher fail_count.
// Also recovers stale reserved leases (worker crash / abandoned batch).
func reclaimExpiredEmailCooldowns(tx *sql.Tx, backend string) error {
	now := time.Now().UTC()
	nowS := now.Format(time.RFC3339)
	// Empty until: treat updated_at older than OTPEmptyEmailCooldown as expired soft-burn.
	emptyCutoff := now.Add(-OTPEmptyEmailCooldown).UTC().Format(time.RFC3339)
	reservedCutoff := now.Add(-30 * time.Minute).UTC().Format(time.RFC3339)

	qCool := `
		UPDATE resource_pool
		SET status='available',
		    lease_id=NULL,
		    leased_at='',
		    cooldown_until='',
		    updated_at=?,
		    last_error=CASE
		      WHEN COALESCE(last_error, '') = '' THEN 'cooldown expired'
		      WHEN instr(lower(COALESCE(last_error, '')), 'cooldown expired') > 0 THEN last_error
		      ELSE last_error || ' | cooldown expired'
		    END
		WHERE resource_type='email'
		  AND status='cooldown'
		  AND (
		    (COALESCE(cooldown_until, '') != '' AND cooldown_until <= ?)
		    OR
		    ((COALESCE(cooldown_until, '') = '') AND COALESCE(updated_at, '') != '' AND updated_at <= ?)
		  )
	`
	if store.IsPostgres(backend) {
		qCool = store.Rebind(backend, `
			UPDATE resource_pool
			SET status='available',
			    lease_id=NULL,
			    leased_at='',
			    cooldown_until='',
			    updated_at=?,
			    last_error=CASE
			      WHEN COALESCE(last_error, '') = '' THEN 'cooldown expired'
			      WHEN position('cooldown expired' in lower(COALESCE(last_error, ''))) > 0 THEN last_error
			      ELSE last_error || ' | cooldown expired'
			    END
			WHERE resource_type='email'
			  AND status='cooldown'
			  AND (
			    (COALESCE(cooldown_until, '') != '' AND cooldown_until <= ?)
			    OR
			    ((COALESCE(cooldown_until, '') = '') AND COALESCE(updated_at, '') != '' AND updated_at <= ?)
			  )
		`)
	}
	if _, err := tx.Exec(qCool, nowS, nowS, emptyCutoff); err != nil {
		return err
	}

	qRes := store.Rebind(backend, `
		UPDATE resource_pool
		SET status='available',
		    lease_id=NULL,
		    leased_at='',
		    cooldown_until='',
		    updated_at=?,
		    last_error=CASE
		      WHEN COALESCE(last_error, '') = '' THEN 'stale reserved recovered'
		      ELSE last_error
		    END
		WHERE resource_type='email'
		  AND status='reserved'
		  AND COALESCE(updated_at, '') != ''
		  AND updated_at <= ?
	`)
	_, err := tx.Exec(qRes, nowS, reservedCutoff)
	return err
}

func parseProviderPayload(provider, key, payload string) (*Account, error) {
	switch provider {
	case ProviderOutlookToken:
		return parseOutlookPayload(key, payload)
	default:
		acc, err := parsePayload(key, payload)
		if err != nil {
			return nil, err
		}
		acc.Provider = ProviderICloudAPI
		return acc, nil
	}
}

func parseOutlookPayload(key, payload string) (*Account, error) {
	var m map[string]any
	if err := json.Unmarshal([]byte(payload), &m); err != nil {
		return nil, err
	}
	get := func(k string) string {
		v, _ := m[k].(string)
		return strings.TrimSpace(v)
	}
	email := get("email")
	if email == "" {
		email = key
	}
	clientID := get("client_id")
	refresh := get("refresh_token")
	if email == "" || clientID == "" || refresh == "" {
		return nil, fmt.Errorf("mailbox: outlook_token payload missing email/client_id/refresh_token for %s", key)
	}
	return &Account{
		Email:        email,
		ResourceKey:  key,
		Provider:     ProviderOutlookToken,
		ClientID:     clientID,
		RefreshToken: refresh,
		Password:     get("password"),
	}, nil
}

// WaitForOTP polls provider-specific OTP sources until a fresh 6-digit code appears.
func WaitForOTP(ctx context.Context, acc *Account, timeout time.Duration) (string, error) {
	if acc == nil {
		return "", fmt.Errorf("mailbox: nil account")
	}
	if normalizeMailboxProvider(acc.Provider) == ProviderOutlookToken ||
		(acc.ClientID != "" && acc.RefreshToken != "" && acc.CodeURL == "" && acc.MailURL == "" && acc.InboxURL == "") {
		return waitOutlookGraphOTP(ctx, acc, timeout)
	}
	return waitICloudAPIOTP(ctx, acc, timeout)
}

// WaitForOTPProxy is WaitForOTP with an optional SOCKS/HTTP proxy for Graph (software high-throughput path).
// proxyURL empty => direct dial (legacy CLI behavior).
func WaitForOTPProxy(ctx context.Context, acc *Account, timeout time.Duration, proxyURL string) (string, error) {
	if acc == nil {
		return "", fmt.Errorf("mailbox: nil account")
	}
	if normalizeMailboxProvider(acc.Provider) == ProviderOutlookToken ||
		(acc.ClientID != "" && acc.RefreshToken != "" && acc.CodeURL == "" && acc.MailURL == "" && acc.InboxURL == "") {
		return waitOutlookGraphOTPProxy(ctx, acc, timeout, proxyURL)
	}
	return waitICloudAPIOTP(ctx, acc, timeout)
}

func waitICloudAPIOTP(ctx context.Context, acc *Account, timeout time.Duration) (string, error) {
	if timeout <= 0 {
		timeout = 360 * time.Second
	}
	deadline := time.Now().Add(timeout)
	client := &http.Client{Timeout: 45 * time.Second}

	// Snapshot any code already present (stale). Never accept it again this wait.
	seen := map[string]struct{}{}
	if code, _, _ := tryFetchOTP(client, acc); code != "" {
		seen[code] = struct{}{}
	}

	var lastErr string
	var lastProbe string
	for time.Now().Before(deadline) {
		select {
		case <-ctx.Done():
			return "", ctx.Err()
		default:
		}
		code, probe, err := tryFetchOTP(client, acc)
		if probe != "" {
			lastProbe = probe
		}
		if err != nil {
			lastErr = err.Error()
		}
		if code != "" {
			if _, old := seen[code]; !old {
				return code, nil
			}
			if lastProbe == "" {
				lastProbe = "seen_baseline"
			}
		}
		poll := 2 * time.Second
		if acc.CodeURL == "" {
			poll = 2500 * time.Millisecond
		}
		select {
		case <-ctx.Done():
			return "", ctx.Err()
		case <-time.After(poll):
		}
	}
	switch {
	case lastErr != "" && lastProbe != "":
		return "", fmt.Errorf("mailbox: OTP timeout for %s after %s: last=%s err=%s", acc.Email, timeout, lastProbe, lastErr)
	case lastProbe != "":
		return "", fmt.Errorf("mailbox: OTP timeout for %s after %s: last=%s", acc.Email, timeout, lastProbe)
	case lastErr != "":
		return "", fmt.Errorf("mailbox: OTP timeout for %s after %s: %s", acc.Email, timeout, lastErr)
	default:
		return "", fmt.Errorf("mailbox: OTP timeout for %s after %s", acc.Email, timeout)
	}
}

func waitOutlookGraphOTP(ctx context.Context, acc *Account, timeout time.Duration) (string, error) {
	return waitOutlookGraphOTPProxy(ctx, acc, timeout, "")
}

func waitOutlookGraphOTPProxy(ctx context.Context, acc *Account, timeout time.Duration, proxyURL string) (string, error) {
	if strings.TrimSpace(acc.ClientID) == "" || strings.TrimSpace(acc.RefreshToken) == "" {
		return "", fmt.Errorf("mailbox: outlook graph missing client_id/refresh_token for %s", acc.Email)
	}
	if timeout <= 0 {
		timeout = 120 * time.Second
	}
	deadline := time.Now().Add(timeout)
	// Prefer codes arriving near this wait (avoid reusing very old OTP under concurrency).
	startedAt := time.Now().UTC().Add(-60 * time.Second)
	waitStarted := time.Now()

	accessToken, client, err := graphCachedAccessTokenViaProxy(ctx, proxyURL, acc.ClientID, acc.RefreshToken)
	if err != nil {
		return "", err
	}

	var lastErr string
	var lastProbe string
	sawOpenAI := false
	emptyStreak := 0
	for time.Now().Before(deadline) {
		select {
		case <-ctx.Done():
			return "", ctx.Err()
		default:
		}
		code, probe, err := tryFetchOutlookOTP(ctx, client, accessToken, startedAt)
		if probe != "" {
			lastProbe = probe
		}
		if err != nil {
			lastErr = err.Error()
			emptyStreak = 0
			if strings.Contains(strings.ToLower(err.Error()), "401") || strings.Contains(strings.ToLower(err.Error()), "invalidauthenticationtoken") {
				graphInvalidateAccessToken(acc.ClientID, acc.RefreshToken)
				if tok, c2, rerr := graphCachedAccessTokenViaProxy(ctx, proxyURL, acc.ClientID, acc.RefreshToken); rerr == nil {
					accessToken = tok
					if c2 != nil {
						client = c2
					}
					lastProbe = "graph_token_refreshed"
				}
			} else if isTransientGraphNetErr(err) {
				if tok, c2, rerr := graphCachedAccessTokenViaProxy(ctx, proxyURL, acc.ClientID, acc.RefreshToken); rerr == nil {
					accessToken = tok
					if c2 != nil {
						client = c2
					}
					lastProbe = "graph_route_recovered"
				}
			}
		} else {
			switch {
			case probe == "graph_found_code" || strings.HasPrefix(probe, "graph_found_code"):
				// handled below
			case probe == "graph_openai_no_code" || strings.HasPrefix(probe, "graph_openai_no_code"):
				// Mail arrived; code parse lag — keep waiting, do not empty-abort.
				sawOpenAI = true
				emptyStreak = 0
			case probe == "graph_no_openai_code" || probe == "graph_empty_inbox" ||
				strings.HasPrefix(probe, "graph_no_openai_code") || strings.HasPrefix(probe, "graph_empty"):
				emptyStreak++
			default:
				emptyStreak = 0
			}
		}
		if code != "" {
			return code, nil
		}
		// Dead-path fast fail: never saw OpenAI mail for ~65s of this wait window.
		// Successful codes almost always land within ~30–45s; sitting to 180s only burns seats.
		// 65s (was 50s) cuts false otp_timeout while still failing dead mailboxes quickly.
		if !sawOpenAI && emptyStreak >= 8 && time.Since(waitStarted) >= 65*time.Second {
			return "", fmt.Errorf("mailbox: outlook OTP timeout for %s after %s: last=%s (no openai mail, early abort)", acc.Email, time.Since(waitStarted).Round(time.Second), lastProbe)
		}
		// Fast poll while young; relax later.
		poll := 2 * time.Second
		if time.Since(waitStarted) > 45*time.Second {
			poll = outlookOTPPollInterval()
			if poll < 3*time.Second {
				poll = 3 * time.Second
			}
		}
		select {
		case <-ctx.Done():
			return "", ctx.Err()
		case <-time.After(poll):
		}
	}
	switch {
	case lastErr != "" && lastProbe != "":
		return "", fmt.Errorf("mailbox: outlook OTP timeout for %s after %s: last=%s err=%s", acc.Email, timeout, lastProbe, lastErr)
	case lastProbe != "":
		return "", fmt.Errorf("mailbox: outlook OTP timeout for %s after %s: last=%s", acc.Email, timeout, lastProbe)
	case lastErr != "":
		return "", fmt.Errorf("mailbox: outlook OTP timeout for %s after %s: %s", acc.Email, timeout, lastErr)
	default:
		return "", fmt.Errorf("mailbox: outlook OTP timeout for %s after %s", acc.Email, timeout)
	}
}

func graphHTTPClient(proxyURL string) (*http.Client, error) {
	proxyURL = strings.TrimSpace(proxyURL)
	key := graphCacheKey("proxy", proxyURL)
	now := time.Now()
	graphClientCacheMu.Lock()
	if entry, ok := graphClientCache[key]; ok {
		entry.lastUsed = now
		graphClientCache[key] = entry
		graphClientCacheMu.Unlock()
		return entry.client, nil
	}
	graphClientCacheMu.Unlock()

	client, err := newGraphHTTPClient(proxyURL)
	if err != nil {
		return nil, err
	}
	graphClientCacheMu.Lock()
	defer graphClientCacheMu.Unlock()
	if entry, ok := graphClientCache[key]; ok {
		entry.lastUsed = now
		graphClientCache[key] = entry
		client.CloseIdleConnections()
		return entry.client, nil
	}
	if len(graphClientCache) >= maxCachedGraphClients {
		var oldestKey string
		var oldest graphClientCacheEntry
		for cacheKey, entry := range graphClientCache {
			if oldestKey == "" || entry.lastUsed.Before(oldest.lastUsed) {
				oldestKey, oldest = cacheKey, entry
			}
		}
		if oldestKey != "" {
			oldest.client.CloseIdleConnections()
			delete(graphClientCache, oldestKey)
		}
	}
	graphClientCache[key] = graphClientCacheEntry{client: client, lastUsed: now}
	return client, nil
}

func newGraphHTTPClient(proxyURL string) (*http.Client, error) {
	baseDialer := &net.Dialer{Timeout: 15 * time.Second, KeepAlive: 30 * time.Second}
	transport := &http.Transport{
		Proxy:                 nil,
		ForceAttemptHTTP2:     true,
		MaxIdleConns:          32,
		MaxIdleConnsPerHost:   8,
		IdleConnTimeout:       90 * time.Second,
		TLSHandshakeTimeout:   15 * time.Second,
		ExpectContinueTimeout: 1 * time.Second,
		DialContext:           baseDialer.DialContext,
	}
	if proxyURL != "" {
		u, err := url.Parse(proxyURL)
		if err != nil {
			return nil, fmt.Errorf("mailbox: bad graph proxy url: %w", err)
		}
		scheme := strings.ToLower(u.Scheme)
		switch scheme {
		case "http", "https":
			transport.Proxy = http.ProxyURL(u)
		case "socks5", "socks5h":
			// SOCKS has remote DNS semantics in x/net; retain the task's sticky route.
			var auth *xproxy.Auth
			if u.User != nil {
				pass, _ := u.User.Password()
				auth = &xproxy.Auth{User: u.User.Username(), Password: pass}
			}
			dialer, err := xproxy.SOCKS5("tcp", u.Host, auth, baseDialer)
			if err != nil {
				return nil, fmt.Errorf("mailbox: socks dialer: %w", err)
			}
			if cd, ok := dialer.(xproxy.ContextDialer); ok {
				transport.DialContext = cd.DialContext
			} else {
				transport.DialContext = func(ctx context.Context, network, address string) (net.Conn, error) {
					return dialer.Dial(network, address)
				}
			}
		default:
			return nil, fmt.Errorf("mailbox: unsupported graph proxy scheme %q", scheme)
		}
	}
	return &http.Client{Timeout: 45 * time.Second, Transport: transport}, nil
}

func refreshGraphAccessToken(ctx context.Context, client *http.Client, clientID, refreshToken string) (string, error) {
	token, _, err := refreshGraphAccessTokenWithExpiry(ctx, client, clientID, refreshToken)
	return token, err
}

func refreshGraphAccessTokenWithExpiry(ctx context.Context, client *http.Client, clientID, refreshToken string) (string, time.Time, error) {
	scopes := []string{graphMailScope}
	if graphMailScopeAlt != "" && graphMailScopeAlt != graphMailScope {
		scopes = append(scopes, graphMailScopeAlt)
	}
	var last error
	for _, scope := range scopes {
		token, expiresAt, err := refreshGraphAccessTokenWithScopeExpiry(ctx, client, clientID, refreshToken, scope)
		if err == nil {
			return token, expiresAt, nil
		}
		last = err
		msg := strings.ToLower(err.Error())
		if !(strings.Contains(msg, "invalid_grant") || strings.Contains(msg, "aadsts70000") || strings.Contains(msg, "scope")) {
			return "", time.Time{}, err
		}
	}
	if last == nil {
		last = fmt.Errorf("mailbox: outlook token refresh failed")
	}
	return "", time.Time{}, last
}

func refreshGraphAccessTokenWithScope(ctx context.Context, client *http.Client, clientID, refreshToken, scope string) (string, error) {
	token, _, err := refreshGraphAccessTokenWithScopeExpiry(ctx, client, clientID, refreshToken, scope)
	return token, err
}

func refreshGraphAccessTokenWithScopeExpiry(ctx context.Context, client *http.Client, clientID, refreshToken, scope string) (string, time.Time, error) {
	form := url.Values{}
	form.Set("client_id", clientID)
	form.Set("grant_type", "refresh_token")
	form.Set("refresh_token", refreshToken)
	form.Set("scope", scope)

	var last error
	const maxAttempts = 3
	for attempt := 1; attempt <= maxAttempts; attempt++ {
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, graphTokenURL, strings.NewReader(form.Encode()))
		if err != nil {
			return "", time.Time{}, err
		}
		req.Header.Set("content-type", "application/x-www-form-urlencoded")
		req.Header.Set("user-agent", "go-email-protocol-mailbox/1.0")
		resp, err := graphRequest(ctx, client, req)
		if err != nil {
			last = fmt.Errorf("mailbox: outlook token refresh: %w", err)
			if !isTransientGraphNetErr(err) || attempt == maxAttempts {
				return "", time.Time{}, last
			}
			select {
			case <-ctx.Done():
				return "", time.Time{}, ctx.Err()
			case <-time.After(time.Duration(attempt) * 400 * time.Millisecond):
			}
			continue
		}
		body, readErr := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
		_ = resp.Body.Close()
		if readErr != nil {
			last = readErr
			if attempt == maxAttempts {
				return "", time.Time{}, last
			}
			continue
		}
		var payload map[string]any
		if err := json.Unmarshal(body, &payload); err != nil {
			return "", time.Time{}, fmt.Errorf("mailbox: outlook token refresh non-json: status=%d body=%s", resp.StatusCode, trim(string(body), 120))
		}
		token, _ := payload["access_token"].(string)
		token = strings.TrimSpace(token)
		if token == "" {
			errCode, _ := payload["error"].(string)
			errDesc, _ := payload["error_description"].(string)
			// Auth errors are permanent for this credential; do not retry transport-style.
			return "", time.Time{}, fmt.Errorf("mailbox: outlook token refresh failed: HTTP %d %s %s", resp.StatusCode, errCode, trim(errDesc, 160))
		}
		expiresIn := 3600 * time.Second
		if raw, ok := payload["expires_in"].(float64); ok && raw > 0 {
			expiresIn = time.Duration(raw * float64(time.Second))
		}
		// Refresh before expiry; never cache a token for less than one second.
		refreshEarly := 2 * time.Minute
		if expiresIn <= refreshEarly {
			refreshEarly = expiresIn / 2
		}
		expiresAt := time.Now().Add(expiresIn - refreshEarly)
		return token, expiresAt, nil
	}
	if last == nil {
		last = fmt.Errorf("mailbox: outlook token refresh failed")
	}
	return "", time.Time{}, last
}

func tryFetchOutlookOTP(ctx context.Context, client *http.Client, accessToken string, startedAt time.Time) (string, string, error) {
	var (
		bestCode  string
		bestProbe string
		sawOpenAI bool
		anyMail   bool
		lastErr   error
		lastProbe string
	)
	folders := graphOTPFolders
	if len(folders) == 0 {
		folders = []string{"inbox"}
	}
	for _, folder := range folders {
		code, probe, err := tryFetchOutlookOTPFolder(ctx, client, accessToken, startedAt, folder)
		if err != nil {
			lastErr = err
			lastProbe = probe
			low := strings.ToLower(err.Error() + " " + probe)
			if strings.Contains(low, "status=401") || strings.Contains(low, "graph_status_401") || strings.Contains(low, "graph_status_403") {
				return "", probe, err
			}
			continue
		}
		if lastProbe == "" {
			lastProbe = probe
		}
		switch {
		case code != "":
			// Prefer inbox over junk when both have codes (folders ordered inbox-first).
			if bestCode == "" {
				bestCode = code
				bestProbe = probe
			}
			if folder == "inbox" {
				return bestCode, bestProbe, nil
			}
		case strings.HasPrefix(probe, "graph_openai_no_code"):
			sawOpenAI = true
		case strings.HasPrefix(probe, "graph_no_openai_code"):
			anyMail = true
		case strings.HasPrefix(probe, "graph_empty"):
			// empty folder is fine
		}
	}
	if bestCode != "" {
		return bestCode, bestProbe, nil
	}
	if lastErr != nil && !sawOpenAI && !anyMail {
		return "", lastProbe, lastErr
	}
	if sawOpenAI {
		return "", "graph_openai_no_code", nil
	}
	if anyMail {
		return "", "graph_no_openai_code", nil
	}
	if lastProbe == "" {
		lastProbe = "graph_empty_inbox"
	}
	return "", lastProbe, lastErr
}

func tryFetchOutlookOTPFolder(ctx context.Context, client *http.Client, accessToken string, startedAt time.Time, folder string) (string, string, error) {
	folder = strings.TrimSpace(strings.ToLower(folder))
	if folder == "" {
		folder = "inbox"
	}
	base := graphMessagesURL
	// Prefer well-known folder path; fall back to configured inbox URL when folder is inbox.
	if folder == "inbox" && strings.Contains(base, "/mailFolders/") {
		// keep graphMessagesURL as-is for tests that override the full path
	} else {
		base = fmt.Sprintf("https://graph.microsoft.com/v1.0/me/mailFolders/%s/messages", folder)
	}
	// When tests override graphMessagesURL to a mock host, rewrite only the path host from that base.
	if folder != "inbox" && strings.Contains(graphMessagesURL, "://") {
		if u, err := url.Parse(graphMessagesURL); err == nil {
			// Replace path after host with junk folder path, keep mock host.
			u.Path = "/v1.0/me/mailFolders/" + folder + "/messages"
			u.RawQuery = ""
			base = u.String()
		}
	}
	u, err := url.Parse(base)
	if err != nil {
		return "", "graph_url_err", err
	}
	q := u.Query()
	q.Set("$top", "50")
	q.Set("$select", "from,subject,body,receivedDateTime")
	q.Set("$orderby", "receivedDateTime desc")
	u.RawQuery = q.Encode()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
	if err != nil {
		return "", "graph_req_err", err
	}
	req.Header.Set("authorization", "Bearer "+accessToken)
	req.Header.Set("prefer", `outlook.body-content-type="text"`)
	req.Header.Set("user-agent", "go-email-protocol-mailbox/1.0")
	resp, err := graphRequest(ctx, client, req)
	if err != nil {
		return "", "graph_http_err", err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 2<<20))
	if err != nil {
		return "", "graph_read_err", err
	}
	if resp.StatusCode >= 400 {
		return "", fmt.Sprintf("graph_status_%d", resp.StatusCode), fmt.Errorf("status=%d body=%s", resp.StatusCode, trim(string(body), 160))
	}
	var payload struct {
		Value []map[string]any `json:"value"`
	}
	if err := json.Unmarshal(body, &payload); err != nil {
		return "", "graph_json_err", err
	}
	probeSuffix := ""
	if folder != "inbox" {
		probeSuffix = "_" + folder
	}
	if len(payload.Value) == 0 {
		if folder == "inbox" {
			return "", "graph_empty_inbox", nil
		}
		return "", "graph_empty" + probeSuffix, nil
	}
	var bestCode string
	var bestAt time.Time
	var sawOpenAI bool
	for _, message := range payload.Value {
		receivedAt := parseGraphReceivedAt(message["receivedDateTime"])
		if !receivedAt.IsZero() && receivedAt.Before(startedAt) {
			continue
		}
		sender := graphSender(message)
		subject, _ := message["subject"].(string)
		bodyText := graphBodyText(message)
		searchable := strings.Join([]string{sender, subject, bodyText}, " ")
		if !openaiMailRe.MatchString(searchable) {
			continue
		}
		sawOpenAI = true
		code := matchOpenAIOTP(bodyText)
		if code == "" {
			code = matchOpenAIOTP(subject)
		}
		if code == "" {
			continue
		}
		if bestCode == "" || receivedAt.After(bestAt) {
			bestCode = code
			bestAt = receivedAt
		}
	}
	if bestCode != "" {
		return bestCode, "graph_found_code" + probeSuffix, nil
	}
	if sawOpenAI {
		return "", "graph_openai_no_code" + probeSuffix, nil
	}
	return "", "graph_no_openai_code" + probeSuffix, nil
}

func graphSender(message map[string]any) string {
	from, _ := message["from"].(map[string]any)
	if from == nil {
		return ""
	}
	addr, _ := from["emailAddress"].(map[string]any)
	if addr == nil {
		return ""
	}
	s, _ := addr["address"].(string)
	return s
}

func graphBodyText(message map[string]any) string {
	body, _ := message["body"].(map[string]any)
	if body == nil {
		return ""
	}
	content, _ := body["content"].(string)
	return content
}

func parseGraphReceivedAt(value any) time.Time {
	raw := strings.TrimSpace(fmt.Sprint(value))
	if raw == "" || raw == "<nil>" {
		return time.Time{}
	}
	if strings.HasSuffix(raw, "Z") {
		if t, err := time.Parse(time.RFC3339Nano, raw); err == nil {
			return t
		}
		if t, err := time.Parse(time.RFC3339, raw); err == nil {
			return t
		}
	}
	if t, err := time.Parse(time.RFC3339Nano, raw); err == nil {
		return t
	}
	if t, err := time.Parse(time.RFC3339, raw); err == nil {
		return t
	}
	return time.Time{}
}

var openaiMailRe = regexp.MustCompile(`(?i)openai|chatgpt`)
