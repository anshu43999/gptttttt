package fingerprint

import (
	"encoding/json"
	"strings"
	"testing"
	"time"

	mathrand "math/rand/v2"
)

func TestGenerateDesktopAndMobile(t *testing.T) {
	rng := mathrand.New(mathrand.NewPCG(1, 2))
	for _, family := range []string{FamilyDesktop, FamilyMobile} {
		b, err := Generate(GenerateOptions{
			RNG:         rng,
			ForceFamily: family,
			Now:         time.Date(2026, 7, 17, 0, 0, 0, 0, time.UTC),
		})
		if err != nil {
			t.Fatalf("Generate %s: %v", family, err)
		}
		if b.Version != BundleVersion {
			t.Fatalf("version %d", b.Version)
		}
		if !b.Consistency.Locked || b.Consistency.Hash == "" {
			t.Fatal("expected frozen hash")
		}
		if err := b.AssertReady(); err != nil {
			t.Fatalf("AssertReady: %v", err)
		}
		if b.Device.UAMajor < 130 {
			t.Fatalf("ua major too old: %d", b.Device.UAMajor)
		}
		if strings.Contains(b.Device.UAFullVersion, ".0.0.0") {
			t.Fatalf("lazy full version: %s", b.Device.UAFullVersion)
		}
		h := b.IdentityHeaders()
		if h["user-agent"] != b.Device.UserAgent {
			t.Fatal("identity header UA mismatch")
		}
		if h["sec-ch-ua-mobile"] != b.ClientHints.SecChUAMobile {
			t.Fatal("mobile hint missing")
		}
		if b.SentinelEnv.UserAgent != b.Device.UserAgent {
			t.Fatal("sentinel env not projected")
		}
		if b.TransportProfileID == "" {
			t.Fatal("transport_profile_id empty")
		}
	}
}

func TestStrictProxyAffinityJP(t *testing.T) {
	rng := mathrand.New(mathrand.NewPCG(9, 9))
	b, err := Generate(GenerateOptions{
		RNG:             rng,
		ForceFamily:     FamilyDesktop,
		ExpectedCountry: "JP",
		TimezonePolicy:  TimezoneStrictMatch,
		Now:             time.Now().UTC(),
	})
	if err != nil {
		t.Fatal(err)
	}
	if b.Locale.TimezoneID != "Asia/Tokyo" {
		t.Fatalf("tz=%s", b.Locale.TimezoneID)
	}
}

func TestStrictProxyAffinityUnknownCountryFails(t *testing.T) {
	rng := mathrand.New(mathrand.NewPCG(3, 4))
	_, err := Generate(GenerateOptions{
		RNG:             rng,
		ForceFamily:     FamilyDesktop,
		ExpectedCountry: "ZZ",
		TimezonePolicy:  TimezoneStrictMatch,
	})
	if err == nil {
		t.Fatal("expected affinity failure")
	}
	if !strings.Contains(err.Error(), "proxy_affinity") && !strings.Contains(err.Error(), "proxy_affinity_mismatch") {
		t.Fatalf("unexpected err: %v", err)
	}
}

func TestDirtyUAFailsValidate(t *testing.T) {
	rng := mathrand.New(mathrand.NewPCG(5, 6))
	b, err := Generate(GenerateOptions{RNG: rng, ForceFamily: FamilyDesktop})
	if err != nil {
		t.Fatal(err)
	}
	b.Device.UserAgent = "Mozilla/5.0 Chrome/99.0.0.0 Safari/537.36"
	b.Device.UAMajor = 142
	err = b.Validate(ValidateOptions{RequireLocked: false, SkipProxyAffinity: true})
	if err == nil {
		t.Fatal("expected inconsistent")
	}
}

func TestHashTamperDetected(t *testing.T) {
	rng := mathrand.New(mathrand.NewPCG(7, 8))
	b, err := Generate(GenerateOptions{RNG: rng, ForceFamily: FamilyDesktop})
	if err != nil {
		t.Fatal(err)
	}
	b.Locale.TimezoneID = "Europe/Paris"
	err = b.AssertReady()
	if err == nil {
		t.Fatal("expected hash mismatch")
	}
	var fe *Error
	if !asError(err, &fe) || fe.Code != CodeHashMismatch {
		// AssertReady wraps only our Error
		if err2, ok := err.(*Error); !ok || err2.Code != CodeHashMismatch {
			t.Fatalf("want hash mismatch got %v", err)
		}
	}
}

func asError(err error, target **Error) bool {
	e, ok := err.(*Error)
	if !ok {
		return false
	}
	*target = e
	return true
}

func TestJSONRoundTrip(t *testing.T) {
	rng := mathrand.New(mathrand.NewPCG(11, 12))
	b, err := Generate(GenerateOptions{
		RNG:             rng,
		ForceFamily:     FamilyMobile,
		NoiseEnabled:    true,
		ExitIP:          "1.2.3.4",
		ExpectedCountry: "US",
		TimezonePolicy:  TimezoneAllowGlobalEN,
	})
	if err != nil {
		t.Fatal(err)
	}
	raw, err := json.Marshal(b)
	if err != nil {
		t.Fatal(err)
	}
	parsed, err := ParseJSON(raw)
	if err != nil {
		t.Fatal(err)
	}
	if err := parsed.AssertReady(); err != nil {
		t.Fatal(err)
	}
	if parsed.Device.AndroidModel == "" {
		t.Fatal("android model lost")
	}
	if !parsed.Noise.Enabled {
		t.Fatal("noise lost")
	}
}

func TestTransportMajorMismatch(t *testing.T) {
	rng := mathrand.New(mathrand.NewPCG(13, 14))
	b, err := Generate(GenerateOptions{RNG: rng, ForceFamily: FamilyDesktop})
	if err != nil {
		t.Fatal(err)
	}
	// inject wrong transport id; Validate transport check runs before hash when RequireLocked false
	b.TransportProfileID = "chrome-100-win-h2-v1"
	err = b.Validate(ValidateOptions{RequireLocked: false, SkipProxyAffinity: true})
	if err == nil {
		t.Fatal("expected transport mismatch")
	}
	if e, ok := err.(*Error); !ok || e.Code != CodeTransportMismatch {
		t.Fatalf("got %v", err)
	}
}

func TestClientHintsEdgeBrand(t *testing.T) {
	// Force edge by generating many desktop until edge appears
	rng := mathrand.New(mathrand.NewPCG(100, 200))
	var found bool
	for range 40 {
		b, err := Generate(GenerateOptions{RNG: rng, ForceFamily: FamilyDesktop})
		if err != nil {
			t.Fatal(err)
		}
		if b.Identity.Browser == BrowserEdge {
			found = true
			if !strings.Contains(b.ClientHints.SecChUA, "Microsoft Edge") {
				t.Fatalf("hints: %s", b.ClientHints.SecChUA)
			}
			if b.Device.EdgeVersion == "" {
				t.Fatal("edge version empty")
			}
			break
		}
	}
	if !found {
		t.Fatal("did not sample edge profile")
	}
}

func TestSchemaKeysNonEmpty(t *testing.T) {
	if len(SchemaKeys()) < 20 {
		t.Fatal("schema keys too short")
	}
	if len(SchemaKeysV2()) < 40 {
		t.Fatal("v2 schema keys too short")
	}
}

func TestToV1(t *testing.T) {
	rng := mathrand.New(mathrand.NewPCG(1, 1))
	b, err := Generate(GenerateOptions{RNG: rng, ForceFamily: FamilyDesktop})
	if err != nil {
		t.Fatal(err)
	}
	v1 := b.ToV1()
	if v1.UserAgent != b.Device.UserAgent || v1.ID != b.Identity.ProfileUUID {
		t.Fatalf("v1 projection: %+v", v1)
	}
}
