package protocol

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"
)

func TestDetectEdgeChallengeRecognizesKnownSignals(t *testing.T) {
	tests := []struct {
		name   string
		status int
		header http.Header
		body   string
		signal string
	}{
		{
			name: "explicit mitigation header", status: 403,
			header: http.Header{"Cf-Mitigated": []string{"challenge"}}, signal: "cf-mitigated",
		},
		{
			name: "challenge redirect", status: 302,
			header: http.Header{"Location": []string{"https://auth.openai.com/cdn-cgi/challenge-platform/h/g/orchestrate/jsch/v1?ray=redacted"}},
			signal: "challenge-location",
		},
		{
			name: "just a moment", status: 403,
			header: http.Header{"Content-Type": []string{"text/html"}},
			body:   "<!doctype html><title>Just a moment...</title>", signal: "just-a-moment",
		},
		{
			name: "browser verification", status: 503,
			header: http.Header{"Content-Type": []string{"text/html"}},
			body:   `<form id="cf-browser-verification"></form>`, signal: "browser-verification",
		},
		{
			name: "challenge script at success status", status: 200,
			header: http.Header{"Content-Type": []string{"text/html; charset=UTF-8"}},
			body:   `<script>window._cf_chl_opt={cvId:"3"}</script>`, signal: "cf-challenge-options",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			resp := responseForDetector(tc.status, tc.header, tc.body)
			err := DetectEdgeChallenge(resp)
			var challenge *EdgeChallengeError
			if !errors.As(err, &challenge) {
				t.Fatalf("error type=%T value=%v", err, err)
			}
			if challenge.Code() != EdgeChallengeRequired || challenge.Retryable() {
				t.Fatalf("challenge=%+v code=%q retryable=%v", challenge, challenge.Code(), challenge.Retryable())
			}
			if challenge.Signal != tc.signal || challenge.StatusCode != tc.status {
				t.Fatalf("challenge=%+v", challenge)
			}
			if challenge.Host != "auth.openai.com" || challenge.Path != "/api/accounts/create" {
				t.Fatalf("sanitized provenance=%+v", challenge)
			}
			if strings.Contains(challenge.Error(), "secret-query") || strings.Contains(challenge.Error(), "user:pass") {
				t.Fatalf("error leaked request credentials/query: %s", challenge.Error())
			}
		})
	}
}

func TestDetectEdgeChallengeDoesNotClassifyEdgeNoise(t *testing.T) {
	tests := []struct {
		name   string
		status int
		header http.Header
		body   string
	}{
		{
			name: "ordinary forbidden JSON", status: 403,
			header: http.Header{"Content-Type": []string{"application/json"}},
			body:   `{"error":"forbidden"}`,
		},
		{
			name: "ordinary Cloudflare response", status: 200,
			header: http.Header{"Content-Type": []string{"text/html"}, "Server": []string{"cloudflare"}, "CF-Ray": []string{"redacted"}},
			body:   `<html><title>Welcome</title><script data-cf-settings="redacted"></script></html>`,
		},
		{
			name: "unrelated just a moment text", status: 200,
			header: http.Header{"Content-Type": []string{"text/html"}},
			body:   `<html><title>Just a moment while the report loads</title></html>`,
		},
		{
			name: "auth email-verification shell with jsd preload", status: 200,
			header: http.Header{"Content-Type": []string{"text/html"}, "Server": []string{"cloudflare"}, "CF-Ray": []string{"redacted"}},
			body:   `<!DOCTYPE html><html><title>受信箱を確認してください - OpenAI</title><form action="/email-verification" method="post"></form><script src="/cdn-cgi/challenge-platform/h/g/orchestrate/jsch/v1"></script><link href="https://auth-cdn.oaistatic.com/assets/x.js"/></html>`,
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			resp := responseForDetector(tc.status, tc.header, tc.body)
			if err := DetectEdgeChallenge(resp); err != nil {
				t.Fatalf("false positive: %v", err)
			}
		})
	}
}

func TestDetectEdgeChallengeRestoresBodyAndNeverRetries(t *testing.T) {
	const original = `<!doctype html><script src="/cdn-cgi/challenge-platform/h/g/orchestrate/jsch/v1"></script>`
	body := &countingBody{Reader: strings.NewReader(original)}
	resp := responseForDetector(503, http.Header{"Content-Type": []string{"text/html"}}, "")
	resp.Body = body

	err := DetectEdgeChallenge(resp)
	var challenge *EdgeChallengeError
	if !errors.As(err, &challenge) {
		t.Fatalf("err=%v", err)
	}
	if challenge.Retryable() {
		t.Fatal("challenge detector must not request a retry")
	}
	got, readErr := io.ReadAll(resp.Body)
	if readErr != nil {
		t.Fatal(readErr)
	}
	if string(got) != original {
		t.Fatalf("restored body=%q want %q", got, original)
	}
	if body.readCalls == 0 {
		t.Fatal("test did not exercise passive body inspection")
	}
	// The detector has no client/factory/callback and therefore cannot issue a
	// request. The only observable activity is reading the supplied response.
	if body.closeCalls != 0 {
		t.Fatalf("detector closed caller-owned response body %d time(s)", body.closeCalls)
	}
}

func TestDetectEdgeChallengeSkipsNormalJSONBody(t *testing.T) {
	body := &countingBody{Reader: strings.NewReader(`{"ok":true}`)}
	resp := responseForDetector(200, http.Header{"Content-Type": []string{"application/json"}}, "")
	resp.Body = body
	if err := DetectEdgeChallenge(resp); err != nil {
		t.Fatal(err)
	}
	if body.readCalls != 0 {
		t.Fatalf("normal JSON body was unnecessarily consumed %d time(s)", body.readCalls)
	}
}

func TestDetectEdgeChallengeClassifiesPartialReadErrorPrefix(t *testing.T) {
	original := []byte(`<!doctype html><script>window._cf_chl_opt={cvId:"3"}</script>`)
	body := &partialErrorBody{data: original}
	resp := responseForDetector(503, http.Header{"Content-Type": []string{"text/html"}}, "")
	resp.Body = body
	err := DetectEdgeChallenge(resp)
	var challenge *EdgeChallengeError
	if !errors.As(err, &challenge) {
		t.Fatalf("expected typed challenge, got %T %v", err, err)
	}
	if challenge.Code() != EdgeChallengeRequired || challenge.Retryable() {
		t.Fatalf("challenge=%+v", challenge)
	}
	restored, readErr := io.ReadAll(resp.Body)
	if readErr != nil {
		t.Fatal(readErr)
	}
	if !bytes.Equal(restored, original) {
		t.Fatalf("restored=%q want=%q", restored, original)
	}
}

func TestClassifyEdgeChallengeBoundsDirectInput(t *testing.T) {
	marker := []byte(`<script>window._cf_chl_opt={}</script>`)
	body := append(bytes.Repeat([]byte{'x'}, maxChallengeInspectionBytes), marker...)
	if challenge := ClassifyEdgeChallenge(503, http.Header{"Content-Type": []string{"text/html"}}, body); challenge != nil {
		t.Fatalf("classifier inspected bytes beyond bound: %+v", challenge)
	}
	body = append(marker, bytes.Repeat([]byte{'x'}, maxChallengeInspectionBytes)...)
	challenge := ClassifyEdgeChallenge(503, http.Header{"Content-Type": []string{"text/html"}}, body)
	if challenge == nil || challenge.Code() != EdgeChallengeRequired {
		t.Fatalf("bounded prefix was not classified: %+v", challenge)
	}
}

func TestDoHTTPAppliesPassiveEdgeChallengeDetection(t *testing.T) {
	t.Run("typed challenge closes response without retry", func(t *testing.T) {
		body := &countingBody{Reader: strings.NewReader(`<script>window._cf_chl_opt={cvId:"3"}</script>`)}
		calls := 0
		engine := &Engine{Do: func(_ context.Context, _ StateID, _ *http.Request) (*http.Response, error) {
			calls++
			return &http.Response{
				StatusCode: http.StatusForbidden,
				Header:     http.Header{"Content-Type": []string{"text/html"}},
				Body:       body,
			}, nil
		}}
		req, err := http.NewRequest(http.MethodPost, "https://auth.openai.com/api/accounts/create_account?state=secret-query", nil)
		if err != nil {
			t.Fatal(err)
		}
		resp, err := engine.doHTTP(context.Background(), S11, req)
		if resp != nil {
			t.Fatal("challenge response reached the state handler")
		}
		var challenge *EdgeChallengeError
		if !errors.As(err, &challenge) || challenge.Code() != EdgeChallengeRequired || challenge.Retryable() {
			t.Fatalf("challenge=%+v err=%v", challenge, err)
		}
		if calls != 1 || body.closeCalls != 1 {
			t.Fatalf("calls=%d body closes=%d", calls, body.closeCalls)
		}
		if challenge.Host != "auth.openai.com" || challenge.Path != "/api/accounts/create_account" || strings.Contains(challenge.Error(), "secret-query") {
			t.Fatalf("unsafe challenge provenance: %+v", challenge)
		}
	})

	t.Run("ordinary body is restored", func(t *testing.T) {
		const original = `<html><title>Welcome</title></html>`
		body := &countingBody{Reader: strings.NewReader(original)}
		engine := &Engine{Do: func(_ context.Context, _ StateID, _ *http.Request) (*http.Response, error) {
			return &http.Response{
				StatusCode: http.StatusOK,
				Header:     http.Header{"Content-Type": []string{"text/html"}},
				Body:       body,
			}, nil
		}}
		req, err := http.NewRequest(http.MethodGet, "https://chatgpt.com/", nil)
		if err != nil {
			t.Fatal(err)
		}
		resp, err := engine.doHTTP(context.Background(), S12, req)
		if err != nil {
			t.Fatal(err)
		}
		got, readErr := io.ReadAll(resp.Body)
		if readErr != nil {
			t.Fatal(readErr)
		}
		_ = resp.Body.Close()
		if string(got) != original || body.readCalls == 0 || body.closeCalls != 1 {
			t.Fatalf("body=%q reads=%d closes=%d", got, body.readCalls, body.closeCalls)
		}
	})
}

func responseForDetector(status int, header http.Header, body string) *http.Response {
	req, _ := http.NewRequest(http.MethodPost, "https://user:pass@auth.openai.com/api/accounts/create?state=secret-query", nil)
	return &http.Response{
		StatusCode: status,
		Header:     header,
		Body:       io.NopCloser(strings.NewReader(body)),
		Request:    req,
	}
}

type countingBody struct {
	*strings.Reader
	readCalls  int
	closeCalls int
}

func (b *countingBody) Read(p []byte) (int, error) {
	b.readCalls++
	return b.Reader.Read(p)
}

func (b *countingBody) Close() error {
	b.closeCalls++
	return nil
}

var errInjectedBodyRead = errors.New("injected body read failure")

type partialErrorBody struct {
	data []byte
	done bool
}

func (b *partialErrorBody) Read(p []byte) (int, error) {
	if b.done {
		return 0, io.EOF
	}
	b.done = true
	n := copy(p, b.data)
	return n, errInjectedBodyRead
}

func (b *partialErrorBody) Close() error { return nil }
