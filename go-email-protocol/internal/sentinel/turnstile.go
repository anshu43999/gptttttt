package sentinel

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"strings"
	"unicode/utf8"
)

// Turnstile holds requirements.turnstile.
type Turnstile struct {
	DX string `json:"dx,omitempty"`
}

// xorCipher matches TS: byte-wise XOR with repeating key (latin1/UTF-8 bytes).
func xorCipher(text, key string) string {
	if key == "" {
		return text
	}
	tb := []byte(text)
	kb := []byte(key)
	out := make([]byte, len(tb))
	for i := range tb {
		out[i] = tb[i] ^ kb[i%len(kb)]
	}
	return string(out)
}

// DecodeTurnstileProgram: base64(dx) → XOR(key) → JSON opcode program.
//
// Key is the requirements REQUEST body field `p` (gAAAAAC…), NOT the response token `c`.
// Live path: same RequirementsToken() value sent as request p and used here.
// Fixture replay: must pass the captured request p.
func DecodeTurnstileProgram(dxB64, key string) ([][]any, error) {
	if strings.TrimSpace(dxB64) == "" {
		return nil, fmt.Errorf("sentinel: empty turnstile.dx")
	}
	if strings.TrimSpace(key) == "" {
		return nil, fmt.Errorf("sentinel: empty turnstile xor key (need requirements request p)")
	}
	raw, err := base64.StdEncoding.DecodeString(dxB64)
	if err != nil {
		raw, err = base64.RawStdEncoding.DecodeString(dxB64)
		if err != nil {
			return nil, fmt.Errorf("sentinel: dx base64: %w", err)
		}
	}
	source := xorCipher(string(raw), key)
	var program [][]any
	if err := json.Unmarshal([]byte(source), &program); err != nil {
		var loose []any
		if err2 := json.Unmarshal([]byte(source), &loose); err2 != nil {
			preview := source
			if len(preview) > 120 {
				preview = preview[:120]
			}
			return nil, fmt.Errorf("sentinel: dx program json: %v preview=%q keyPrefix=%q", err, preview, trimPrefix(key, 24))
		}
		program = make([][]any, 0, len(loose))
		for _, row := range loose {
			arr, ok := row.([]any)
			if !ok {
				return nil, fmt.Errorf("sentinel: dx program row not array")
			}
			program = append(program, arr)
		}
	}
	return program, nil
}

func trimPrefix(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}

func looksLikeEncodedError(value string) bool {
	if value == "" {
		return false
	}
	if strings.Contains(value, "TypeError") ||
		strings.Contains(value, "ReferenceError") ||
		strings.Contains(value, "SyntaxError") {
		return true
	}
	for i := 0; i < len(value) && i < 6; i++ {
		if value[i] == ':' {
			return strings.Contains(value, "Error")
		}
		if value[i] < '0' || value[i] > '9' {
			break
		}
	}
	return false
}

func tryDecodeBase64UTF8(value string) string {
	raw, err := base64.StdEncoding.DecodeString(value)
	if err != nil {
		return ""
	}
	if !utf8.Valid(raw) {
		return string(raw)
	}
	return string(raw)
}

// ComputeTurnstileDx mirrors Node: try pinned SDK first, then XOR+VM.
// xorKey MUST be the requirements request p (gAAAAAC…), same as Node reqToken.
// Response token is unknown here — SDK path uses xorKey as token fallback.
func ComputeTurnstileDx(ctx context.Context, env Env, dxB64, xorKey string) (string, string, error) {
	return ComputeTurnstileDxFull(ctx, env, dxB64, xorKey, xorKey, nil)
}

// ComputeTurnstileDxFull is the Node-faithful entry:
//
//	key / XOR = request p (gAAAAAC…)
//	requirements.token = response c when known
//	requirements.proofofwork optional seed/difficulty map
func ComputeTurnstileDxFull(ctx context.Context, env Env, dxB64, requestP, responseToken string, pow map[string]any) (string, string, error) {
	if strings.TrimSpace(dxB64) == "" {
		return "", "", nil
	}
	if strings.TrimSpace(requestP) == "" {
		return "", "", fmt.Errorf("sentinel: empty turnstile request p")
	}
	select {
	case <-ctx.Done():
		return "", "", ctx.Err()
	default:
	}

	respTok := strings.TrimSpace(responseToken)
	if respTok == "" {
		respTok = requestP
	}

	var sdkErr error
	if out, err := computeTurnstileDxViaSDKWithRequirements(ctx, env, dxB64, requestP, respTok, pow); err == nil {
		decoded := tryDecodeBase64UTF8(out)
		if looksLikeEncodedError(decoded) {
			sdkErr = fmt.Errorf("sdk returned encoded error: %s", decoded)
		} else if len(out) > 8 {
			return out, "sdk", nil
		} else {
			sdkErr = fmt.Errorf("sdk result too short: len=%d", len(out))
		}
	} else {
		sdkErr = err
	}

	program, err := DecodeTurnstileProgram(dxB64, requestP)
	if err != nil {
		return "", "", MapRuntimeFailure(&Error{
			Code:    CodeTurnstileDXFailed,
			Message: fmt.Sprintf("turnstile decode failed after sdk err=%v: %v", sdkErr, err),
		})
	}
	vm := NewTurnstileVM(env)
	// slot 16 used as nested XOR key in opcode 0 — seed with request p
	vm.state[16] = requestP
	encoded, err := vm.Run(ctx, program)
	if err != nil {
		return "", "", MapRuntimeFailure(&Error{
			Code:    CodeTurnstileDXFailed,
			Message: fmt.Sprintf("turnstile vm failed ops=%d sdkErr=%v: %v", len(program), sdkErr, err),
		})
	}
	if len(encoded) <= 8 {
		return "", "", MapRuntimeFailure(&Error{
			Code:    CodeTurnstileDXFailed,
			Message: fmt.Sprintf("turnstile dx result too short ops=%d encoded=%q", len(program), encoded),
		})
	}
	return encoded, "vm", nil
}
