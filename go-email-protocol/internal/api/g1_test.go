package api_test

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	_ "modernc.org/sqlite"

	"github.com/gpt-register/go-email-protocol/internal/admission"
	"github.com/gpt-register/go-email-protocol/internal/api"
	"github.com/gpt-register/go-email-protocol/internal/cryptostore"
	"github.com/gpt-register/go-email-protocol/internal/job"
	"github.com/gpt-register/go-email-protocol/internal/ledger"
	"github.com/gpt-register/go-email-protocol/internal/transport"
)

func testEnv(t *testing.T, maxActive int, cfg job.RunnerConfig) (*api.Server, *job.Manager, *ledger.Ledger, *cryptostore.Store, func()) {
	t.Helper()
	// API tests do not exercise restart persistence; use an in-memory ledger to
	// avoid Windows WAL handle cleanup races. Crash recovery tests open their own
	// file-backed ledger below.
	led, err := ledger.Open(":memory:")
	if err != nil {
		t.Fatal(err)
	}
	store, _, err := cryptostore.NewRandomKey()
	if err != nil {
		t.Fatal(err)
	}
	if maxActive <= 0 {
		maxActive = admission.DefaultMaxActive
	}
	adm := admission.New(admission.Config{MaxActive: maxActive, MaxQueued: maxActive})
	mgr := job.NewManager(led, adm, store, transport.FakeFactory{}, cfg)
	srv := api.New(mgr, "test-g1", api.RuntimeInfo{Runner: "protocol", ProtocolMode: "synthetic", Transport: "fake", GraphMaxConcurrent: 64})
	cleanup := func() {
		mgr.Close()
		_ = led.Close()
	}
	return srv, mgr, led, store, cleanup
}

func createBody(i int, fp string, gen int64) map[string]any {
	if fp == "" {
		fp = fmt.Sprintf("sha256:fp-%d", i)
	}
	if gen == 0 {
		gen = 1
	}
	return map[string]any{
		"task_id":             fmt.Sprintf("task_%d", i),
		"attempt_id":          1,
		"idempotency_key":     fmt.Sprintf("idem_%d", i),
		"request_fingerprint": fp,
		"email":               fmt.Sprintf("user%d@example.com", i),
		"password":            fmt.Sprintf("SecretPass-%d!", i),
		"resource_grant": map[string]any{
			"email_key":   fmt.Sprintf("email_key_%d", i),
			"proxy_key":   fmt.Sprintf("proxy_key_%d", i),
			"lease_fence": 10 + i,
			"exit_ip":     fmt.Sprintf("1.2.3.%d", i%250),
			"bridge": map[string]any{
				"bridge_id":  fmt.Sprintf("br_%d", i),
				"url":        "http://127.0.0.1:18766",
				"capability": fmt.Sprintf("bridge-cap-%d-secret", i),
				"generation": gen,
				"protocol":   "http-connect",
			},
		},
		"profile": map[string]any{
			"id": fmt.Sprintf("profile_%d", i),
		},
		"skip_phone": true,
	}
}

func doJSON(t *testing.T, h http.Handler, method, path string, body any, headers map[string]string) (int, map[string]any) {
	t.Helper()
	var rdr io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			t.Fatal(err)
		}
		rdr = bytes.NewReader(b)
	}
	req := httptest.NewRequest(method, path, rdr)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, req)
	var out map[string]any
	_ = json.Unmarshal(rr.Body.Bytes(), &out)
	return rr.Code, out
}

func TestHealth(t *testing.T) {
	srv, _, _, _, cleanup := testEnv(t, 10, job.RunnerConfig{ToOTPDelay: time.Hour})
	defer cleanup()
	code, out := doJSON(t, srv.Handler(), "GET", "/health", nil, nil)
	if code != 200 || out["status"] != "ok" || out["phase"] != "g1" {
		t.Fatalf("health: code=%d out=%v", code, out)
	}
	if out["runner"] != "protocol" || out["transport"] != "fake" {
		t.Fatalf("health runtime fields: %v", out)
	}
}

func TestDiagnosticsIsLoopbackOnlyAndAggregate(t *testing.T) {
	srv, _, _, _, cleanup := testEnv(t, 2, job.RunnerConfig{ToOTPDelay: time.Hour, HoldInRunning: true})
	defer cleanup()
	h := srv.Handler()

	body := createBody(900, "", 1)
	code, created := doJSON(t, h, "POST", "/v2/email-register", body, nil)
	if code != http.StatusOK {
		t.Fatalf("create: code=%d body=%v", code, created)
	}

	req := httptest.NewRequest(http.MethodGet, "/diagnostics", nil)
	req.RemoteAddr = "127.0.0.1:12345"
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, req)
	if rr.Code != http.StatusOK {
		t.Fatalf("loopback diagnostics: code=%d body=%s", rr.Code, rr.Body.String())
	}
	var got map[string]any
	if err := json.Unmarshal(rr.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	want := map[string]any{
		"phase":                "g1",
		"version":              "test-g1",
		"runner":               "protocol",
		"protocol_mode":        "synthetic",
		"transport":            "fake",
		"max_active":           float64(2),
		"active_count":         float64(1),
		"queued_count":         float64(0),
		"graph_max_concurrent": float64(64),
	}
	if len(got) != len(want) {
		t.Fatalf("diagnostics exposed unexpected fields: %v", got)
	}
	for key, value := range want {
		if got[key] != value {
			t.Fatalf("diagnostics[%q]=%v want %v", key, got[key], value)
		}
	}
	for _, sensitive := range []string{
		body["email"].(string),
		body["password"].(string),
		created["job_id"].(string),
		created["job_capability"].(string),
		"proxy_key_900",
		"bridge-cap-900-secret",
	} {
		if strings.Contains(rr.Body.String(), sensitive) {
			t.Fatalf("diagnostics exposed sensitive job data %q", sensitive)
		}
	}

	req = httptest.NewRequest(http.MethodGet, "/diagnostics", nil)
	req.RemoteAddr = "198.51.100.9:54321"
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, req)
	if rr.Code != http.StatusForbidden {
		t.Fatalf("non-loopback diagnostics: code=%d body=%s", rr.Code, rr.Body.String())
	}
}

func TestAdmissionRejectDoesNotExposeProxyKey(t *testing.T) {
	srv, _, _, _, cleanup := testEnv(t, 10, job.RunnerConfig{ToOTPDelay: time.Hour, HoldInRunning: true})
	defer cleanup()
	h := srv.Handler()

	body := createBody(1, "", 1)
	grant := body["resource_grant"].(map[string]any)
	grant["proxy_key"] = "user:secret@proxy.local:10000"
	code, out := doJSON(t, h, http.MethodPost, "/v2/email-register", body, nil)
	if code != http.StatusOK {
		t.Fatalf("first create: code=%d out=%v", code, out)
	}

	body2 := createBody(2, "", 1)
	grant2 := body2["resource_grant"].(map[string]any)
	grant2["proxy_key"] = "user:secret@proxy.local:10000"
	code, out = doJSON(t, h, http.MethodPost, "/v2/email-register", body2, nil)
	if code != http.StatusTooManyRequests {
		t.Fatalf("second create: code=%d out=%v", code, out)
	}
	encoded, _ := json.Marshal(out)
	text := string(encoded)
	if strings.Contains(text, "user:secret") || strings.Contains(text, "secret") {
		t.Fatalf("admission rejection exposed proxy password: %s", text)
	}
	if !strings.Contains(text, "user:***@proxy.local:10000") {
		t.Fatalf("admission rejection did not keep useful proxy bucket: %s", text)
	}
	if !strings.Contains(text, "admission rejected: proxy") {
		t.Fatalf("admission rejection did not keep reason: %s", text)
	}
}

func TestIdempotencyAndFingerprintConflict(t *testing.T) {
	srv, _, _, _, cleanup := testEnv(t, 10, job.RunnerConfig{ToOTPDelay: time.Hour, HoldInRunning: true})
	defer cleanup()
	h := srv.Handler()

	body := createBody(1, "sha256:same", 1)
	code, a := doJSON(t, h, "POST", "/v2/email-register", body, nil)
	if code != 200 {
		t.Fatalf("create: %d %v", code, a)
	}
	jobID := a["job_id"].(string)
	cap := a["job_capability"].(string)
	if jobID == "" || cap == "" {
		t.Fatalf("missing ids: %v", a)
	}

	code, b := doJSON(t, h, "POST", "/v2/email-register", body, nil)
	if code != 200 {
		t.Fatalf("replay: %d %v", code, b)
	}
	if b["job_id"] != jobID {
		t.Fatalf("idempotency: got %v want %s", b["job_id"], jobID)
	}

	body2 := createBody(1, "sha256:other", 1)
	code, c := doJSON(t, h, "POST", "/v2/email-register", body2, nil)
	if code != 409 {
		t.Fatalf("fingerprint conflict want 409 got %d %v", code, c)
	}

	body3 := createBody(1, "sha256:same", 99)
	code, d := doJSON(t, h, "POST", "/v2/email-register", body3, nil)
	if code != 409 {
		t.Fatalf("generation conflict want 409 got %d %v", code, d)
	}
}

func TestCapabilityAuth(t *testing.T) {
	srv, _, _, _, cleanup := testEnv(t, 10, job.RunnerConfig{ToOTPDelay: time.Hour, HoldInRunning: true})
	defer cleanup()
	h := srv.Handler()
	code, a := doJSON(t, h, "POST", "/v2/email-register", createBody(2, "", 1), nil)
	if code != 200 {
		t.Fatalf("create: %d %v", code, a)
	}
	jobID := a["job_id"].(string)
	cap := a["job_capability"].(string)

	code, _ = doJSON(t, h, "GET", "/v2/email-register/"+jobID, nil, nil)
	if code != 401 {
		t.Fatalf("missing cap want 401 got %d", code)
	}
	code, _ = doJSON(t, h, "GET", "/v2/email-register/"+jobID, nil, map[string]string{"X-Job-Capability": "wrong"})
	if code != 401 {
		t.Fatalf("wrong cap want 401 got %d", code)
	}
	code, got := doJSON(t, h, "GET", "/v2/email-register/"+jobID, nil, map[string]string{"X-Job-Capability": cap})
	if code != 200 || got["job_id"] != jobID {
		t.Fatalf("good cap: %d %v", code, got)
	}
	code, _ = doJSON(t, h, "GET", "/v2/email-register/"+jobID, nil, map[string]string{"Authorization": "Bearer " + cap})
	if code != 200 {
		t.Fatalf("bearer: %d", code)
	}
}

func TestCancel(t *testing.T) {
	srv, _, _, _, cleanup := testEnv(t, 10, job.RunnerConfig{ToOTPDelay: time.Hour, HoldInRunning: true})
	defer cleanup()
	h := srv.Handler()
	code, a := doJSON(t, h, "POST", "/v2/email-register", createBody(3, "", 1), nil)
	if code != 200 {
		t.Fatalf("create: %d %v", code, a)
	}
	jobID := a["job_id"].(string)
	cap := a["job_capability"].(string)
	hdr := map[string]string{"X-Job-Capability": cap}

	code, c := doJSON(t, h, "DELETE", "/v2/email-register/"+jobID, nil, hdr)
	if code != 200 {
		t.Fatalf("cancel: %d %v", code, c)
	}
	deadline := time.Now().Add(2 * time.Second)
	var status string
	for time.Now().Before(deadline) {
		code, got := doJSON(t, h, "GET", "/v2/email-register/"+jobID, nil, hdr)
		if code != 200 {
			t.Fatalf("get: %d %v", code, got)
		}
		status, _ = got["status"].(string)
		if status == "cancelled" {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if status != "cancelled" {
		t.Fatalf("want cancelled got %s", status)
	}
	code, _ = doJSON(t, h, "DELETE", "/v2/email-register/"+jobID, nil, hdr)
	if code != 200 {
		t.Fatalf("delete again: %d", code)
	}
}

func TestOTPVersionConflictAndSuccess(t *testing.T) {
	srv, _, _, _, cleanup := testEnv(t, 10, job.RunnerConfig{
		ToOTPDelay:     15 * time.Millisecond,
		ToSuccessDelay: 10 * time.Millisecond,
	})
	defer cleanup()
	h := srv.Handler()
	code, a := doJSON(t, h, "POST", "/v2/email-register", createBody(4, "", 1), nil)
	if code != 200 {
		t.Fatalf("create: %d %v", code, a)
	}
	jobID := a["job_id"].(string)
	cap := a["job_capability"].(string)
	hdr := map[string]string{"X-Job-Capability": cap}

	var challengeID string
	var version float64
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		code, got := doJSON(t, h, "GET", "/v2/email-register/"+jobID+"?wait_ms=50", nil, hdr)
		if code != 200 {
			t.Fatalf("get: %d %v", code, got)
		}
		if got["status"] == "waiting_for_otp" {
			ch, _ := got["challenge"].(map[string]any)
			if ch != nil {
				challengeID, _ = ch["challenge_id"].(string)
				version, _ = ch["state_version"].(float64)
			}
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if challengeID == "" {
		t.Fatal("no challenge")
	}

	code, bad := doJSON(t, h, "POST", "/v2/email-register/"+jobID+"/otp", map[string]any{
		"challenge_id":  challengeID,
		"state_version": int64(version) - 1,
		"code":          "111111",
	}, hdr)
	if code != 409 {
		t.Fatalf("stale version want 409 got %d %v", code, bad)
	}

	code, bad = doJSON(t, h, "POST", "/v2/email-register/"+jobID+"/otp", map[string]any{
		"challenge_id":  "oc_wrong",
		"state_version": int64(version),
		"code":          "111111",
	}, hdr)
	if code != 409 {
		t.Fatalf("wrong challenge want 409 got %d %v", code, bad)
	}

	code, ok := doJSON(t, h, "POST", "/v2/email-register/"+jobID+"/otp", map[string]any{
		"challenge_id":  challengeID,
		"state_version": int64(version),
		"code":          "654321",
	}, hdr)
	if code != 200 {
		t.Fatalf("otp: %d %v", code, ok)
	}

	status := ""
	deadline = time.Now().Add(2 * time.Second)
	var session map[string]any
	for time.Now().Before(deadline) {
		code, got := doJSON(t, h, "GET", "/v2/email-register/"+jobID+"?wait_ms=50", nil, hdr)
		if code != 200 {
			t.Fatalf("get: %d", code)
		}
		status, _ = got["status"].(string)
		if status == "succeeded" {
			session, _ = got["session"].(map[string]any)
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if status != "succeeded" {
		t.Fatalf("want succeeded got %s", status)
	}
	if session == nil || session["access_token"] == nil || session["access_token"] == "" {
		t.Fatalf("missing access_token in session: %v", session)
	}
}

func TestHundredIsolationAndBackpressure(t *testing.T) {
	const n = 100
	srv, mgr, _, _, cleanup := testEnv(t, n, job.RunnerConfig{ToOTPDelay: time.Hour, HoldInRunning: true})
	defer cleanup()
	h := srv.Handler()

	type created struct {
		id  string
		cap string
	}
	jobs := make([]created, n)
	var mu sync.Mutex
	var wg sync.WaitGroup
	errs := make(chan error, n)

	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			code, out := doJSON(t, h, "POST", "/v2/email-register", createBody(1000+i, "", 1), nil)
			if code != 200 {
				errs <- fmt.Errorf("create %d: %d %v", i, code, out)
				return
			}
			mu.Lock()
			jobs[i] = created{id: out["job_id"].(string), cap: out["job_capability"].(string)}
			mu.Unlock()
		}(i)
	}
	wg.Wait()
	close(errs)
	for e := range errs {
		if e != nil {
			t.Fatal(e)
		}
	}
	if mgr.Admission().ActiveCount() != n {
		t.Fatalf("active=%d want %d", mgr.Admission().ActiveCount(), n)
	}
	seenProf := make(map[string]int)
	for i := 0; i < n; i++ {
		rt := mgr.Runtime(jobs[i].id)
		if rt == nil {
			t.Fatalf("missing runtime %d", i)
		}
		jid, proxy, prof, jar := rt.IsolationProbe()
		if jid != jobs[i].id {
			t.Fatalf("job id mismatch")
		}
		wantProxy := fmt.Sprintf("proxy_key_%d", 1000+i)
		if proxy != wantProxy {
			t.Fatalf("proxy cross-talk i=%d got %s want %s", i, proxy, wantProxy)
		}
		// Phase B: legacy {id:profile_N} is server-upgraded to FingerprintBundle v2;
		// isolation means each job has a unique non-empty bundle_id, not the stub id.
		if prof == "" {
			t.Fatalf("empty profile marker i=%d", i)
		}
		if prev, ok := seenProf[prof]; ok {
			t.Fatalf("profile cross-talk i=%d shares marker with i=%d: %s", i, prev, prof)
		}
		seenProf[prof] = i
		if jar != jobs[i].id {
			t.Fatalf("jar cross-talk i=%d jar=%s job=%s", i, jar, jobs[i].id)
		}
	}

	code, _ := doJSON(t, h, "DELETE", "/v2/email-register/"+jobs[0].id, nil, map[string]string{"X-Job-Capability": jobs[0].cap})
	if code != 200 {
		t.Fatalf("cancel A: %d", code)
	}
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		code, got := doJSON(t, h, "GET", "/v2/email-register/"+jobs[0].id, nil, map[string]string{"X-Job-Capability": jobs[0].cap})
		if code == 200 && got["status"] == "cancelled" {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	code, b := doJSON(t, h, "GET", "/v2/email-register/"+jobs[1].id, nil, map[string]string{"X-Job-Capability": jobs[1].cap})
	if code != 200 {
		t.Fatalf("get B: %d", code)
	}
	if b["status"] == "cancelled" {
		t.Fatalf("cancel A affected B")
	}

	code, rep := doJSON(t, h, "POST", "/v2/email-register", createBody(9999, "", 1), nil)
	if code != 200 {
		t.Fatalf("replacement: %d %v", code, rep)
	}
	if mgr.Admission().ActiveCount() != n {
		t.Fatalf("after refill active=%d want %d", mgr.Admission().ActiveCount(), n)
	}
	code, back := doJSON(t, h, "POST", "/v2/email-register", createBody(10000, "", 1), nil)
	if code != http.StatusOK {
		t.Fatalf("101st active job should queue, got %d %v", code, back)
	}
	if back["status"] != "queued" || back["stage"] != "admission_queued" {
		t.Fatalf("101st active job status=%v stage=%v want queued/admission_queued", back["status"], back["stage"])
	}
	if mgr.Admission().ActiveCount() != n {
		t.Fatalf("active after queue=%d want %d", mgr.Admission().ActiveCount(), n)
	}
}

func TestCrashRecovery(t *testing.T) {
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "recover.db")
	key := []byte("0123456789abcdef0123456789abcdef")
	store, err := cryptostore.NewFromKey(key)
	if err != nil {
		t.Fatal(err)
	}

	led1, err := ledger.Open(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	adm1 := admission.New(admission.Config{MaxActive: 10})
	mgr1 := job.NewManager(led1, adm1, store, transport.FakeFactory{}, job.RunnerConfig{ToOTPDelay: time.Hour, HoldInRunning: true})
	srv1 := api.New(mgr1, "t", api.RuntimeInfo{Runner: "protocol", ProtocolMode: "synthetic", Transport: "fake"})
	code, a := doJSON(t, srv1.Handler(), "POST", "/v2/email-register", createBody(50, "sha256:rec", 1), nil)
	if code != 200 {
		t.Fatalf("create: %d %v", code, a)
	}
	jobID := a["job_id"].(string)
	cap := a["job_capability"].(string)

	code, a2 := doJSON(t, srv1.Handler(), "POST", "/v2/email-register", createBody(51, "sha256:term", 1), nil)
	if code != 200 {
		t.Fatalf("create2: %d", code)
	}
	job2 := a2["job_id"].(string)
	cap2 := a2["job_capability"].(string)
	_, _ = doJSON(t, srv1.Handler(), "DELETE", "/v2/email-register/"+job2, nil, map[string]string{"X-Job-Capability": cap2})
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		_, got := doJSON(t, srv1.Handler(), "GET", "/v2/email-register/"+job2, nil, map[string]string{"X-Job-Capability": cap2})
		if got["status"] == "cancelled" {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	mgr1.Close()
	if err := led1.Close(); err != nil {
		t.Fatalf("close led1: %v", err)
	}

	led2, err := ledger.Open(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	adm2 := admission.New(admission.Config{MaxActive: 10})
	mgr2 := job.NewManager(led2, adm2, store, transport.FakeFactory{}, job.RunnerConfig{ToOTPDelay: time.Hour, HoldInRunning: true})
	if err := mgr2.RecoverNonTerminal(context.Background()); err != nil {
		t.Fatal(err)
	}
	rec, err := led2.GetByID(context.Background(), jobID)
	if err != nil {
		t.Fatal(err)
	}
	if ledger.Terminal(rec.Status) {
		t.Fatalf("nonterminal became terminal: %s", rec.Status)
	}
	if mgr2.Runtime(jobID) == nil {
		t.Fatal("runtime not recovered")
	}
	srv2 := api.New(mgr2, "t", api.RuntimeInfo{Runner: "protocol", ProtocolMode: "synthetic", Transport: "fake"})
	code, got := doJSON(t, srv2.Handler(), "GET", "/v2/email-register/"+jobID, nil, map[string]string{"X-Job-Capability": cap})
	if code != 200 {
		t.Fatalf("get recovered: %d %v", code, got)
	}
	rec2, err := led2.GetByID(context.Background(), job2)
	if err != nil {
		t.Fatal(err)
	}
	if rec2.Status != ledger.StatusCancelled {
		t.Fatalf("terminal changed: %s", rec2.Status)
	}

	code, a3 := doJSON(t, srv2.Handler(), "POST", "/v2/email-register", createBody(52, "sha256:fence", 1), nil)
	if code != 200 {
		t.Fatalf("create3: %d %v", code, a3)
	}
	job3 := a3["job_id"].(string)
	mgr2.Close()
	if err := led2.Close(); err != nil {
		t.Fatalf("close led2: %v", err)
	}

	db, err := sql.Open("sqlite", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	_, err = db.Exec(`UPDATE jobs SET secret_blob = NULL WHERE job_id = ?`, job3)
	_ = db.Close()
	if err != nil {
		t.Fatal(err)
	}

	led3, err := ledger.Open(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	adm3 := admission.New(admission.Config{MaxActive: 10})
	mgr3 := job.NewManager(led3, adm3, store, transport.FakeFactory{}, job.RunnerConfig{HoldInRunning: true, ToOTPDelay: time.Hour})
	if err := mgr3.RecoverNonTerminal(context.Background()); err != nil {
		t.Fatal(err)
	}
	rec3, err := led3.GetByID(context.Background(), job3)
	if err != nil {
		t.Fatal(err)
	}
	if rec3.Status != ledger.StatusReconcileRequired {
		t.Fatalf("want reconcile_required got %s", rec3.Status)
	}
	mgr3.Close()
	if err := led3.Close(); err != nil {
		t.Fatalf("close led3: %v", err)
	}
}

func TestSecretsNotInPlainColumns(t *testing.T) {
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "sec.db")
	store, _, err := cryptostore.NewRandomKey()
	if err != nil {
		t.Fatal(err)
	}
	led, err := ledger.Open(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	adm := admission.New(admission.Config{MaxActive: 5})
	mgr := job.NewManager(led, adm, store, transport.FakeFactory{}, job.RunnerConfig{HoldInRunning: true, ToOTPDelay: time.Hour})
	srv := api.New(mgr, "t", api.RuntimeInfo{Runner: "protocol", ProtocolMode: "synthetic", Transport: "fake"})
	password := "SuperSecret-Password-XYZ!"
	bridgeCap := "bridge-capability-secret-abc"
	body := createBody(77, "", 1)
	body["password"] = password
	rg := body["resource_grant"].(map[string]any)
	br := rg["bridge"].(map[string]any)
	br["capability"] = bridgeCap
	code, a := doJSON(t, srv.Handler(), "POST", "/v2/email-register", body, nil)
	if code != 200 {
		t.Fatalf("create: %d %v", code, a)
	}
	cap := a["job_capability"].(string)
	mgr.Close()
	_ = led.Close()

	raw, err := os.ReadFile(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	s := string(raw)
	for _, secret := range []string{password, bridgeCap, cap} {
		if secret != "" && strings.Contains(s, secret) {
			t.Fatalf("secret found in sqlite file plain bytes")
		}
	}
}

func TestOTPIsolationAcrossJobs(t *testing.T) {
	srv, _, _, _, cleanup := testEnv(t, 10, job.RunnerConfig{
		ToOTPDelay:     15 * time.Millisecond,
		ToSuccessDelay: 15 * time.Millisecond,
	})
	defer cleanup()
	h := srv.Handler()

	mk := func(i int) (string, string) {
		code, a := doJSON(t, h, "POST", "/v2/email-register", createBody(200+i, "", 1), nil)
		if code != 200 {
			t.Fatalf("create: %d %v", code, a)
		}
		return a["job_id"].(string), a["job_capability"].(string)
	}
	idA, capA := mk(1)
	idB, capB := mk(2)
	hdrA := map[string]string{"X-Job-Capability": capA}
	hdrB := map[string]string{"X-Job-Capability": capB}

	waitOTP := func(id string, hdr map[string]string) (string, int64) {
		deadline := time.Now().Add(2 * time.Second)
		for time.Now().Before(deadline) {
			_, got := doJSON(t, h, "GET", "/v2/email-register/"+id+"?wait_ms=30", nil, hdr)
			if got["status"] == "waiting_for_otp" {
				ch := got["challenge"].(map[string]any)
				return ch["challenge_id"].(string), int64(ch["state_version"].(float64))
			}
			time.Sleep(5 * time.Millisecond)
		}
		t.Fatal("timeout otp")
		return "", 0
	}
	chA, verA := waitOTP(idA, hdrA)
	chB, verB := waitOTP(idB, hdrB)
	if chA == chB {
		t.Fatal("challenge ids collided")
	}
	code, _ := doJSON(t, h, "POST", "/v2/email-register/"+idA+"/otp", map[string]any{
		"challenge_id": chA, "state_version": verA, "code": "111111",
	}, hdrA)
	if code != 200 {
		t.Fatalf("otp A: %d", code)
	}
	time.Sleep(30 * time.Millisecond)
	_, gotB := doJSON(t, h, "GET", "/v2/email-register/"+idB, nil, hdrB)
	if gotB["status"] == "succeeded" {
		t.Fatal("OTP A advanced B")
	}
	code, _ = doJSON(t, h, "POST", "/v2/email-register/"+idB+"/otp", map[string]any{
		"challenge_id": chA, "state_version": verB, "code": "222222",
	}, hdrB)
	if code != 409 {
		t.Fatalf("cross challenge want 409 got %d", code)
	}
	code, _ = doJSON(t, h, "POST", "/v2/email-register/"+idB+"/otp", map[string]any{
		"challenge_id": chB, "state_version": verB, "code": "222222",
	}, hdrB)
	if code != 200 {
		t.Fatalf("otp B: %d", code)
	}
}
