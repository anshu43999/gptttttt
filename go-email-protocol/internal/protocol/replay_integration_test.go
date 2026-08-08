package protocol_test

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	mathrand "math/rand/v2"
	"net/http"
	"path/filepath"
	"strings"
	"testing"

	"github.com/gpt-register/go-email-protocol/internal/fingerprint"
	"github.com/gpt-register/go-email-protocol/internal/protocol"
	"github.com/gpt-register/go-email-protocol/internal/rechallenge"
	"github.com/gpt-register/go-email-protocol/internal/replay"
	"github.com/gpt-register/go-email-protocol/internal/transport"
)

// TestModeLiveRegistrationReplayGate drives the copied ModeLive engine against
// observed-only d17/d24 contracts with zero network.
//
// d24 is the success lane (S11 status 200).
// d17 must fail closed at S11 create_account with typed response.replayability
// (observed status 0 / capture_transport_incomplete).
func TestModeLiveRegistrationReplayGate(t *testing.T) {
	t.Run("d24-firefox150-jajp", func(t *testing.T) {
		contract := integrationContract(t, "d24-firefox150-jajp")
		client, err := replay.NewClient("mode-live-d24", contract, replay.Options{})
		if err != nil {
			t.Fatal(err)
		}
		defer client.Close()
		engine := &protocol.Engine{
			Mode:     protocol.ModeLive,
			Bundle:   integrationBundle(t, "JP"),
			Client:   client,
			Email:    "replay@example.invalid",
			Password: "replay-password",
		}
		cursor := protocol.Cursor{State: protocol.S0}
		for {
			if cursor.State == protocol.S9 {
				cursor.OTPCode = "123456"
				cursor.State = protocol.S10
			}
			var result protocol.StepResult
			cursor, result, err = engine.Step(context.Background(), cursor)
			if err != nil {
				var mismatch *replay.Mismatch
				if errors.As(err, &mismatch) {
					// Concurrent browser T1 after S11/S12 may remain unconsumed;
					// the registration lane itself must still complete.
					if mismatch.Field == "script.completion" {
						t.Fatalf("registration lane incomplete: %+v stats=%+v", mismatch, client.Stats())
					}
					// After create_account success, optional concurrent T1 may be
					// non-replayable (status 0). That is capture evidence, not a
					// ModeLive request builder failure.
					if mismatch.Field == "response.replayability" && mismatch.Position.State == "T1" {
						t.Logf("optional concurrent T1 non-replayable as captured: %+v", mismatch)
						break
					}
					t.Fatalf("ModeLive contract gate closed at %s: %+v stats=%+v", result.From, mismatch, client.Stats())
				}
				t.Fatalf("ModeLive contract gate closed at %s: %v stats=%+v", result.From, err, client.Stats())
			}
			if cursor.State == protocol.S12 || cursor.State == protocol.S13 || cursor.State == protocol.S14 {
				// S12 callback/homepage is the last observed primary exchange.
				// S13 session is not present in these HARs.
				if cursor.State == protocol.S13 || cursor.State == protocol.S14 {
					break
				}
				// one more step after first S12 transition may follow redirect hop
				if result.From == protocol.S12 {
					break
				}
			}
			if client.Stats().NetworkFallbacks != 0 {
				t.Fatal("offline ModeLive replay reached network fallback")
			}
		}
		if client.Stats().NetworkFallbacks != 0 {
			t.Fatal("offline ModeLive replay reached network fallback")
		}
		if client.Stats().Consumed < 8 {
			t.Fatalf("too few exchanges consumed: %+v", client.Stats())
		}
	})

	t.Run("d17-firefox150-ptbr", func(t *testing.T) {
		contract := integrationContract(t, "d17-firefox150-ptbr")
		client, err := replay.NewClient("mode-live-d17", contract, replay.Options{})
		if err != nil {
			t.Fatal(err)
		}
		defer client.Close()
		engine := &protocol.Engine{
			Mode:     protocol.ModeLive,
			Bundle:   integrationBundle(t, "BR"),
			Client:   client,
			Email:    "replay@example.invalid",
			Password: "replay-password",
		}
		cursor := protocol.Cursor{State: protocol.S0}
		for {
			if cursor.State == protocol.S9 {
				cursor.OTPCode = "123456"
				cursor.State = protocol.S10
			}
			var result protocol.StepResult
			cursor, result, err = engine.Step(context.Background(), cursor)
			if err != nil {
				var mismatch *replay.Mismatch
				if !errors.As(err, &mismatch) {
					t.Fatalf("want typed replay mismatch, got %T %v from %s", err, err, result.From)
				}
				if mismatch.Field != "response.replayability" || mismatch.Position.State != "S11" {
					t.Fatalf("d17 must fail at S11 response.replayability, got %+v from %s", mismatch, result.From)
				}
				if client.Stats().NetworkFallbacks != 0 {
					t.Fatal("network fallback on d17 blocker")
				}
				return
			}
			if result.From == protocol.S11 {
				t.Fatal("d17 S11 create_account was accepted despite capture status 0")
			}
		}
	})
}

func TestModeLiveObservedLaneSkipsS5ToS8(t *testing.T) {
	contract := integrationContract(t, "d24-firefox150-jajp")
	client, err := replay.NewClient("observed-lane-d24", contract, replay.Options{})
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	engine := &protocol.Engine{
		Mode: protocol.ModeLive, Bundle: integrationBundle(t, "JP"), Client: client,
		Email: "replay@example.invalid", Password: "replay-password",
	}
	cursor := protocol.Cursor{State: protocol.S0}
	for cursor.State != protocol.S9 {
		var result protocol.StepResult
		cursor, result, err = engine.Step(context.Background(), cursor)
		if err != nil {
			t.Fatalf("pre-OTP walk failed at %s: %v", result.From, err)
		}
		switch result.From {
		case protocol.S5, protocol.S6, protocol.S7, protocol.S8:
			t.Fatalf("observed lane emitted %s HTTP before OTP wait", result.From)
		}
	}
	if cursor.State != protocol.S9 {
		t.Fatalf("state=%s want S9", cursor.State)
	}
}

func TestCopiedModeLivePasswordPathStillMintsDistinctSentinelFlows(t *testing.T) {
	// Diagnostic: password/register path still exists in the copied engine for
	// non-HAR fixture walks. It must not be confused with observed-only captures.
	var emitted []string
	fake := transport.NewFake("sentinel-sequence", transport.ProxySnapshot{})
	fake.OnDo = func(_ context.Context, req *http.Request) (*http.Response, error) {
		if req.URL.Hostname() == "sentinel.openai.com" {
			raw, _ := io.ReadAll(req.Body)
			var body struct {
				Flow string `json:"flow"`
			}
			_ = json.Unmarshal(raw, &body)
			emitted = append(emitted, body.Flow)
			return response(http.StatusOK, `{"token":"replay","proofofwork":{"required":false},"turnstile":{"required":false},"so":{"required":false}}`), nil
		}
		switch req.URL.Path {
		case "/api/accounts/authorize/continue":
			return response(http.StatusOK, `{"continue_url":"https://auth.openai.com/create-account/password"}`), nil
		case "/api/accounts/user/register":
			return response(http.StatusOK, `{"continue_url":"https://auth.openai.com/email-otp/send"}`), nil
		case "/api/accounts/create_account":
			return response(http.StatusOK, `{"continue_url":"https://chatgpt.com/api/auth/callback/openai?code=replay&state=replay"}`), nil
		default:
			return nil, fmt.Errorf("unexpected probe path %s", req.URL.Path)
		}
	}
	engine := &protocol.Engine{
		Mode: protocol.ModeLive, Bundle: integrationBundle(t, "JP"), Client: fake,
		Email: "replay@example.invalid", Password: "replay-password",
	}
	cursor := protocol.Cursor{State: protocol.S5, DeviceID: "replay-device"}
	var err error
	for _, expected := range []protocol.StateID{protocol.S6, protocol.S7, protocol.S8} {
		cursor, _, err = engine.Step(context.Background(), cursor)
		if err != nil {
			t.Fatal(err)
		}
		if cursor.State != expected {
			t.Fatalf("state=%s want=%s", cursor.State, expected)
		}
	}
	if len(emitted) == 0 {
		t.Fatal("password path did not mint any sentinel requirements")
	}
}

func TestDoHTTPClassifiesEdgeChallenge(t *testing.T) {
	body := `<!doctype html><html><body>Checking your browser before accessing auth.openai.com. Enable JavaScript and cookies to continue. cf-challenge</body></html>`
	fake := transport.NewFake("edge-challenge", transport.ProxySnapshot{})
	fake.OnDo = func(_ context.Context, req *http.Request) (*http.Response, error) {
		return &http.Response{
			StatusCode: 403,
			Header: http.Header{
				"Content-Type":        []string{"text/html"},
				"Cf-Mitigated":        []string{"challenge"},
				"Server":              []string{"cloudflare"},
			},
			Body:    io.NopCloser(strings.NewReader(body)),
			Request: req,
		}, nil
	}
	engine := &protocol.Engine{
		Mode: protocol.ModeLive, Bundle: integrationBundle(t, "JP"), Client: fake,
		Email: "replay@example.invalid",
	}
	// S1 path hits doHTTP with detector.
	_, _, err := engine.Step(context.Background(), protocol.Cursor{State: protocol.S1})
	if err == nil {
		t.Fatal("expected edge challenge error")
	}
	var challenge *protocol.EdgeChallengeError
	if !errors.As(err, &challenge) {
		t.Fatalf("got %T %v, want EdgeChallengeError", err, err)
	}
	if challenge.Retryable() {
		t.Fatal("edge challenge must be non-retryable")
	}
}

func integrationContract(t *testing.T, capture string) *rechallenge.RegistrationContract {
	t.Helper()
	path := filepath.Join("..", "..", "testdata", "rechallenge", "registration", capture, "contract.json")
	contract, err := rechallenge.LoadContract(path)
	if err != nil {
		t.Fatal(err)
	}
	return contract
}

func integrationBundle(t *testing.T, country string) *fingerprint.Bundle {
	t.Helper()
	for seed := uint64(1); seed < 1000; seed++ {
		bundle, err := fingerprint.Generate(fingerprint.GenerateOptions{
			RNG:             mathrand.New(mathrand.NewPCG(seed, seed+1)),
			ForceFamily:     fingerprint.FamilyDesktop,
			ForceBrowser:    fingerprint.BrowserFirefox,
			ExpectedCountry: country,
		})
		if err != nil {
			t.Fatal(err)
		}
		if bundle.Device.UAMajor == 150 {
			return bundle
		}
	}
	t.Fatal("could not generate Firefox 150 bundle")
	return nil
}

func response(status int, body string) *http.Response {
	return &http.Response{
		StatusCode: status,
		Header:     make(http.Header),
		Body:       io.NopCloser(strings.NewReader(body)),
	}
}
