// Package mailbox leases email resources and polls OTP from mailbox providers.
package mailbox

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"

	"github.com/gpt-register/go-email-protocol/internal/store"
)

// Account is a leased mailbox resource row (icloud_api or outlook_token).
type Account struct {
	Email        string
	InboxURL     string
	CodeURL      string
	MailURL      string
	ResourceKey  string
	ID           int64
	Provider     string
	ClientID     string
	RefreshToken string
	Password     string
}


// Default soft-fail cooldowns. graph_no_openai is NOT a dead mailbox — mail may
// arrive late or the registration OTP was dropped; park the row and retry later.
const (
	DefaultEmailCooldown   = 1 * time.Hour
	OTPEmptyEmailCooldown  = 6 * time.Hour
	SessionEmailCooldown   = 30 * time.Minute
)

// MarkUsed marks the leased row used/failed/available.
// status=cooldown always writes cooldown_until so lease reclaim can revive the row.
// Without until, Go-only lease (status=available only) permanently shelves the email.
func MarkUsed(dbPath string, id int64, status, errMsg string) error {
	if id <= 0 {
		return nil
	}
	if status == "" {
		status = "used"
	}
	db, backend, err := store.OpenPath(dbPath)
	if err != nil {
		return err
	}
	defer db.Close()
	now := time.Now().UTC()
	nowS := now.Format(time.RFC3339Nano)
	cooldownUntil := ""
	if status == "cooldown" {
		cooldownUntil = now.Add(cooldownDurationFor(errMsg)).UTC().Format(time.RFC3339)
	}
	// Clear lease; bump fail_count on terminal soft/hard burns; clear until on release-to-available.
	q := store.Rebind(backend, `
		UPDATE resource_pool
		SET status=?,
		    last_error=?,
		    updated_at=?,
		    lease_id=NULL,
		    cooldown_until=?,
		    fail_count = CASE
		      WHEN ? IN ('cooldown', 'disabled', 'used') THEN COALESCE(fail_count, 0) + 1
		      ELSE COALESCE(fail_count, 0)
		    END
		WHERE id=?
	`)
	_, err = db.Exec(q, status, errMsg, nowS, cooldownUntil, status, id)
	return err
}

// cooldownDurationFor picks park length. OTP empty ≠ blacklist.
func cooldownDurationFor(errMsg string) time.Duration {
	s := strings.ToLower(errMsg)
	switch {
	case strings.Contains(s, "graph_no_openai_code"),
		strings.Contains(s, "graph_empty_inbox"),
		strings.Contains(s, "no openai mail"),
		strings.Contains(s, "otp timeout"),
		strings.Contains(s, "outlook otp timeout"),
		strings.Contains(s, "early abort"):
		return OTPEmptyEmailCooldown
	case strings.Contains(s, "session_invalid"),
		strings.Contains(s, "invalid_state"),
		strings.Contains(s, "no longer valid"):
		return SessionEmailCooldown
	default:
		return DefaultEmailCooldown
	}
}

func parsePayload(key, payload string) (*Account, error) {
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
	acc := &Account{
		Email:       email,
		InboxURL:    get("inbox_url"),
		CodeURL:     get("code_url"),
		MailURL:     get("mail_url"),
		ResourceKey: key,
	}
	if acc.CodeURL == "" && acc.MailURL == "" && acc.InboxURL == "" {
		return nil, fmt.Errorf("mailbox: empty API urls for %s", email)
	}
	return acc, nil
}

var otpRe = regexp.MustCompile(`(?i)(?:^|[^0-9])(\d{6})(?:[^0-9]|$)`)


// tryFetchOTP returns (code, probeDiag, err).
// probeDiag is a compact last-seen status for timeout errors (found/stale/empty/…).
func tryFetchOTP(client *http.Client, acc *Account) (string, string, error) {
	var lastErr error
	var lastProbe string
	// Prefer dedicated code_url (akkkk found/stale semantics).
	if acc.CodeURL != "" {
		body, err := httpGet(client, acc.CodeURL)
		if err != nil {
			lastErr = err
			lastProbe = "code_url_err"
		} else {
			c, probe := extractCodeWithProbe(body)
			lastProbe = "code:" + probe
			if c != "" {
				return c, lastProbe, nil
			}
		}
	}
	if acc.MailURL != "" {
		body, err := httpGet(client, acc.MailURL)
		if err != nil {
			lastErr = err
			if lastProbe == "" {
				lastProbe = "mail_url_err"
			}
		} else {
			c, probe := extractCodeWithProbe(body)
			if lastProbe == "" || strings.HasPrefix(lastProbe, "code:") {
				// keep code_url probe if it was more specific; else use mail
				if lastProbe == "" {
					lastProbe = "mail:" + probe
				} else if c != "" {
					lastProbe = "mail:" + probe
				}
			}
			if c != "" {
				return c, lastProbe, nil
			}
		}
	}
	if acc.InboxURL != "" {
		body, err := httpGet(client, acc.InboxURL)
		if err != nil {
			lastErr = err
			if lastProbe == "" {
				lastProbe = "inbox_url_err"
			}
		} else if c, probe := extractCodeWithProbe(body); c != "" {
			return c, "inbox:" + probe, nil
		} else if lastProbe == "" {
			lastProbe = "inbox:" + probe
		}
	}
	return "", lastProbe, lastErr
}

func extractCodeWithProbe(body string) (code string, probe string) {
	body = strings.TrimSpace(body)
	if body == "" {
		return "", "empty_body"
	}
	var root any
	if json.Unmarshal([]byte(body), &root) != nil {
		if c := matchOpenAIOTP(body); c != "" {
			return c, "text_code"
		}
		return "", "text_no_code"
	}
	data := payloadData(root)
	// code API style
	if c := explicitCodeField(data); c != "" {
		found, hasFound := boolField(data, "found")
		stale, hasStale := boolField(data, "stale_code")
		switch {
		case hasStale && stale:
			return "", "stale_code"
		case hasFound && !found:
			return "", "found_false"
		default:
			return c, "found_code"
		}
	}
	if c := stringField(data, "latest_verification_code"); c != "" {
		return c, "latest_code"
	}
	if c := extractOTPFromAPIJSON(root); c != "" {
		return c, "json_code"
	}
	// distinguish empty-shell vs waiting
	found, hasFound := boolField(data, "found")
	if hasFound && !found {
		return "", "waiting_found_false"
	}
	if msg := stringField(data, "message"); msg != "" {
		return "", "msg:" + trim(msg, 40)
	}
	return "", "no_code"
}

func httpGet(client *http.Client, rawURL string) (string, error) {
	u, err := url.Parse(rawURL)
	if err != nil {
		return "", err
	}
	q := u.Query()
	q.Set("_", fmt.Sprintf("%d", time.Now().Unix()))
	u.RawQuery = q.Encode()
	req, err := http.NewRequest(http.MethodGet, u.String(), nil)
	if err != nil {
		return "", err
	}
	req.Header.Set("user-agent", "go-email-protocol-mailbox/1.0")
	resp, err := client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	b, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return "", err
	}
	if resp.StatusCode >= 400 {
		return "", fmt.Errorf("status=%d body=%s", resp.StatusCode, trim(string(b), 120))
	}
	return string(b), nil
}

func extractCodeFromJSONOrText(body string) string {
	body = strings.TrimSpace(body)
	if body == "" {
		return ""
	}
	var root any
	if json.Unmarshal([]byte(body), &root) != nil {
		// plain text
		return matchOpenAIOTP(body)
	}
	return extractOTPFromAPIJSON(root)
}

func extractOTPFromAPIJSON(root any) string {
	data := payloadData(root)
	// 1) code API style: data.code / verification_code with found != false, stale_code != true
	if c := explicitCodeField(data); c != "" {
		found, hasFound := boolField(data, "found")
		stale, _ := boolField(data, "stale_code")
		if stale {
			return ""
		}
		if hasFound && !found {
			return ""
		}
		return c
	}
	// 2) mail API latest_verification_code
	if c := stringField(data, "latest_verification_code"); c != "" {
		return c
	}
	// 3) messages / archive_messages text
	for _, key := range []string{"messages", "archive_messages"} {
		arr, _ := data[key].([]any)
		// newest first
		for i := len(arr) - 1; i >= 0; i-- {
			mail, _ := arr[i].(map[string]any)
			if mail == nil {
				continue
			}
			raw := strings.Join([]string{
				stringField(mail, "subject"),
				stringField(mail, "text"),
				stringField(mail, "content"),
				stringField(mail, "html"),
				stringField(mail, "body"),
				stringField(mail, "body_text"),
				stringField(mail, "body_preview"),
				stringField(mail, "snippet"),
				stringField(mail, "msg"),
			}, " ")
			if c := matchOpenAIOTP(raw); c != "" {
				return c
			}
		}
	}
	// 4) poualiis-style {msg,status}
	if msg := stringField(data, "msg"); msg != "" {
		if st, ok := data["status"]; ok {
			if b, isBool := st.(bool); isBool && !b {
				return ""
			}
		}
		if c := matchOpenAIOTP(msg); c != "" {
			return c
		}
	}
	return ""
}

func payloadData(root any) map[string]any {
	m, _ := root.(map[string]any)
	if m == nil {
		return map[string]any{}
	}
	if d, ok := m["data"].(map[string]any); ok {
		return d
	}
	return m
}

func explicitCodeField(data map[string]any) string {
	for _, k := range []string{"code", "verification_code", "email_otp", "otp", "latest_verification_code"} {
		if c := stringField(data, k); c != "" {
			return c
		}
	}
	return ""
}

func stringField(m map[string]any, k string) string {
	if m == nil {
		return ""
	}
	v, ok := m[k]
	if !ok || v == nil {
		return ""
	}
	switch t := v.(type) {
	case string:
		return strings.TrimSpace(t)
	case float64:
		// never treat numeric ids as OTP
		return ""
	case json.Number:
		return ""
	default:
		return strings.TrimSpace(fmt.Sprint(t))
	}
}

func boolField(m map[string]any, k string) (val bool, ok bool) {
	if m == nil {
		return false, false
	}
	v, exists := m[k]
	if !exists || v == nil {
		return false, false
	}
	switch t := v.(type) {
	case bool:
		return t, true
	case string:
		s := strings.ToLower(strings.TrimSpace(t))
		if s == "true" || s == "1" {
			return true, true
		}
		if s == "false" || s == "0" {
			return false, true
		}
	case float64:
		return t != 0, true
	}
	return false, false
}

var (
	otpPhraseRe = regexp.MustCompile(`(?i)(?:verification code|temporary verification code|認証コード|検証コード|临时验证码|code is|enter this temporary verification code|enter the code)[^\d]{0,80}(\d{6})`)
)

func matchOpenAIOTP(text string) string {
	text = strings.TrimSpace(text)
	if text == "" {
		return ""
	}
	if m := otpPhraseRe.FindStringSubmatch(text); len(m) > 1 {
		return m[1]
	}
	// pure 6-digit only when the whole string is the code
	if len(text) == 6 {
		ok := true
		for _, r := range text {
			if r < '0' || r > '9' {
				ok = false
				break
			}
		}
		if ok {
			return text
		}
	}
	// last resort: OpenAI context nearby
	low := strings.ToLower(text)
	if strings.Contains(low, "openai") || strings.Contains(low, "chatgpt") || strings.Contains(low, "verification") {
		if m := otpRe.FindStringSubmatch(text); len(m) > 1 {
			return m[1]
		}
	}
	return ""
}

func trim(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}
