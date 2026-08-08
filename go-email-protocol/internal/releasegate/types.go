// Package releasegate evaluates the immutable evidence required before a
// rechallenge runtime may admit work.
package releasegate

import (
	"fmt"

	"github.com/gpt-register/go-email-protocol/internal/rechallenge"
	"github.com/gpt-register/go-email-protocol/internal/sentinel"
	"github.com/gpt-register/go-email-protocol/internal/transport"
)

// GateState is the machine-readable result of one startup check.
type GateState string

const (
	GatePass GateState = "pass"
	GateFail GateState = "fail"
)

// Status is the aggregate admission state.
type Status string

const (
	StatusOpen   Status = "open"
	StatusClosed Status = "closed"
)

// Purpose determines which transport implementations are admissible.
type Purpose string

const (
	PurposeLiveAdmission Purpose = "live_admission"
	PurposeWireCanary    Purpose = "wire_canary"
	PurposeOfflineReplay Purpose = "offline_replay"
	PurposeDiagnostic    Purpose = "diagnostic"
)

// ResumeMode makes fresh startup versus checkpoint recovery explicit.
type ResumeMode string

const (
	ResumeFresh    ResumeMode = "fresh"
	ResumeRecovery ResumeMode = "recovery"
)

const (
	CodeRuntimeGateClosed          = "runtime_gate_closed"
	CodeContractMissing            = "contract_missing"
	CodeContractUnapproved         = "contract_unapproved"
	CodeContractIncomplete         = "contract_incomplete"
	CodeSentinelReleaseMismatch    = "sentinel_release_mismatch"
	CodeContractBindingMismatch    = "contract_binding_mismatch"
	CodeSentinelReleaseMissing     = "sentinel_release_missing"
	CodeSentinelReleaseUnapproved  = "sentinel_release_unapproved"
	CodeSentinelReleaseIncomplete  = "sentinel_release_incomplete"
	CodeTransportProfileInactive   = "transport_profile_inactive"
	CodeTransportProfileIncomplete = "transport_profile_incomplete"
	CodeTransportProfileMismatch   = "transport_profile_mismatch"
	CodeTransportDiagnosticOnly    = "transport_diagnostic_only"
	CodeHeaderContractMissing      = "header_contract_missing"
	CodeHeaderContractUnapproved   = "header_contract_unapproved"
	CodeHeaderContractIncomplete   = "header_contract_incomplete"
	CodeHeaderContractMismatch     = "header_contract_mismatch"
	CodeCheckpointReleaseMismatch  = "checkpoint_release_mismatch"
	CodeCheckpointIncomplete       = "checkpoint_incomplete"
	CodeMaxActiveMissing           = "max_active_missing"
	CodeMaxActiveMismatch          = "max_active_mismatch"
)

// Failure describes one closed check without carrying raw capture or request
// material. Component and Code are stable API fields; Message is diagnostic.
type Failure struct {
	Component string `json:"component"`
	Code      string `json:"code"`
	Message   string `json:"message"`
}

// Vector is safe to publish through health or diagnostics endpoints.
type Vector struct {
	Purpose            Purpose   `json:"purpose"`
	Contract           GateState `json:"contract"`
	ResumeMode         ResumeMode `json:"resume_mode"`
	SentinelRelease    GateState `json:"sentinel_release"`
	TransportProfile   GateState `json:"transport_profile"`
	HeaderContract     GateState `json:"header_contract"`
	CheckpointCompat   GateState `json:"checkpoint_compat"`
	MaxActiveAlignment GateState `json:"max_active_alignment"`
	Status             Status    `json:"status"`
	Failures           []Failure `json:"failures,omitempty"`
}

// ClosedError lets callers fail startup or admission while retaining the
// complete vector for machine-readable diagnostics.
type ClosedError struct {
	Vector Vector
}

func (e *ClosedError) Error() string {
	if e == nil {
		return ""
	}
	return fmt.Sprintf("%s: %d startup gate check(s) failed", CodeRuntimeGateClosed, len(e.Vector.Failures))
}

// Code returns the stable aggregate failure code.
func (e *ClosedError) Code() string { return CodeRuntimeGateClosed }

// ContractEvidence is the approved normalized registration contract binding.
type ContractEvidence struct {
	ReleaseID                  string `json:"release_id"`
	CanonicalSHA256            string `json:"canonical_sha256"`
	SentinelReleaseID          string `json:"sentinel_release_id"`
	TransportProfileID         string `json:"transport_profile_id"`
	TransportProfileSHA256     string `json:"transport_profile_sha256"`
	HeaderContractID           string `json:"header_contract_id"`
	HeaderContractSHA256       string `json:"header_contract_sha256"`
	Approved                   bool   `json:"approved"`
	Complete                   bool   `json:"complete"`
}

// SentinelEvidence is produced only after loader -> versioned SDK relationship
// and hashes have been verified by the Sentinel release package.
type SentinelEvidence struct {
	ReleaseID       string `json:"release_id"`
	ManifestSHA256 string `json:"manifest_sha256"`
	LoaderSHA256   string `json:"loader_sha256"`
	SDKSHA256      string `json:"sdk_sha256"`
	Approved        bool   `json:"approved"`
	Complete        bool   `json:"complete"`
}

// HeaderEvidence binds state header policy to the contract and effective
// transport profile. StatePresets must cover every request-producing FSM state.
type HeaderEvidence struct {
	ID                 string            `json:"id"`
	CanonicalSHA256    string            `json:"canonical_sha256"`
	ContractReleaseID  string            `json:"contract_release_id"`
	TransportProfileID string            `json:"transport_profile_id"`
	StatePresets       map[string]string `json:"state_presets"`
	Approved           bool              `json:"approved"`
	Complete           bool              `json:"complete"`
}

// ReleaseBinding is persisted in a job checkpoint. A recovering runtime must
// use these exact immutable release/profile identities.
type ReleaseBinding struct {
	WireReleaseID             string `json:"wire_release_id"`
	WireManifestSHA256        string `json:"wire_manifest_sha256"`
	ContractReleaseID       string `json:"contract_release_id"`
	ContractCanonicalSHA256 string `json:"contract_canonical_sha256"`
	SentinelReleaseID       string `json:"sentinel_release_id"`
	SentinelManifestSHA256  string `json:"sentinel_manifest_sha256"`
	TransportProfileID      string `json:"transport_profile_id"`
	TransportProfileSHA256  string `json:"transport_profile_sha256"`
	HeaderContractID        string `json:"header_contract_id"`
	HeaderContractSHA256    string `json:"header_contract_sha256"`
}

// MaxActiveEvidence names every runtime source that could otherwise select a
// different concurrency ceiling. Zero is never treated as an implicit default.
type MaxActiveEvidence struct {
	Authoritative int `json:"authoritative"`
	Admission     int `json:"admission"`
	Worker        int `json:"worker"`
	TasksService  int `json:"tasks_service"`
	Config        int `json:"config"`
}

// Input is the complete startup admission evidence. Checkpoint is nil for a
// fresh job and non-nil for recovery.
type Input struct {
	Purpose          Purpose
	ResumeMode       ResumeMode
	UserAgent        string
	Contract          ContractEvidence
	ContractDocument  *rechallenge.RegistrationContract
	WireManifest      *WireManifest
	SentinelRelease   SentinelEvidence
	SentinelManifest  *sentinel.ReleaseManifest
	TransportProfile  transport.Profile
	TransportFactory transport.FactoryName
	EffectiveTransport *transport.EffectiveProfile
	HeaderContract   HeaderEvidence
	Checkpoint       *ReleaseBinding
	MaxActive        MaxActiveEvidence
}
