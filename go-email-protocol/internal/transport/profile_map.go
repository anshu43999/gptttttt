package transport

import (
	"fmt"
	"strconv"
	"strings"
)

// ChromeProfileNameForMajor returns the tls-client profile label for a UA major.
// Shared by tagged and untagged builds for tests / diagnostics.
func ChromeProfileNameForMajor(major int) (string, error) {
	if major <= 0 {
		return "Chrome_133", nil
	}
	switch {
	case major >= 133:
		return "Chrome_133", nil
	case major >= 131:
		return "Chrome_131", nil
	case major >= 124:
		return "Chrome_124", nil
	case major >= 120:
		return "Chrome_120", nil
	case major >= 117:
		return "Chrome_117", nil
	case major >= 112:
		return "Chrome_112", nil
	case major >= 111:
		return "Chrome_111", nil
	case major >= 110:
		return "Chrome_110", nil
	case major >= 109:
		return "Chrome_109", nil
	case major >= 108:
		return "Chrome_108", nil
	case major >= 107:
		return "Chrome_107", nil
	case major >= 106:
		return "Chrome_106", nil
	case major >= 105:
		return "Chrome_105", nil
	case major >= 104:
		return "Chrome_104", nil
	case major >= 103:
		return "Chrome_103", nil
	default:
		return "", fmt.Errorf("transport: chrome major %d below supported floor 103", major)
	}
}

// EffectiveProfile is the read-only outcome used to choose a tls-client
// profile. Fallback is explicit and therefore cannot masquerade as wire parity.
type EffectiveProfile struct {
	RequestedBrowser string `json:"requested_browser"`
	RequestedMajor   int    `json:"requested_major"`
	EffectiveBrowser string `json:"effective_browser"`
	EffectiveMajor   int    `json:"effective_major"`
	ProfileName      string `json:"profile_name"`
	Fallback         bool   `json:"fallback"`
}

// ResolveEffectiveTLSProfile exposes the same profile-name mapping consumed by
// the tagged TLS factory without constructing a network client.
func ResolveEffectiveTLSProfile(browser string, major int) (EffectiveProfile, error) {
	requested := strings.ToLower(strings.TrimSpace(browser))
	switch requested {
	case "ff", "gecko":
		requested = "firefox"
	case "chromium":
		requested = "chrome"
	}
	if major <= 0 {
		return EffectiveProfile{}, fmt.Errorf("transport: browser major must be positive")
	}
	var effectiveBrowser, name string
	var err error
	switch requested {
	case "firefox":
		effectiveBrowser = "firefox"
		name, err = FirefoxProfileNameForMajor(major)
	case "chrome":
		effectiveBrowser = "chrome"
		name, err = ChromeProfileNameForMajor(major)
	case "edge":
		// The current factory falls through to Chrome profiles for Edge.
		effectiveBrowser = "chrome"
		name, err = ChromeProfileNameForMajor(major)
	default:
		return EffectiveProfile{}, fmt.Errorf("transport: unsupported browser %q", browser)
	}
	if err != nil {
		return EffectiveProfile{}, err
	}
	effectiveMajor, err := profileNameMajor(name)
	if err != nil {
		return EffectiveProfile{}, err
	}
	return EffectiveProfile{
		RequestedBrowser: requested,
		RequestedMajor: major,
		EffectiveBrowser: effectiveBrowser,
		EffectiveMajor: effectiveMajor,
		ProfileName: name,
		Fallback: effectiveBrowser != requested || effectiveMajor != major,
	}, nil
}

// FirefoxProfileNameForMajor mirrors the exact profile set linked by tls-client v1.9.1.
func FirefoxProfileNameForMajor(major int) (string, error) {
	if major <= 0 {
		return "", fmt.Errorf("transport: firefox major must be positive")
	}
	switch {
	case major >= 135:
		return "Firefox_135", nil
	case major >= 133:
		return "Firefox_133", nil
	case major >= 132:
		return "Firefox_132", nil
	case major >= 123:
		return "Firefox_123", nil
	case major >= 120:
		return "Firefox_120", nil
	case major >= 117:
		return "Firefox_117", nil
	case major >= 110:
		return "Firefox_110", nil
	case major >= 108:
		return "Firefox_108", nil
	case major >= 106:
		return "Firefox_106", nil
	case major >= 105:
		return "Firefox_105", nil
	case major >= 104:
		return "Firefox_104", nil
	default:
		return "Firefox_102", nil
	}
}

func profileNameMajor(name string) (int, error) {
	underscore := strings.LastIndexByte(name, '_')
	if underscore < 0 || underscore == len(name)-1 {
		return 0, fmt.Errorf("transport: invalid client profile name %q", name)
	}
	major, err := strconv.Atoi(name[underscore+1:])
	if err != nil || major <= 0 {
		return 0, fmt.Errorf("transport: invalid client profile name %q", name)
	}
	return major, nil
}
