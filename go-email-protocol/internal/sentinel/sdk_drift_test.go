package sentinel

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestEnsurePinnedSDKReady(t *testing.T) {
	if err := EnsurePinnedSDKReady(); err != nil {
		t.Fatal(err)
	}
}

func TestExtractBuildIDs(t *testing.T) {
	html := `<script src="https://sentinel.openai.com/sentinel/20260219f9f6/sdk.js"></script>
	<meta content="20260301aabb">`
	ids := extractBuildIDs(html)
	if len(ids) < 1 {
		t.Fatal(ids)
	}
	joined := strings.Join(ids, ",")
	if !strings.Contains(joined, "20260219f9f6") {
		t.Fatal(ids)
	}
}

func TestCheckSDKDriftAgainstMock(t *testing.T) {
	// Serve the exact pinned bytes → Match
	src, pinHash, err := LoadPinnedSDK()
	if err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(src))
	}))
	defer srv.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	res := CheckSDKDrift(ctx, srv.URL)
	if !res.Match || res.LiveHash != pinHash {
		t.Fatalf("%+v", res)
	}

	// Serve different body → hash mismatch code
	srv2 := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("not-the-sdk"))
	}))
	defer srv2.Close()
	res2 := CheckSDKDrift(ctx, srv2.URL)
	if res2.Match || res2.Code != CodeSDKHashMismatch {
		t.Fatalf("%+v", res2)
	}
	if err := res2.DriftError(); err == nil {
		t.Fatal("expected error")
	} else if fe, ok := err.(*Error); !ok || fe.Code != CodeSDKHashMismatch {
		t.Fatalf("%v", err)
	}
}

func TestCheckBuildDriftMockFrame(t *testing.T) {
	// Override discovery by testing extract + Full path via httptest is hard
	// because FrameURLTemplate is fixed host. Unit-test extract + MapRuntimeFailure instead.
	htmlSame := `sentinel/20260219f9f6/sdk.js`
	ids := extractBuildIDs(htmlSame)
	if len(ids) != 1 || ids[0] != "20260219f9f6" {
		t.Fatal(ids)
	}
	htmlNew := `sentinel/20260301dead/sdk.js and sentinel/20260219f9f6/sdk.js`
	ids2 := extractBuildIDs(htmlNew)
	foreign := false
	for _, id := range ids2 {
		if id != PinnedSDKVersion {
			foreign = true
		}
	}
	if !foreign {
		t.Fatal(ids2)
	}
}

func TestMapRuntimeFailureCodes(t *testing.T) {
	cases := []struct {
		in   error
		code string
	}{
		{&Error{Code: "protocol_incompatible", Message: "sdk.js patch hook not found"}, CodeSDKHookMissing},
		{&Error{Code: "protocol_incompatible", Message: "turnstile vm failed ops=3"}, CodeTurnstileDXFailed},
		{&Error{Code: CodeSDKHashMismatch, Message: "x"}, CodeSDKHashMismatch},
		{fmtErr("hash mismatch live=a pin=b"), CodeSDKHashMismatch},
	}
	for _, c := range cases {
		out := MapRuntimeFailure(c.in)
		fe, ok := out.(*Error)
		if !ok || fe.Code != c.code {
			t.Fatalf("in=%v out=%v want %s", c.in, out, c.code)
		}
	}
}

func fmtErr(s string) error { return &plainErr{s} }

type plainErr struct{ s string }

func (e *plainErr) Error() string { return e.s }

func TestStartupDriftCheckOffline(t *testing.T) {
	res, err := StartupDriftCheck(context.Background(), StartupDriftOptions{SkipNetwork: true})
	if err != nil {
		t.Fatal(err)
	}
	if !res.Match || res.PinnedVersion != PinnedSDKVersion {
		t.Fatalf("%+v", res)
	}
}

func TestStartupDriftCheckLiveSoft(t *testing.T) {
	// May pass or soft-network depending on environment; must not panic.
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	res, err := StartupDriftCheck(ctx, StartupDriftOptions{Timeout: 12 * time.Second})
	// hard fail only if Code set
	if err != nil {
		if fe, ok := err.(*Error); ok {
			if fe.Code != CodeSDKHashMismatch && fe.Code != CodeSDKBuildMismatch &&
				fe.Code != CodeSDKHookMissing && fe.Code != CodeSDKDrift {
				t.Fatalf("unexpected code %v", err)
			}
			t.Logf("live drift hard: %+v", res)
			return
		}
		t.Fatal(err)
	}
	t.Logf("live drift ok/soft: match=%v kind=%s builds=%v hash=%s err=%s",
		res.Match, res.Kind, res.LiveBuilds, res.LiveHash, res.Error)
	storeDrift(res)
	last := LastDriftResult()
	if last.PinnedVersion != PinnedSDKVersion {
		t.Fatal(last)
	}
}
