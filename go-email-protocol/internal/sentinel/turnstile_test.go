package sentinel

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"strings"
	"testing"
	"time"
)

func TestLoadPinnedSDK(t *testing.T) {
	src, hash, err := LoadPinnedSDK()
	if err != nil {
		t.Fatal(err)
	}
	if len(src) < 1000 {
		t.Fatalf("sdk too small %d", len(src))
	}
	if len(hash) != 64 {
		t.Fatalf("hash %s", hash)
	}
	if !strings.Contains(src, PatchHook) {
		t.Fatal("missing patch hook")
	}
	if !strings.Contains(src, "SentinelSDK") {
		t.Fatal("missing SentinelSDK")
	}
	patched, err := PatchSDK(src)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(patched, "__codexTurnstileDx") {
		t.Fatal("patch not applied")
	}
	if PinnedSDKVersion != "20260219f9f6" {
		t.Fatal(PinnedSDKVersion)
	}
}

func TestXorCipherRoundTrip(t *testing.T) {
	plain := `[[2,3,"hello-turnstile-token-value"],[35,3]]`
	key := "gAAAAACtestkey"
	enc := xorCipher(plain, key)
	if enc == plain {
		t.Fatal("xor noop")
	}
	if xorCipher(enc, key) != plain {
		t.Fatal("roundtrip")
	}
}

func TestDecodeTurnstileProgram(t *testing.T) {
	// program: set slot 3 value via op2, then settle via op3
	// Note: op2 sets slot from literal; settle op3 takes value arg.
	program := [][]any{
		{float64(2), float64(50), "hello-turnstile-token-value"},
		{float64(3), "hello-turnstile-token-value"},
	}
	raw, err := json.Marshal(program)
	if err != nil {
		t.Fatal(err)
	}
	key := "req-token-key"
	dx := base64.StdEncoding.EncodeToString([]byte(xorCipher(string(raw), key)))
	got, err := DecodeTurnstileProgram(dx, key)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 {
		t.Fatalf("ops %d", len(got))
	}
}

func TestTurnstileVMSynthetic(t *testing.T) {
	env := Env{UserAgent: "UA", Language: "en", ScreenWidth: 100, ScreenHeight: 100}
	// op3 settles with base64(latin1(value))
	program := [][]any{
		{float64(3), "hello-turnstile-token-value"},
	}
	vm := NewTurnstileVM(env)
	out, err := vm.Run(context.Background(), program)
	if err != nil {
		t.Fatal(err)
	}
	// Node encodes settle value as base64
	want := base64.StdEncoding.EncodeToString([]byte("hello-turnstile-token-value"))
	if out != want {
		t.Fatalf("got %q want %q", out, want)
	}
}

func TestComputeTurnstileDxVMPath(t *testing.T) {
	env := Env{
		UserAgent: "UA", Language: "en", Languages: []string{"en"},
		ScreenWidth: 100, ScreenHeight: 100, BuildHash: PinnedSDKVersion,
		ScriptSources: []string{PinnedSDKURL},
	}
	program := [][]any{
		{float64(3), "hello-turnstile-token-value"},
	}
	raw, _ := json.Marshal(program)
	key := "gAAAAACreq"
	dx := base64.StdEncoding.EncodeToString([]byte(xorCipher(string(raw), key)))
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	out, src, err := ComputeTurnstileDx(ctx, env, dx, key)
	if err != nil {
		t.Fatal(err)
	}
	want := base64.StdEncoding.EncodeToString([]byte("hello-turnstile-token-value"))
	if out != want {
		t.Fatalf("out=%q want=%q src=%s", out, want, src)
	}
	if src != "vm" && src != "sdk" {
		t.Fatal(src)
	}
}

func TestAssembleHeaderIncludesT(t *testing.T) {
	hdr, err := AssembleHeaderJSON("gAAAAABp", "turnstile-t-value-xx", "gAAAAACc", "dev", FlowAuthorizeContinue)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(hdr, `"t":"turnstile-t-value-xx"`) {
		t.Fatal(hdr)
	}
}
