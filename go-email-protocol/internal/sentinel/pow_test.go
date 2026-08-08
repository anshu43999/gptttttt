package sentinel

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/gpt-register/go-email-protocol/internal/fingerprint"
	mathrand "math/rand/v2"
)

func TestCollectFingerprintLen25(t *testing.T) {
	env := Env{
		UserAgent: "UA", Language: "en-US", Languages: []string{"en-US", "en"},
		ScreenWidth: 1920, ScreenHeight: 1080, JSHeapSizeLimit: 4294967296,
		HardwareConcurrency: 8, ScriptSources: []string{"a"}, DocumentKeys: []string{"b"},
		WindowKeys: []string{"c"}, SearchParamKeys: []string{}, BuildHash: "h", TimeOrigin: 1,
	}
	data := CollectFingerprintData(env, "sid-1", mathrand.New(mathrand.NewPCG(1, 2)))
	if len(data) != PayloadIndexCount {
		t.Fatalf("len %d", len(data))
	}
	if data[0] != 1920+1080 {
		t.Fatalf("sum %v", data[0])
	}
	if data[4] != "UA" {
		t.Fatalf("ua %v", data[4])
	}
	if data[14] != "sid-1" {
		t.Fatalf("sid %v", data[14])
	}
	// fixed flags
	if data[18] != 0 || data[19] != 1 || data[24] != 1 {
		t.Fatalf("flags %v", data[18:25])
	}
}

func TestSentinelHashHexStable(t *testing.T) {
	// known vector: empty string
	h := sentinelHashHex("")
	if len(h) != 8 {
		t.Fatalf("len %s", h)
	}
	// same input same output
	if sentinelHashHex("abc") != sentinelHashHex("abc") {
		t.Fatal("unstable")
	}
	if sentinelHashHex("abc") == sentinelHashHex("abd") {
		t.Fatal("collision trivial")
	}
}

func TestGenerateAnswerDifficultyZero(t *testing.T) {
	env := Env{
		UserAgent: "UA", Language: "en", Languages: []string{"en"},
		ScreenWidth: 100, ScreenHeight: 100, JSHeapSizeLimit: 1,
		HardwareConcurrency: 4, ScriptSources: []string{"s"}, DocumentKeys: []string{"d"},
		WindowKeys: []string{"w"}, BuildHash: "b", TimeOrigin: 1,
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	ans, n, err := GenerateAnswer(ctx, env, "sid", "seed", "0", 100000)
	if err != nil {
		t.Fatal(err)
	}
	if n < 1 || !strings.HasSuffix(ans, "~S") {
		t.Fatalf("ans=%s n=%d", ans, n)
	}
	// prefix before ~S is base64 JSON array
	raw := strings.TrimSuffix(ans, "~S")
	bin, err := base64.StdEncoding.DecodeString(raw)
	if err != nil {
		t.Fatal(err)
	}
	var arr []any
	if err := json.Unmarshal(bin, &arr); err != nil {
		t.Fatal(err)
	}
	if len(arr) != 25 {
		t.Fatalf("decoded len %d", len(arr))
	}
}

func TestEnforcementAndRequirementsPrefix(t *testing.T) {
	b, err := fingerprint.Generate(fingerprint.GenerateOptions{
		RNG: mathrand.New(mathrand.NewPCG(9, 9)), ForceFamily: fingerprint.FamilyDesktop,
	})
	if err != nil {
		t.Fatal(err)
	}
	env := EnvFromBundle(b)
	ctx := context.Background()
	sid := NewSID()
	reqTok, err := RequirementsToken(ctx, env, sid, 100000)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(reqTok, "gAAAAAC") {
		t.Fatal(reqTok[:10])
	}
	enf, err := EnforcementToken(ctx, env, sid, "powseed", "0", 100000)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(enf, "gAAAAAB") {
		t.Fatal(enf[:10])
	}
	hdr, err := AssembleHeaderJSON(enf, "", reqTok, "device-1", FlowAuthorizeContinue)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(hdr, `"p"`) || !strings.Contains(hdr, FlowAuthorizeContinue) {
		t.Fatal(hdr)
	}
}

func TestNavigatorPropertyUnicodeMinus(t *testing.T) {
	s := randomNavigatorProperty(mathrand.New(mathrand.NewPCG(1, 1)), Env{
		UserAgent: "U", Language: "en", HardwareConcurrency: 2,
	})
	if !strings.Contains(s, "\u2212") {
		t.Fatalf("want unicode minus: %q", s)
	}
}

func TestCollectFingerprintHARFirefox(t *testing.T) {
	env := Env{
		UserAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:150.0) Gecko/20100101 Firefox/150.0",
		Language: "pt-BR", Languages: []string{"pt-BR", "pt", "en-US", "en"},
		ScreenWidth: 1920, ScreenHeight: 1080, JSHeapSizeLimit: 0,
		HardwareConcurrency: 12, ScriptSources: []string{"https://sentinel.openai.com/sentinel/20260219f9f6/sdk.js"},
		DocumentKeys: []string{"__reactContainer$x"}, WindowKeys: []string{"ondragexit"},
		SearchParamKeys: []string{}, BuildHash: "20260219f9f6", TimeOrigin: 1,
		TimezoneID: "America/Manaus", HeapNull: true, BuildNull: true, FlagsHAR: true,
	}
	data := CollectFingerprintData(env, "sid-har", mathrand.New(mathrand.NewPCG(1, 2)))
	if data[2] != nil {
		t.Fatalf("heap want null got %v", data[2])
	}
	if data[6] != nil {
		t.Fatalf("build want null got %v", data[6])
	}
	// flags HAR 0,0,0,0,0,1,1
	want := []any{0, 0, 0, 0, 0, 1, 1}
	for i := 0; i < 7; i++ {
		if data[18+i] != want[i] {
			t.Fatalf("flag[%d]=%v want %v full=%v", 18+i, data[18+i], want[i], data[18:25])
		}
	}
	ds, _ := data[1].(string)
	if !strings.Contains(ds, "GMT") {
		t.Fatalf("date string %q", ds)
	}
}

func TestAssembleSOHeaderJSON(t *testing.T) {
	s, err := AssembleSOHeaderJSON("SOraw", "gAAAAACc", "dev", "oauth_create_account")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(s, `"so":"SOraw"`) || !strings.Contains(s, `"flow":"oauth_create_account"`) {
		t.Fatal(s)
	}
	empty, err := AssembleSOHeaderJSON("", "c", "id", "f")
	if err != nil || empty != "" {
		t.Fatalf("empty so: %q %v", empty, err)
	}
}

func TestEnvFromBundleFirefoxHARFlags(t *testing.T) {
	b, err := fingerprint.Generate(fingerprint.GenerateOptions{
		ForceFamily: fingerprint.FamilyDesktop, ForceBrowser: fingerprint.BrowserFirefox,
	})
	if err != nil {
		t.Fatal(err)
	}
	env := EnvFromBundle(b)
	if !env.HeapNull || !env.BuildNull || !env.FlagsHAR {
		t.Fatalf("firefox env flags HeapNull=%v BuildNull=%v FlagsHAR=%v", env.HeapNull, env.BuildNull, env.FlagsHAR)
	}
	data := CollectFingerprintData(env, "s", mathrand.New(mathrand.NewPCG(3, 4)))
	if data[2] != nil || data[6] != nil {
		t.Fatalf("payload nulls heap=%v build=%v", data[2], data[6])
	}
}

