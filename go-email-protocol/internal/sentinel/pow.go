package sentinel

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"math"
	mathrand "math/rand/v2"
	"strings"
	"time"

	"github.com/gpt-register/go-email-protocol/internal/fingerprint"
	"github.com/google/uuid"
)

// PayloadIndexCount is fixed 25 (indices 0–24) matching TS collectFingerprintData.
// (Also declared in types.go as PayloadIndexCount — keep value 25.)

// Env is the browser-like environment projected into the 25-item payload.
type Env struct {
	UserAgent           string
	Language            string
	Languages           []string
	ScreenWidth         int
	ScreenHeight        int
	JSHeapSizeLimit     int64
	HardwareConcurrency int
	ScriptSources       []string
	DocumentKeys        []string
	WindowKeys          []string
	SearchParamKeys     []string
	// BuildHash is used by Turnstile/SDK frame URL; payload[6] may still be null (HAR Firefox).
	BuildHash string
	TimeOrigin float64
	// TimezoneID drives payload[1] Date().toString()-like rendering (IANA, e.g. America/Manaus).
	TimezoneID string
	// HeapNull / BuildNull: emit JSON null at payload[2]/[6] (Firefox HAR 2026-07-17).
	HeapNull  bool
	BuildNull bool
	// FlagsHAR: when true use HAR Firefox tail flags [18..24]=0,0,0,0,0,1,1
	// instead of legacy Node collectFingerprintData 0,1,1,0,0,0,1.
	FlagsHAR bool
}

// EnvFromBundle maps FingerprintBundle / SentinelEnv into PoW Env.
// Firefox profiles follow the 2026-07-17 HAR capture (null heap/build, HAR flags).
func EnvFromBundle(b *fingerprint.Bundle) Env {
	if b == nil {
		return Env{}
	}
	se := b.SentinelEnv
	ua := firstNonEmpty(se.UserAgent, b.Device.UserAgent)
	env := Env{
		UserAgent:           ua,
		Language:            firstNonEmpty(se.Language, b.Locale.Locale),
		Languages:           append([]string(nil), se.Languages...),
		ScreenWidth:         se.ScreenWidth,
		ScreenHeight:        se.ScreenHeight,
		JSHeapSizeLimit:     se.JSHeapSizeLimit,
		HardwareConcurrency: se.HardwareConcurrency,
		ScriptSources:       append([]string(nil), se.ScriptSources...),
		DocumentKeys:        append([]string(nil), se.DocumentKeys...),
		WindowKeys:          append([]string(nil), se.WindowKeys...),
		SearchParamKeys:     append([]string(nil), se.SearchParamKeys...),
		BuildHash:           se.BuildHash,
		TimeOrigin:          float64(time.Now().UnixMilli()),
		TimezoneID:          firstNonEmpty(se.TimezoneID, b.Locale.TimezoneID),
	}
	if len(env.Languages) == 0 {
		env.Languages = append([]string(nil), b.Locale.Languages...)
	}
	if env.ScreenWidth == 0 {
		env.ScreenWidth = b.Geometry.ScreenWidth
	}
	if env.ScreenHeight == 0 {
		env.ScreenHeight = b.Geometry.ScreenHeight
	}
	if env.JSHeapSizeLimit == 0 {
		env.JSHeapSizeLimit = b.Navigator.JSHeapSizeLimit
	}
	if env.HardwareConcurrency == 0 {
		env.HardwareConcurrency = b.Navigator.HardwareConcurrency
	}
	if env.TimeOrigin == 0 {
		env.TimeOrigin = float64(time.Now().UnixMilli())
	}
	// Defaults for empty catalog slots.
	// Turnstile/SDK path needs pinned build + sdk.js URL even when payload[6] is null.
	if len(env.ScriptSources) == 0 {
		env.ScriptSources = []string{PinnedSDKURL}
	}
	if len(env.DocumentKeys) == 0 {
		env.DocumentKeys = []string{"body", "documentElement", "location"}
	}
	if len(env.WindowKeys) == 0 {
		// Firefox HAR samples window keys like ondragexit / __oai_so_cn — keep generic pool.
		if isFirefoxEnv(env) {
			env.WindowKeys = []string{"ondragexit", "__oai_so_cn", "document", "navigator", "location"}
		} else {
			env.WindowKeys = []string{"chrome", "document", "navigator"}
		}
	}
	if len(env.SearchParamKeys) == 0 {
		env.SearchParamKeys = []string{}
	}
	if env.BuildHash == "" || env.BuildHash == "prod" {
		env.BuildHash = PinnedSDKVersion
	}
	// Thorough HAR align for Firefox (chatgpt.com_Archive 26-07-17).
	if isFirefoxEnv(env) || b.Identity.Browser == fingerprint.BrowserFirefox {
		env.HeapNull = true
		env.BuildNull = true
		env.FlagsHAR = true
	}
	return env
}

func isFirefoxEnv(env Env) bool {
	return fingerprint.IsFirefoxUA(env.UserAgent) || strings.Contains(env.UserAgent, "Firefox/")
}

// CollectFingerprintData builds the 25-element array (TS order).
// Indices 3 and 9 are mutated during PoW (attempt, elapsed ms).
// Firefox HAR (2026-07-17): [2]=null [6]=null [18..24]=0,0,0,0,0,1,1
// Legacy Node collectFingerprintData used [18..24]=0,1,1,0,0,0,1 — kept when FlagsHAR=false.
func CollectFingerprintData(env Env, sid string, rng *mathrand.Rand) []any {
	if rng == nil {
		rng = mathrand.New(mathrand.NewChaCha8(seed32()))
	}
	var heap any = env.JSHeapSizeLimit
	if env.HeapNull {
		heap = nil
	}
	var build any = env.BuildHash
	if env.BuildNull {
		build = nil
	}
	// flags 18-24
	f18, f19, f20, f21, f22, f23, f24 := 0, 1, 1, 0, 0, 0, 1
	if env.FlagsHAR {
		f18, f19, f20, f21, f22, f23, f24 = 0, 0, 0, 0, 0, 1, 1
	}
	return []any{
		env.ScreenWidth + env.ScreenHeight, // 0
		jsDateString(env),                  // 1 — Date().toString()-like in env timezone
		heap,                               // 2
		rng.Float64(),                      // 3 — overwritten with attempt
		env.UserAgent,                      // 4
		randomPick(rng, env.ScriptSources), // 5
		build,                              // 6
		env.Language,                       // 7
		strings.Join(env.Languages, ","),   // 8
		rng.Float64(),                      // 9 — overwritten with elapsed ms
		randomNavigatorProperty(rng, env),  // 10
		randomPick(rng, env.DocumentKeys),  // 11
		randomPick(rng, env.WindowKeys),    // 12
		performanceNow(),                   // 13
		sid,                                // 14
		strings.Join(env.SearchParamKeys, ","), // 15
		env.HardwareConcurrency,            // 16
		env.TimeOrigin,                     // 17
		f18, f19, f20, f21, f22, f23, f24,  // 18-24
	}
}

// jsDateString approximates JS Date.prototype.toString() in env.TimezoneID.
// Example: Fri Jul 17 2026 16:25:53 GMT-0400 (Amazon Standard Time)
func jsDateString(env Env) string {
	loc := time.Local
	if tz := strings.TrimSpace(env.TimezoneID); tz != "" {
		if l, err := time.LoadLocation(tz); err == nil {
			loc = l
		}
	}
	now := time.Now().In(loc)
	zone, offset := now.Zone()
	// GMT±HHMM
	sign := "+"
	if offset < 0 {
		sign = "-"
		offset = -offset
	}
	oh := offset / 3600
	om := (offset % 3600) / 60
	gmt := fmt.Sprintf("GMT%s%02d%02d", sign, oh, om)
	// Mon Jan 2 15:04:05 2006 → reorder to JS: Mon Jan 2 2006 15:04:05
	// use fixed English weekday/month like V8 en-US core.
	core := now.Format("Mon Jan 2 2006 15:04:05")
	if zone == "" {
		zone = env.TimezoneID
	}
	if zone == "" {
		return core + " " + gmt
	}
	return fmt.Sprintf("%s %s (%s)", core, gmt, zone)
}

// GenerateAnswer runs the FNV-style PoW matching TS generateAnswer.
// Returns the suffix after gAAAAAB / gAAAAAC (encoded~S or error marker).
func GenerateAnswer(ctx context.Context, env Env, sid, seed, difficulty string, maxAttempts int) (string, int, error) {
	if maxAttempts <= 0 {
		maxAttempts = 500_000
	}
	if difficulty == "" {
		difficulty = "0"
	}
	rng := mathrand.New(mathrand.NewChaCha8(seed32()))
	start := performanceNow()
	data := CollectFingerprintData(env, sid, rng)
	if len(data) != PayloadIndexCount {
		return "", 0, fmt.Errorf("sentinel: payload len %d want %d", len(data), PayloadIndexCount)
	}

	for attempt := 0; attempt < maxAttempts; attempt++ {
		select {
		case <-ctx.Done():
			return "", attempt, ctx.Err()
		default:
		}
		data[3] = attempt
		data[9] = int(math.Round(performanceNow() - start))
		encoded, err := base64JSON(data)
		if err != nil {
			return "", attempt, err
		}
		digest := sentinelHashHex(seed + encoded)
		if len(digest) >= len(difficulty) && digest[:len(difficulty)] <= difficulty {
			return encoded + "~S", attempt + 1, nil
		}
	}
	// Match TS exhausted marker prefix
	msg, _ := base64JSON("max attempts exceeded")
	return "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D" + msg, maxAttempts, &Error{
		Code:    "sentinel_pow_exhausted",
		Message: fmt.Sprintf("pow exhausted after %d", maxAttempts),
	}
}

// RequirementsToken is gAAAAAC + answer for difficulty "0".
func RequirementsToken(ctx context.Context, env Env, sid string, maxAttempts int) (string, error) {
	seed := fmt.Sprintf("%f", mathrand.Float64())
	ans, _, err := GenerateAnswer(ctx, env, sid, seed, "0", maxAttempts)
	if err != nil {
		return "", err
	}
	return "gAAAAAC" + ans, nil
}

// EnforcementToken is gAAAAAB + answer for requirements.proofofwork.
func EnforcementToken(ctx context.Context, env Env, sid, seed, difficulty string, maxAttempts int) (string, error) {
	ans, _, err := GenerateAnswer(ctx, env, sid, seed, difficulty, maxAttempts)
	if err != nil {
		return "", err
	}
	return "gAAAAAB" + ans, nil
}

// AssembleHeaderJSON builds the openai-sentinel-token JSON object (p,t,c,id,flow).
func AssembleHeaderJSON(p, t, c, deviceID, flow string) (string, error) {
	obj := map[string]any{
		"p":    p,
		"t":    t,
		"c":    c,
		"id":   deviceID,
		"flow": flow,
	}
	// omit empty t
	if t == "" {
		delete(obj, "t")
	}
	raw, err := json.Marshal(obj)
	if err != nil {
		return "", err
	}
	return string(raw), nil
}

// AssembleSOHeaderJSON builds openai-sentinel-so-token {so,c,id,flow} (HAR create_account).
// so is opaque SDK sessionObserver material; empty so returns "".
func AssembleSOHeaderJSON(so, c, deviceID, flow string) (string, error) {
	so = strings.TrimSpace(so)
	if so == "" {
		return "", nil
	}
	obj := map[string]any{
		"so":   so,
		"c":    c,
		"id":   deviceID,
		"flow": flow,
	}
	raw, err := json.Marshal(obj)
	if err != nil {
		return "", err
	}
	return string(raw), nil
}

// NewSID returns a random UUID string for payload index 14.
func NewSID() string {
	return uuid.NewString()
}

func base64JSON(v any) (string, error) {
	raw, err := json.Marshal(v)
	if err != nil {
		return "", err
	}
	return base64.StdEncoding.EncodeToString(raw), nil
}

// sentinelHashHex is the TS FNV-1a-ish 32-bit mixer (not crypto SHA).
func sentinelHashHex(input string) string {
	var hash uint32 = 2166136261
	for i := 0; i < len(input); i++ {
		hash ^= uint32(input[i])
		hash = (hash * 16777619)
	}
	hash ^= hash >> 16
	hash = hash * 2246822507
	hash ^= hash >> 13
	hash = hash * 3266489909
	hash ^= hash >> 16
	return fmt.Sprintf("%08x", hash)
}

func performanceNow() float64 {
	return float64(time.Now().UnixMilli())
}

func randomPick(rng *mathrand.Rand, items []string) string {
	if len(items) == 0 {
		return ""
	}
	return items[rng.IntN(len(items))]
}

func randomNavigatorProperty(rng *mathrand.Rand, env Env) string {
	// TS uses Unicode minus U+2212 between key and value.
	const minus = "\u2212"
	type kv struct {
		k string
		v string
	}
	cands := []kv{
		{"userAgent", env.UserAgent},
		{"language", env.Language},
		{"hardwareConcurrency", fmt.Sprintf("%d", env.HardwareConcurrency)},
		// HAR samples: globalPrivacyControl−false, requestMIDIAccess−function ...
		{"globalPrivacyControl", "false"},
		{"requestMIDIAccess", "function requestMIDIAccess() {\n    [native code]\n}"},
		{"webdriver", "false"},
	}
	x := cands[rng.IntN(len(cands))]
	return x.k + minus + x.v
}

func firstNonEmpty(a, b string) string {
	if strings.TrimSpace(a) != "" {
		return a
	}
	return b
}

func seed32() [32]byte {
	var b [32]byte
	_, _ = rand.Read(b[:])
	return b
}
