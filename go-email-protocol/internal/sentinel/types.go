// Package sentinel holds Sentinel input observation schema stubs for G0 fixtures.
package sentinel

// Flow names used by protected Auth POSTs (plan section 9 / TS oracle).
const (
	FlowAuthorizeContinue      = "authorize_continue"
	FlowPasswordVerify         = "password_verify"
	FlowUsernamePasswordCreate = "username_password_create"
	FlowOAuthCreateAccount     = "oauth_create_account"
)

// RequirementsBodyKeys are T1 JSON body field names.
func RequirementsBodyKeys() []string {
	return []string{"p", "id", "flow"}
}

// EnforcementHeaderKeys are T2 openai-sentinel-token JSON object keys.
func EnforcementHeaderKeys() []string {
	return []string{"p", "t", "c", "id", "flow"}
}

// PayloadIndexCount is the fixed 25-item PoW fingerprint payload (index 0-24).
const PayloadIndexCount = 25

// RequirementsResponseKeys are response fields consumed after T1.
func RequirementsResponseKeys() []string {
	return []string{"token", "proofofwork.required", "proofofwork.seed", "proofofwork.difficulty", "turnstile.dx"}
}
