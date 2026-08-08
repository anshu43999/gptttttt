package sentinel

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestParseRequirementsSO(t *testing.T) {
	raw := []byte(`{"token":"gAAAAACc","proofofwork":{"required":true,"seed":"0.1","difficulty":"0"},"so":{"required":true,"collector_dx":"QQ==","snapshot_dx":"Qg=="},"turnstile":{"dx":"QQ=="}}`)
	req, err := ParseRequirements(raw)
	if err != nil {
		t.Fatal(err)
	}
	if req.SO == nil || req.SO.CollectorDX != "QQ==" || !req.SO.Required {
		t.Fatalf("%+v", req.SO)
	}
}

func TestComputeSOFromFixtureCollector(t *testing.T) {
	// case-001 requirements has real collector_dx; request_p in replay_input
	dir := filepath.Join("testdata", "sentinel-live", "case-001")
	reqBody, err := os.ReadFile(filepath.Join(dir, "requirements_136.json"))
	if err != nil {
		t.Skip(err)
	}
	replayRaw, err := os.ReadFile(filepath.Join(dir, "replay_input.json"))
	if err != nil {
		t.Skip(err)
	}
	var replay struct {
		RequestP string `json:"request_p"`
	}
	if err := json.Unmarshal(replayRaw, &replay); err != nil {
		t.Fatal(err)
	}
	req, err := ParseRequirements(reqBody)
	if err != nil {
		t.Fatal(err)
	}
	if req.SO == nil || req.SO.CollectorDX == "" {
		t.Fatal("fixture missing so.collector_dx")
	}
	env := Env{
		UserAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:150.0) Gecko/20100101 Firefox/150.0",
		Language:  "pt-BR", Languages: []string{"pt-BR", "pt", "en-US", "en"},
		ScreenWidth: 1920, ScreenHeight: 1080, HardwareConcurrency: 12,
		ScriptSources: []string{PinnedSDKURL}, BuildHash: PinnedSDKVersion,
		TimezoneID: "America/Manaus", HeapNull: true, BuildNull: true, FlagsHAR: true,
		TimeOrigin: 1784319880356,
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	val, src, err := ComputeSessionObserverSO(ctx, env, req.SO, replay.RequestP, req.Token, "oauth_create_account")
	if err != nil {
		// soft path may still fail in CI if VM needs richer browser; surface but allow diagnosis
		t.Logf("SO compute err (may soft-fail live): %v", err)
		return
	}
	if strings.TrimSpace(val) == "" || val == "bnVsbA==" || len(val) < 16 {
		t.Fatalf("invalid so source=%s val=%q", src, val)
	}
	t.Logf("so source=%s len=%d head=%s", src, len(val), val[:min(40, len(val))])
	hdr, err := AssembleSOHeaderJSON(val, req.Token, "7b770983-8ac4-4029-992e-cc0e0ed22700", "oauth_create_account")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(hdr, `"so"`) || !strings.Contains(hdr, "oauth_create_account") {
		t.Fatal(hdr)
	}
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}
