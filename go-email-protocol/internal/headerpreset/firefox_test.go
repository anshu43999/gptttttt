package headerpreset

import (
	"strings"
	"testing"
	"time"

	"github.com/gpt-register/go-email-protocol/internal/fingerprint"
	mathrand "math/rand/v2"
)

func TestFirefoxSentinelReqNoClientHints(t *testing.T) {
	rng := mathrand.New(mathrand.NewPCG(1, 2))
	b, err := fingerprint.Generate(fingerprint.GenerateOptions{
		RNG:          rng,
		ForceFamily:  fingerprint.FamilyDesktop,
		ForceBrowser: fingerprint.BrowserFirefox,
		Now:          time.Date(2026, 7, 17, 0, 0, 0, 0, time.UTC),
	})
	if err != nil {
		t.Fatal(err)
	}
	hs, err := Build(SentinelReq, b, map[string]string{
		"origin":  "https://sentinel.openai.com",
		"referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html?sv=20260219f9f6",
	}, Options{})
	if err != nil {
		t.Fatal(err)
	}
	keys := Keys(hs)
	for _, k := range keys {
		if strings.HasPrefix(k, "sec-ch-") {
			t.Fatalf("firefox must not send %s in %v", k, keys)
		}
	}
	m := Map(hs)
	if !strings.Contains(m["user-agent"], "Firefox/") {
		t.Fatal(m["user-agent"])
	}
	if m["content-type"] != "text/plain;charset=UTF-8" {
		t.Fatal(m["content-type"])
	}
	if !strings.Contains(m["accept-encoding"], "zstd") {
		t.Fatal(m["accept-encoding"])
	}
	// order: user-agent before accept-language (HAR)
	uaIdx, alIdx := -1, -1
	for i, k := range keys {
		if k == "user-agent" {
			uaIdx = i
		}
		if k == "accept-language" {
			alIdx = i
		}
	}
	if uaIdx < 0 || alIdx < 0 || uaIdx > alIdx {
		t.Fatalf("order %v", keys)
	}
}

func TestFirefoxDocumentAccept(t *testing.T) {
	rng := mathrand.New(mathrand.NewPCG(3, 4))
	b, err := fingerprint.Generate(fingerprint.GenerateOptions{
		RNG: rng, ForceFamily: fingerprint.FamilyDesktop, ForceBrowser: fingerprint.BrowserFirefox,
	})
	if err != nil {
		t.Fatal(err)
	}
	hs, err := Build(DocumentNavigation, b, nil, Options{})
	if err != nil {
		t.Fatal(err)
	}
	m := Map(hs)
	// HAR: no image/avif in Firefox document accept
	if strings.Contains(m["accept"], "image/avif") {
		t.Fatalf("firefox accept should be simple: %s", m["accept"])
	}
	if !strings.Contains(m["accept"], "text/html") {
		t.Fatal(m["accept"])
	}
}

func TestChromeStillRequiresCH(t *testing.T) {
	rng := mathrand.New(mathrand.NewPCG(5, 6))
	b, err := fingerprint.Generate(fingerprint.GenerateOptions{
		RNG: rng, ForceFamily: fingerprint.FamilyDesktop, ForceBrowser: fingerprint.BrowserChrome,
	})
	if err != nil {
		t.Fatal(err)
	}
	hs, err := Build(SentinelReq, b, map[string]string{"origin": "https://sentinel.openai.com"}, Options{})
	if err != nil {
		t.Fatal(err)
	}
	m := Map(hs)
	if m["sec-ch-ua"] == "" {
		t.Fatal("chrome must still emit sec-ch-ua")
	}
}
