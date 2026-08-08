package job

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/gpt-register/go-email-protocol/internal/fingerprint"
)

func TestPrepareProfileServerGenerateLegacy(t *testing.T) {
	m := &Manager{}
	req := CreateRequest{
		Profile: json.RawMessage(`{"id":"profile_1"}`),
		ResourceGrant: ResourceGrant{
			ExitIP:          "1.2.3.4",
			ExpectedCountry: "US",
		},
	}
	b, raw, err := m.prepareProfile(req)
	if err != nil {
		t.Fatal(err)
	}
	if b == nil || !b.Consistency.Locked {
		t.Fatal("expected frozen bundle")
	}
	if err := b.AssertReady(); err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(b.BundleID, "fpb_") {
		t.Fatalf("bundle_id %s", b.BundleID)
	}
	if profileIDFromBundle(b, raw) != b.BundleID {
		t.Fatal("profile id")
	}
	var probe map[string]any
	if json.Unmarshal(raw, &probe) != nil || probe["version"].(float64) != 2 {
		t.Fatalf("raw not v2: %s", raw)
	}
}

func TestPrepareProfileEmptyGenerates(t *testing.T) {
	m := &Manager{}
	b, _, err := m.prepareProfile(CreateRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if b.Source != fingerprint.SourceGenerated {
		t.Fatalf("source %s", b.Source)
	}
}

func TestPrepareProfileRejectsDirtyV2(t *testing.T) {
	m := &Manager{}
	// claims v2 but incomplete
	raw := []byte(`{"version":2,"bundle_id":"fpb_x","identity":{"profile_uuid":"u"}}`)
	_, _, err := m.prepareProfile(CreateRequest{Profile: raw})
	if err == nil {
		t.Fatal("expected reject")
	}
	ve, ok := err.(*ValidationError)
	if !ok || ve.Code == "" {
		t.Fatalf("want ValidationError with code, got %T %v", err, err)
	}
}

func TestPrepareProfileAcceptsGeneratedV2(t *testing.T) {
	m := &Manager{}
	gen, err := fingerprint.Generate(fingerprint.GenerateOptions{
		ForceFamily: fingerprint.FamilyDesktop,
	})
	if err != nil {
		t.Fatal(err)
	}
	raw, err := json.Marshal(gen)
	if err != nil {
		t.Fatal(err)
	}
	b, out, err := m.prepareProfile(CreateRequest{
		Profile: raw,
		ResourceGrant: ResourceGrant{
			ExitIP: "9.9.9.9",
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if b.ProxyAffinity.ExitIP != "9.9.9.9" {
		t.Fatalf("exit ip not applied: %s", b.ProxyAffinity.ExitIP)
	}
	if err := b.AssertReady(); err != nil {
		t.Fatal(err)
	}
	_ = out
}

func TestLooksLikeAndLegacy(t *testing.T) {
	if !isLegacyProfileStub([]byte(`{"id":"p1"}`)) {
		t.Fatal("legacy")
	}
	if isLegacyProfileStub([]byte(`{"version":2,"bundle_id":"x"}`)) {
		t.Fatal("not legacy")
	}
	if !looksLikeBundleV2([]byte(`{"version":2}`)) {
		t.Fatal("v2")
	}
	if looksLikeBundleV2([]byte(`{"id":"p1"}`)) {
		t.Fatal("stub should not look v2")
	}
}
