package sentinel

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"sort"
	"strconv"
	"strings"

	"github.com/gpt-register/go-email-protocol/internal/fingerprint"
)

const (
	ReleaseSchemaVersion = 1

	PinnedLoaderURL    = "https://sentinel.openai.com/backend-api/sentinel/sdk.js"
	PinnedLoaderHash   = "a656b4b050e98ad23afc481a8d2fd7d0a316813ee38cf52bec07860e264d57cb"
	PinnedPatchHookID  = "turnstile-and-so-v1"
	PayloadIndex5Known = "sample_known_source"
	PayloadIndex6Null  = "firefox_null"

	CodeReleaseInvalid         = "sentinel_release_invalid"
	CodeManifestHashMismatch   = "sentinel_manifest_hash_mismatch"
	CodeSourceUntrusted        = "sentinel_source_untrusted"
	CodeLoaderHashMismatch     = "sentinel_loader_hash_mismatch"
	CodeReleaseSDKHashMismatch = "sentinel_sdk_hash_mismatch"
	CodeReleaseBuildMismatch   = "sentinel_build_mismatch"
	CodeReleaseHookMismatch    = "sentinel_hook_missing"
)

// LoaderManifest identifies the public loader and the versioned SDK it injects.
// Values returned by ReleaseManifest accessors are copies.
type LoaderManifest struct {
	URL        string `json:"url"`
	SHA256     string `json:"sha256"`
	ResolvesTo string `json:"resolves_to"`
}

// SDKManifest identifies the executable SDK content and its approved local patch.
// Values returned by ReleaseManifest accessors are copies.
type SDKManifest struct {
	URL         string `json:"url"`
	SHA256      string `json:"sha256"`
	PatchHookID string `json:"patch_hook_id"`
}

type releaseDocument struct {
	SchemaVersion         int            `json:"schema_version"`
	ReleaseID             string         `json:"release_id"`
	FrameSV               string         `json:"frame_sv"`
	Loader                LoaderManifest `json:"loader"`
	SDK                   SDKManifest    `json:"sdk"`
	ObservedScriptSources []string       `json:"observed_script_sources"`
	PayloadIndex5Policy   string         `json:"payload_index_5_policy"`
	PayloadIndex6Policy   string         `json:"payload_index_6_policy"`
	ManifestSHA256        string         `json:"manifest_sha256,omitempty"`
}

// ReleaseManifest is an immutable, validated Sentinel release. Its state is
// deliberately private; slice and nested-structure accessors return copies.
type ReleaseManifest struct {
	doc releaseDocument
}

// ScriptSourceKind distinguishes the loader URL from the versioned SDK URL.
type ScriptSourceKind string

const (
	ScriptSourceLoader ScriptSourceKind = "loader"
	ScriptSourceSDK    ScriptSourceKind = "versioned_sdk"
)

// ResolvedSource maps an observed script URL to its verified SDK content identity.
type ResolvedSource struct {
	ReleaseID             string           `json:"release_id"`
	ManifestSHA256        string           `json:"manifest_sha256"`
	ObservedURL           string           `json:"observed_url"`
	Kind                  ScriptSourceKind `json:"kind"`
	SourceSHA256          string           `json:"source_sha256"`
	SDKURL                string           `json:"sdk_url"`
	SDKSHA256             string           `json:"sdk_sha256"`
	ContentIdentitySHA256 string           `json:"content_identity_sha256"`
	FrameSV               string           `json:"frame_sv"`
	PatchHookID           string           `json:"patch_hook_id"`
}

// LoadRelease reads, strictly decodes, and fully validates an immutable release.
func LoadRelease(path string) (*ReleaseManifest, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, releaseError(CodeReleaseInvalid, "read release manifest: %v", err)
	}
	return ParseRelease(raw)
}

// ParseRelease strictly decodes and fully validates an immutable release.
func ParseRelease(raw []byte) (*ReleaseManifest, error) {
	if len(bytes.TrimSpace(raw)) == 0 {
		return nil, releaseError(CodeReleaseInvalid, "empty release manifest")
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	var doc releaseDocument
	if err := dec.Decode(&doc); err != nil {
		return nil, releaseError(CodeReleaseInvalid, "decode release manifest: %v", err)
	}
	var trailing any
	if err := dec.Decode(&trailing); !errors.Is(err, io.EOF) {
		if err == nil {
			return nil, releaseError(CodeReleaseInvalid, "release manifest contains multiple JSON values")
		}
		return nil, releaseError(CodeReleaseInvalid, "decode trailing release data: %v", err)
	}
	if err := validateReleaseDocument(&doc); err != nil {
		return nil, err
	}
	r := &ReleaseManifest{doc: doc}
	if err := r.ValidateEmbeddedPin(); err != nil {
		return nil, err
	}
	return r, nil
}

// MarshalJSON emits the validated normalized document without exposing mutable state.
func (r ReleaseManifest) MarshalJSON() ([]byte, error) {
	return json.Marshal(r.doc)
}

func (r *ReleaseManifest) SchemaVersion() int {
	if r == nil {
		return 0
	}
	return r.doc.SchemaVersion
}

func (r *ReleaseManifest) ReleaseID() string {
	if r == nil {
		return ""
	}
	return r.doc.ReleaseID
}

func (r *ReleaseManifest) FrameSV() string {
	if r == nil {
		return ""
	}
	return r.doc.FrameSV
}

func (r *ReleaseManifest) Loader() LoaderManifest {
	if r == nil {
		return LoaderManifest{}
	}
	return r.doc.Loader
}

func (r *ReleaseManifest) SDK() SDKManifest {
	if r == nil {
		return SDKManifest{}
	}
	return r.doc.SDK
}

func (r *ReleaseManifest) ObservedScriptSources() []string {
	if r == nil {
		return nil
	}
	return append([]string(nil), r.doc.ObservedScriptSources...)
}

func (r *ReleaseManifest) PayloadIndex5Policy() string {
	if r == nil {
		return ""
	}
	return r.doc.PayloadIndex5Policy
}

func (r *ReleaseManifest) PayloadIndex6Policy() string {
	if r == nil {
		return ""
	}
	return r.doc.PayloadIndex6Policy
}

// ManifestSHA256 returns the canonical release document identity.
func (r *ReleaseManifest) ManifestSHA256() string {
	if r == nil {
		return ""
	}
	return r.doc.ManifestSHA256
}

// CanonicalJSON returns a copy of the normalized manifest JSON.
func (r *ReleaseManifest) CanonicalJSON() ([]byte, error) {
	if r == nil {
		return nil, releaseError(CodeReleaseInvalid, "nil release manifest")
	}
	return json.Marshal(r.doc)
}

// ValidateEmbeddedPin proves that the manifest still names the embedded SDK,
// its frame build, and the complete Turnstile + session-observer patch hook.
func (r *ReleaseManifest) ValidateEmbeddedPin() error {
	if r == nil {
		return releaseError(CodeReleaseInvalid, "nil release manifest")
	}
	doc := r.doc
	doc.ObservedScriptSources = append([]string(nil), r.doc.ObservedScriptSources...)
	if err := validateReleaseDocument(&doc); err != nil {
		return err
	}
	return validateEmbeddedSDK(doc)
}

func validateEmbeddedSDK(doc releaseDocument) error {
	source, embeddedHash, err := LoadPinnedSDK()
	if err != nil {
		return err
	}
	if doc.SDK.SHA256 != embeddedHash {
		return releaseError(CodeReleaseSDKHashMismatch, "sdk hash got=%s want=%s", doc.SDK.SHA256, embeddedHash)
	}
	patched, err := PatchSDKForSO(source)
	if err != nil {
		return releaseError(CodeReleaseHookMismatch, "embedded patch hook failed: %v", err)
	}
	if !strings.Contains(patched, "__codexTurnstileDx") || !strings.Contains(patched, "__codexSessionObserverSO") {
		return releaseError(CodeReleaseHookMismatch, "embedded SDK does not expose the approved Turnstile and SO hooks")
	}
	return nil
}

// ResolveScriptSource maps both the loader and versioned URL to one executable
// SDK content identity. Unknown URLs fail closed.
func (r *ReleaseManifest) ResolveScriptSource(url string) (ResolvedSource, error) {
	if err := r.ValidateEmbeddedPin(); err != nil {
		return ResolvedSource{}, err
	}
	if strings.TrimSpace(url) == "" {
		return ResolvedSource{}, releaseError(CodeSourceUntrusted, "empty script source")
	}
	var kind ScriptSourceKind
	var sourceHash string
	switch url {
	case r.doc.Loader.URL:
		kind = ScriptSourceLoader
		sourceHash = r.doc.Loader.SHA256
	case r.doc.SDK.URL:
		kind = ScriptSourceSDK
		sourceHash = r.doc.SDK.SHA256
	default:
		return ResolvedSource{}, releaseError(CodeSourceUntrusted, "script source %q is not in release %s", url, r.doc.ReleaseID)
	}
	return ResolvedSource{
		ReleaseID:             r.doc.ReleaseID,
		ManifestSHA256:        r.doc.ManifestSHA256,
		ObservedURL:           url,
		Kind:                  kind,
		SourceSHA256:          sourceHash,
		SDKURL:                r.doc.SDK.URL,
		SDKSHA256:             r.doc.SDK.SHA256,
		ContentIdentitySHA256: "sha256:" + r.doc.SDK.SHA256,
		FrameSV:               r.doc.FrameSV,
		PatchHookID:           r.doc.SDK.PatchHookID,
	}, nil
}

// ValidateLoaderObservation validates captured loader bytes and injection target.
func (r *ReleaseManifest) ValidateLoaderObservation(url, hash, resolvesTo string) (ResolvedSource, error) {
	resolved, err := r.ResolveScriptSource(url)
	if err != nil {
		return ResolvedSource{}, err
	}
	if resolved.Kind != ScriptSourceLoader {
		return ResolvedSource{}, releaseError(CodeSourceUntrusted, "source %q is not the release loader", url)
	}
	if strings.TrimSpace(hash) == "" {
		return ResolvedSource{}, releaseError(CodeLoaderHashMismatch, "empty loader hash")
	}
	if strings.TrimSpace(resolvesTo) == "" {
		return ResolvedSource{}, releaseError(CodeSourceUntrusted, "empty loader resolve target")
	}
	if normalizeObservedHash(hash) != r.doc.Loader.SHA256 {
		return ResolvedSource{}, releaseError(CodeLoaderHashMismatch, "loader hash got=%q want=%q", hash, r.doc.Loader.SHA256)
	}
	if resolvesTo != r.doc.Loader.ResolvesTo || resolvesTo != r.doc.SDK.URL {
		return ResolvedSource{}, releaseError(CodeSourceUntrusted, "loader resolve target got=%q want=%q", resolvesTo, r.doc.SDK.URL)
	}
	return resolved, nil
}

// ValidateSDKObservation validates captured SDK bytes, frame build, and patch hook.
func (r *ReleaseManifest) ValidateSDKObservation(url, hash, frameSV, patchHookID string) (ResolvedSource, error) {
	resolved, err := r.ResolveScriptSource(url)
	if err != nil {
		return ResolvedSource{}, err
	}
	if resolved.Kind != ScriptSourceSDK {
		return ResolvedSource{}, releaseError(CodeSourceUntrusted, "source %q is not the versioned SDK", url)
	}
	if strings.TrimSpace(hash) == "" {
		return ResolvedSource{}, releaseError(CodeReleaseSDKHashMismatch, "empty sdk hash")
	}
	if strings.TrimSpace(frameSV) == "" {
		return ResolvedSource{}, releaseError(CodeReleaseBuildMismatch, "empty frame build")
	}
	if strings.TrimSpace(patchHookID) == "" {
		return ResolvedSource{}, releaseError(CodeReleaseHookMismatch, "empty patch hook id")
	}
	if normalizeObservedHash(hash) != r.doc.SDK.SHA256 {
		return ResolvedSource{}, releaseError(CodeReleaseSDKHashMismatch, "sdk hash got=%q want=%q", hash, r.doc.SDK.SHA256)
	}
	if frameSV != r.doc.FrameSV {
		return ResolvedSource{}, releaseError(CodeReleaseBuildMismatch, "frame build got=%q want=%q", frameSV, r.doc.FrameSV)
	}
	if patchHookID != r.doc.SDK.PatchHookID {
		return ResolvedSource{}, releaseError(CodeReleaseHookMismatch, "patch hook id got=%q want=%q", patchHookID, r.doc.SDK.PatchHookID)
	}
	return resolved, nil
}

// BindBundle clones b, installs the complete source set and frame build,
// recomputes consistency, and freezes the clone. The original is never mutated.
func (r *ReleaseManifest) BindBundle(b *fingerprint.Bundle) (*fingerprint.Bundle, error) {
	if err := r.ValidateEmbeddedPin(); err != nil {
		return nil, err
	}
	if b == nil {
		return nil, releaseError(CodeReleaseInvalid, "nil fingerprint bundle")
	}
	if b.Consistency.Locked || b.Consistency.Hash != "" {
		if err := b.AssertReady(); err != nil {
			return nil, fmt.Errorf("sentinel: bind release to frozen bundle: %w", err)
		}
	} else if err := b.Validate(fingerprint.ValidateOptions{}); err != nil {
		return nil, fmt.Errorf("sentinel: bind release to bundle: %w", err)
	}
	bound := b.Clone()
	bound.Consistency = fingerprint.Consistency{}
	bound.SentinelEnv.ScriptSources = r.ObservedScriptSources()
	bound.SentinelEnv.BuildHash = r.doc.FrameSV
	if err := bound.Freeze(); err != nil {
		return nil, fmt.Errorf("sentinel: freeze release-bound bundle: %w", err)
	}
	if err := bound.AssertReady(); err != nil {
		return nil, fmt.Errorf("sentinel: validate release-bound bundle: %w", err)
	}
	return &bound, nil
}

func validateReleaseDocument(doc *releaseDocument) error {
	if doc.SchemaVersion != ReleaseSchemaVersion {
		return releaseError(CodeReleaseInvalid, "schema_version got=%d want=%d", doc.SchemaVersion, ReleaseSchemaVersion)
	}
	if doc.FrameSV != PinnedSDKVersion {
		return releaseError(CodeReleaseBuildMismatch, "frame build got=%q want=%q", doc.FrameSV, PinnedSDKVersion)
	}
	if doc.Loader.URL != PinnedLoaderURL {
		return releaseError(CodeSourceUntrusted, "loader URL %q is not pinned", doc.Loader.URL)
	}
	if !validManifestDigest(doc.Loader.SHA256) || doc.Loader.SHA256 != PinnedLoaderHash {
		return releaseError(CodeLoaderHashMismatch, "loader hash got=%q want=%q", doc.Loader.SHA256, PinnedLoaderHash)
	}
	if doc.SDK.URL != PinnedSDKURL {
		return releaseError(CodeSourceUntrusted, "sdk URL %q is not pinned", doc.SDK.URL)
	}
	if doc.Loader.ResolvesTo != doc.SDK.URL {
		return releaseError(CodeSourceUntrusted, "loader resolve target got=%q want=%q", doc.Loader.ResolvesTo, doc.SDK.URL)
	}
	if !validManifestDigest(doc.SDK.SHA256) || doc.SDK.SHA256 != PinnedSDKHash() {
		return releaseError(CodeReleaseSDKHashMismatch, "sdk hash got=%q want=%q", doc.SDK.SHA256, PinnedSDKHash())
	}
	if doc.SDK.PatchHookID != PinnedPatchHookID {
		return releaseError(CodeReleaseHookMismatch, "patch hook id got=%q want=%q", doc.SDK.PatchHookID, PinnedPatchHookID)
	}
	if !validReleaseID(doc.ReleaseID, doc.FrameSV) {
		return releaseError(CodeReleaseInvalid, "release_id %q does not bind frame %s", doc.ReleaseID, doc.FrameSV)
	}
	if doc.PayloadIndex5Policy != PayloadIndex5Known {
		return releaseError(CodeReleaseInvalid, "payload_index_5_policy got=%q want=%q", doc.PayloadIndex5Policy, PayloadIndex5Known)
	}
	if doc.PayloadIndex6Policy != PayloadIndex6Null {
		return releaseError(CodeReleaseInvalid, "payload_index_6_policy got=%q want=%q", doc.PayloadIndex6Policy, PayloadIndex6Null)
	}
	if len(doc.ObservedScriptSources) != 2 {
		return releaseError(CodeSourceUntrusted, "observed script source set must contain exactly loader and versioned SDK")
	}
	seen := make(map[string]struct{}, len(doc.ObservedScriptSources))
	for _, source := range doc.ObservedScriptSources {
		if source != doc.Loader.URL && source != doc.SDK.URL {
			return releaseError(CodeSourceUntrusted, "script source %q is not loader or versioned SDK", source)
		}
		if _, duplicate := seen[source]; duplicate {
			return releaseError(CodeSourceUntrusted, "duplicate script source %q", source)
		}
		seen[source] = struct{}{}
	}
	if _, ok := seen[doc.Loader.URL]; !ok {
		return releaseError(CodeSourceUntrusted, "loader URL missing from observed script source set")
	}
	if _, ok := seen[doc.SDK.URL]; !ok {
		return releaseError(CodeSourceUntrusted, "versioned SDK URL missing from observed script source set")
	}
	sort.Strings(doc.ObservedScriptSources)
	if !validPrefixedDigest(doc.ManifestSHA256) {
		return releaseError(CodeManifestHashMismatch, "manifest_sha256 must be sha256:<64 lowercase hex>")
	}
	got, err := computeManifestSHA256(*doc)
	if err != nil {
		return releaseError(CodeReleaseInvalid, "canonicalize manifest: %v", err)
	}
	if doc.ManifestSHA256 != got {
		return releaseError(CodeManifestHashMismatch, "manifest hash got=%q want=%q", doc.ManifestSHA256, got)
	}
	return nil
}

func computeManifestSHA256(doc releaseDocument) (string, error) {
	doc.ManifestSHA256 = ""
	doc.ObservedScriptSources = append([]string(nil), doc.ObservedScriptSources...)
	sort.Strings(doc.ObservedScriptSources)
	raw, err := json.Marshal(doc)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(raw)
	return "sha256:" + hex.EncodeToString(sum[:]), nil
}

func validReleaseID(id, frameSV string) bool {
	prefix := "sentinel-" + frameSV + "-r"
	if !strings.HasPrefix(id, prefix) {
		return false
	}
	revision, err := strconv.Atoi(strings.TrimPrefix(id, prefix))
	return err == nil && revision > 0
}

func validManifestDigest(value string) bool {
	if len(value) != sha256.Size*2 || value != strings.ToLower(value) {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

func validPrefixedDigest(value string) bool {
	return strings.HasPrefix(value, "sha256:") && validManifestDigest(strings.TrimPrefix(value, "sha256:"))
}

func normalizeObservedHash(value string) string {
	value = strings.TrimSpace(strings.ToLower(value))
	return strings.TrimPrefix(value, "sha256:")
}

func releaseError(code, format string, args ...any) *Error {
	return &Error{Code: code, Message: fmt.Sprintf(format, args...)}
}
