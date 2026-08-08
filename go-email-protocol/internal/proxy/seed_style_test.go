package proxy

import "testing"

func TestNextSeedStyleRoundRobin(t *testing.T) {
	styles := []string{"bestgo", "1024"}
	if got := NextSeedStyle(styles, ""); got != "bestgo" {
		t.Fatalf("empty → %s", got)
	}
	if got := NextSeedStyle(styles, "bestgo"); got != "1024" {
		t.Fatalf("bestgo → %s", got)
	}
	if got := NextSeedStyle(styles, "1024"); got != "bestgo" {
		t.Fatalf("1024 → %s", got)
	}
	// lajiao is 1024 alias
	if got := NextSeedStyle(styles, "lajiao"); got != "bestgo" {
		t.Fatalf("lajiao → %s want bestgo", got)
	}
}

func TestNormalizeStyleList(t *testing.T) {
	got := NormalizeStyleList([]string{"bestgo,1024", "BESTGO", " 1024 "})
	if len(got) != 2 || got[0] != "bestgo" || got[1] != "1024" {
		t.Fatalf("got %#v", got)
	}
}

func TestStyleFromProxyURL(t *testing.T) {
	if got := StyleFromProxyURL("socks5h://USER-zone-custom-region-JP-session-abc@us.rrp.bestgo.work:10000"); got != "bestgo" {
		t.Fatalf("got %q", got)
	}
	if got := StyleFromProxyURL("socks5h://acc-region-US-sid-x-t-15@us.1024proxy.io:3000"); got != "lajiao" {
		t.Fatalf("got %q", got)
	}
}
