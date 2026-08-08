// Package job implements JobRuntime, synthetic stage runner, and FSM transitions for G1.
package job

import (
	"encoding/json"
	"time"
)

// Status mirrors ledger statuses.
const (
	StatusQueued            = "queued"
	StatusRunning           = "running"
	StatusWaitingForOTP     = "waiting_for_otp"
	StatusCancelling        = "cancelling"
	StatusCancelled         = "cancelled"
	StatusFailed            = "failed"
	StatusSucceeded         = "succeeded"
	StatusReconcileRequired = "reconcile_required"
)

// CreateRequest is the V2 create body (parsed).
type CreateRequest struct {
	TaskID              string          `json:"task_id"`
	AttemptID           int             `json:"attempt_id"`
	IdempotencyKey      string          `json:"idempotency_key"`
	RequestFingerprint  string          `json:"request_fingerprint"`
	Email               string          `json:"email"`
	Password            string          `json:"password"`
	MailboxClientID     string          `json:"mailbox_client_id,omitempty"`
	MailboxRefreshToken string          `json:"mailbox_refresh_token,omitempty"`
	OTPTimeoutSeconds   int             `json:"otp_timeout_seconds,omitempty"`
	ResourceGrant       ResourceGrant   `json:"resource_grant"`
	Profile             json.RawMessage `json:"profile"`
	SkipPhone           bool            `json:"skip_phone"`
	DeadlineAt          time.Time       `json:"deadline_at"`
}

// ResourceGrant is the immutable resource snapshot from Python.
type ResourceGrant struct {
	EmailKey        string      `json:"email_key"`
	ProxyKey        string      `json:"proxy_key"`
	Bridge          BridgeGrant `json:"bridge"`
	LeaseFence      int64       `json:"lease_fence"`
	LeaseExpiresAt  time.Time   `json:"lease_expires_at"`
	ExitIP          string      `json:"exit_ip"`
	ExpectedCountry string      `json:"expected_country"`
}

// BridgeGrant is the local CONNECT bridge grant.
type BridgeGrant struct {
	BridgeID   string    `json:"bridge_id"`
	URL        string    `json:"url"`
	Capability string    `json:"capability"`
	Generation int64     `json:"generation"`
	Protocol   string    `json:"protocol"`
	ExpiresAt  time.Time `json:"expires_at"`
}

// OTPSubmit is the OTP POST body.
type OTPSubmit struct {
	ChallengeID  string `json:"challenge_id"`
	StateVersion int64  `json:"state_version"`
	Code         string `json:"code"`
}

// Challenge is returned when waiting_for_otp.
type Challenge struct {
	ChallengeID  string    `json:"challenge_id"`
	StateVersion int64     `json:"state_version"`
	IssuedAt     time.Time `json:"issued_at"`
	DeadlineAt   time.Time `json:"deadline_at"`
	RetryAfterMS int       `json:"retry_after_ms"`
}

// SessionDocument is the success payload (token only in API response, not logs).
type SessionDocument struct {
	SchemaVersion int             `json:"schema_version"`
	Email         string          `json:"email"`
	AccessToken   string          `json:"access_token"`
	AccountID     string          `json:"account_id"`
	PlanType      string          `json:"plan_type"`
	ObtainedAt    time.Time       `json:"obtained_at"`
	Profile       json.RawMessage `json:"profile,omitempty"`
	Cookies       []any           `json:"cookies,omitempty"`
	Origins       []any           `json:"origins,omitempty"`
}

// StatusView is the public job status response.
type StatusView struct {
	JobID         string `json:"job_id"`
	JobCapability string `json:"job_capability,omitempty"`
	Status        string `json:"status"`
	StateVersion  int64  `json:"state_version"`
	Stage         string `json:"stage"`
	RetryAfterMS  int    `json:"retry_after_ms"`
	FailureCode   string `json:"failure_code,omitempty"`
	// Message is a sanitized terminal failure detail; it never contains session secrets.
	Message                      string           `json:"message,omitempty"`
	Retryable                    bool             `json:"retryable"`
	RegistrationMayHaveSucceeded bool             `json:"registration_may_have_succeeded"`
	Challenge                    *Challenge       `json:"challenge,omitempty"`
	Session                      *SessionDocument `json:"session,omitempty"`
}

// RunnerConfig tunes synthetic / mailat / protocol-engine runners.
type RunnerConfig struct {
	// Delay before moving to waiting_for_otp (synthetic).
	ToOTPDelay time.Duration
	// Delay after OTP before succeeded (synthetic).
	ToSuccessDelay time.Duration
	// FailInject if non-empty fails with this code at start (synthetic).
	FailInject string
	// HoldInRunning if true stays running until cancel (synthetic).
	HoldInRunning bool
	// Mailat enables real protocol execution via mailat/codex_register.
	// Takes precedence over ProtocolMode when Enabled (production default path).
	Mailat MailatConfig
	// ProtocolMode selects Go-native runner when Mailat is disabled:
	//   ""|"synthetic" — G1 stage delays (default tests)
	//   "engine"       — protocol.Engine walks S0→S9 (synthetic/fixture sockets)
	//   "live"         — real OpenAI via transport Factory (S0–S14; OTP via SubmitOTP)
	ProtocolMode string
	// BusinessDBPath points to the resource/account/task database. env.db may
	// still select PostgreSQL; this path is the SQLite fallback only.
	BusinessDBPath string
	// SessionRemints is max full S0 restarts with optional proxy remint
	// (edge CF / EOF / timeout / session_invalid). 0 → default 2 (canary parity).
	SessionRemints int
}
