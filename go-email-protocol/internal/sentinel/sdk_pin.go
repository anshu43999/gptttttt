package sentinel

import (
	"crypto/sha256"
	"embed"
	"encoding/hex"
	"fmt"
	"os"
	"strings"
	"sync"
)

//go:embed sdk/sdk.js sdk/sdk.sha256 sdk/VERSION
var sdkFS embed.FS

// PinnedSDKVersion is the OpenAI Sentinel build id for the pinned sdk.js.
const PinnedSDKVersion = "20260219f9f6"

// PinnedSDKURL is the upstream URL Node records for this pin.
const PinnedSDKURL = "https://sentinel.openai.com/sentinel/20260219f9f6/sdk.js"

// PatchHook is the exact substring Node patches to inject __codexTurnstileDx.
const PatchHook = "t.init=we,t.sessionObserverToken=async function(t){"

// PatchReplacement injects the codex turnstile entrypoint (same as Node sentinel.ts).
const PatchReplacement = "t.__codexTurnstileDx=function(requirements,key,dx){D(requirements,key);return _n(requirements,dx)},t.init=we,t.sessionObserverToken=async function(t){"

var (
	sdkOnce   sync.Once
	sdkSource string
	sdkHash   string
	sdkErr    error
)

// LoadPinnedSDK returns the embedded sdk.js source after SHA-256 pin check.
func LoadPinnedSDK() (source string, hash string, err error) {
	sdkOnce.Do(func() {
		raw, e := sdkFS.ReadFile("sdk/sdk.js")
		if e != nil {
			sdkErr = fmt.Errorf("sentinel: embed sdk.js: %w", e)
			return
		}
		wantBytes, e := sdkFS.ReadFile("sdk/sdk.sha256")
		if e != nil {
			sdkErr = fmt.Errorf("sentinel: embed sdk.sha256: %w", e)
			return
		}
		want := strings.TrimSpace(string(wantBytes))
		sum := sha256.Sum256(raw)
		got := hex.EncodeToString(sum[:])
		if !strings.EqualFold(got, want) {
			sdkErr = &Error{
				Code:    CodeSDKHashMismatch,
				Message: fmt.Sprintf("embedded sdk.js hash mismatch got=%s want=%s", got, want),
			}
			return
		}
		if !strings.Contains(string(raw), PatchHook) {
			sdkErr = &Error{
				Code:    CodeSDKHookMissing,
				Message: "sdk.js patch hook not found",
			}
			return
		}
		sdkSource = string(raw)
		sdkHash = got
	})
	return sdkSource, sdkHash, sdkErr
}

// PatchSDK injects the Node __codexTurnstileDx hook. Fail-closed if hook missing.
func PatchSDK(source string) (string, error) {
	if source == "" {
		return "", &Error{Code: CodeSDKHookMissing, Message: "empty sdk source"}
	}
	if !strings.Contains(source, PatchHook) {
		return "", &Error{Code: CodeSDKHookMissing, Message: "sdk.js patch hook not found"}
	}
	out := strings.Replace(source, PatchHook, PatchReplacement, 1)
	if out == source {
		return "", &Error{Code: CodeSDKHookMissing, Message: "sdk.js patch failed"}
	}
	return out, nil
}

// PinnedSDKHash returns the expected SHA-256 of the embedded pin.
func PinnedSDKHash() string {
	_, h, err := LoadPinnedSDK()
	if err != nil {
		return ""
	}
	return h
}

// LoadSDKFromPath loads an external sdk.js and verifies it matches the pin
// (optional override for ops; default is embed).
func LoadSDKFromPath(path string) (string, string, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return "", "", err
	}
	sum := sha256.Sum256(raw)
	got := hex.EncodeToString(sum[:])
	want := PinnedSDKHash()
	if want != "" && !strings.EqualFold(got, want) {
		return "", "", &Error{
			Code:    CodeSDKHashMismatch,
			Message: fmt.Sprintf("external sdk.js hash mismatch got=%s want=%s", got, want),
		}
	}
	if !strings.Contains(string(raw), PatchHook) {
		return "", "", &Error{Code: CodeSDKHookMissing, Message: "sdk.js patch hook not found"}
	}
	return string(raw), got, nil
}
