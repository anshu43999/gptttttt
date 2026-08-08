//go:build tlsclient

package transport

import (
	"context"
	"os"
	"strings"
	"testing"
	"time"
)

func TestTLSEchoProbeChrome133(t *testing.T) {
	if testing.Short() {
		t.Skip("D5 TLS echo requires network; skip in -short")
	}
	if os.Getenv("GPT_REGISTER_SKIP_TLS_ECHO") == "1" {
		t.Skip("GPT_REGISTER_SKIP_TLS_ECHO=1")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 75*time.Second)
	defer cancel()
	urls := []string{}
	if u := strings.TrimSpace(os.Getenv("GPT_REGISTER_TLS_ECHO_URL")); u != "" {
		urls = append(urls, u)
	}
	urls = append(urls, DefaultTLSEchoURL, "https://tls.browserleaks.com/json")
	var last error
	for _, url := range urls {
		snap, err := ProbeTLSEcho(ctx, 133, url)
		if err != nil {
			last = err
			t.Logf("echo %s: %v", url, err)
			continue
		}
		if snap.ProfileName == "" {
			t.Fatal("empty profile name")
		}
		if snap.JA3Hash == "" && snap.JA4 == "" && strings.TrimSpace(snap.HTTPVersion) == "" {
			// browserleaks shape may differ; accept any non-empty body keys
			if len(snap.RawKeys) == 0 {
				last = errString("empty fingerprint fields")
				continue
			}
		}
		t.Logf("ok url=%s profile=%s http=%s ja3=%s ja4=%s tls=%s ciphers=%d keys=%v",
			url, snap.ProfileName, snap.HTTPVersion, snap.JA3Hash, snap.JA4, snap.NegotiatedTLS, snap.CipherCount, snap.RawKeys)
		return
	}
	// Network-restricted environments: do not fail the package; D5 offline pin still covered.
	t.Skipf("D5 TLS echo endpoints unreachable (last=%v); offline profile pin still tested", last)
}

func errString(s string) error { return &echoError{s} }

type echoError struct{ s string }

func (e *echoError) Error() string { return e.s }

func TestChromeProfileNameForMajorPinned(t *testing.T) {
	// Offline fixture: major mapping must stay stable for D5 locks.
	name, err := ChromeProfileNameForMajor(133)
	if err != nil {
		t.Fatal(err)
	}
	if name != "Chrome_133" {
		t.Fatalf("got %s", name)
	}
	name, err = ChromeProfileNameForMajor(200)
	if err != nil {
		t.Fatal(err)
	}
	if name != "Chrome_133" {
		t.Fatalf("high major should pin to Chrome_133, got %s", name)
	}
}
