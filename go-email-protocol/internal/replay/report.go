package replay

import "fmt"

const (
	// CodeWireContractDrift is returned for any request/order/response contract mismatch.
	CodeWireContractDrift = "wire_contract_drift"
	// CodeReplayClosed is returned when a closed replay client is used.
	CodeReplayClosed = "replay_client_closed"
	// CodeRedirectLimit is returned when the real HTTP redirect loop exceeds the contract limit.
	CodeRedirectLimit = "replay_redirect_limit"
	// CodeScriptedTransportFailure identifies an injected transport outcome used to prove ambiguity handling.
	CodeScriptedTransportFailure = "scripted_transport_failure"
)

// Position identifies one immutable exchange without containing request secrets.
type Position struct {
	CaptureID          string `json:"capture_id,omitempty"`
	CaptureSequence    int    `json:"capture_sequence"`
	State              string `json:"state"`
	ExchangeIndex      int    `json:"exchange_index"`
	SentinelOccurrence *int   `json:"sentinel_occurrence,omitempty"`
}

func (p Position) String() string {
	prefix := fmt.Sprintf("capture_sequence=%d state=%s exchange=%d", p.CaptureSequence, p.State, p.ExchangeIndex)
	if p.CaptureID != "" {
		prefix = "capture=" + p.CaptureID + " " + prefix
	}
	if p.SentinelOccurrence != nil {
		return fmt.Sprintf("%s sentinel_occurrence=%d", prefix, *p.SentinelOccurrence)
	}
	return prefix
}

// Mismatch is a machine-readable, redaction-safe replay failure.
// Expected and Actual contain structural descriptions only; callers must not put
// raw header, Cookie, query, body, OTP, or credential values in these fields.
type Mismatch struct {
	ContractID string   `json:"contract_id,omitempty"`
	Position   Position `json:"position"`
	Field      string   `json:"field"`
	Expected   string   `json:"expected,omitempty"`
	Actual     string   `json:"actual,omitempty"`
	Detail     string   `json:"detail,omitempty"`
}

func (e *Mismatch) Error() string {
	if e == nil {
		return ""
	}
	return fmt.Sprintf("%s: %s %s expected=%s actual=%s", CodeWireContractDrift, e.Position, e.Field, e.Expected, e.Actual)
}

// Code returns the stable drift category.
func (e *Mismatch) Code() string { return CodeWireContractDrift }

// ClientError is a typed replay lifecycle failure.
type ClientError struct {
	FailureCode string   `json:"code"`
	Position    Position `json:"position,omitempty"`
	Detail      string   `json:"detail,omitempty"`
}

func (e *ClientError) Error() string {
	if e == nil {
		return ""
	}
	if e.Detail == "" {
		return e.FailureCode
	}
	return e.FailureCode + ": " + e.Detail
}

// Code returns the stable failure category.
func (e *ClientError) Code() string {
	if e == nil {
		return ""
	}
	return e.FailureCode
}

// TransportFailure simulates a deterministic transport outcome without network.
// Sent reports whether request consumption became ambiguous before the error.
type TransportFailure struct {
	Position Position `json:"position"`
	Sent     bool     `json:"sent"`
}

func (e *TransportFailure) Error() string {
	if e == nil {
		return ""
	}
	return fmt.Sprintf("%s: %s sent=%t", CodeScriptedTransportFailure, e.Position, e.Sent)
}

// Code returns the stable transport-failure category.
func (e *TransportFailure) Code() string { return CodeScriptedTransportFailure }
