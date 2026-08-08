package rechallenge

import "fmt"

const (
	CodeCaptureRoleMismatch       = "capture_role_mismatch"
	CodeContractRedactionViolation = "contract_redaction_violation"
	CodeWireContractDrift         = "wire_contract_drift"
	CodeSourceHashMismatch        = "capture_source_hash_mismatch"
)

type ContractError struct {
	Code       string `json:"code"`
	Stage      string `json:"stage"`
	Category   string `json:"category"`
	Field      string `json:"field,omitempty"`
	Provenance string `json:"provenance,omitempty"`
	Cause      error  `json:"-"`
}

func (e *ContractError) Error() string {
	if e == nil {
		return "<nil>"
	}
	if e.Cause != nil {
		return fmt.Sprintf("rechallenge: %s stage=%s category=%s field=%s provenance=%s: %v", e.Code, e.Stage, e.Category, e.Field, e.Provenance, e.Cause)
	}
	return fmt.Sprintf("rechallenge: %s stage=%s category=%s field=%s provenance=%s", e.Code, e.Stage, e.Category, e.Field, e.Provenance)
}

func (e *ContractError) Unwrap() error { return e.Cause }

func contractError(code, stage, category, field, provenance string, cause error) *ContractError {
	return &ContractError{Code: code, Stage: stage, Category: category, Field: field, Provenance: provenance, Cause: cause}
}
