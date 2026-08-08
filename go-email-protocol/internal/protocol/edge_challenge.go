package protocol

import (
	"bytes"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
)

// EdgeChallengeRequired is the stable failure code for a recognizable edge
// challenge. The copied lab observes and reports these responses; it never
// solves them or retries the request.
const EdgeChallengeRequired = "edge_challenge_required"

const maxChallengeInspectionBytes = 256 << 10

// EdgeChallengeError is safe diagnostic provenance for a detected response.
// Host and Path intentionally exclude query parameters and credentials.
type EdgeChallengeError struct {
	StatusCode int    `json:"status_code"`
	Host       string `json:"host,omitempty"`
	Path       string `json:"path,omitempty"`
	Signal     string `json:"signal"`
}

func (e *EdgeChallengeError) Error() string {
	if e == nil {
		return ""
	}
	where := e.Host + e.Path
	if where == "" {
		where = "response"
	}
	return fmt.Sprintf("%s: %s status=%d signal=%s", EdgeChallengeRequired, where, e.StatusCode, e.Signal)
}

// Code returns the stable typed failure code.
func (e *EdgeChallengeError) Code() string { return EdgeChallengeRequired }

// Retryable is always false. A challenge response may follow an uncertain POST;
// retry or challenge solving must never be inferred by this detector.
func (e *EdgeChallengeError) Retryable() bool { return false }

// DetectEdgeChallenge passively inspects a response and returns a typed
// EdgeChallengeError when a known Cloudflare/edge challenge signal is present.
// It performs no transport calls and restores every byte read from Body so the
// normal response consumer observes the original stream.
func DetectEdgeChallenge(resp *http.Response) error {
	if resp == nil {
		return nil
	}
	if detected := ClassifyEdgeChallenge(resp.StatusCode, resp.Header, nil); detected != nil {
		attachChallengeRequest(detected, resp.Request)
		return detected
	}
	if resp.Body == nil || !challengeBodyCandidate(resp) {
		return nil
	}

	original := resp.Body
	prefix, err := io.ReadAll(io.LimitReader(original, maxChallengeInspectionBytes+1))
	resp.Body = &joinedReadCloser{
		Reader: io.MultiReader(bytes.NewReader(prefix), original),
		Closer: original,
	}
	inspect := prefix
	if len(inspect) > maxChallengeInspectionBytes {
		inspect = inspect[:maxChallengeInspectionBytes]
	}
	if detected := ClassifyEdgeChallenge(resp.StatusCode, resp.Header, inspect); detected != nil {
		attachChallengeRequest(detected, resp.Request)
		return detected
	}
	if err != nil {
		return fmt.Errorf("protocol: inspect edge challenge response: %w", err)
	}
	return nil
}

// ClassifyEdgeChallenge is the allocation-light classifier for callers that
// already hold sanitized response bytes. Ordinary Cloudflare responses are not
// challenges: server/cf-ray headers alone never classify.
func ClassifyEdgeChallenge(statusCode int, header http.Header, body []byte) *EdgeChallengeError {
	if header == nil {
		header = http.Header{}
	}
	if strings.EqualFold(strings.TrimSpace(header.Get("CF-Mitigated")), "challenge") {
		return &EdgeChallengeError{StatusCode: statusCode, Signal: "cf-mitigated"}
	}
	if challengeLocation(header.Get("Location")) {
		return &EdgeChallengeError{StatusCode: statusCode, Signal: "challenge-location"}
	}
	if len(body) == 0 {
		return nil
	}

	if len(body) > maxChallengeInspectionBytes {
		body = body[:maxChallengeInspectionBytes]
	}

	lower := bytes.ToLower(body)
	// Hard interstitial markers: always classify (even on 200), because these
	// are challenge orchestration scripts, not ordinary app shells.
	if bytes.Contains(lower, []byte("window._cf_chl_opt")) {
		return &EdgeChallengeError{StatusCode: statusCode, Signal: "cf-challenge-options"}
	}
	if bytes.Contains(lower, []byte("cf-browser-verification")) {
		return &EdgeChallengeError{StatusCode: statusCode, Signal: "browser-verification"}
	}

	challengeStatus := statusCode == http.StatusForbidden || statusCode == http.StatusTooManyRequests || statusCode == http.StatusServiceUnavailable
	cloudflareHeaders := strings.EqualFold(strings.TrimSpace(header.Get("Server")), "cloudflare") || strings.TrimSpace(header.Get("CF-Ray")) != ""

	// "Just a moment" / "Attention Required" need challenge status or CF headers.
	if bytes.Contains(lower, []byte("<title>just a moment")) && (challengeStatus || cloudflareHeaders) {
		return &EdgeChallengeError{StatusCode: statusCode, Signal: "just-a-moment"}
	}
	if bytes.Contains(lower, []byte("attention required")) && bytes.Contains(lower, []byte("cloudflare")) && (challengeStatus || cloudflareHeaders) {
		return &EdgeChallengeError{StatusCode: statusCode, Signal: "attention-required"}
	}

	// /cdn-cgi/challenge-platform/ appears as a noise preload on real auth HTML
	// (passwordless email-verification). Only classify when the response looks
	// like an interstitial: challenge status, or CF page without auth app shell.
	if bytes.Contains(lower, []byte("/cdn-cgi/challenge-platform/")) {
		authShell := bytes.Contains(lower, []byte("email-verification")) ||
			bytes.Contains(lower, []byte("passwordless")) ||
			bytes.Contains(lower, []byte("auth-cdn.oaistatic.com")) ||
			bytes.Contains(lower, []byte("create-account"))
		if challengeStatus {
			return &EdgeChallengeError{StatusCode: statusCode, Signal: "challenge-platform"}
		}
		if cloudflareHeaders && !authShell {
			return &EdgeChallengeError{StatusCode: statusCode, Signal: "challenge-platform"}
		}
	}
	return nil
}

func challengeBodyCandidate(resp *http.Response) bool {
	if resp.StatusCode == http.StatusForbidden || resp.StatusCode == http.StatusTooManyRequests || resp.StatusCode == http.StatusServiceUnavailable {
		return true
	}
	contentType := strings.ToLower(resp.Header.Get("Content-Type"))
	if strings.Contains(contentType, "text/html") || strings.Contains(contentType, "javascript") {
		return true
	}
	return strings.EqualFold(strings.TrimSpace(resp.Header.Get("Server")), "cloudflare") || strings.TrimSpace(resp.Header.Get("CF-Ray")) != ""
}

func challengeLocation(raw string) bool {
	if strings.TrimSpace(raw) == "" {
		return false
	}
	parsed, err := url.Parse(raw)
	if err != nil {
		return false
	}
	path := strings.ToLower(parsed.Path)
	return strings.HasPrefix(path, "/cdn-cgi/challenge-platform/") || strings.HasPrefix(path, "/cdn-cgi/challenge/")
}

func attachChallengeRequest(detected *EdgeChallengeError, req *http.Request) {
	if detected == nil || req == nil || req.URL == nil {
		return
	}
	detected.Host = req.URL.Hostname()
	detected.Path = req.URL.EscapedPath()
	if detected.Path == "" {
		detected.Path = "/"
	}
}

type joinedReadCloser struct {
	io.Reader
	io.Closer
}
