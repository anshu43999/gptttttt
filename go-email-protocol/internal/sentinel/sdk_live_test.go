package sentinel

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestSDKPathOnLiveFixture(t *testing.T) {
	reqP, dx, capturedT, capturedC, _, _, pow := loadCase001(t)
	env := Env{
		UserAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:150.0) Gecko/20100101 Firefox/150.0",
		Language:  "pt-BR", Languages: []string{"pt-BR", "pt", "en-US", "en"},
		ScreenWidth: 1280, ScreenHeight: 720, HardwareConcurrency: 12,
		BuildHash: PinnedSDKVersion, ScriptSources: []string{PinnedSDKURL},
		TimeOrigin: 1784319880356, JSHeapSizeLimit: 0,
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()

	// Match Node: key = request p; requirements.token = response c
	out, err := computeTurnstileDxViaSDKWithRequirements(ctx, env, dx, reqP, capturedC, pow)
	if err != nil {
		t.Fatalf("sdk_err=%v", err)
	}
	if len(out) < 200 {
		t.Fatalf("sdk t too short: len=%d out=%q", len(out), trimPrefix(out, 40))
	}
	if dec := tryDecodeBase64UTF8(out); looksLikeEncodedError(dec) {
		t.Fatalf("sdk returned encoded error: %s", dec)
	}
	// Capture is env-bound; require shared high-entropy prefix, not full equality.
	shared := 0
	n := len(out)
	if len(capturedT) < n {
		n = len(capturedT)
	}
	for i := 0; i < n; i++ {
		if out[i] != capturedT[i] {
			break
		}
		shared++
	}
	if shared < 40 {
		t.Fatalf("prefix mismatch shared=%d out=%q cap=%q", shared, trimPrefix(out, 40), trimPrefix(capturedT, 40))
	}
	t.Logf("sdk_out_len=%d shared_prefix=%d cap_len=%d out_prefix=%q", len(out), shared, len(capturedT), trimPrefix(out, 40))

	full, src, err2 := ComputeTurnstileDx(ctx, env, dx, reqP)
	if err2 != nil {
		t.Fatalf("ComputeTurnstileDx: %v", err2)
	}
	if src != "sdk" {
		t.Fatalf("expected sdk source, got %q len=%d", src, len(full))
	}
	if len(full) < 200 {
		t.Fatalf("ComputeTurnstileDx short: len=%d", len(full))
	}
	_ = os.WriteFile(filepath.Join("testdata", "sentinel-live", "case-001", "last_sdk_out.txt"), []byte(out), 0o644)
}
