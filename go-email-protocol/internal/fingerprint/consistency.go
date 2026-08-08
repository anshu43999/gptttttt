package fingerprint

import (
	"fmt"
	"strconv"
	"strings"
)

// ErrorCode is a stable failure code for API/ledger.
type ErrorCode string

const (
	CodeInconsistent      ErrorCode = "fingerprint_inconsistent"
	CodeTransportMismatch ErrorCode = "transport_profile_mismatch"
	CodeProxyAffinity     ErrorCode = "proxy_affinity_mismatch"
	CodeHashMismatch      ErrorCode = "fingerprint_hash_mismatch"
	CodeNotLocked         ErrorCode = "fingerprint_not_locked"
)

// Error is a typed fingerprint failure.
type Error struct {
	Code    ErrorCode
	Message string
}

func (e *Error) Error() string {
	if e == nil {
		return ""
	}
	return fmt.Sprintf("%s: %s", e.Code, e.Message)
}

// ValidateOptions tunes Validate.
type ValidateOptions struct {
	RequireLocked          bool
	ExpectedTransportMajor int // 0 = derive from transport_profile_id or skip
	SkipProxyAffinity      bool
}

// Validate runs the full consistency engine (plan §3.12).
func (b Bundle) Validate(opts ValidateOptions) error {
	if b.Version != 0 && b.Version != BundleVersion {
		return &Error{Code: CodeInconsistent, Message: fmt.Sprintf("unsupported version %d", b.Version)}
	}
	if strings.TrimSpace(b.Identity.ProfileUUID) == "" {
		return &Error{Code: CodeInconsistent, Message: "missing identity.profile_uuid"}
	}
	if strings.TrimSpace(b.Device.UserAgent) == "" {
		return &Error{Code: CodeInconsistent, Message: "missing device.user_agent"}
	}

	family := b.Identity.Family
	browser := b.Identity.Browser
	osName := b.Identity.OS

	switch family {
	case FamilyDesktop, FamilyMobile:
	default:
		return &Error{Code: CodeInconsistent, Message: "identity.family must be desktop|mobile"}
	}
	switch browser {
	case BrowserChrome, BrowserEdge, BrowserFirefox:
	default:
		return &Error{Code: CodeInconsistent, Message: "identity.browser must be chrome|edge|firefox"}
	}
	if family == FamilyMobile && browser != BrowserChrome {
		return &Error{Code: CodeInconsistent, Message: "mobile family requires browser=chrome"}
	}
	if browser == BrowserFirefox && family != FamilyDesktop {
		return &Error{Code: CodeInconsistent, Message: "firefox requires family=desktop"}
	}
	switch osName {
	case OSWindows, OSAndroid:
	default:
		return &Error{Code: CodeInconsistent, Message: "identity.os must be windows|android"}
	}
	if family == FamilyDesktop && osName != OSWindows {
		return &Error{Code: CodeInconsistent, Message: "desktop requires os=windows"}
	}
	if family == FamilyMobile && osName != OSAndroid {
		return &Error{Code: CodeInconsistent, Message: "mobile requires os=android"}
	}

	// navigator ↔ family
	if family == FamilyDesktop {
		if b.Navigator.IsMobile || b.Navigator.HasTouch || b.Navigator.MaxTouchPoints != 0 {
			return &Error{Code: CodeInconsistent, Message: "desktop navigator touch/mobile flags invalid"}
		}
		if b.Navigator.Platform != "Win32" {
			return &Error{Code: CodeInconsistent, Message: "desktop platform must be Win32"}
		}
		// Chrome/Edge: Google Inc.; Firefox: empty vendor (Gecko).
		if browser == BrowserFirefox {
			if b.Navigator.Vendor != "" {
				return &Error{Code: CodeInconsistent, Message: "firefox vendor must be empty"}
			}
		} else if b.Navigator.Vendor != "Google Inc." {
			// soft? keep existing strict for chromium
			if b.Navigator.Vendor != "Google Inc." {
				return &Error{Code: CodeInconsistent, Message: "chromium vendor must be Google Inc."}
			}
		}
	}
	if family == FamilyMobile {
		if !b.Navigator.IsMobile || !b.Navigator.HasTouch || b.Navigator.MaxTouchPoints <= 0 {
			return &Error{Code: CodeInconsistent, Message: "mobile navigator touch/mobile flags invalid"}
		}
		if b.Navigator.Platform != "Linux armv8l" {
			return &Error{Code: CodeInconsistent, Message: "mobile platform must be Linux armv8l"}
		}
		if strings.TrimSpace(b.Device.AndroidModel) == "" {
			return &Error{Code: CodeInconsistent, Message: "mobile requires device.android_model"}
		}
	}

	// UA parse consistency
	major, full, err := ParseUAVersions(b.Device.UserAgent)
	if err != nil {
		return &Error{Code: CodeInconsistent, Message: err.Error()}
	}
	if b.Device.UAMajor != 0 && b.Device.UAMajor != major {
		return &Error{Code: CodeInconsistent, Message: fmt.Sprintf("ua_major %d != parsed %d", b.Device.UAMajor, major)}
	}
	// Modern Chromium UAs often reduce to M.0.0.0 while Client Hints carry the real full version (SAZ Edge 150).
	// Allow Device.UAFullVersion to be richer than the reduced UA token when UA literally contains M.0.0.0.
	reducedUA := strings.Contains(b.Device.UserAgent, fmt.Sprintf("%d.0.0.0", major))
	if b.Device.UAFullVersion != "" && b.Device.UAFullVersion != full {
		if !(reducedUA && strings.HasPrefix(b.Device.UAFullVersion, fmt.Sprintf("%d.", major))) {
			return &Error{Code: CodeInconsistent, Message: "ua_full_version mismatch with UA string"}
		}
		// Prefer stored full for CH validation below.
		full = b.Device.UAFullVersion
	}
	// forbid lazy M.0.0.0 in Device.UAFullVersion unless UA is also reduced or identical
	if strings.HasSuffix(b.Device.UAFullVersion, ".0.0.0") && !strings.Contains(b.Device.UserAgent, b.Device.UAFullVersion) {
		return &Error{Code: CodeInconsistent, Message: "refusing synthetic M.0.0.0 full version"}
	}

// geometry basics
	if b.Geometry.ViewportWidth <= 0 || b.Geometry.ViewportHeight <= 0 {
		return &Error{Code: CodeInconsistent, Message: "invalid viewport"}
	}
	if b.Geometry.ScreenWidth <= 0 || b.Geometry.ScreenHeight <= 0 {
		return &Error{Code: CodeInconsistent, Message: "invalid screen"}
	}
	if b.Geometry.ColorDepth == 0 {
		return &Error{Code: CodeInconsistent, Message: "color_depth required"}
	}

	// locale atomic
	if b.Locale.Locale == "" || b.Locale.AcceptLanguage == "" || b.Locale.TimezoneID == "" {
		return &Error{Code: CodeInconsistent, Message: "locale tuple incomplete"}
	}
	if len(b.Locale.Languages) == 0 {
		return &Error{Code: CodeInconsistent, Message: "locale.languages empty"}
	}

	// client hints if present (post-freeze)
	if b.ClientHints.SecChUA != "" {
		if err := validateClientHints(b, major, full); err != nil {
			return err
		}
	}

	// transport profile major binding
	if b.TransportProfileID != "" {
		if tpMajor, ok := parseTransportMajor(b.TransportProfileID); ok {
			if tpMajor != major {
				return &Error{
					Code:    CodeTransportMismatch,
					Message: fmt.Sprintf("transport_profile_id major %d != ua major %d", tpMajor, major),
				}
			}
		}
	}
	if opts.ExpectedTransportMajor > 0 && opts.ExpectedTransportMajor != major {
		return &Error{
			Code:    CodeTransportMismatch,
			Message: fmt.Sprintf("expected transport major %d got ua %d", opts.ExpectedTransportMajor, major),
		}
	}

	if !opts.SkipProxyAffinity {
		if err := validateProxyAffinity(b); err != nil {
			return err
		}
	}

	if opts.RequireLocked {
		if !b.Consistency.Locked || b.Consistency.Hash == "" {
			return &Error{Code: CodeNotLocked, Message: "bundle must be Freeze()'d before use"}
		}
		sum, err := b.ComputeHash()
		if err != nil {
			return err
		}
		if sum != b.Consistency.Hash {
			return &Error{Code: CodeHashMismatch, Message: "consistency.hash does not match body"}
		}
	}
	return nil
}

func validateClientHints(b Bundle, major int, full string) error {
	ch := b.ClientHints
	majorStr := strconv.Itoa(major)
	if !strings.Contains(ch.SecChUA, majorStr) {
		return &Error{Code: CodeInconsistent, Message: "sec-ch-ua missing ua major"}
	}
	if !strings.Contains(ch.SecChUAFullVersionList, full) {
		return &Error{Code: CodeInconsistent, Message: "sec-ch-ua-full-version-list missing full version"}
	}
	wantMobile := "?0"
	if b.Navigator.IsMobile {
		wantMobile = "?1"
	}
	if ch.SecChUAMobile != wantMobile {
		return &Error{Code: CodeInconsistent, Message: "sec-ch-ua-mobile mismatch"}
	}
	if b.Navigator.IsMobile {
		if ch.SecChUAPlatform != `"Android"` {
			return &Error{Code: CodeInconsistent, Message: "sec-ch-ua-platform expected Android"}
		}
	} else if ch.SecChUAPlatform != `"Windows"` {
		return &Error{Code: CodeInconsistent, Message: "sec-ch-ua-platform expected Windows"}
	}
	wantVP := fmt.Sprintf(`"%d"`, b.Geometry.ViewportWidth)
	if ch.SecChViewportWidth != wantVP {
		return &Error{Code: CodeInconsistent, Message: "sec-ch-viewport-width mismatch"}
	}
	// brand family + full-version header
	if b.Identity.Browser == BrowserEdge {
		if !strings.Contains(ch.SecChUA, "Microsoft Edge") {
			return &Error{Code: CodeInconsistent, Message: "edge bundle missing Edge brand"}
		}
		// SAZ: sec-ch-ua-full-version is Edge build, not Chromium full.
		edgeFull := b.Device.EdgeVersion
		if edgeFull == "" {
			edgeFull = full
		}
		if ch.SecChUAFullVersion != "" && ch.SecChUAFullVersion != fmt.Sprintf(`"%s"`, edgeFull) {
			return &Error{Code: CodeInconsistent, Message: "sec-ch-ua-full-version mismatch edge"}
		}
	} else {
		if !strings.Contains(ch.SecChUA, "Google Chrome") {
			return &Error{Code: CodeInconsistent, Message: "chrome bundle missing Google Chrome brand"}
		}
		if ch.SecChUAFullVersion != "" && ch.SecChUAFullVersion != fmt.Sprintf(`"%s"`, full) {
			return &Error{Code: CodeInconsistent, Message: "sec-ch-ua-full-version mismatch"}
		}
	}
	return nil
}

func validateProxyAffinity(b Bundle) error {
	policy := b.ProxyAffinity.TimezonePolicy
	if policy == "" {
		policy = b.ProxyAffinity.LocalePolicy
	}
	country := NormalizeCountry(b.ProxyAffinity.ExpectedCountry)
	if country == "" || policy == "" || policy == TimezoneCatalogOnly || policy == TimezoneAllowGlobalEN {
		return nil
	}
	if policy != TimezoneStrictMatch && policy != "strict" {
		return nil
	}
	// Map timezone → acceptable countries (minimal catalog)
	tz := b.Locale.TimezoneID
	ok := countryMatchesTimezone(country, tz)
	if !ok {
		return &Error{
			Code:    CodeProxyAffinity,
			Message: fmt.Sprintf("country %s incompatible with timezone %s under %s", country, tz, policy),
		}
	}
	return nil
}

func countryMatchesTimezone(country, tz string) bool {
	// Expand carefully; fail closed on unknown pairs under strict.
	table := map[string][]string{
		"US": {"America/Los_Angeles", "America/New_York", "America/Chicago", "America/Denver"},
		"GB": {"Europe/London"},
		"UK": {"Europe/London"},
		"CN": {"Asia/Shanghai"},
		"JP": {"Asia/Tokyo"},
		"DE": {"Europe/Berlin"},
		// HAR Amazonas GMT-0400 + common BR zones
		"BR": {"America/Manaus", "America/Sao_Paulo", "America/Recife", "America/Fortaleza", "America/Belem"},
	}
	list, ok := table[country]
	if !ok {
		return false
	}
	for _, t := range list {
		if t == tz {
			return true
		}
	}
	return false
}

func parseTransportMajor(id string) (int, bool) {
	// chrome-142-win-h2-v1
	parts := strings.Split(id, "-")
	if len(parts) < 2 {
		return 0, false
	}
	if parts[0] != "chrome" && parts[0] != "edge" {
		return 0, false
	}
	n, err := strconv.Atoi(parts[1])
	if err != nil || n <= 0 {
		return 0, false
	}
	return n, true
}

// AssertReady is Validate with RequireLocked for request path.
func (b Bundle) AssertReady() error {
	return b.Validate(ValidateOptions{RequireLocked: true})
}
