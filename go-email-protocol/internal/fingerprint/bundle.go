// Package fingerprint implements FingerprintBundle v2: full device profile,
// client hints, consistency checks, and catalog generation for pure-Go protocol.
// Spec: docs/PURE_GO_FULL_FINGERPRINT_PLAN.md
package fingerprint

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"time"
)

// BundleVersion is the current durable schema version.
const BundleVersion = 2

// Family / browser / OS enums.
const (
	FamilyDesktop = "desktop"
	FamilyMobile  = "mobile"

	BrowserChrome  = "chrome"
	BrowserEdge    = "edge"
	BrowserFirefox = "firefox"
	OSWindows = "windows"
	OSAndroid = "android"

	SourceGenerated  = "generated"
	SourceGranted    = "granted"
	SourceCheckpoint = "checkpoint"

	TimezoneStrictMatch   = "strict_match"
	TimezoneAllowGlobalEN = "allow_global_en"
	TimezoneCatalogOnly   = "catalog_only"
)

// Bundle is FingerprintBundle v2 — one job, one world.
type Bundle struct {
	Version            int            `json:"version"`
	BundleID           string         `json:"bundle_id"`
	CreatedAt          time.Time      `json:"created_at"`
	Source             string         `json:"source,omitempty"`
	CatalogID          string         `json:"catalog_id,omitempty"`
	TransportProfileID string         `json:"transport_profile_id,omitempty"`
	Identity           Identity       `json:"identity"`
	Device             Device         `json:"device"`
	Locale             Locale         `json:"locale"`
	Geometry           Geometry       `json:"geometry"`
	Navigator          Navigator      `json:"navigator"`
	ClientHints        ClientHints    `json:"client_hints"`
	HeaderIdentity     HeaderIdentity `json:"header_identity"`
	SentinelEnv        SentinelEnv    `json:"sentinel_env"`
	Noise              Noise          `json:"noise"`
	ProxyAffinity      ProxyAffinity  `json:"proxy_affinity"`
	Consistency        Consistency    `json:"consistency"`
}

// Identity is stable job-local profile identity (not server oai-did).
type Identity struct {
	ProfileUUID      string `json:"profile_uuid"`
	Family           string `json:"family"`
	Browser          string `json:"browser"`
	OS               string `json:"os"`
	OSVersion        string `json:"os_version"`
	ImpersonateLabel string `json:"impersonate_label,omitempty"`
}

// Device holds UA and parsed versions.
type Device struct {
	UserAgent     string `json:"user_agent"`
	UAMajor       int    `json:"ua_major"`
	UAFullVersion string `json:"ua_full_version"`
	EdgeVersion   string `json:"edge_version,omitempty"`
	AndroidModel  string `json:"android_model,omitempty"`
}

// Locale is an atomic locale tuple.
type Locale struct {
	Locale         string   `json:"locale"`
	Languages      []string `json:"languages"`
	AcceptLanguage string   `json:"accept_language"`
	TimezoneID     string   `json:"timezone_id"`
}

// Geometry is an atomic viewport/screen tuple.
type Geometry struct {
	ViewportWidth     int     `json:"viewport_width"`
	ViewportHeight    int     `json:"viewport_height"`
	ScreenWidth       int     `json:"screen_width"`
	ScreenHeight      int     `json:"screen_height"`
	OuterWidth        int     `json:"outer_width"`
	OuterHeight       int     `json:"outer_height"`
	DeviceScaleFactor float64 `json:"device_scale_factor"`
	ColorDepth        int     `json:"color_depth"`
	PixelDepth        int     `json:"pixel_depth"`
}

// Navigator mirrors browser navigator capability fields.
type Navigator struct {
	HardwareConcurrency int    `json:"hardware_concurrency"`
	DeviceMemory        int    `json:"device_memory"`
	JSHeapSizeLimit     int64  `json:"js_heap_size_limit"`
	Platform            string `json:"platform"`
	Vendor              string `json:"vendor"`
	MaxTouchPoints      int    `json:"max_touch_points"`
	HasTouch            bool   `json:"has_touch"`
	IsMobile            bool   `json:"is_mobile"`
}

// ClientHints are derived once and frozen.
type ClientHints struct {
	SecChUA                string `json:"sec_ch_ua"`
	SecChUAFullVersionList string `json:"sec_ch_ua_full_version_list"`
	SecChUAMobile          string `json:"sec_ch_ua_mobile"`
	SecChUAPlatform        string `json:"sec_ch_ua_platform"`
	SecChUAPlatformVersion string `json:"sec_ch_ua_platform_version"`
	SecChViewportWidth     string `json:"sec_ch_viewport_width"`
	SecChUAFullVersion     string `json:"sec_ch_ua_full_version"`
	SecChUAArch            string `json:"sec_ch_ua_arch"`
	SecChUABitness         string `json:"sec_ch_ua_bitness"`
	SecChUAModel           string `json:"sec_ch_ua_model"`
}

// HeaderIdentity is default identity header material.
type HeaderIdentity struct {
	UserAgent             string `json:"user_agent"`
	AcceptLanguage        string `json:"accept_language"`
	AcceptEncodingDefault string `json:"accept_encoding_default"`
	PriorityDefaultFetch  string `json:"priority_default_fetch"`
}

// SentinelEnv projects Bundle into Sentinel 29-field env (release keys optional).
type SentinelEnv struct {
	UserAgent           string   `json:"userAgent"`
	Language            string   `json:"language"`
	Languages           []string `json:"languages"`
	Locale              string   `json:"locale"`
	TimezoneID          string   `json:"timezoneId"`
	ScreenWidth         int      `json:"screenWidth"`
	ScreenHeight        int      `json:"screenHeight"`
	InnerWidth          int      `json:"innerWidth"`
	InnerHeight         int      `json:"innerHeight"`
	OuterWidth          int      `json:"outerWidth"`
	OuterHeight         int      `json:"outerHeight"`
	DevicePixelRatio    float64  `json:"devicePixelRatio"`
	HardwareConcurrency int      `json:"hardwareConcurrency"`
	DeviceMemory        int      `json:"deviceMemory"`
	JSHeapSizeLimit     int64    `json:"jsHeapSizeLimit"`
	Platform            string   `json:"platform"`
	Vendor              string   `json:"vendor"`
	MaxTouchPoints      int      `json:"maxTouchPoints"`
	HasTouch            bool     `json:"hasTouch"`
	IsMobile            bool     `json:"isMobile"`
	ColorDepth          int      `json:"colorDepth"`
	PixelDepth          int      `json:"pixelDepth"`
	// Release-sticky (filled by sentinel package when known).
	ScriptSources   []string `json:"scriptSources,omitempty"`
	BuildHash       string   `json:"buildHash,omitempty"`
	DocumentKeys    []string `json:"documentKeys,omitempty"`
	WindowKeys      []string `json:"windowKeys,omitempty"`
	SearchParamKeys []string `json:"searchParamKeys,omitempty"`
}

// Noise is optional non-wire metadata.
type Noise struct {
	Enabled         bool   `json:"enabled"`
	GPUVendor       string `json:"gpu_vendor,omitempty"`
	GPUModel        string `json:"gpu_model,omitempty"`
	CanvasHash      string `json:"canvas_hash,omitempty"`
	MathFingerprint string `json:"math_fingerprint,omitempty"`
}

// ProxyAffinity binds locale/timezone policy to exit country.
type ProxyAffinity struct {
	ExpectedCountry string `json:"expected_country,omitempty"`
	ExitIP          string `json:"exit_ip,omitempty"`
	TimezonePolicy  string `json:"timezone_policy,omitempty"`
	LocalePolicy    string `json:"locale_policy,omitempty"`
}

// Consistency locks the bundle hash after freeze.
type Consistency struct {
	Locked bool   `json:"locked"`
	Hash   string `json:"hash,omitempty"`
}

// BundleV1 is retained for G0 fixture compatibility (plan §8.1 stub shape).
type BundleV1 struct {
	Version        int      `json:"version"`
	ID             string   `json:"id,omitempty"`
	Browser        string   `json:"browser,omitempty"`
	BrowserVersion string   `json:"browser_version,omitempty"`
	UserAgent      string   `json:"user_agent,omitempty"`
	SchemaKeys     []string `json:"schema_keys,omitempty"`
}

// SchemaKeys lists documented FingerprintBundle field names (v1 flat + v2 nested roots).
func SchemaKeys() []string {
	return []string{
		// v1 flat (fixtures / plan §8)
		"version", "id", "browser", "browser_version", "user_agent",
		"accept_language", "platform", "vendor", "is_mobile", "has_touch",
		"max_touch_points", "hardware_concurrency", "device_memory",
		"screen_width", "screen_height", "viewport_width", "viewport_height",
		"device_pixel_ratio", "color_depth", "pixel_depth", "timezone_id",
		"locale", "languages",
		// v2 roots
		"bundle_id", "created_at", "source", "catalog_id", "transport_profile_id",
		"identity", "device", "geometry", "navigator", "client_hints",
		"header_identity", "sentinel_env", "noise", "proxy_affinity", "consistency",
	}
}

// SchemaKeysV2 is the full nested field checklist from PURE_GO_FULL_FINGERPRINT_PLAN appendix A.
func SchemaKeysV2() []string {
	return []string{
		"version", "bundle_id", "created_at", "source", "catalog_id", "transport_profile_id",
		"identity.profile_uuid", "identity.family", "identity.browser", "identity.os",
		"identity.os_version", "identity.impersonate_label",
		"device.user_agent", "device.ua_major", "device.ua_full_version",
		"device.edge_version", "device.android_model",
		"locale.locale", "locale.languages", "locale.accept_language", "locale.timezone_id",
		"geometry.viewport_width", "geometry.viewport_height",
		"geometry.screen_width", "geometry.screen_height",
		"geometry.outer_width", "geometry.outer_height",
		"geometry.device_scale_factor", "geometry.color_depth", "geometry.pixel_depth",
		"navigator.hardware_concurrency", "navigator.device_memory",
		"navigator.js_heap_size_limit", "navigator.platform", "navigator.vendor",
		"navigator.max_touch_points", "navigator.has_touch", "navigator.is_mobile",
		"client_hints.sec_ch_ua", "client_hints.sec_ch_ua_full_version_list",
		"client_hints.sec_ch_ua_mobile", "client_hints.sec_ch_ua_platform",
		"client_hints.sec_ch_ua_platform_version", "client_hints.sec_ch_viewport_width",
		"client_hints.sec_ch_ua_full_version", "client_hints.sec_ch_ua_arch",
		"client_hints.sec_ch_ua_bitness", "client_hints.sec_ch_ua_model",
		"header_identity.user_agent", "header_identity.accept_language",
		"header_identity.accept_encoding_default", "header_identity.priority_default_fetch",
		"sentinel_env", "noise", "proxy_affinity", "consistency.locked", "consistency.hash",
	}
}

// Clone returns a deep copy safe for mutation before Freeze.
func (b Bundle) Clone() Bundle {
	out := b
	if b.Locale.Languages != nil {
		out.Locale.Languages = append([]string(nil), b.Locale.Languages...)
	}
	if b.SentinelEnv.Languages != nil {
		out.SentinelEnv.Languages = append([]string(nil), b.SentinelEnv.Languages...)
	}
	if b.SentinelEnv.ScriptSources != nil {
		out.SentinelEnv.ScriptSources = append([]string(nil), b.SentinelEnv.ScriptSources...)
	}
	if b.SentinelEnv.DocumentKeys != nil {
		out.SentinelEnv.DocumentKeys = append([]string(nil), b.SentinelEnv.DocumentKeys...)
	}
	if b.SentinelEnv.WindowKeys != nil {
		out.SentinelEnv.WindowKeys = append([]string(nil), b.SentinelEnv.WindowKeys...)
	}
	if b.SentinelEnv.SearchParamKeys != nil {
		out.SentinelEnv.SearchParamKeys = append([]string(nil), b.SentinelEnv.SearchParamKeys...)
	}
	return out
}

// IdentityHeaders returns wire headers derived from frozen client hints + UA.
// Caller still applies HeaderPreset allow-lists / order.
// Firefox emits no Client Hints — empty CH values are omitted.
func (b Bundle) IdentityHeaders() map[string]string {
	h := map[string]string{
		"user-agent":      b.HeaderIdentity.UserAgent,
		"accept-language": b.HeaderIdentity.AcceptLanguage,
	}
	// Chromium Client Hints only when present (Chrome/Edge).
	put := func(k, v string) {
		if strings.TrimSpace(v) != "" {
			h[k] = v
		}
	}
	put("sec-ch-ua", b.ClientHints.SecChUA)
	put("sec-ch-ua-full-version-list", b.ClientHints.SecChUAFullVersionList)
	put("sec-ch-ua-mobile", b.ClientHints.SecChUAMobile)
	put("sec-ch-ua-platform", b.ClientHints.SecChUAPlatform)
	put("sec-ch-ua-platform-version", b.ClientHints.SecChUAPlatformVersion)
	put("sec-ch-viewport-width", b.ClientHints.SecChViewportWidth)
	put("sec-ch-ua-full-version", b.ClientHints.SecChUAFullVersion)
	put("sec-ch-ua-arch", b.ClientHints.SecChUAArch)
	put("sec-ch-ua-bitness", b.ClientHints.SecChUABitness)
	put("sec-ch-ua-model", b.ClientHints.SecChUAModel)
	return h
}

// ToV1 projects a flat BundleV1 for legacy fixtures.
func (b Bundle) ToV1() BundleV1 {
	return BundleV1{
		Version:        1,
		ID:             b.Identity.ProfileUUID,
		Browser:        b.Identity.Browser,
		BrowserVersion: b.Device.UAFullVersion,
		UserAgent:      b.Device.UserAgent,
		SchemaKeys:     SchemaKeys(),
	}
}

// MarshalJSON canonicalizes CreatedAt as RFC3339 for stable hashing when needed.
func (b Bundle) MarshalCanonical() ([]byte, error) {
	type alias Bundle
	// Zero CreatedAt for hash material (plan: hash excludes created_at).
	cp := b
	cp.CreatedAt = time.Time{}
	cp.Consistency = Consistency{} // hash must not include itself
	return json.Marshal(alias(cp))
}

// ComputeHash returns sha256 hex of canonical bundle body.
func (b Bundle) ComputeHash() (string, error) {
	raw, err := b.MarshalCanonical()
	if err != nil {
		return "", err
	}
	// Stable key order: re-encode via sorted map walk is heavy; json.Marshal
	// on structs is field-order stable in Go. Good enough for freeze check.
	sum := sha256.Sum256(raw)
	return "sha256:" + hex.EncodeToString(sum[:]), nil
}

// Freeze derives hints/sentinel/header identity, locks consistency hash.
func (b *Bundle) Freeze() error {
	if b == nil {
		return fmt.Errorf("fingerprint: nil bundle")
	}
	b.Version = BundleVersion
	if b.Device.UserAgent == "" {
		return fmt.Errorf("fingerprint: empty user_agent")
	}
	if b.Device.UAMajor == 0 || b.Device.UAFullVersion == "" {
		major, full, err := ParseUAVersions(b.Device.UserAgent)
		if err != nil {
			return err
		}
		b.Device.UAMajor = major
		b.Device.UAFullVersion = full
	}
	b.ClientHints = DeriveClientHints(*b)
	b.HeaderIdentity = HeaderIdentity{
		UserAgent:             b.Device.UserAgent,
		AcceptLanguage:        b.Locale.AcceptLanguage,
		AcceptEncodingDefault: defaultAcceptEncoding,
		PriorityDefaultFetch:  defaultPriorityFetch,
	}
	b.SentinelEnv = ProjectSentinelEnv(*b)
	if b.Identity.ImpersonateLabel == "" && b.Device.UAMajor > 0 {
		switch b.Identity.Browser {
		case BrowserFirefox:
			b.Identity.ImpersonateLabel = fmt.Sprintf("firefox_%d", b.Device.UAMajor)
		case BrowserEdge:
			b.Identity.ImpersonateLabel = fmt.Sprintf("edge_%d", b.Device.UAMajor)
		default:
			b.Identity.ImpersonateLabel = fmt.Sprintf("chrome_%d", b.Device.UAMajor)
		}
	}
	if b.TransportProfileID == "" && b.Device.UAMajor > 0 {
		osPart := "win"
		if b.Identity.OS == OSAndroid {
			osPart = "android"
		}
		browserPart := "chrome"
		switch b.Identity.Browser {
		case BrowserFirefox:
			browserPart = "firefox"
		case BrowserEdge:
			browserPart = "edge"
		}
		b.TransportProfileID = fmt.Sprintf("%s-%d-%s-h2-v1", browserPart, b.Device.UAMajor, osPart)
	}
	hash, err := b.ComputeHash()
	if err != nil {
		return err
	}
	b.Consistency = Consistency{Locked: true, Hash: hash}
	return nil
}

// ProjectSentinelEnv builds sentinel_env from bundle fields.
func ProjectSentinelEnv(b Bundle) SentinelEnv {
	lang := b.Locale.Locale
	if len(b.Locale.Languages) > 0 {
		lang = b.Locale.Languages[0]
	}
	return SentinelEnv{
		UserAgent:           b.Device.UserAgent,
		Language:            lang,
		Languages:           append([]string(nil), b.Locale.Languages...),
		Locale:              b.Locale.Locale,
		TimezoneID:          b.Locale.TimezoneID,
		ScreenWidth:         b.Geometry.ScreenWidth,
		ScreenHeight:        b.Geometry.ScreenHeight,
		InnerWidth:          b.Geometry.ViewportWidth,
		InnerHeight:         b.Geometry.ViewportHeight,
		OuterWidth:          b.Geometry.OuterWidth,
		OuterHeight:         b.Geometry.OuterHeight,
		DevicePixelRatio:    b.Geometry.DeviceScaleFactor,
		HardwareConcurrency: b.Navigator.HardwareConcurrency,
		DeviceMemory:        b.Navigator.DeviceMemory,
		JSHeapSizeLimit:     b.Navigator.JSHeapSizeLimit,
		Platform:            b.Navigator.Platform,
		Vendor:              b.Navigator.Vendor,
		MaxTouchPoints:      b.Navigator.MaxTouchPoints,
		HasTouch:            b.Navigator.HasTouch,
		IsMobile:            b.Navigator.IsMobile,
		ColorDepth:          b.Geometry.ColorDepth,
		PixelDepth:          b.Geometry.PixelDepth,
		// preserve release-sticky if already set
		ScriptSources:   append([]string(nil), b.SentinelEnv.ScriptSources...),
		BuildHash:       b.SentinelEnv.BuildHash,
		DocumentKeys:    append([]string(nil), b.SentinelEnv.DocumentKeys...),
		WindowKeys:      append([]string(nil), b.SentinelEnv.WindowKeys...),
		SearchParamKeys: append([]string(nil), b.SentinelEnv.SearchParamKeys...),
	}
}

const (
	// HAR Firefox sends gzip, deflate, br, zstd — align default encoding.
	defaultAcceptEncoding = "gzip, deflate, br, zstd"
	defaultPriorityFetch  = "u=1, i"
)

// ParseJSON unmarshals a bundle from API/checkpoint JSON.
func ParseJSON(raw []byte) (*Bundle, error) {
	if len(raw) == 0 || string(raw) == "null" {
		return nil, fmt.Errorf("fingerprint: empty profile")
	}
	var b Bundle
	if err := json.Unmarshal(raw, &b); err != nil {
		return nil, fmt.Errorf("fingerprint: parse: %w", err)
	}
	if b.Version == 0 {
		b.Version = BundleVersion
	}
	return &b, nil
}

// Must be after Freeze for production use; Validate still works pre-freeze with locked=false.
func (b Bundle) String() string {
	return fmt.Sprintf("Bundle{id=%s family=%s browser=%s ua_major=%d locked=%v}",
		b.BundleID, b.Identity.Family, b.Identity.Browser, b.Device.UAMajor, b.Consistency.Locked)
}

// SortedSchemaKeys returns SchemaKeys sorted (test helper).
func SortedSchemaKeys() []string {
	keys := SchemaKeys()
	sort.Strings(keys)
	return keys
}

// NormalizeCountry uppercases ISO-like country codes.
func NormalizeCountry(c string) string {
	return strings.ToUpper(strings.TrimSpace(c))
}
