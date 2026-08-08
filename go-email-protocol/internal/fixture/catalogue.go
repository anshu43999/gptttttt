package fixture

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/gpt-register/go-email-protocol/internal/protocol"
)

// Catalogue is the loaded set of fixtures keyed by state ID.
type Catalogue struct {
	Root     string
	Fixtures map[protocol.StateID]*Fixture
}

// LoadCatalogue reads all *.json fixtures under root (recursive) and validates them.
func LoadCatalogue(root string) (*Catalogue, error) {
	c := &Catalogue{
		Root:     root,
		Fixtures: make(map[protocol.StateID]*Fixture),
	}
	err := filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			// rechallenge contracts are a separate schema; never load as v1 fixtures.
			if path != root && strings.EqualFold(d.Name(), "rechallenge") {
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(strings.ToLower(d.Name()), ".json") {
			return nil
		}
		base := strings.ToLower(d.Name())
		if strings.Contains(base, "schema") || strings.HasPrefix(base, "_") {
			return nil
		}
		raw, err := os.ReadFile(path)
		if err != nil {
			return fmt.Errorf("read %s: %w", path, err)
		}
		if err := ValidateRedactedJSON(path, raw); err != nil {
			return err
		}
		var f Fixture
		if err := json.Unmarshal(raw, &f); err != nil {
			return fmt.Errorf("decode %s: %w", path, err)
		}
		if err := ValidateFixture(&f, path); err != nil {
			return err
		}
		if _, ok := c.Fixtures[f.ID]; ok {
			return fmt.Errorf("duplicate fixture for %s at %s", f.ID, path)
		}
		cp := f
		c.Fixtures[f.ID] = &cp
		return nil
	})
	if err != nil {
		return nil, err
	}
	return c, nil
}

// ValidateFixture checks structural rules for a single fixture.
func ValidateFixture(f *Fixture, path string) error {
	if f == nil {
		return fmt.Errorf("%s: nil fixture", path)
	}
	if f.SchemaVersion != CurrentSchemaVersion {
		return fmt.Errorf("%s: schema_version %d != %d", path, f.SchemaVersion, CurrentSchemaVersion)
	}
	if f.ID == "" {
		return fmt.Errorf("%s: missing id", path)
	}
	if !protocol.IsKnown(f.ID) {
		return fmt.Errorf("%s: unknown state id %q", path, f.ID)
	}
	wantKind := protocol.KindOf(f.ID)
	if f.Kind != wantKind {
		return fmt.Errorf("%s: kind %q does not match id %s (want %s)", path, f.Kind, f.ID, wantKind)
	}
	switch f.Status {
	case StatusSpecified, StatusCaptureRequired:
	default:
		return fmt.Errorf("%s: invalid status %q", path, f.Status)
	}
	if f.Title == "" {
		return fmt.Errorf("%s: missing title", path)
	}
	if f.Status == StatusSpecified {
		if err := validateSpecifiedShape(f, path); err != nil {
			return err
		}
	}
	for i, ck := range f.Cookies {
		switch ck.ValuePolicy {
		case "omit", "hash", "redacted_marker":
		default:
			return fmt.Errorf("%s: cookies[%d].value_policy %q invalid", path, i, ck.ValuePolicy)
		}
		if ck.Name == "" {
			return fmt.Errorf("%s: cookies[%d].name empty", path, i)
		}
	}
	return nil
}

func validateSpecifiedShape(f *Fixture, path string) error {
	switch f.Kind {
	case protocol.KindLocal, protocol.KindContinuation:
		return nil
	case protocol.KindSentinel:
		if f.Sentinel == nil && f.Request == nil {
			return fmt.Errorf("%s: specified sentinel state needs sentinel or request", path)
		}
		if f.Request != nil {
			if err := validateHTTPRequestShape(f.Request, path, f.ID); err != nil {
				return err
			}
		}
		return nil
	case protocol.KindMain:
		switch f.ID {
		case protocol.S0, protocol.S5, protocol.S9, protocol.S14:
			// Local/orchestrator steps: request optional.
			if f.Request != nil {
				return validateHTTPRequestShape(f.Request, path, f.ID)
			}
			return nil
		}
		if f.Request == nil {
			return fmt.Errorf("%s: specified main state %s missing request", path, f.ID)
		}
		return validateHTTPRequestShape(f.Request, path, f.ID)
	default:
		return fmt.Errorf("%s: unknown kind %q", path, f.Kind)
	}
}

func validateHTTPRequestShape(r *protocol.RequestShape, path string, id protocol.StateID) error {
	if r.Method == "" || r.URLTemplate == "" {
		return fmt.Errorf("%s: request method/url_template required for %s", path, id)
	}
	if strings.TrimSpace(r.HeaderPreset) == "" {
		return fmt.Errorf("%s: header_preset required for specified HTTP state %s", path, id)
	}
	// header_keys must be present (may be empty only for documented none; nil means omitted from JSON).
	if r.HeaderKeys == nil {
		return fmt.Errorf("%s: header_keys must be present (ordered list) for %s", path, id)
	}
	bodyKind := strings.ToLower(strings.TrimSpace(r.BodyKind))
	if bodyKind == "" {
		return fmt.Errorf("%s: body_kind required for specified HTTP state %s", path, id)
	}
	switch bodyKind {
	case "none", "empty":
		// no body fields required
	case "json", "form":
		if len(r.BodyFields) == 0 {
			return fmt.Errorf("%s: body_fields required when body_kind=%s for %s", path, bodyKind, id)
		}
		for i, bf := range r.BodyFields {
			if strings.TrimSpace(bf.Name) == "" {
				return fmt.Errorf("%s: body_fields[%d].name empty for %s", path, i, id)
			}
		}
	default:
		return fmt.Errorf("%s: unsupported body_kind %q for %s", path, r.BodyKind, id)
	}
	return nil
}

// MissingRequired returns required state IDs not present in the catalogue.
func (c *Catalogue) MissingRequired() []protocol.StateID {
	var missing []protocol.StateID
	for _, id := range protocol.RequiredStateIDs() {
		if _, ok := c.Fixtures[id]; !ok {
			missing = append(missing, id)
		}
	}
	return missing
}

// CaptureRequired returns state IDs that are shells awaiting wire capture.
func (c *Catalogue) CaptureRequired() []protocol.StateID {
	var out []protocol.StateID
	for id, f := range c.Fixtures {
		if f.Status == StatusCaptureRequired {
			out = append(out, id)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}

// Specified returns fully specified state IDs.
func (c *Catalogue) Specified() []protocol.StateID {
	var out []protocol.StateID
	for id, f := range c.Fixtures {
		if f.Status == StatusSpecified {
			out = append(out, id)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}

// ValidateCompleteness ensures every required state has a fixture entry.
func (c *Catalogue) ValidateCompleteness() error {
	missing := c.MissingRequired()
	if len(missing) == 0 {
		return nil
	}
	parts := make([]string, len(missing))
	for i, id := range missing {
		parts[i] = string(id)
	}
	return fmt.Errorf("catalogue missing required states: %s", strings.Join(parts, ", "))
}
