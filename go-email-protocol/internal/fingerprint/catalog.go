package fingerprint

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	mathrand "math/rand/v2"
	"strings"
	"time"
)

// GenerateOptions controls catalog generation.
type GenerateOptions struct {
	// RNG if nil, uses math/rand/v2 global-less ChaCha8 from crypto seed.
	RNG *mathrand.Rand
	// DesktopRatio default 0.68
	DesktopRatio float64
	// ForceFamily: desktop|mobile|""
	ForceFamily string
	// ForceBrowser: chrome|edge|firefox|"" — when set, overrides desktop browser pick.
	// Aligns with HAR learning (user capture is Firefox/150).
	ForceBrowser string
	// TransportProfileID optional override
	TransportProfileID string
	// ExpectedCountry / ExitIP for proxy affinity
	ExpectedCountry string
	ExitIP          string
	// TimezonePolicy default strict_match when country set, else catalog_only
	TimezonePolicy string
	LocalePolicy   string
	// NoiseEnabled attaches gpu/canvas noise
	NoiseEnabled bool
	// Source defaults to generated
	Source string
	// Now for CreatedAt
	Now time.Time
}

type localeTuple struct {
	locale, accept string
	languages      []string
	timezone       string
	// countries that prefer this tuple under strict_match (ISO upper)
	countries []string
}

type viewportTuple struct {
	vw, vh, sw, sh int
	dpr            float64
}

var desktopLocales = []localeTuple{
	// HAR capture (user): pt-BR primary with en-US fallback; America/Manaus (GMT-0400 Amazonas).
	{locale: "pt-BR", languages: []string{"pt-BR", "pt", "en-US", "en"}, accept: "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7", timezone: "America/Manaus", countries: []string{"BR"}},
	{locale: "en-US", languages: []string{"en-US", "en"}, accept: "en-US,en;q=0.9", timezone: "America/Los_Angeles", countries: []string{"US"}},
	{locale: "en-GB", languages: []string{"en-GB", "en"}, accept: "en-GB,en;q=0.9", timezone: "Europe/London", countries: []string{"GB", "UK"}},
	{locale: "zh-CN", languages: []string{"zh-CN", "zh"}, accept: "zh-CN,zh;q=0.9,en;q=0.8", timezone: "Asia/Shanghai", countries: []string{"CN"}},
	{locale: "ja-JP", languages: []string{"ja-JP", "ja"}, accept: "ja,en-US;q=0.9,en;q=0.8", timezone: "Asia/Tokyo", countries: []string{"JP"}},
	{locale: "de-DE", languages: []string{"de-DE", "de", "en"}, accept: "de-DE,de;q=0.9,en;q=0.8", timezone: "Europe/Berlin", countries: []string{"DE"}},
}

var mobileLocales = []localeTuple{
	{locale: "en-US", languages: []string{"en-US", "en"}, accept: "en-US,en;q=0.9", timezone: "America/New_York", countries: []string{"US"}},
	{locale: "en-GB", languages: []string{"en-GB", "en"}, accept: "en-GB,en;q=0.9", timezone: "Europe/London", countries: []string{"GB", "UK"}},
	{locale: "zh-CN", languages: []string{"zh-CN", "zh"}, accept: "zh-CN,zh;q=0.9,en;q=0.8", timezone: "Asia/Shanghai", countries: []string{"CN"}},
	{locale: "ja-JP", languages: []string{"ja-JP", "ja"}, accept: "ja,en-US;q=0.9,en;q=0.8", timezone: "Asia/Tokyo", countries: []string{"JP"}},
	{locale: "pt-BR", languages: []string{"pt-BR", "pt", "en-US", "en"}, accept: "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7", timezone: "America/Sao_Paulo", countries: []string{"BR"}},
}

var desktopViewports = []viewportTuple{
	// HAR Datadog RUM display.viewport ≈ 1280x705
	{1280, 720, 1280, 720, 1},
	{1280, 705, 1280, 720, 1},
	{1365, 768, 1366, 768, 1},
	{1440, 900, 1440, 900, 1},
	{1536, 864, 1536, 864, 1.25},
	{1600, 900, 1600, 900, 1},
	{1710, 1067, 1728, 1117, 1.5},
	{1920, 1080, 1920, 1080, 1},
}

var mobileViewports = []viewportTuple{
	{360, 800, 360, 800, 3},
	{390, 844, 390, 844, 3},
	{393, 873, 393, 873, 2.75},
	{412, 915, 412, 915, 2.625},
	{430, 932, 430, 932, 3},
}

var androidModels = []string{
	"Pixel 7", "Pixel 8", "Pixel 8 Pro", "SM-S918B", "SM-S928B", "CPH2487", "MI 13",
}

var gpuPool = [][2]string{
	{"Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
	{"Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
	{"Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
	{"Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 580 Series Direct3D11 vs_5_0 ps_5_0, D3D11)"},
}

// Generate builds a full Bundle v2 from catalog, freezes it, and validates.
func Generate(opts GenerateOptions) (*Bundle, error) {
	rng := opts.RNG
	if rng == nil {
		var seed [32]byte
		if _, err := rand.Read(seed[:]); err != nil {
			return nil, fmt.Errorf("fingerprint: rng seed: %w", err)
		}
		rng = mathrand.New(mathrand.NewChaCha8(seed))
	}
	ratio := opts.DesktopRatio
	if ratio <= 0 {
		ratio = 0.68
	}
	family := opts.ForceFamily
	if family == "" {
		if rng.Float64() < ratio {
			family = FamilyDesktop
		} else {
			family = FamilyMobile
		}
	}

	var b Bundle
	var err error
	switch family {
	case FamilyMobile:
		b, err = buildMobile(rng)
	default:
		b, err = buildDesktop(rng, opts.ForceBrowser)
	}
	if err != nil {
		return nil, err
	}

	now := opts.Now
	if now.IsZero() {
		now = time.Now().UTC()
	}
	b.Version = BundleVersion
	b.BundleID = newBundleID()
	b.CreatedAt = now
	b.Source = opts.Source
	if b.Source == "" {
		b.Source = SourceGenerated
	}
	if opts.TransportProfileID != "" {
		b.TransportProfileID = opts.TransportProfileID
	}

	// Locale selection with optional country affinity
	country := NormalizeCountry(opts.ExpectedCountry)
	policyTZ := opts.TimezonePolicy
	policyLoc := opts.LocalePolicy
	if policyTZ == "" {
		if country != "" {
			policyTZ = TimezoneStrictMatch
		} else {
			policyTZ = TimezoneCatalogOnly
		}
	}
	if policyLoc == "" {
		policyLoc = policyTZ
	}
	locales := desktopLocales
	if family == FamilyMobile {
		locales = mobileLocales
	}
	loc, err := pickLocale(rng, locales, country, policyLoc)
	if err != nil {
		return nil, err
	}
	b.Locale = Locale{
		Locale:         loc.locale,
		Languages:      append([]string(nil), loc.languages...),
		AcceptLanguage: loc.accept,
		TimezoneID:     loc.timezone,
	}
	b.ProxyAffinity = ProxyAffinity{
		ExpectedCountry: country,
		ExitIP:          opts.ExitIP,
		TimezonePolicy:  policyTZ,
		LocalePolicy:    policyLoc,
	}

	if opts.NoiseEnabled {
		g := gpuPool[rng.IntN(len(gpuPool))]
		b.Noise = Noise{
			Enabled:         true,
			GPUVendor:       g[0],
			GPUModel:        g[1],
			CanvasHash:      randomHex(8),
			MathFingerprint: randomHex(4),
		}
	}

	if err := b.Freeze(); err != nil {
		return nil, err
	}
	if err := b.Validate(ValidateOptions{RequireLocked: true}); err != nil {
		return nil, err
	}
	return &b, nil
}

func buildDesktop(rng *mathrand.Rand, forceBrowser string) (Bundle, error) {
	browser := BrowserChrome
	fb := strings.ToLower(strings.TrimSpace(forceBrowser))
	switch fb {
	case BrowserFirefox, "ff", "gecko":
		browser = BrowserFirefox
	case BrowserEdge:
		browser = BrowserEdge
	case BrowserChrome, "chromium":
		browser = BrowserChrome
	default:
		// Catalog mix: Chrome ~55%, Edge ~20%, Firefox ~25% (learned from user HAR + Chromium mainline).
		r := rng.Float64()
		switch {
		case r < 0.55:
			browser = BrowserChrome
		case r < 0.75:
			browser = BrowserEdge
		default:
			browser = BrowserFirefox
		}
	}

	if browser == BrowserFirefox {
		return buildDesktopFirefox(rng)
	}

	// SAZ Edge/Chrome capture is 150; keep small jitter 147–150.
	chromeMajor := 147 + rng.IntN(4) // 147..150
	build := 6000 + rng.IntN(4000)
	patch := 50 + rng.IntN(171)
	full := fmt.Sprintf("%d.0.%d.%d", chromeMajor, build, patch)

	var ua string
	var edgeFull string
	if browser == BrowserEdge {
		// SAZ: UA reduced Chrome/M.0.0.0 + Edg/M.0.0.0; full versions only in Client Hints.
		edgeMajor := chromeMajor
		edgeBuild := 4000 + rng.IntN(500) // ~4078 range observed
		edgePatch := 40 + rng.IntN(80)
		edgeFull = fmt.Sprintf("%d.0.%d.%d", edgeMajor, edgeBuild, edgePatch)
		ua = fmt.Sprintf(
			"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/%d.0.0.0 Safari/537.36 Edg/%d.0.0.0",
			chromeMajor, edgeMajor,
		)
		// Keep full chromium build for CH full-version-list.
	} else {
		// Chrome desktop UA often reduced M.0.0.0 as well on modern builds.
		ua = fmt.Sprintf(
			"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/%d.0.0.0 Safari/537.36",
			chromeMajor,
		)
	}

	vp := desktopViewports[rng.IntN(len(desktopViewports))]
	outerW := vp.vw + 8 + rng.IntN(9)
	outerH := vp.vh + 72 + rng.IntN(25)

	hw := pickInt(rng, []int{4, 8, 8, 12, 14, 16})
	mem := pickInt(rng, []int{4, 8, 8, 16})
	heap := pickInt64(rng, []int64{4293918720, 4294705152, 4294967296})

	return Bundle{
		CatalogID: "chrome-windows-desktop-v2",
		Identity: Identity{
			ProfileUUID: newUUID(),
			Family:      FamilyDesktop,
			Browser:     browser,
			OS:          OSWindows,
			OSVersion:   "10.0",
		},
		Device: Device{
			UserAgent:     ua,
			UAMajor:       chromeMajor,
			UAFullVersion: full,
			EdgeVersion:   edgeFull,
		},
		Geometry: Geometry{
			ViewportWidth:     vp.vw,
			ViewportHeight:    vp.vh,
			ScreenWidth:       vp.sw,
			ScreenHeight:      vp.sh,
			OuterWidth:        outerW,
			OuterHeight:       outerH,
			DeviceScaleFactor: vp.dpr,
			ColorDepth:        24,
			PixelDepth:        24,
		},
		Navigator: Navigator{
			HardwareConcurrency: hw,
			DeviceMemory:        mem,
			JSHeapSizeLimit:     heap,
			Platform:            "Win32",
			Vendor:              "Google Inc.",
			MaxTouchPoints:      0,
			HasTouch:            false,
			IsMobile:            false,
		},
	}, nil
}

// buildDesktopFirefox aligns with user HAR:
// Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:150.0) Gecko/20100101 Firefox/150.0
func buildDesktopFirefox(rng *mathrand.Rand) (Bundle, error) {
	// Firefox: HAR gold is 150. Keep light jitter only in 148–150 (no ancient 129).
	ffMajor := 150
	if r := rng.Float64(); r < 0.15 {
		ffMajor = 148
	} else if r < 0.35 {
		ffMajor = 149
	}
	ua := fmt.Sprintf(
		"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:%d.0) Gecko/20100101 Firefox/%d.0",
		ffMajor, ffMajor,
	)
	vp := desktopViewports[rng.IntN(len(desktopViewports))]
	outerW := vp.vw + 8 + rng.IntN(9)
	outerH := vp.vh + 72 + rng.IntN(25)
	hw := pickInt(rng, []int{4, 8, 8, 12, 14, 16})
	// Firefox often omits deviceMemory; keep for bundle completeness, sentinel may not use it.
	mem := pickInt(rng, []int{4, 8, 8, 16})
	// PoW payload index 2 was null in HAR — keep heap as 0 for Firefox to match.
	return Bundle{
		CatalogID: "firefox-windows-desktop-v2",
		Identity: Identity{
			ProfileUUID: newUUID(),
			Family:      FamilyDesktop,
			Browser:     BrowserFirefox,
			OS:          OSWindows,
			OSVersion:   "10.0",
		},
		Device: Device{
			UserAgent:     ua,
			UAMajor:       ffMajor,
			UAFullVersion: fmt.Sprintf("%d.0", ffMajor),
		},
		Geometry: Geometry{
			ViewportWidth:     vp.vw,
			ViewportHeight:    vp.vh,
			ScreenWidth:       vp.sw,
			ScreenHeight:      vp.sh,
			OuterWidth:        outerW,
			OuterHeight:       outerH,
			DeviceScaleFactor: vp.dpr,
			ColorDepth:        24,
			PixelDepth:        24,
		},
		Navigator: Navigator{
			HardwareConcurrency: hw,
			DeviceMemory:        mem,
			JSHeapSizeLimit:     0, // HAR payload[2] null
			Platform:            "Win32",
			Vendor:              "", // Gecko
			MaxTouchPoints:      0,
			HasTouch:            false,
			IsMobile:            false,
		},
	}, nil
}

func buildMobile(rng *mathrand.Rand) (Bundle, error) {
	chromeMajor := 133 + rng.IntN(14) // 133..146
	build := 6000 + rng.IntN(4000)
	patch := 50 + rng.IntN(171)
	full := fmt.Sprintf("%d.0.%d.%d", chromeMajor, build, patch)
	androidMajor := pickInt(rng, []int{12, 13, 14, 15})
	model := androidModels[rng.IntN(len(androidModels))]
	ua := fmt.Sprintf(
		"Mozilla/5.0 (Linux; Android %d; %s) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/%s Mobile Safari/537.36",
		androidMajor, model, full,
	)
	vp := mobileViewports[rng.IntN(len(mobileViewports))]
	hw := pickInt(rng, []int{4, 6, 8})
	mem := pickInt(rng, []int{4, 6, 8, 8})
	heap := pickInt64(rng, []int64{2147483648, 3221225472, 4294967296})
	touch := pickInt(rng, []int{5, 10})

	return Bundle{
		CatalogID: "chrome-android-mobile-v2",
		Identity: Identity{
			ProfileUUID: newUUID(),
			Family:      FamilyMobile,
			Browser:     BrowserChrome,
			OS:          OSAndroid,
			OSVersion:   fmt.Sprintf("%d.0.0", androidMajor),
		},
		Device: Device{
			UserAgent:     ua,
			UAMajor:       chromeMajor,
			UAFullVersion: full,
			AndroidModel:  model,
		},
		Geometry: Geometry{
			ViewportWidth:     vp.vw,
			ViewportHeight:    vp.vh,
			ScreenWidth:       vp.sw,
			ScreenHeight:      vp.sh,
			OuterWidth:        vp.vw,
			OuterHeight:       vp.vh,
			DeviceScaleFactor: vp.dpr,
			ColorDepth:        24,
			PixelDepth:        24,
		},
		Navigator: Navigator{
			HardwareConcurrency: hw,
			DeviceMemory:        mem,
			JSHeapSizeLimit:     heap,
			Platform:            "Linux armv8l",
			Vendor:              "Google Inc.",
			MaxTouchPoints:      touch,
			HasTouch:            true,
			IsMobile:            true,
		},
	}, nil
}

func pickLocale(rng *mathrand.Rand, locales []localeTuple, country, policy string) (localeTuple, error) {
	if country != "" && (policy == TimezoneStrictMatch || policy == "strict") {
		var matched []localeTuple
		for _, l := range locales {
			for _, c := range l.countries {
				if NormalizeCountry(c) == country {
					matched = append(matched, l)
					break
				}
			}
		}
		if len(matched) == 0 {
			// Prefer ja-JP catalog entry synthesis for JP etc. — fail closed for strict
			return localeTuple{}, fmt.Errorf("fingerprint: proxy_affinity_mismatch: no locale for country %s under %s", country, policy)
		}
		return matched[rng.IntN(len(matched))], nil
	}
	// allow_global_en / catalog_only: any catalog locale
	return locales[rng.IntN(len(locales))], nil
}

func pickInt(rng *mathrand.Rand, vals []int) int {
	return vals[rng.IntN(len(vals))]
}

func pickInt64(rng *mathrand.Rand, vals []int64) int64 {
	return vals[rng.IntN(len(vals))]
}

func newUUID() string {
	var b [16]byte
	_, _ = rand.Read(b[:])
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16])
}

func newBundleID() string {
	return "fpb_" + randomHex(12)
}

func randomHex(nBytes int) string {
	b := make([]byte, nBytes)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}
