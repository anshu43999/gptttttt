// Package fixture implements G0 protocol fixture schema, redaction, and catalogue loading.
package fixture

import (
	"github.com/gpt-register/go-email-protocol/internal/protocol"
)

// Status of a fixture entry.
type Status string

const (
	// StatusSpecified means request/response shapes are filled from plan/oracle.
	StatusSpecified Status = "specified"
	// StatusCaptureRequired is a typed shell: fields unknown, zero secrets, not invented.
	StatusCaptureRequired Status = "capture_required"
)

// Fixture is one redacted protocol/transport/sentinel observation for a state ID.
type Fixture struct {
	// SchemaVersion is the fixture document schema (not protocol version).
	SchemaVersion int `json:"schema_version"`

	// ID is the state id (S0–S15, T1–T3, C0–C6, L1–L3).
	ID protocol.StateID `json:"id"`

	// Kind groups the state (main|sentinel|continuation|local).
	Kind protocol.Kind `json:"kind"`

	// Status is specified or capture_required.
	Status Status `json:"status"`

	// Title is a short human label.
	Title string `json:"title"`

	// SourceRefs point at plan sections or TS oracle files (no secrets).
	SourceRefs []string `json:"source_refs,omitempty"`

	// Request is the redacted HTTP shape when applicable.
	Request *protocol.RequestShape `json:"request,omitempty"`

	// Cookies lists cookie metadata (names only / hashed values).
	Cookies []protocol.CookieMeta `json:"cookies,omitempty"`

	// ResponseUsed lists fields/discriminators consumed after the step.
	ResponseUsed *protocol.ResponseUsed `json:"response_used,omitempty"`

	// Sentinel holds flow name / input field names when applicable.
	Sentinel *protocol.SentinelObservation `json:"sentinel,omitempty"`

	// Transport holds profile/ALPN/bridge observation slots.
	Transport *protocol.TransportObservation `json:"transport,omitempty"`

	// NextStates are typical successor state IDs (not a full graph).
	NextStates []protocol.StateID `json:"next_states,omitempty"`

	// Notes for implementers; must never contain secrets.
	Notes string `json:"notes,omitempty"`
}

// CurrentSchemaVersion is the G0 fixture schema version.
const CurrentSchemaVersion = 1
