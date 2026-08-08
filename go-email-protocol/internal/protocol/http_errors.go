package protocol

import (
	"fmt"
	"strings"
)

// classifyHTTPFailure maps auth HTTP status+body to failure_code and retryable.
// Used by LiveStep error returns so job runner can restart S0 or rotate proxy.
func classifyHTTPFailure(step string, status int, body []byte) (code string, retryable bool, msg string) {
	b := strings.ToLower(string(body))
	trim := trimBody(body, 240)
	msg = fmt.Sprintf("protocol: %s status %d body=%s", step, status, trim)

	// Cloudflare interstitial / challenge HTML
	if status == 403 || status == 503 || status == 429 {
		if strings.Contains(b, "just a moment") ||
			strings.Contains(b, "cf-browser-verification") ||
			strings.Contains(b, "cdn-cgi/challenge") ||
			strings.Contains(b, "attention required") ||
			strings.Contains(b, "cloudflare") && strings.Contains(b, "ray id") {
			return "cf_challenge", true, msg
		}
	}
	if status == 429 {
		return "http_429", true, msg
	}
	if status == 409 && (strings.Contains(b, "invalid_state") ||
		strings.Contains(b, "no longer valid") ||
		strings.Contains(b, "start over")) {
		return "session_invalid", true, msg
	}
	if status == 403 {
		return "http_403", true, msg
	}
	if status >= 500 {
		return "server_error", true, msg
	}
	if status == 400 {
		if strings.Contains(b, "user_already_exists") || strings.Contains(b, "already exists") {
			return "email_already_used", false, msg
		}
		if strings.Contains(b, "account_creation_failed") {
			return "account_creation_failed", true, msg
		}
		if strings.Contains(b, "wrong code") || strings.Contains(b, "wrong_code") {
			return "otp_wrong_code", true, msg
		}
	}
	if status == 401 {
		if strings.Contains(b, "wrong") {
			return "otp_wrong_code", true, msg
		}
	}
	return "protocol_step_failed", true, msg
}

// isCloudflareBody is a lightweight detector for CF HTML without status.
func isCloudflareBody(body []byte) bool {
	b := strings.ToLower(string(body))
	return strings.Contains(b, "just a moment") ||
		strings.Contains(b, "cf-browser-verification") ||
		strings.Contains(b, "cdn-cgi/challenge")
}
