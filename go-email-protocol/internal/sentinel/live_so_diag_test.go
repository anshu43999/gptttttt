package sentinel

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestLiveWireSOCompute(t *testing.T) {
	wire := filepath.Join("..", "..", "..", "cases", "sentinel-har-align", "wire", "20260718_181348")
	reqBody, err := os.ReadFile(filepath.Join(wire, "101505.491_S5_req.body.bin"))
	if err != nil {
		t.Fatal(err)
	}
	respBody, err := os.ReadFile(filepath.Join(wire, "101505.491_S5_resp.body.bin"))
	if err != nil {
		t.Fatal(err)
	}
	var reqWire struct {
		P    string `json:"p"`
		Flow string `json:"flow"`
		ID   string `json:"id"`
	}
	if err := json.Unmarshal(reqBody, &reqWire); err != nil {
		t.Fatal(err)
	}
	req, err := ParseRequirements(respBody)
	if err != nil {
		t.Fatal(err)
	}
	if req.SO == nil {
		t.Fatal("no so")
	}
	dxLen := 0
	if req.Turnstile != nil {
		dxLen = len(req.Turnstile.DX)
	}
	t.Logf("flow=%s id=%s p_len=%d token_len=%d collector=%d snapshot=%d dx=%d",
		reqWire.Flow, reqWire.ID, len(reqWire.P), len(req.Token), len(req.SO.CollectorDX), len(req.SO.SnapshotDX), dxLen)

	env := Env{
		UserAgent:     "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
		Language:      "ja",
		Languages:     []string{"ja", "en-US", "en"},
		TimezoneID:    "Asia/Tokyo",
		ScreenWidth:   1920,
		ScreenHeight:  1080,
		BuildHash:     PinnedSDKVersion,
		ScriptSources: []string{PinnedSDKURL},
		HeapNull:      true,
		BuildNull:     true,
		FlagsHAR:      true,
	}
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	defer cancel()

	v, e := computeSOViaSDK(ctx, env, req.SO, reqWire.P, req.Token, reqWire.Flow)
	t.Logf("sdk so err=%v val_len=%d valid=%v head=%q", e, len(v), soLooksValid(v), trimHead(v, 80))

	v2, src, e2 := ComputeTurnstileDxFull(ctx, env, req.SO.CollectorDX, reqWire.P, req.Token, nil)
	t.Logf("vm collector err=%v src=%s val_len=%d valid=%v head=%q", e2, src, len(v2), soLooksValid(v2), trimHead(v2, 80))

	v3, src3, e3 := ComputeTurnstileDxFull(ctx, env, req.SO.SnapshotDX, reqWire.P, req.Token, nil)
	t.Logf("vm snapshot err=%v src=%s val_len=%d valid=%v head=%q", e3, src3, len(v3), soLooksValid(v3), trimHead(v3, 80))

	v4, src4, e4 := ComputeSessionObserverSO(ctx, env, req.SO, reqWire.P, req.Token, reqWire.Flow)
	t.Logf("full so err=%v src=%s val_len=%d head=%q", e4, src4, len(v4), trimHead(v4, 80))
}

func TestHARReplaySOCompute(t *testing.T) {
	dir := filepath.Join("..", "..", "..", "cases", "sentinel-har-align", "so_samples")
	reqBody, err := os.ReadFile(filepath.Join(dir, "har_prior_req_body.json"))
	if err != nil {
		t.Skip(err)
	}
	respBody, err := os.ReadFile(filepath.Join(dir, "har_prior_requirements.json"))
	if err != nil {
		t.Skip(err)
	}
	goldRaw, err := os.ReadFile(filepath.Join(dir, "har_create_account_so.json"))
	if err != nil {
		t.Skip(err)
	}
	var reqWire struct {
		P    string `json:"p"`
		Flow string `json:"flow"`
		ID   string `json:"id"`
	}
	if err := json.Unmarshal(reqBody, &reqWire); err != nil {
		t.Fatal(err)
	}
	req, err := ParseRequirements(respBody)
	if err != nil {
		t.Fatal(err)
	}
	var goldWrap struct {
		SOHeader string `json:"so_header"`
	}
	if err := json.Unmarshal(goldRaw, &goldWrap); err != nil {
		t.Fatal(err)
	}
	var gold map[string]any
	if err := json.Unmarshal([]byte(goldWrap.SOHeader), &gold); err != nil {
		t.Fatal(err)
	}
	goldSO, _ := gold["so"].(string)
	t.Logf("gold so len=%d head=%s", len(goldSO), trimHead(goldSO, 60))
	t.Logf("har p_len=%d token_len=%d collector=%d snapshot=%d", len(reqWire.P), len(req.Token), len(req.SO.CollectorDX), len(req.SO.SnapshotDX))

	env := Env{
		UserAgent:     "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:150.0) Gecko/20100101 Firefox/150.0",
		Language:      "pt-BR",
		Languages:     []string{"pt-BR", "pt", "en-US", "en"},
		TimezoneID:    "America/Manaus",
		ScreenWidth:   1920,
		ScreenHeight:  1080,
		BuildHash:     PinnedSDKVersion,
		ScriptSources: []string{PinnedSDKURL},
		HeapNull:      true,
		BuildNull:     true,
		FlagsHAR:      true,
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	// Direct SDK path only — no Turnstile _n fallback pollution.
	sdkVal, sdkErr := computeSOViaSDK(ctx, env, req.SO, reqWire.P, req.Token, "oauth_create_account")
	t.Logf("sdk-only err=%v val_len=%d head=%q", sdkErr, len(sdkVal), trimHead(sdkVal, 80))
	if sdkErr == nil {
		t.Logf("sdk==gold %v sdk_len=%d gold_len=%d", sdkVal == goldSO, len(sdkVal), len(goldSO))
		if sdkVal != goldSO && len(sdkVal) > 0 && len(goldSO) > 0 {
			// common prefix of decoded bytes
			gb, _ := decodeB64Loose(goldSO)
			sb, _ := decodeB64Loose(sdkVal)
			n := 0
			for n < len(gb) && n < len(sb) && gb[n] == sb[n] {
				n++
			}
			t.Logf("decoded prefix match %d/%d gold %d sdk", n, len(gb), len(sb))
		}
	}

	v, src, err := ComputeSessionObserverSO(ctx, env, req.SO, reqWire.P, req.Token, "oauth_create_account")
	t.Logf("compute err=%v src=%s val_len=%d head=%q", err, src, len(v), trimHead(v, 80))
	if err == nil && v == goldSO {
		t.Log("EXACT MATCH gold so")
	} else if err == nil {
		t.Logf("mismatch equal=%v", v == goldSO)
	}
}

func decodeB64Loose(s string) ([]byte, error) {
	pad := (4 - len(s)%4) % 4
	return base64.StdEncoding.DecodeString(s + strings.Repeat("=", pad))
}

func trimHead(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}
