package job

import (
	"bytes"
	"encoding/json"
	"strings"

	"github.com/gpt-register/go-email-protocol/internal/fingerprint"
)

// prepareProfile resolves FingerprintBundle v2 for a create request.
// - Full v2 JSON: parse + AssertReady + grant affinity checks
// - Empty or legacy {id:...}: server-generate desktop bundle (G1 synthetic compat)
// - Non-empty invalid claiming v2: fail closed
func (m *Manager) prepareProfile(req CreateRequest) (*fingerprint.Bundle, []byte, error) {
	raw := bytesTrimProfile(req.Profile)
	if looksLikeBundleV2(raw) {
		b, err := fingerprint.ParseJSON(raw)
		if err != nil {
			return nil, nil, &ValidationError{Code: string(fingerprint.CodeInconsistent), Message: err.Error()}
		}
		if err := applyGrantAffinity(b, req.ResourceGrant); err != nil {
			return nil, nil, err
		}
		if err := b.AssertReady(); err != nil {
			return nil, nil, mapFingerprintErr(err)
		}
		out, err := json.Marshal(b)
		if err != nil {
			return nil, nil, err
		}
		return b, out, nil
	}
	if len(raw) > 0 && !isLegacyProfileStub(raw) {
		return nil, nil, &ValidationError{
			Code:    string(fingerprint.CodeInconsistent),
			Message: "profile must be FingerprintBundle v2 or empty/legacy id stub",
		}
	}
	// empty / legacy stub → server generate (does not change Python default backend)
	opts := fingerprint.GenerateOptions{
		ForceFamily:     fingerprint.FamilyDesktop,
		// HAR gold path is Firefox; do not randomly mix Chrome/Edge for worker-generated profiles.
		ForceBrowser:    fingerprint.BrowserFirefox,
		ExpectedCountry: req.ResourceGrant.ExpectedCountry,
		ExitIP:          req.ResourceGrant.ExitIP,
		Source:          fingerprint.SourceGenerated,
		TimezonePolicy:  fingerprint.TimezoneAllowGlobalEN,
	}
	if strings.TrimSpace(req.ResourceGrant.ExpectedCountry) != "" {
		opts.TimezonePolicy = fingerprint.TimezoneStrictMatch
		b, err := fingerprint.Generate(opts)
		if err != nil {
			opts.TimezonePolicy = fingerprint.TimezoneAllowGlobalEN
			b, err = fingerprint.Generate(opts)
			if err != nil {
				return nil, nil, err
			}
			return marshalPreparedBundle(b)
		}
		return marshalPreparedBundle(b)
	}
	b, err := fingerprint.Generate(opts)
	if err != nil {
		return nil, nil, err
	}
	return marshalPreparedBundle(b)
}

func marshalPreparedBundle(b *fingerprint.Bundle) (*fingerprint.Bundle, []byte, error) {
	out, err := json.Marshal(b)
	if err != nil {
		return nil, nil, err
	}
	return b, out, nil
}

func applyGrantAffinity(b *fingerprint.Bundle, g ResourceGrant) error {
	if b == nil {
		return nil
	}
	country := fingerprint.NormalizeCountry(g.ExpectedCountry)
	changed := false
	if country != "" && b.ProxyAffinity.ExpectedCountry != country {
		b.ProxyAffinity.ExpectedCountry = country
		changed = true
	}
	if ip := strings.TrimSpace(g.ExitIP); ip != "" && b.ProxyAffinity.ExitIP != ip {
		b.ProxyAffinity.ExitIP = ip
		changed = true
	}
	if changed {
		if err := b.Freeze(); err != nil {
			return mapFingerprintErr(err)
		}
	}
	if err := b.Validate(fingerprint.ValidateOptions{RequireLocked: true}); err != nil {
		return mapFingerprintErr(err)
	}
	return nil
}

func mapFingerprintErr(err error) error {
	if err == nil {
		return nil
	}
	if fe, ok := err.(*fingerprint.Error); ok {
		return &ValidationError{Code: string(fe.Code), Message: fe.Message}
	}
	return &ValidationError{Code: string(fingerprint.CodeInconsistent), Message: err.Error()}
}

func looksLikeBundleV2(raw []byte) bool {
	if len(raw) == 0 {
		return false
	}
	var probe struct {
		Version  int    `json:"version"`
		BundleID string `json:"bundle_id"`
		Identity *struct {
			ProfileUUID string `json:"profile_uuid"`
		} `json:"identity"`
	}
	if json.Unmarshal(raw, &probe) != nil {
		return false
	}
	if probe.Version == fingerprint.BundleVersion {
		return true
	}
	if probe.BundleID != "" {
		return true
	}
	if probe.Identity != nil && probe.Identity.ProfileUUID != "" {
		return true
	}
	return false
}

func isLegacyProfileStub(raw []byte) bool {
	var m map[string]any
	if json.Unmarshal(raw, &m) != nil {
		return false
	}
	// classic G1 test: {"id":"profile_N"} only
	if _, ok := m["id"]; ok && m["version"] == nil && m["bundle_id"] == nil && m["identity"] == nil {
		return true
	}
	return false
}

func bytesTrimProfile(p json.RawMessage) []byte {
	s := bytes.TrimSpace(p)
	if len(s) == 0 || string(s) == "null" {
		return nil
	}
	return s
}

func profileIDFromBundle(b *fingerprint.Bundle, raw []byte) string {
	if b != nil {
		if b.BundleID != "" {
			return b.BundleID
		}
		if b.Identity.ProfileUUID != "" {
			return b.Identity.ProfileUUID
		}
	}
	return profileIDFrom(raw)
}
