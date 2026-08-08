// Package transport holds TransportProfile and per-job client interfaces.
package transport

import "strings"

// ProfileV1 matches plan section 10.1 TransportProfile (hashes / ids).
// Status values: active | capture_required | retired
type ProfileV1 struct {
	ID                         string            `json:"id"`
	BaselineCaptureID          string            `json:"baseline_capture_id,omitempty"`
	Browser                    string            `json:"browser,omitempty"`
	BrowserVersion             string            `json:"browser_version,omitempty"`
	BrowserMajor               int               `json:"browser_major,omitempty"`
	OS                         string            `json:"os,omitempty"`
	GoVersion                  string            `json:"go_version,omitempty"`
	ModuleGraphFixture         string            `json:"module_graph_fixture,omitempty"`
	TLSClientHelloFixture      string            `json:"tls_client_hello_fixture,omitempty"`
	TLSExtensionOrderFixture   string            `json:"tls_extension_order_fixture,omitempty"`
	ALPN                       []string          `json:"alpn,omitempty"`
	HTTP2SettingsFixture       string            `json:"http2_settings_fixture,omitempty"`
	HTTP2ConnectionFlowFixture string            `json:"http2_connection_flow_fixture,omitempty"`
	HTTP2PseudoHeaderOrder     []string          `json:"http2_pseudo_header_order,omitempty"`
	HeaderPresets              map[string]string `json:"header_presets,omitempty"`
	ResponseContentEncodingFix string            `json:"response_content_encoding_fixture,omitempty"`
	RedirectMaxHops            int               `json:"redirect_max_hops,omitempty"`
	CertificateValidation      bool              `json:"certificate_validation"`
	BridgeRequired             bool              `json:"bridge_required"`
	Status                     string            `json:"status,omitempty"`
}

// Profile is the alias used by G2+ code paths.
type Profile = ProfileV1

// DefaultObservation returns a capture_required transport shell for fixtures.
func DefaultObservation() ProfileV1 {
	return ProfileV1{
		ID:                     "chrome-baseline-capture_required",
		GoVersion:              "go1.22.12",
		ALPN:                   []string{"h2", "http/1.1"},
		HTTP2PseudoHeaderOrder: []string{":method", ":authority", ":scheme", ":path"},
		HeaderPresets: map[string]string{
			"document_navigation": "capture_required",
			"same_origin_fetch":   "capture_required",
			"cross_origin_oauth":  "capture_required",
			"otp_sparse":          "capture_required",
			"sentinel_req":        "capture_required",
		},
		RedirectMaxHops:       10,
		CertificateValidation: true,
		BridgeRequired:        true,
		Status:                "capture_required",
	}
}

// ProfileForMajor returns a draft profile id binding for a Chrome major (still capture_required until wire fixtures land).
func ProfileForMajor(major int, osName string) ProfileV1 {
	p := DefaultObservation()
	osPart := "win"
	if osName == "android" {
		osPart = "android"
	}
	p.ID = formatProfileID(major, osPart)
	p.Browser = "chrome"
	p.BrowserMajor = major
	p.OS = osName
	if osName == "" {
		p.OS = "windows"
	}
	return p
}

func formatProfileID(major int, osPart string) string {
	return "chrome-" + itoa(major) + "-" + osPart + "-h2-v1"
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var b [12]byte
	i := len(b)
	for n > 0 {
		i--
		b[i] = byte('0' + n%10)
		n /= 10
	}
	return string(b[i:])
}

// MajorFromProfileID parses chrome-<major>- / firefox-<major>- / edge-<major>- ids.
func MajorFromProfileID(id string) (int, bool) {
	id = strings.ToLower(strings.TrimSpace(id))
	for _, prefix := range []string{"chrome-", "firefox-", "edge-"} {
		if len(id) < len(prefix)+1 || !strings.HasPrefix(id, prefix) {
			continue
		}
		rest := id[len(prefix):]
		n := 0
		i := 0
		for i < len(rest) && rest[i] >= '0' && rest[i] <= '9' {
			n = n*10 + int(rest[i]-'0')
			i++
		}
		if i == 0 || n <= 0 {
			return 0, false
		}
		return n, true
	}
	return 0, false
}

// BrowserFromProfileID returns chrome|firefox|edge from transport profile id.
func BrowserFromProfileID(id string) string {
	id = strings.ToLower(strings.TrimSpace(id))
	switch {
	case strings.HasPrefix(id, "firefox-"):
		return "firefox"
	case strings.HasPrefix(id, "edge-"):
		return "edge"
	case strings.HasPrefix(id, "chrome-"):
		return "chrome"
	default:
		return ""
	}
}
