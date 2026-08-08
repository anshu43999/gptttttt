package releasegate

import (
	"bytes"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"
)

const WireManifestSchemaVersion = 1

const (
	VersionPolicyExact = "exact"
	VersionPolicyMajor = "major"
)

//go:embed testdata/approved-wire-manifest.json
var approvedWireManifestBytes []byte

type wireManifestDocument struct {
	SchemaVersion           int    `json:"schema_version"`
	ReleaseID               string `json:"release_id"`
	ContractReleaseID       string `json:"contract_release_id"`
	ContractCanonicalSHA256 string `json:"contract_canonical_sha256"`
	SentinelReleaseID       string `json:"sentinel_release_id"`
	SentinelManifestSHA256  string `json:"sentinel_manifest_sha256"`
	TransportProfileID      string `json:"transport_profile_id"`
	TransportProfileSHA256  string `json:"transport_profile_sha256"`
	HeaderContractID        string `json:"header_contract_id"`
	HeaderContractSHA256    string `json:"header_contract_sha256"`
	Browser                  string `json:"browser"`
	BrowserVersion          string `json:"browser_version"`
	BrowserVersionPolicy    string `json:"browser_version_policy"`
	ManifestSHA256          string `json:"manifest_sha256"`
}

// WireManifest is an immutable, independently pinned release binding. Its
// private document prevents callers from recomputing approval evidence from a
// substituted Profile object during startup.
type WireManifest struct {
	doc wireManifestDocument
}

func LoadWireManifest(path string) (*WireManifest, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("releasegate: read wire manifest: %w", err)
	}
	return ParseWireManifest(raw)
}

func ParseWireManifest(raw []byte) (*WireManifest, error) {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	var doc wireManifestDocument
	if err := decoder.Decode(&doc); err != nil {
		return nil, fmt.Errorf("releasegate: decode wire manifest: %w", err)
	}
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		if err == nil {
			return nil, fmt.Errorf("releasegate: wire manifest contains multiple JSON values")
		}
		return nil, fmt.Errorf("releasegate: decode trailing wire manifest: %w", err)
	}
	if err := validateWireManifestDocument(doc); err != nil {
		return nil, err
	}
	return &WireManifest{doc: doc}, nil
}

func validateWireManifestDocument(doc wireManifestDocument) error {
	if doc.SchemaVersion != WireManifestSchemaVersion || strings.TrimSpace(doc.ReleaseID) == "" ||
		strings.TrimSpace(doc.ContractReleaseID) == "" || !validSHA256(doc.ContractCanonicalSHA256) ||
		strings.TrimSpace(doc.SentinelReleaseID) == "" || !validSHA256(doc.SentinelManifestSHA256) ||
		strings.TrimSpace(doc.TransportProfileID) == "" || !validSHA256(doc.TransportProfileSHA256) ||
		strings.TrimSpace(doc.HeaderContractID) == "" || !validSHA256(doc.HeaderContractSHA256) ||
		strings.TrimSpace(doc.Browser) == "" || strings.TrimSpace(doc.BrowserVersion) == "" || !validSHA256(doc.ManifestSHA256) {
		return fmt.Errorf("releasegate: wire manifest identity is incomplete")
	}
	if doc.BrowserVersionPolicy != VersionPolicyExact && doc.BrowserVersionPolicy != VersionPolicyMajor {
		return fmt.Errorf("releasegate: unsupported browser version policy %q", doc.BrowserVersionPolicy)
	}
	if _, ok := browserVersionMajor(doc.BrowserVersion); !ok {
		return fmt.Errorf("releasegate: wire manifest browser version is malformed")
	}
	want, err := wireManifestIdentity(doc)
	if err != nil {
		return err
	}
	if doc.ManifestSHA256 != want {
		return fmt.Errorf("releasegate: wire manifest canonical SHA-256 mismatch")
	}
	var approved wireManifestDocument
	if err := json.Unmarshal(approvedWireManifestBytes, &approved); err != nil {
		return fmt.Errorf("releasegate: embedded wire authority is invalid: %w", err)
	}
	if doc != approved {
		return fmt.Errorf("releasegate: wire manifest is not the embedded approved release")
	}
	return nil
}

func wireManifestIdentity(doc wireManifestDocument) (string, error) {
	doc.ManifestSHA256 = ""
	raw, err := json.Marshal(doc)
	if err != nil {
		return "", fmt.Errorf("releasegate: canonicalize wire manifest: %w", err)
	}
	return sha256Identity(raw), nil
}

func (m *WireManifest) ManifestSHA256() string {
	if m == nil { return "" }
	return m.doc.ManifestSHA256
}
func (m *WireManifest) ReleaseID() string {
	if m == nil { return "" }
	return m.doc.ReleaseID
}
func (m *WireManifest) ContractReleaseID() string {
	if m == nil { return "" }
	return m.doc.ContractReleaseID
}
func (m *WireManifest) ContractCanonicalSHA256() string {
	if m == nil { return "" }
	return m.doc.ContractCanonicalSHA256
}
func (m *WireManifest) SentinelReleaseID() string {
	if m == nil { return "" }
	return m.doc.SentinelReleaseID
}
func (m *WireManifest) SentinelManifestSHA256() string {
	if m == nil { return "" }
	return m.doc.SentinelManifestSHA256
}
func (m *WireManifest) TransportProfileID() string {
	if m == nil { return "" }
	return m.doc.TransportProfileID
}
func (m *WireManifest) TransportProfileSHA256() string {
	if m == nil { return "" }
	return m.doc.TransportProfileSHA256
}
func (m *WireManifest) HeaderContractID() string {
	if m == nil { return "" }
	return m.doc.HeaderContractID
}
func (m *WireManifest) HeaderContractSHA256() string {
	if m == nil { return "" }
	return m.doc.HeaderContractSHA256
}
func (m *WireManifest) Browser() string {
	if m == nil { return "" }
	return m.doc.Browser
}
func (m *WireManifest) BrowserVersion() string {
	if m == nil { return "" }
	return m.doc.BrowserVersion
}
func (m *WireManifest) BrowserVersionPolicy() string {
	if m == nil { return "" }
	return m.doc.BrowserVersionPolicy
}
