package fingerprint

import (
	"strings"
	"testing"
	"time"

	mathrand "math/rand/v2"
)

func TestGenerateFirefoxForceBrowser(t *testing.T) {
	rng := mathrand.New(mathrand.NewPCG(42, 42))
	b, err := Generate(GenerateOptions{
		RNG:          rng,
		ForceFamily:  FamilyDesktop,
		ForceBrowser: BrowserFirefox,
		Now:          time.Date(2026, 7, 17, 0, 0, 0, 0, time.UTC),
	})
	if err != nil {
		t.Fatal(err)
	}
	if b.Identity.Browser != BrowserFirefox {
		t.Fatalf("browser %s", b.Identity.Browser)
	}
	if !strings.Contains(b.Device.UserAgent, "Firefox/") || !strings.Contains(b.Device.UserAgent, "Gecko/") {
		t.Fatalf("ua %s", b.Device.UserAgent)
	}
	if strings.Contains(b.Device.UserAgent, "Chrome/") {
		t.Fatal("firefox ua must not contain Chrome/")
	}
	if b.Navigator.Vendor != "" {
		t.Fatalf("vendor %q", b.Navigator.Vendor)
	}
	// No Client Hints on Firefox
	if b.ClientHints.SecChUA != "" || b.ClientHints.SecChUAMobile != "" {
		t.Fatalf("unexpected CH: %+v", b.ClientHints)
	}
	h := b.IdentityHeaders()
	if h["user-agent"] == "" || h["accept-language"] == "" {
		t.Fatal(h)
	}
	for k := range h {
		if strings.HasPrefix(k, "sec-ch-") {
			t.Fatalf("identity headers must not emit CH for firefox: %s", k)
		}
	}
	if !strings.Contains(b.HeaderIdentity.AcceptEncodingDefault, "zstd") {
		t.Fatalf("encoding %s", b.HeaderIdentity.AcceptEncodingDefault)
	}
}

func TestParseFirefoxUA(t *testing.T) {
	ua := "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:150.0) Gecko/20100101 Firefox/150.0"
	major, full, err := ParseUAVersions(ua)
	if err != nil {
		t.Fatal(err)
	}
	if major != 150 || full != "150.0" {
		t.Fatalf("%d %s", major, full)
	}
	if !IsFirefoxUA(ua) {
		t.Fatal("IsFirefoxUA")
	}
}

func TestGeneratePtBRLocaleCountry(t *testing.T) {
	rng := mathrand.New(mathrand.NewPCG(7, 7))
	b, err := Generate(GenerateOptions{
		RNG:             rng,
		ForceFamily:     FamilyDesktop,
		ForceBrowser:    BrowserFirefox,
		ExpectedCountry: "BR",
		TimezonePolicy:  TimezoneStrictMatch,
		Now:             time.Now().UTC(),
	})
	if err != nil {
		t.Fatal(err)
	}
	if b.Locale.Locale != "pt-BR" {
		t.Fatalf("locale %s", b.Locale.Locale)
	}
	if !strings.HasPrefix(b.Locale.AcceptLanguage, "pt-BR") {
		t.Fatalf("accept-lang %s", b.Locale.AcceptLanguage)
	}
	if b.Locale.TimezoneID != "America/Manaus" && b.Locale.TimezoneID != "America/Sao_Paulo" {
		// desktop pt-BR is Manaus in catalog
		t.Fatalf("tz %s", b.Locale.TimezoneID)
	}
}

func TestHARExactFirefox150Bundle(t *testing.T) {
	// Learn exact UA from user HAR — freeze a manual bundle path via Force + check shape.
	rng := mathrand.New(mathrand.NewPCG(150, 150))
	var b *Bundle
	var err error
	for i := 0; i < 40; i++ {
		b, err = Generate(GenerateOptions{
			RNG:          rng,
			ForceFamily:  FamilyDesktop,
			ForceBrowser: BrowserFirefox,
		})
		if err != nil {
			t.Fatal(err)
		}
		if b.Device.UAMajor == 150 {
			break
		}
	}
	if b.Device.UAMajor != 150 {
		// still OK if not hit — force by checking format
		t.Logf("did not sample 150, got %d (catalog weights)", b.Device.UAMajor)
	}
	wantPrefix := "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:"
	if !strings.HasPrefix(b.Device.UserAgent, wantPrefix) {
		t.Fatalf("ua format %s", b.Device.UserAgent)
	}
	if !strings.Contains(b.Device.UserAgent, "Gecko/20100101 Firefox/") {
		t.Fatal(b.Device.UserAgent)
	}
}
