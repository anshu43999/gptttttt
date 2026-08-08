package proxy

import "testing"

func TestNormalizeURL(t *testing.T) {
	if got := NormalizeURL("user:pass@host:7878", "socks5"); got != "socks5://user:pass@host:7878" {
		t.Fatalf("got %q", got)
	}
	if got := NormalizeURL("socks5://user:pass@host:7878", ""); got != "socks5://user:pass@host:7878" {
		t.Fatalf("got %q", got)
	}
	if got := NormalizeURL("user:pass@host:7878", "http"); !stringsHasPrefix(got, "socks5://") {
		t.Fatalf("expected socks5 force, got %q", got)
	}
}

func stringsHasPrefix(s, p string) bool {
	return len(s) >= len(p) && s[:len(p)] == p
}
