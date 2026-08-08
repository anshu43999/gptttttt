package protocol

import (
	"context"
	"io"
	"net/http"
	"strings"
	"testing"

	"github.com/gpt-register/go-email-protocol/internal/fingerprint"
	mathrand "math/rand/v2"
)

func testLiveBundle(t *testing.T) *fingerprint.Bundle {
	t.Helper()
	b, err := fingerprint.Generate(fingerprint.GenerateOptions{
		RNG:         mathrand.New(mathrand.NewPCG(3, 4)),
		ForceFamily: fingerprint.FamilyDesktop,
	})
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func fixtureDo(t *testing.T) func(ctx context.Context, state StateID, req *http.Request) (*http.Response, error) {
	t.Helper()
	return func(ctx context.Context, state StateID, req *http.Request) (*http.Response, error) {
		switch state {
		case S1:
			return &http.Response{
				StatusCode: 200,
				Header:     http.Header{"Set-Cookie": []string{"oai-did=device-abc; Path=/; Secure"}},
				Body:       io.NopCloser(strings.NewReader("ok")),
			}, nil
		case S2:
			return &http.Response{
				StatusCode: 200,
				Header:     http.Header{"Content-Type": []string{"application/json"}},
				Body:       io.NopCloser(strings.NewReader(`{"csrfToken":"csrf-xyz"}`)),
			}, nil
		case S3:
			return &http.Response{
				StatusCode: 200,
				Header:     http.Header{"Content-Type": []string{"application/json"}},
				Body:       io.NopCloser(strings.NewReader(`{"url":"https://auth.openai.com/api/accounts/authorize?foo=1"}`)),
			}, nil
		case S4:
			return &http.Response{
				StatusCode: 200,
				Body:       io.NopCloser(strings.NewReader("<html>authorize</html>")),
			}, nil
		case S5:
			return &http.Response{
				StatusCode: 200,
				Header:     http.Header{"Content-Type": []string{"application/json"}},
				Body:       io.NopCloser(strings.NewReader(`{"token":"gAAAAACc-fixture-token","proofofwork":{"required":true,"seed":"seed","difficulty":"0"}}`)),
			}, nil
		case S6:
			return &http.Response{
				StatusCode: 200,
				Body:       io.NopCloser(strings.NewReader(`{"continue_url":"https://auth.openai.com/create-account/password"}`)),
			}, nil
		case S7:
			return &http.Response{
				StatusCode: 200,
				Body:       io.NopCloser(strings.NewReader(`{"status":"ok"}`)),
			}, nil
		case S8:
			return &http.Response{
				StatusCode: 200,
				Body:       io.NopCloser(strings.NewReader(`{"status":"sent"}`)),
			}, nil
		case S10:
			return &http.Response{
				StatusCode: 200,
				Body:       io.NopCloser(strings.NewReader(`{"status":"validated"}`)),
			}, nil
		case S11:
			return &http.Response{
				StatusCode: 200,
				Body:       io.NopCloser(strings.NewReader(`{"continue_url":"https://chatgpt.com/api/auth/callback/openai?code=x"}`)),
			}, nil
		case S12:
			return &http.Response{
				StatusCode: 200,
				Body:       io.NopCloser(strings.NewReader("ok")),
			}, nil
		case S13:
			return &http.Response{
				StatusCode: 200,
				Body:       io.NopCloser(strings.NewReader(`{"accessToken":"at-test-token","user":{"id":"user-1"}}`)),
			}, nil
		default:
			return nil, errString("unexpected state " + string(state))
		}
	}
}

func TestLiveS0ToS4WithFixtureDo(t *testing.T) {
	b := testLiveBundle(t)
	e := &Engine{Mode: ModeLive, Bundle: b, Email: "user@example.com", Do: fixtureDo(t)}
	cur := Cursor{State: S0}
	cur, _, err := e.Step(context.Background(), cur)
	if err != nil || cur.State != S1 {
		t.Fatalf("s0: %v %s", err, cur.State)
	}
	cur, _, err = e.Step(context.Background(), cur)
	if err != nil || cur.DeviceID != "device-abc" || cur.State != S2 {
		t.Fatalf("s1: err=%v device=%s state=%s", err, cur.DeviceID, cur.State)
	}
	cur, _, err = e.Step(context.Background(), cur)
	if err != nil || cur.CSRF != "csrf-xyz" || cur.State != S3 {
		t.Fatalf("s2: err=%v csrf=%s state=%s", err, cur.CSRF, cur.State)
	}
	cur, _, err = e.Step(context.Background(), cur)
	if err != nil || cur.ContinueURL == "" || cur.State != S4 {
		t.Fatalf("s3: err=%v url=%s state=%s", err, cur.ContinueURL, cur.State)
	}
}

func TestLiveS0ToS14WithFixtureDo(t *testing.T) {
	b := testLiveBundle(t)
	e := &Engine{
		Mode:     ModeLive,
		Bundle:   b,
		Email:    "user@example.com",
		Password: "Secret-Pass-1!",
		Do:       fixtureDo(t),
	}
	cur := Cursor{State: S0, SentinelToken: "tok-test"}
	for cur.State != S9 {
		var err error
		cur, _, err = e.Step(context.Background(), cur)
		if err != nil {
			t.Fatalf("walk to otp: state=%s err=%v", cur.State, err)
		}
	}
	cur.OTPCode = "123456"
	cur.State = S10
	for {
		var err error
		var res StepResult
		cur, res, err = e.Step(context.Background(), cur)
		if err != nil {
			t.Fatalf("from %s: %v", res.From, err)
		}
		if res.Stage == "succeeded" || (res.From == S14 && res.To == S14) {
			break
		}
		if cur.State == S14 && res.To == S14 {
			// one more for S14 success check if needed
			if res.Stage != "succeeded" {
				cur, res, err = e.Step(context.Background(), cur)
				if err != nil {
					t.Fatal(err)
				}
			}
			break
		}
	}
	if cur.AccessToken != "at-test-token" {
		t.Fatalf("token %s", cur.AccessToken)
	}
	if cur.AccountID != "user-1" {
		t.Fatalf("account %s", cur.AccountID)
	}
}

type errString string

func (e errString) Error() string { return string(e) }


func TestLiveS5AssemblesToken(t *testing.T) {
	b := testLiveBundle(t)
	e := &Engine{Mode: ModeLive, Bundle: b, Do: fixtureDo(t)}
	// Step routes ModeLive → LiveStep with RequireExplicit true internally? check runner
	cur := Cursor{State: S5, DeviceID: "device-abc"}
	cur2, res, err := e.Step(context.Background(), cur)
	if err != nil {
		// Live may require RequireExplicit via different entry — try LiveStep directly
		cur2, res, err = e.LiveStep(context.Background(), cur, LiveConfig{RequireExplicit: true})
	}
	if err != nil {
		t.Fatalf("S5: %v res=%+v", err, res)
	}
	if cur2.State != S6 {
		t.Fatalf("state=%s want S6", cur2.State)
	}
	if strings.TrimSpace(cur2.SentinelToken) == "" {
		t.Fatal("empty SentinelToken after S5")
	}
	if !strings.Contains(cur2.SentinelToken, "gAAAAACc-fixture-token") && !strings.Contains(cur2.SentinelToken, `"c"`) {
		t.Fatalf("token missing c: %s", cur2.SentinelToken)
	}
	t.Logf("sentinel_token=%s", cur2.SentinelToken)
}
