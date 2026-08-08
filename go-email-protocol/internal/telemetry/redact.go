// Package telemetry provides shared redaction helpers for logs/events (G0 subset).
package telemetry

import "strings"

// Redacted is the standard marker for secret fields in events.
const Redacted = "[REDACTED]"

// ForbiddenLabelKeys must never appear as metric labels (plan section 13).
var ForbiddenLabelKeys = []string{
	"email", "otp", "token", "access_token", "password",
	"proxy_url", "capability", "job_id", "cookie",
}

// IsForbiddenLabel reports whether a metric label key is banned.
func IsForbiddenLabel(key string) bool {
	lk := strings.ToLower(key)
	for _, f := range ForbiddenLabelKeys {
		if lk == f {
			return true
		}
	}
	return false
}

// RedactValue always returns the redacted marker (G0 never logs secrets).
func RedactValue(_ string) string {
	return Redacted
}
