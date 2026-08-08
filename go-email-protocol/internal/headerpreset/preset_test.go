package headerpreset

import (
	"strings"
	"testing"

	"github.com/gpt-register/go-email-protocol/internal/fingerprint"
	mathrand "math/rand/v2"
)

func testBundle(t *testing.T) *fingerprint.Bundle {
	t.Helper()
	// Legacy tests assume Chromium Client Hints; pin Chrome so random Firefox mix does not break them.
	b, err := fingerprint.Generate(fingerprint.GenerateOptions{
		RNG:          mathrand.New(mathrand.NewPCG(42, 7)),
		ForceFamily:  fingerprint.FamilyDesktop,
		ForceBrowser: fingerprint.BrowserChrome,
	})
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func TestDocumentNavigationOrder(t *testing.T) {
	b := testBundle(t)
	hs, err := Build(DocumentNavigation, b, nil, Options{})
	if err != nil {
		t.Fatal(err)
	}
	keys := Keys(hs)
	// identity block first
	if keys[0] != "user-agent" || keys[1] != "accept-language" {
		t.Fatalf("keys prefix %v", keys[:4])
	}
	if !contains(keys, "sec-fetch-dest") || !contains(keys, "sec-ch-ua") {
		t.Fatalf("missing keys %v", keys)
	}
	// no content-type
	if contains(keys, "content-type") {
		t.Fatal("content-type forbidden on document")
	}
	m := Map(hs)
	if m["user-agent"] != b.Device.UserAgent {
		t.Fatal("ua mismatch")
	}
	if m["sec-fetch-mode"] != "navigate" {
		t.Fatal(m["sec-fetch-mode"])
	}
}

func TestOTPSparseOmitsClientHints(t *testing.T) {
	b := testBundle(t)
	hs, err := Build(OTPSparse, b, map[string]string{
		"origin":  "https://auth.openai.com",
		"referer": "https://auth.openai.com/email-verification",
	}, Options{})
	if err != nil {
		t.Fatal(err)
	}
	for _, k := range Keys(hs) {
		if strings.HasPrefix(k, "sec-ch-") {
			t.Fatalf("otp_sparse must omit CH, got %s in %v", k, Keys(hs))
		}
	}
	if !contains(Keys(hs), "user-agent") {
		t.Fatal("need ua")
	}
}

func TestForbiddenOverrideRejected(t *testing.T) {
	b := testBundle(t)
	_, err := Build(DocumentNavigation, b, map[string]string{"content-type": "text/plain"}, Options{})
	if err == nil {
		t.Fatal("expected forbidden")
	}
}

func TestDatadogDefaultOff(t *testing.T) {
	b := testBundle(t)
	hs, err := Build(SameOriginFetch, b, nil, Options{})
	if err != nil {
		t.Fatal(err)
	}
	for _, k := range Keys(hs) {
		if strings.HasPrefix(k, "x-datadog") {
			t.Fatal("datadog should be off")
		}
	}
}

func TestDatadogOnFreshIDs(t *testing.T) {
	b := testBundle(t)
	a, err := Build(SameOriginFetch, b, nil, Options{DatadogRUM: true})
	if err != nil {
		t.Fatal(err)
	}
	c, err := Build(SameOriginFetch, b, nil, Options{DatadogRUM: true})
	if err != nil {
		t.Fatal(err)
	}
	if Map(a)["x-datadog-trace-id"] == "" {
		t.Fatal("missing trace")
	}
	if Map(a)["x-datadog-trace-id"] == Map(c)["x-datadog-trace-id"] {
		t.Fatal("trace ids must not be constant across builds")
	}
}

func TestSentinelRequiresCH(t *testing.T) {
	b := testBundle(t)
	hs, err := Build(SentinelReq, b, map[string]string{
		"origin": "https://chatgpt.com",
	}, Options{})
	if err != nil {
		t.Fatal(err)
	}
	if !contains(Keys(hs), "sec-ch-ua") {
		t.Fatal(Keys(hs))
	}
}

func contains(ss []string, x string) bool {
	for _, s := range ss {
		if s == x {
			return true
		}
	}
	return false
}
