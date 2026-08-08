package sentinel

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"net/http"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"
)

// Error codes for SDK / Turnstile drift (fail-closed, not vague).
const (
	CodeSDKDrift          = "sdk_drift"
	CodeSDKHookMissing    = "sdk_drift_hook_missing"
	CodeSDKHashMismatch   = "sdk_drift_hash_mismatch"
	CodeSDKBuildMismatch  = "sdk_drift_build_mismatch"
	CodeTurnstileDXFailed = "sdk_drift_turnstile_dx"
	CodeProtocolIncompat  = "protocol_incompatible"
)

// FrameURLTemplate probes which build id the sentinel frame still serves.
const FrameURLTemplate = "https://sentinel.openai.com/backend-api/sentinel/frame.html?sv=%s"

// DriftKind classifies how we know the pin is stale / broken.
type DriftKind string

const (
	DriftNone          DriftKind = ""
	DriftContentHash   DriftKind = "content_hash" // same URL, different body
	DriftBuildID       DriftKind = "build_id"     // live HTML shows another build
	DriftHookMissing   DriftKind = "hook_missing" // patch hook gone
	DriftTurnstileFail DriftKind = "turnstile_dx" // runtime dx path failed
	DriftNetwork       DriftKind = "network"      // could not check (soft)
)

// DriftResult is the outcome of comparing pinned sdk.js to live sources.
type DriftResult struct {
	PinnedHash    string    `json:"pinned_hash"`
	PinnedVersion string    `json:"pinned_version"`
	LiveHash      string    `json:"live_hash,omitempty"`
	LiveBuilds    []string  `json:"live_builds,omitempty"`
	URL           string    `json:"url"`
	FrameURL      string    `json:"frame_url,omitempty"`
	Match         bool      `json:"match"`
	Kind          DriftKind `json:"kind,omitempty"`
	CheckedAt     time.Time `json:"checked_at"`
	Error         string    `json:"error,omitempty"`
	Code          string    `json:"code,omitempty"`
}

// DriftError converts a non-match DriftResult into a typed *Error.
func (r DriftResult) DriftError() error {
	if r.Match || r.Code == "" {
		return nil
	}
	msg := r.Error
	if msg == "" {
		msg = string(r.Kind)
	}
	return &Error{Code: r.Code, Message: msg}
}

// CheckSDKDrift fetches PinnedSDKURL (or overrideURL) and compares SHA-256 to the embed pin.
// Network failure → Kind=network, Code empty (soft). Content mismatch → CodeSDKHashMismatch.
func CheckSDKDrift(ctx context.Context, overrideURL string) DriftResult {
	_, pinHash, err := LoadPinnedSDK()
	res := DriftResult{
		PinnedHash:    pinHash,
		PinnedVersion: PinnedSDKVersion,
		URL:           PinnedSDKURL,
		CheckedAt:     time.Now().UTC(),
	}
	if err != nil {
		res.Error = err.Error()
		res.Code = CodeSDKDrift
		res.Kind = DriftHookMissing
		if fe, ok := err.(*Error); ok {
			res.Code = fe.Code
			res.Error = fe.Message
		}
		return res
	}
	url := PinnedSDKURL
	if strings.TrimSpace(overrideURL) != "" {
		url = overrideURL
		res.URL = url
	}
	body, status, ferr := httpGetLimited(ctx, url, 2<<20)
	if ferr != nil {
		res.Kind = DriftNetwork
		res.Error = ferr.Error()
		return res
	}
	if status != http.StatusOK {
		res.Kind = DriftNetwork
		res.Error = fmt.Sprintf("http %d", status)
		return res
	}
	sum := sha256.Sum256(body)
	res.LiveHash = hex.EncodeToString(sum[:])
	res.Match = strings.EqualFold(res.LiveHash, res.PinnedHash)
	if !res.Match {
		res.Kind = DriftContentHash
		res.Code = CodeSDKHashMismatch
		res.Error = fmt.Sprintf("sdk content hash mismatch live=%s pin=%s version=%s url=%s",
			res.LiveHash, res.PinnedHash, res.PinnedVersion, res.URL)
	}
	return res
}

var sdkPathRe = regexp.MustCompile(`sentinel/([0-9a-fA-F]{6,})/sdk\.js`)
var buildTokenRe = regexp.MustCompile(`\b(20\d{6}[a-fA-F0-9]{4,})\b`)

// DiscoverLiveBuildIDs loads the sentinel frame and extracts build ids.
func DiscoverLiveBuildIDs(ctx context.Context, frameSV string) (builds []string, frameURL string, err error) {
	if strings.TrimSpace(frameSV) == "" {
		frameSV = PinnedSDKVersion
	}
	frameURL = fmt.Sprintf(FrameURLTemplate, frameSV)
	body, status, ferr := httpGetLimited(ctx, frameURL, 1<<20)
	if ferr != nil {
		return nil, frameURL, ferr
	}
	if status != http.StatusOK {
		return nil, frameURL, fmt.Errorf("frame http %d", status)
	}
	return extractBuildIDs(string(body)), frameURL, nil
}

func extractBuildIDs(text string) []string {
	seen := map[string]struct{}{}
	var out []string
	add := func(id string) {
		id = strings.TrimSpace(id)
		if id == "" {
			return
		}
		if _, ok := seen[id]; ok {
			return
		}
		seen[id] = struct{}{}
		out = append(out, id)
	}
	for _, m := range sdkPathRe.FindAllStringSubmatch(text, -1) {
		if len(m) > 1 {
			add(m[1])
		}
	}
	for _, m := range buildTokenRe.FindAllStringSubmatch(text, -1) {
		if len(m) > 1 {
			add(m[1])
		}
	}
	sort.Strings(out)
	return out
}

// CheckBuildDrift compares pin version to builds discovered from live frame HTML.
func CheckBuildDrift(ctx context.Context) DriftResult {
	res := DriftResult{
		PinnedVersion: PinnedSDKVersion,
		PinnedHash:    PinnedSDKHash(),
		URL:           PinnedSDKURL,
		CheckedAt:     time.Now().UTC(),
		Match:         true,
	}
	builds, frameURL, err := DiscoverLiveBuildIDs(ctx, PinnedSDKVersion)
	res.FrameURL = frameURL
	res.LiveBuilds = builds
	if err != nil {
		res.Match = false
		res.Kind = DriftNetwork
		res.Error = err.Error()
		return res
	}
	if len(builds) == 0 {
		res.Match = false
		res.Kind = DriftNetwork
		res.Error = "no build id found in frame HTML"
		return res
	}
	foreign := make([]string, 0)
	pinSeen := false
	for _, b := range builds {
		if strings.EqualFold(b, PinnedSDKVersion) {
			pinSeen = true
			continue
		}
		foreign = append(foreign, b)
	}
	if len(foreign) > 0 {
		res.Match = false
		res.Kind = DriftBuildID
		res.Code = CodeSDKBuildMismatch
		res.Error = fmt.Sprintf("live frame advertises build(s) %v; pin=%s", foreign, PinnedSDKVersion)
		return res
	}
	if !pinSeen {
		res.Match = false
		res.Kind = DriftBuildID
		res.Code = CodeSDKBuildMismatch
		res.Error = fmt.Sprintf("live frame builds %v do not include pin %s", builds, PinnedSDKVersion)
		return res
	}
	res.Match = true
	return res
}

// FullDriftCheck runs content-hash check then build-id discovery.
func FullDriftCheck(ctx context.Context) DriftResult {
	hashRes := CheckSDKDrift(ctx, "")
	if hashRes.Code != "" {
		return hashRes
	}
	buildRes := CheckBuildDrift(ctx)
	if buildRes.Code != "" {
		if hashRes.LiveHash != "" {
			buildRes.LiveHash = hashRes.LiveHash
		}
		return buildRes
	}
	if hashRes.Kind == DriftNetwork {
		if buildRes.Match {
			hashRes.Match = true
			hashRes.LiveBuilds = buildRes.LiveBuilds
			hashRes.FrameURL = buildRes.FrameURL
			hashRes.Error = "content hash skipped (network): " + hashRes.Error
			return hashRes
		}
		return hashRes
	}
	hashRes.Match = true
	hashRes.LiveBuilds = buildRes.LiveBuilds
	hashRes.FrameURL = buildRes.FrameURL
	return hashRes
}

// EnsurePinnedSDKReady validates embed pin + patch hook (no network).
func EnsurePinnedSDKReady() error {
	src, _, err := LoadPinnedSDK()
	if err != nil {
		return asDriftError(err, CodeSDKDrift)
	}
	if _, err := PatchSDK(src); err != nil {
		return &Error{Code: CodeSDKHookMissing, Message: err.Error()}
	}
	return nil
}

// StartupDriftOptions controls worker boot checks.
type StartupDriftOptions struct {
	SkipNetwork   bool
	FailOnNetwork bool
	Timeout       time.Duration
}

// StartupDriftCheck is the boot gate: embed pin+hook (hard), then optional live checks.
func StartupDriftCheck(ctx context.Context, opt StartupDriftOptions) (DriftResult, error) {
	if err := EnsurePinnedSDKReady(); err != nil {
		res := DriftResult{
			PinnedVersion: PinnedSDKVersion,
			PinnedHash:    PinnedSDKHash(),
			Match:         false,
			Kind:          DriftHookMissing,
			CheckedAt:     time.Now().UTC(),
			Code:          CodeSDKHookMissing,
			Error:         err.Error(),
		}
		if fe, ok := err.(*Error); ok {
			res.Code = fe.Code
			res.Error = fe.Message
		}
		return res, err
	}
	if opt.SkipNetwork {
		return DriftResult{
			PinnedVersion: PinnedSDKVersion,
			PinnedHash:    PinnedSDKHash(),
			Match:         true,
			CheckedAt:     time.Now().UTC(),
			URL:           PinnedSDKURL,
		}, nil
	}
	timeout := opt.Timeout
	if timeout <= 0 {
		timeout = 15 * time.Second
	}
	cctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	res := FullDriftCheck(cctx)
	if res.Code != "" {
		return res, res.DriftError()
	}
	if res.Kind == DriftNetwork && opt.FailOnNetwork {
		return res, &Error{Code: CodeSDKDrift, Message: "network: " + res.Error}
	}
	return res, nil
}

// MapRuntimeFailure turns hook/dx failures into explicit sdk_drift_* errors.
func MapRuntimeFailure(err error) error {
	if err == nil {
		return nil
	}
	if fe, ok := err.(*Error); ok {
		switch {
		case fe.Code == CodeSDKHookMissing || fe.Code == CodeSDKHashMismatch ||
			fe.Code == CodeSDKBuildMismatch || fe.Code == CodeTurnstileDXFailed ||
			fe.Code == CodeSDKDrift || strings.HasPrefix(fe.Code, "sdk_drift"):
			return fe
		case strings.Contains(fe.Message, "patch hook"):
			return &Error{Code: CodeSDKHookMissing, Message: fe.Message}
		case strings.Contains(fe.Message, "hash mismatch"):
			return &Error{Code: CodeSDKHashMismatch, Message: fe.Message}
		case strings.Contains(fe.Message, "turnstile"):
			return &Error{Code: CodeTurnstileDXFailed, Message: fe.Message}
		case fe.Code == CodeProtocolIncompat || fe.Code == "protocol_incompatible":
			msg := fe.Message
			if strings.Contains(msg, "patch hook") || strings.Contains(msg, "hook not found") {
				return &Error{Code: CodeSDKHookMissing, Message: msg}
			}
			if strings.Contains(msg, "turnstile") || strings.Contains(msg, "dx") {
				return &Error{Code: CodeTurnstileDXFailed, Message: msg}
			}
			return fe
		default:
			return fe
		}
	}
	msg := err.Error()
	switch {
	case strings.Contains(msg, "patch hook") || strings.Contains(msg, "hook not found"):
		return &Error{Code: CodeSDKHookMissing, Message: msg}
	case strings.Contains(msg, "hash mismatch"):
		return &Error{Code: CodeSDKHashMismatch, Message: msg}
	case strings.Contains(msg, "turnstile") || strings.Contains(msg, "dx "):
		return &Error{Code: CodeTurnstileDXFailed, Message: msg}
	default:
		return err
	}
}

func asDriftError(err error, code string) error {
	if err == nil {
		return nil
	}
	if fe, ok := err.(*Error); ok {
		return fe
	}
	return &Error{Code: code, Message: err.Error()}
}

func httpGetLimited(ctx context.Context, url string, limit int64) ([]byte, int, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, 0, err
	}
	req.Header.Set("User-Agent", "go-email-protocol-sdk-drift/1.0")
	req.Header.Set("Accept", "text/html,application/javascript,*/*")
	client := &http.Client{Timeout: 20 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return nil, 0, fmt.Errorf("fetch: %w", err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, limit))
	if err != nil {
		return nil, resp.StatusCode, fmt.Errorf("read: %w", err)
	}
	return body, resp.StatusCode, nil
}

var (
	lastDriftMu sync.Mutex
	lastDrift   DriftResult
)

// LastDriftResult returns the most recent Startup/Recheck result.
func LastDriftResult() DriftResult {
	lastDriftMu.Lock()
	defer lastDriftMu.Unlock()
	return lastDrift
}

func storeDrift(r DriftResult) {
	lastDriftMu.Lock()
	lastDrift = r
	lastDriftMu.Unlock()
}

// RecheckDriftStore records a precomputed DriftResult (startup path).
func RecheckDriftStore(r DriftResult) {
	storeDrift(r)
}

// RecheckDrift runs FullDriftCheck and stores the result.
func RecheckDrift(ctx context.Context) DriftResult {
	res := FullDriftCheck(ctx)
	storeDrift(res)
	return res
}
