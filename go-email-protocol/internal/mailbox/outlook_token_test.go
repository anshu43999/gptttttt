package mailbox

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func TestNormalizeMailboxProvider(t *testing.T) {
	cases := map[string]string{
		"":              ProviderICloudAPI,
		"icloud_api":    ProviderICloudAPI,
		"HME":           ProviderICloudAPI,
		"outlook":       ProviderOutlookToken,
		"outlook_token": ProviderOutlookToken,
		"hotmail":       ProviderOutlookToken,
		"graph":         ProviderOutlookToken,
	}
	for in, want := range cases {
		if got := normalizeMailboxProvider(in); got != want {
			t.Fatalf("normalize(%q)=%q want %q", in, got, want)
		}
	}
}

func TestParseOutlookPayload(t *testing.T) {
	payload := `{"email":"a@outlook.com","password":"p","client_id":"cid","refresh_token":"rt"}`
	acc, err := parseOutlookPayload("a@outlook.com", payload)
	if err != nil {
		t.Fatal(err)
	}
	if acc.Email != "a@outlook.com" || acc.ClientID != "cid" || acc.RefreshToken != "rt" || acc.Provider != ProviderOutlookToken {
		t.Fatalf("unexpected account: %+v", acc)
	}
}

func TestParseOutlookPayloadMissing(t *testing.T) {
	if _, err := parseOutlookPayload("x", `{"email":"x@outlook.com"}`); err == nil {
		t.Fatal("expected missing field error")
	}
}

func TestWaitForOTPOutlookGraph(t *testing.T) {
	tokenHits := 0
	mailHits := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.Contains(r.URL.Path, "/oauth2/v2.0/token"):
			tokenHits++
			_ = r.ParseForm()
			if r.Form.Get("client_id") != "cid" || r.Form.Get("refresh_token") != "rt" {
				http.Error(w, "bad form", 400)
				return
			}
			// accept either primary or fallback scope used by production tokens
			scope := r.Form.Get("scope")
			if !strings.Contains(scope, "graph.microsoft.com") {
				http.Error(w, "bad scope", 400)
				return
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"access_token": "atok"})
		case strings.Contains(r.URL.Path, "/mailFolders/inbox/messages"):
			mailHits++
			if got := r.Header.Get("Authorization"); got != "Bearer atok" {
				http.Error(w, "bad auth", 401)
				return
			}
			if mailHits == 1 {
				_ = json.NewEncoder(w).Encode(map[string]any{
					"value": []map[string]any{
						{
							"subject":          "Welcome",
							"receivedDateTime": time.Now().UTC().Format(time.RFC3339),
							"from":             map[string]any{"emailAddress": map[string]any{"address": "noreply@example.com"}},
							"body":             map[string]any{"content": "hello"},
						},
					},
				})
				return
			}
			_ = json.NewEncoder(w).Encode(map[string]any{
				"value": []map[string]any{
					{
						"subject":          "Your ChatGPT code",
						"receivedDateTime": time.Now().UTC().Format(time.RFC3339),
						"from":             map[string]any{"emailAddress": map[string]any{"address": "noreply@openai.com"}},
						"body":             map[string]any{"content": "Your verification code is 246810"},
					},
				},
			})
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()

	oldTokenURL := graphTokenURL
	oldMessagesURL := graphMessagesURL
	oldPoll := outlookOTPPoll
	graphTokenURL = srv.URL + "/oauth2/v2.0/token"
	graphMessagesURL = srv.URL + "/v1.0/me/mailFolders/inbox/messages"
	outlookOTPPoll = 50 * time.Millisecond
	defer func() {
		graphTokenURL = oldTokenURL
		graphMessagesURL = oldMessagesURL
		outlookOTPPoll = oldPoll
	}()

	acc := &Account{
		Email:        "a@outlook.com",
		Provider:     ProviderOutlookToken,
		ClientID:     "cid",
		RefreshToken: "rt",
	}
	code, err := WaitForOTP(context.Background(), acc, 8*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if code != "246810" {
		t.Fatalf("code=%q", code)
	}
	if tokenHits < 1 || mailHits < 2 {
		t.Fatalf("tokenHits=%d mailHits=%d", tokenHits, mailHits)
	}
}

func TestWaitForOTPOutlookGraphJunkFolder(t *testing.T) {
	tokenHits := 0
	inboxHits := 0
	junkHits := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.Contains(r.URL.Path, "/oauth2/v2.0/token"):
			tokenHits++
			_ = json.NewEncoder(w).Encode(map[string]any{"access_token": "atok"})
		case strings.Contains(r.URL.Path, "/mailFolders/junkemail/messages"):
			junkHits++
			_ = json.NewEncoder(w).Encode(map[string]any{
				"value": []map[string]any{
					{
						"subject":          "Your ChatGPT code",
						"receivedDateTime": time.Now().UTC().Format(time.RFC3339),
						"from":             map[string]any{"emailAddress": map[string]any{"address": "noreply@openai.com"}},
						"body":             map[string]any{"content": "Your verification code is 135790"},
					},
				},
			})
		case strings.Contains(r.URL.Path, "/mailFolders/inbox/messages"):
			inboxHits++
			// Inbox has only noise; OTP lives in junk.
			_ = json.NewEncoder(w).Encode(map[string]any{
				"value": []map[string]any{
					{
						"subject":          "Promo",
						"receivedDateTime": time.Now().UTC().Format(time.RFC3339),
						"from":             map[string]any{"emailAddress": map[string]any{"address": "ads@example.com"}},
						"body":             map[string]any{"content": "hello"},
					},
				},
			})
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()

	oldTokenURL := graphTokenURL
	oldMessagesURL := graphMessagesURL
	oldPoll := outlookOTPPoll
	oldFolders := graphOTPFolders
	graphTokenURL = srv.URL + "/oauth2/v2.0/token"
	graphMessagesURL = srv.URL + "/v1.0/me/mailFolders/inbox/messages"
	outlookOTPPoll = 50 * time.Millisecond
	graphOTPFolders = []string{"inbox", "junkemail"}
	defer func() {
		graphTokenURL = oldTokenURL
		graphMessagesURL = oldMessagesURL
		outlookOTPPoll = oldPoll
		graphOTPFolders = oldFolders
	}()

	acc := &Account{
		Email:        "junk@outlook.com",
		Provider:     ProviderOutlookToken,
		ClientID:     "cid",
		RefreshToken: "rt",
	}
	code, err := WaitForOTP(context.Background(), acc, 5*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if code != "135790" {
		t.Fatalf("code=%q", code)
	}
	// token may be served from graph token cache; folder hits prove multi-folder scan.
	if inboxHits < 1 || junkHits < 1 {
		t.Fatalf("tokenHits=%d inboxHits=%d junkHits=%d", tokenHits, inboxHits, junkHits)
	}
}

func TestGraphTokenCacheCoalescesConcurrentRefresh(t *testing.T) {
	var tokenHits atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !strings.Contains(r.URL.Path, "/oauth2/v2.0/token") {
			http.NotFound(w, r)
			return
		}
		tokenHits.Add(1)
		time.Sleep(20 * time.Millisecond)
		_ = json.NewEncoder(w).Encode(map[string]any{
			"access_token": "cached-token",
			"expires_in":   3600,
		})
	}))
	defer srv.Close()

	oldTokenURL := graphTokenURL
	graphTokenURL = srv.URL + "/oauth2/v2.0/token"
	defer func() { graphTokenURL = oldTokenURL }()

	client, err := graphHTTPClient("")
	if err != nil {
		t.Fatal(err)
	}
	const workers = 8
	start := make(chan struct{})
	errs := make(chan error, workers)
	var wg sync.WaitGroup
	for range workers {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			token, err := graphCachedAccessToken(context.Background(), client, "cache-client-id", "cache-refresh-token")
			if err != nil {
				errs <- err
				return
			}
			if token != "cached-token" {
				errs <- fmt.Errorf("token=%q", token)
			}
		}()
	}
	close(start)
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Fatal(err)
	}
	if got := tokenHits.Load(); got != 1 {
		t.Fatalf("token refresh requests=%d want 1", got)
	}
}

func TestGraphProxyCandidatesPreferSocks5h(t *testing.T) {
	cands := graphProxyCandidates("socks5://user:pass@us.rrp.bestgo.work:10000")
	if len(cands) < 3 {
		t.Fatalf("candidates=%v", cands)
	}
	if !strings.HasPrefix(cands[0], "socks5h://") {
		t.Fatalf("first candidate should be socks5h, got %q", cands[0])
	}
	// Empty direct must be last.
	if cands[len(cands)-1] != "" {
		t.Fatalf("last candidate should be direct empty, got %q", cands[len(cands)-1])
	}
}

func TestIsTransientGraphNetErr(t *testing.T) {
	if !isTransientGraphNetErr(fmt.Errorf(`mailbox: outlook token refresh: Post "https://login.microsoftonline.com/consumers/oauth2/v2.0/token": EOF`)) {
		t.Fatal("EOF should be transient")
	}
	if isTransientGraphNetErr(fmt.Errorf("mailbox: outlook token refresh failed: HTTP 400 invalid_grant AADSTS70000")) {
		t.Fatal("invalid_grant must not be transient")
	}
}

func TestRefreshGraphAccessTokenRetriesEOF(t *testing.T) {
	var hits atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n := hits.Add(1)
		if n == 1 {
			hj, ok := w.(http.Hijacker)
			if !ok {
				http.Error(w, "no hijack", 500)
				return
			}
			conn, _, err := hj.Hijack()
			if err != nil {
				return
			}
			_ = conn.Close() // force EOF on first attempt
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"access_token": "after-retry",
			"expires_in":   3600,
		})
	}))
	defer srv.Close()

	old := graphTokenURL
	graphTokenURL = srv.URL + "/oauth2/v2.0/token"
	defer func() { graphTokenURL = old }()

	client, err := graphHTTPClient("")
	if err != nil {
		t.Fatal(err)
	}
	token, err := refreshGraphAccessToken(context.Background(), client, "cid", "rt")
	if err != nil {
		t.Fatal(err)
	}
	if token != "after-retry" {
		t.Fatalf("token=%q", token)
	}
	if hits.Load() < 2 {
		t.Fatalf("hits=%d want >=2", hits.Load())
	}
}
