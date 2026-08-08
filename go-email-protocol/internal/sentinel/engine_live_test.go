package sentinel

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"
)

// TestEngineRunLiveFixture assembles full openai-sentinel-token from case-001.
func TestEngineRunLiveFixture(t *testing.T) {
	reqP, dx, capturedT, capturedC, flow, deviceID, pow := loadCase001(t)
	if dx == "" || reqP == "" {
		t.Fatal("fixture missing dx/request p")
	}

	body := map[string]any{
		"token":     capturedC,
		"turnstile": map[string]any{"dx": dx},
		"flow":      flow,
	}
	if pow != nil {
		// keep seed; ease difficulty for unit speed
		m := map[string]any{}
		for k, v := range pow {
			m[k] = v
		}
		m["required"] = true
		m["difficulty"] = "0"
		body["proofofwork"] = m
	} else {
		body["proofofwork"] = map[string]any{"required": true, "seed": "seed", "difficulty": "0"}
	}
	raw, err := json.Marshal(body)
	if err != nil {
		t.Fatal(err)
	}

	e := &Engine{Cfg: Config{
		MaxAttempts: 200_000,
		Timeout:     45 * time.Second,
		DeviceID:    deviceID,
		Flow:        flow,
		RequestP:    reqP,
	}}
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	defer cancel()

	res, err := e.Run(ctx, raw)
	if err != nil {
		t.Fatalf("Engine.Run: %v", err)
	}
	if res.HeaderValue == "" {
		t.Fatal("empty header")
	}
	if res.TurnstileSource != "sdk" {
		t.Fatalf("expected sdk source, got %q t_len=%d", res.TurnstileSource, len(res.T))
	}
	if len(res.T) < 200 {
		t.Fatalf("t too short: %d", len(res.T))
	}
	if !strings.HasPrefix(res.P, "gAAAAAB") {
		t.Fatalf("p prefix: %s", trimPrefix(res.P, 20))
	}
	if res.C != capturedC {
		t.Fatalf("c mismatch")
	}
	if res.RequirementsToken != reqP {
		t.Fatalf("request p not preserved")
	}
	var hdr map[string]any
	if err := json.Unmarshal([]byte(res.HeaderValue), &hdr); err != nil {
		t.Fatal(err)
	}
	for _, k := range []string{"p", "t", "c", "id", "flow"} {
		if _, ok := hdr[k]; !ok {
			t.Fatalf("header missing %s: %s", k, res.HeaderValue)
		}
	}
	shared := 0
	n := len(res.T)
	if len(capturedT) < n {
		n = len(capturedT)
	}
	for i := 0; i < n; i++ {
		if res.T[i] != capturedT[i] {
			break
		}
		shared++
	}
	if shared < 30 {
		t.Fatalf("t prefix shared=%d out=%q cap=%q", shared, trimPrefix(res.T, 40), trimPrefix(capturedT, 40))
	}
	t.Logf("header_len=%d t_len=%d shared_prefix=%d src=%s", len(res.HeaderValue), len(res.T), shared, res.TurnstileSource)
}
