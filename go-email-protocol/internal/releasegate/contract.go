package releasegate

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sort"
	"strings"

	"github.com/gpt-register/go-email-protocol/internal/rechallenge"
	"github.com/gpt-register/go-email-protocol/internal/transport"
)

// ContractEvidenceFromDocument validates the normalized immutable wire
// contract before deriving the explicit release identity used by the gate and
// recovery checkpoint.
func ContractEvidenceFromDocument(contract *rechallenge.RegistrationContract, manifest *WireManifest, approved bool) (ContractEvidence, error) {
	if contract == nil {
		return ContractEvidence{}, fmt.Errorf("releasegate: nil registration contract")
	}
	if err := rechallenge.ValidateContract(contract); err != nil {
		return ContractEvidence{}, fmt.Errorf("releasegate: validate registration contract: %w", err)
	}
	if strings.TrimSpace(contract.ContractID) == "" || strings.TrimSpace(contract.CanonicalSHA256) == "" {
		return ContractEvidence{}, fmt.Errorf("releasegate: registration contract is not finalized")
	}
	if manifest == nil {
		return ContractEvidence{}, fmt.Errorf("releasegate: validated wire manifest is required")
	}
	if err := validateWireManifestDocument(manifest.doc); err != nil {
		return ContractEvidence{}, err
	}
	headerHash, err := HeaderPolicyCanonicalSHA256(contract)
	if err != nil {
		return ContractEvidence{}, err
	}
	if contract.ContractID != manifest.ContractReleaseID() || contract.CanonicalSHA256 != manifest.ContractCanonicalSHA256() ||
		contract.SentinelReleaseID != manifest.SentinelReleaseID() || contract.TransportProfileID != manifest.TransportProfileID() ||
		headerHash != manifest.HeaderContractSHA256() {
		return ContractEvidence{}, fmt.Errorf("releasegate: registration contract differs from approved wire manifest")
	}
	return ContractEvidence{
		ReleaseID:              contract.ContractID,
		CanonicalSHA256:        contract.CanonicalSHA256,
		SentinelReleaseID:      contract.SentinelReleaseID,
		TransportProfileID:     contract.TransportProfileID,
		TransportProfileSHA256: manifest.TransportProfileSHA256(),
		HeaderContractID:       manifest.HeaderContractID(),
		HeaderContractSHA256:   manifest.HeaderContractSHA256(),
		Approved:               approved,
		Complete:               true,
	}, nil
}

// TransportProfileCanonicalSHA256 computes the immutable effective profile
// content identity. JSON map keys are deterministically ordered by encoding/json.
func TransportProfileCanonicalSHA256(profile transport.Profile) (string, error) {
	raw, err := json.Marshal(profile)
	if err != nil {
		return "", fmt.Errorf("releasegate: canonicalize transport profile: %w", err)
	}
	return sha256Identity(raw), nil
}

// HeaderPolicyCanonicalSHA256 derives header-policy identity from the
// normalized contract rather than trusting a syntactically valid supplied hash.
func HeaderPolicyCanonicalSHA256(contract *rechallenge.RegistrationContract) (string, error) {
	if contract == nil {
		return "", fmt.Errorf("releasegate: nil registration contract")
	}
	type headerExchange struct {
		State         string                    `json:"state"`
		ExchangeIndex int                       `json:"exchange_index"`
		Method        string                    `json:"method"`
		Host          string                    `json:"host"`
		Path          string                    `json:"path"`
		Headers       []rechallenge.HeaderRule  `json:"headers"`
		captureID      string
		captureSequence int
	}
	policy := make([]headerExchange, 0, len(contract.Exchanges))
	for _, exchange := range contract.Exchanges {
		headers := append([]rechallenge.HeaderRule(nil), exchange.Request.Headers...)
		for index := range headers {
			headers[index].Provenance = append([]string(nil), headers[index].Provenance...)
			sort.Strings(headers[index].Provenance)
		}
		policy = append(policy, headerExchange{
			State: string(exchange.State), ExchangeIndex: exchange.ExchangeIndex,
			Method: exchange.Request.Method, Host: exchange.Request.Host, Path: exchange.Request.Path,
			Headers: headers,
			captureID: exchange.Provenance.CaptureID,
			captureSequence: exchange.CaptureSequence,
		})
	}
	sort.SliceStable(policy, func(i, j int) bool {
		if policy[i].captureID != policy[j].captureID {
			return policy[i].captureID < policy[j].captureID
		}
		return policy[i].captureSequence < policy[j].captureSequence
	})
	raw, err := json.Marshal(policy)
	if err != nil {
		return "", fmt.Errorf("releasegate: canonicalize header policy: %w", err)
	}
	return sha256Identity(raw), nil
}

func sha256Identity(raw []byte) string {
	sum := sha256.Sum256(raw)
	return "sha256:" + hex.EncodeToString(sum[:])
}
