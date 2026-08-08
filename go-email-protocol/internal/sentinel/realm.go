package sentinel

import (
	"fmt"
	"sync"

	"github.com/dop251/goja"
	"github.com/gpt-register/go-email-protocol/internal/fingerprint"
)

// Realm is an isolated JS environment for Sentinel SDK evaluation.
// One realm per challenge — never share across jobs.
type Realm struct {
	mu     sync.Mutex
	rt     *goja.Runtime
	jobKey string
	closed bool
}

// NewRealm builds a goja runtime with navigator/screen projected from Bundle.
func NewRealm(jobKey string, b *fingerprint.Bundle) (*Realm, error) {
	if b == nil {
		return nil, fmt.Errorf("sentinel: bundle required for realm")
	}
	if err := b.AssertReady(); err != nil {
		return nil, fmt.Errorf("sentinel: %w", err)
	}
	rt := goja.New()
	// Minimal browser-like globals from Bundle (not a full DOM).
	nav := map[string]any{
		"userAgent":           b.Device.UserAgent,
		"language":            b.Locale.Locale,
		"languages":           b.Locale.Languages,
		"platform":            b.Navigator.Platform,
		"vendor":              b.Navigator.Vendor,
		"hardwareConcurrency": b.Navigator.HardwareConcurrency,
		"deviceMemory":        b.Navigator.DeviceMemory,
		"maxTouchPoints":      b.Navigator.MaxTouchPoints,
		"webdriver":           false,
	}
	screen := map[string]any{
		"width":       b.Geometry.ScreenWidth,
		"height":      b.Geometry.ScreenHeight,
		"colorDepth":  b.Geometry.ColorDepth,
		"pixelDepth":  b.Geometry.PixelDepth,
		"availWidth":  b.Geometry.ScreenWidth,
		"availHeight": b.Geometry.ScreenHeight,
	}
	if err := rt.Set("navigator", nav); err != nil {
		return nil, err
	}
	if err := rt.Set("screen", screen); err != nil {
		return nil, err
	}
	// window self-ref
	win := map[string]any{
		"innerWidth":       b.Geometry.ViewportWidth,
		"innerHeight":      b.Geometry.ViewportHeight,
		"outerWidth":       b.Geometry.OuterWidth,
		"outerHeight":      b.Geometry.OuterHeight,
		"devicePixelRatio": b.Geometry.DeviceScaleFactor,
		"navigator":        nav,
		"screen":           screen,
	}
	if err := rt.Set("window", win); err != nil {
		return nil, err
	}
	if err := rt.Set("self", win); err != nil {
		return nil, err
	}
	return &Realm{rt: rt, jobKey: jobKey}, nil
}

// JobKey returns the owning job id for isolation tests.
func (r *Realm) JobKey() string {
	if r == nil {
		return ""
	}
	return r.jobKey
}

// Eval runs JS and returns the value (tests / SDK pin harness).
func (r *Realm) Eval(src string) (goja.Value, error) {
	if r == nil || r.rt == nil {
		return nil, fmt.Errorf("sentinel: nil realm")
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.closed {
		return nil, fmt.Errorf("sentinel: realm closed")
	}
	return r.rt.RunString(src)
}

// NavigatorUserAgent reads navigator.userAgent from the realm.
func (r *Realm) NavigatorUserAgent() (string, error) {
	v, err := r.Eval(`navigator.userAgent`)
	if err != nil {
		return "", err
	}
	return v.String(), nil
}

// Close drops the runtime (idempotent).
func (r *Realm) Close() {
	if r == nil {
		return
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	r.closed = true
	r.rt = nil
}
