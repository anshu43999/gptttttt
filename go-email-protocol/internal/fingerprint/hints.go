package fingerprint

import (
	"fmt"
	"regexp"
	"strconv"
	"strings"
)

var (
	reChromeFull  = regexp.MustCompile(`(?:Chrome|CriOS)/(\d+)\.(\d+)\.(\d+)\.(\d+)`)
	reEdgeFull    = regexp.MustCompile(`Edg/(\d+)\.(\d+)\.(\d+)\.(\d+)`)
	reFirefoxFull = regexp.MustCompile(`Firefox/(\d+)(?:\.(\d+))?`)
	reFirefoxRV   = regexp.MustCompile(`rv:(\d+)(?:\.(\d+))?`)
)

// ParseUAVersions extracts browser major and full version from UA.
// Chrome/Edge: M.build.patch style. Firefox: major (and optional minor) as "150.0".
func ParseUAVersions(ua string) (major int, full string, err error) {
	if m := reChromeFull.FindStringSubmatch(ua); m != nil {
		major, _ = strconv.Atoi(m[1])
		full = fmt.Sprintf("%s.%s.%s.%s", m[1], m[2], m[3], m[4])
		if major <= 0 {
			return 0, "", fmt.Errorf("fingerprint: invalid Chrome major in UA")
		}
		return major, full, nil
	}
	if m := reFirefoxFull.FindStringSubmatch(ua); m != nil {
		major, _ = strconv.Atoi(m[1])
		minor := "0"
		if len(m) > 2 && m[2] != "" {
			minor = m[2]
		}
		// Prefer rv: if present and matches family
		if rv := reFirefoxRV.FindStringSubmatch(ua); rv != nil {
			if rmaj, _ := strconv.Atoi(rv[1]); rmaj > 0 {
				major = rmaj
				if len(rv) > 2 && rv[2] != "" {
					minor = rv[2]
				} else {
					minor = "0"
				}
			}
		}
		if major <= 0 {
			return 0, "", fmt.Errorf("fingerprint: invalid Firefox major in UA")
		}
		full = fmt.Sprintf("%d.%s", major, minor)
		return major, full, nil
	}
	return 0, "", fmt.Errorf("fingerprint: cannot parse browser version from UA")
}

// ParseEdgeVersion extracts Edg/ full version if present.
func ParseEdgeVersion(ua string) string {
	m := reEdgeFull.FindStringSubmatch(ua)
	if m == nil {
		return ""
	}
	return fmt.Sprintf("%s.%s.%s.%s", m[1], m[2], m[3], m[4])
}

// IsFirefoxUA reports Gecko Firefox desktop/mobile UA (not Chrome).
func IsFirefoxUA(ua string) bool {
	return strings.Contains(ua, "Firefox/") && strings.Contains(ua, "Gecko/") &&
		!strings.Contains(ua, "Chrome/") && !strings.Contains(ua, "Edg/")
}

// DeriveClientHints builds frozen Client Hints from identity + device + geometry.
// Firefox does not send Client Hints — returns zero-value (empty) CH.
// Windows platform-version aligns with SAZ Edge 150 capture "19.0.0" (Win11).
func DeriveClientHints(b Bundle) ClientHints {
	if b.Identity.Browser == BrowserFirefox || IsFirefoxUA(b.Device.UserAgent) {
		return ClientHints{} // no sec-ch-ua* on Firefox wire
	}

	major := b.Device.UAMajor
	full := b.Device.UAFullVersion
	if major == 0 || full == "" {
		if m, f, err := ParseUAVersions(b.Device.UserAgent); err == nil {
			major, full = m, f
		}
	}
	majorStr := strconv.Itoa(major)
	// SAZ 2026-07: "Not;A=Brand";v="8" / full 8.0.0.0 (not legacy Not.A/Brand v=24).
	notBrand := "8"
	notBrandFull := "8.0.0.0"

	var brands []string
	var fullBrands []string
	fullVersionHeader := full // sec-ch-ua-full-version: Edge uses Edge build (SAZ)
	switch b.Identity.Browser {
	case BrowserEdge:
		edgeFull := b.Device.EdgeVersion
		if edgeFull == "" {
			edgeFull = ParseEdgeVersion(b.Device.UserAgent)
		}
		if edgeFull == "" {
			edgeFull = full
		}
		edgeMajor := majorStr
		if parts := strings.Split(edgeFull, "."); len(parts) > 0 {
			edgeMajor = parts[0]
		}
		fullVersionHeader = edgeFull
		// SAZ order: Not;A=Brand, Chromium, Microsoft Edge
		brands = []string{
			fmt.Sprintf(`"Not;A=Brand";v="%s"`, notBrand),
			fmt.Sprintf(`"Chromium";v="%s"`, majorStr),
			fmt.Sprintf(`"Microsoft Edge";v="%s"`, edgeMajor),
		}
		fullBrands = []string{
			fmt.Sprintf(`"Not;A=Brand";v="%s"`, notBrandFull),
			fmt.Sprintf(`"Chromium";v="%s"`, full),
			fmt.Sprintf(`"Microsoft Edge";v="%s"`, edgeFull),
		}
	default:
		// Chrome modern order commonly Not;A=Brand, Chromium, Google Chrome
		brands = []string{
			fmt.Sprintf(`"Not;A=Brand";v="%s"`, notBrand),
			fmt.Sprintf(`"Chromium";v="%s"`, majorStr),
			fmt.Sprintf(`"Google Chrome";v="%s"`, majorStr),
		}
		fullBrands = []string{
			fmt.Sprintf(`"Not;A=Brand";v="%s"`, notBrandFull),
			fmt.Sprintf(`"Chromium";v="%s"`, full),
			fmt.Sprintf(`"Google Chrome";v="%s"`, full),
		}
	}

	mobile := "?0"
	platform := `"Windows"`
	platformVersion := `"19.0.0"` // SAZ Win11 Edge 150
	arch := `"x86"`
	bitness := `"64"`
	model := `""`

	if b.Identity.Family == FamilyMobile || b.Navigator.IsMobile || b.Identity.OS == OSAndroid {
		mobile = "?1"
		platform = `"Android"`
		// Android platform version ≈ OS major
		ver := b.Identity.OSVersion
		if ver == "" {
			ver = "14.0.0"
		}
		if !strings.Contains(ver, ".") {
			ver = ver + ".0.0"
		}
		platformVersion = fmt.Sprintf(`"%s"`, ver)
		arch = `""`
		bitness = `""`
		if b.Device.AndroidModel != "" {
			model = fmt.Sprintf(`"%s"`, b.Device.AndroidModel)
		}
	}

	return ClientHints{
		SecChUA:                strings.Join(brands, ", "),
		SecChUAFullVersionList: strings.Join(fullBrands, ", "),
		SecChUAMobile:          mobile,
		SecChUAPlatform:        platform,
		SecChUAPlatformVersion: platformVersion,
		SecChViewportWidth:     fmt.Sprintf(`"%d"`, b.Geometry.ViewportWidth),
		SecChUAFullVersion:     fmt.Sprintf(`"%s"`, fullVersionHeader),
		SecChUAArch:            arch,
		SecChUABitness:         bitness,
		SecChUAModel:           model,
	}
}
