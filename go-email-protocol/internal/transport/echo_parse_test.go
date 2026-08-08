package transport

import (
	"encoding/json"
	"testing"
)

// Offline D5: parse shapes of public echo endpoints without network.
// Live ProbeTLSEcho is under //go:build tlsclient + network.

func TestParsePeetEchoShape(t *testing.T) {
	// Minimal peet.ws/api/all shape (fields we care about).
	raw := []byte(`{
	  "http_version": "h2",
	  "tls": {
	    "ja3_hash": "deadbeefdeadbeefdeadbeefdeadbeef",
	    "ja4": "t13d1516h2_8daaf6152771_02713d6af862",
	    "tls_version_negotiated": "771",
	    "ciphers": ["a","b","c","d","e","f","g","h"]
	  }
	}`)
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		t.Fatal(err)
	}
	snap := extractEchoSnapshot("Chrome_133", m, "HTTP/2.0")
	if snap.HTTPVersion != "h2" {
		t.Fatalf("http=%s", snap.HTTPVersion)
	}
	if snap.JA3Hash != "deadbeefdeadbeefdeadbeefdeadbeef" {
		t.Fatalf("ja3=%s", snap.JA3Hash)
	}
	if snap.JA4 == "" || snap.CipherCount != 8 {
		t.Fatalf("%+v", snap)
	}
}

func TestParseBrowserleaksEchoShape(t *testing.T) {
	raw := []byte(`{
	  "user_agent": "Mozilla/5.0",
	  "ja3_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
	  "ja4": "t13d311200_e8f1e7e78f70_550dd08",
	  "tls_version": "TLS 1.3"
	}`)
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		t.Fatal(err)
	}
	snap := extractEchoSnapshot("Chrome_133", m, "HTTP/2.0")
	if snap.JA3Hash == "" || snap.JA4 == "" {
		t.Fatalf("%+v", snap)
	}
	if snap.HTTPVersion == "" {
		t.Fatalf("expected proto fallback: %+v", snap)
	}
}
