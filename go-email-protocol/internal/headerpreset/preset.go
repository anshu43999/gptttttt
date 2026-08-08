// Package headerpreset builds ordered HTTP headers from FingerprintBundle identity
// and per-endpoint presets. Spec: docs/PURE_GO_FULL_FINGERPRINT_PLAN.md §5.
package headerpreset

import (
	"fmt"
	"strings"

	"github.com/gpt-register/go-email-protocol/internal/fingerprint"
)

// Name identifies a header preset class.
type Name string

const (
	DocumentNavigation Name = "document_navigation"
	SameOriginFetch    Name = "same_origin_fetch"
	CrossOriginOAuth   Name = "cross_origin_oauth"
	OTPSparse          Name = "otp_sparse"
	SentinelReq        Name = "sentinel_req"
	CallbackNavigation Name = "callback_navigation"
)

// Options tunes optional telemetry.
type Options struct {
	// DatadogRUM when true injects fresh dd-* trace headers (default false).
	DatadogRUM bool
	// TraceID/ParentID only used when DatadogRUM; if empty, generated per Build call.
	TraceID  string
	ParentID string
}

// Build produces a lower-case ordered header list for the preset.
// Identity keys come only from the frozen Bundle; overrides may add endpoint fields.
// Firefox bundles omit Client Hints — RequiredIdentity that names missing CH is relaxed.
func Build(name Name, b *fingerprint.Bundle, overrides map[string]string, opts Options) ([]Header, error) {
	if b == nil {
		return nil, fmt.Errorf("headerpreset: bundle required")
	}
	if err := b.AssertReady(); err != nil {
		return nil, fmt.Errorf("headerpreset: %w", err)
	}
	def, ok := catalog[name]
	if !ok {
		return nil, fmt.Errorf("headerpreset: unknown preset %q", name)
	}

	isFirefox := b.Identity.Browser == fingerprint.BrowserFirefox || fingerprint.IsFirefoxUA(b.Device.UserAgent)

	ident := b.IdentityHeaders()
	vals := make(map[string]string, len(def.Order)+len(overrides)+8)
	for _, k := range def.IdentityKeys {
		v := ident[k]
		if v == "" {
			continue
		}
		// Never inject CH on Firefox even if present by mistake.
		if isFirefox && strings.HasPrefix(k, "sec-ch-") {
			continue
		}
		vals[k] = v
	}
	for k, v := range def.Base {
		vals[strings.ToLower(k)] = v
	}
	// Firefox document Accept is simpler (HAR).
	if isFirefox {
		switch name {
		case DocumentNavigation, CallbackNavigation:
			vals["accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
		}
		vals["accept-encoding"] = "gzip, deflate, br, zstd"
	}
	for k, v := range overrides {
		lk := strings.ToLower(k)
		if def.Forbidden[lk] {
			return nil, fmt.Errorf("headerpreset: forbidden key %q on %s", lk, name)
		}
		if isFirefox && strings.HasPrefix(lk, "sec-ch-") {
			continue
		}
		vals[lk] = v
	}
	for k := range def.Forbidden {
		delete(vals, k)
	}
	// required identity — skip CH requirements for Firefox
	for _, k := range def.RequiredIdentity {
		if isFirefox && strings.HasPrefix(k, "sec-ch-") {
			continue
		}
		if strings.TrimSpace(vals[k]) == "" {
			return nil, fmt.Errorf("headerpreset: missing required identity %q for %s", k, name)
		}
	}

	if opts.DatadogRUM {
		tid := opts.TraceID
		pid := opts.ParentID
		if tid == "" {
			tid = newTraceHex(16)
		}
		if pid == "" {
			pid = newTraceHex(8)
		}
		vals["x-datadog-origin"] = "rum"
		vals["x-datadog-sampling-priority"] = "1"
		vals["x-datadog-trace-id"] = tid
		vals["x-datadog-parent-id"] = pid
	}

	// Firefox-like order closer to HAR: UA, Accept, Accept-Language, Accept-Encoding, then rest.
	order := def.Order
	if isFirefox {
		order = firefoxOrder(def.Order)
	}

	seen := make(map[string]bool, len(order))
	out := make([]Header, 0, len(order)+4)
	for _, k := range order {
		if v, ok := vals[k]; ok && v != "" {
			out = append(out, Header{Key: k, Value: v})
			seen[k] = true
		}
	}
	if opts.DatadogRUM {
		for _, k := range []string{
			"x-datadog-origin",
			"x-datadog-sampling-priority",
			"x-datadog-trace-id",
			"x-datadog-parent-id",
		} {
			if !seen[k] {
				if v := vals[k]; v != "" {
					out = append(out, Header{Key: k, Value: v})
					seen[k] = true
				}
			}
		}
	}
	var extra []string
	for k := range vals {
		if !seen[k] {
			extra = append(extra, k)
		}
	}
	sortStrings(extra)
	for _, k := range extra {
		out = append(out, Header{Key: k, Value: vals[k]})
	}
	return out, nil
}

// firefoxOrder reorders keys to match Firefox HAR: user-agent, accept, accept-language,
// accept-encoding early; drop sec-ch-* from order.
func firefoxOrder(base []string) []string {
	priority := []string{
		"user-agent", "accept", "accept-language", "accept-encoding",
		"referer", "content-type", "content-length", "origin",
		"upgrade-insecure-requests",
		"sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site", "sec-fetch-user",
		"priority", "pragma", "cache-control", "te",
	}
	seen := map[string]bool{}
	out := make([]string, 0, len(base)+len(priority))
	for _, k := range priority {
		// only include if base wanted it or it's core identity
		for _, b := range base {
			if b == k {
				out = append(out, k)
				seen[k] = true
				break
			}
		}
		if k == "user-agent" || k == "accept-language" || k == "accept" || k == "accept-encoding" {
			if !seen[k] {
				out = append(out, k)
				seen[k] = true
			}
		}
	}
	for _, k := range base {
		if seen[k] {
			continue
		}
		if strings.HasPrefix(k, "sec-ch-") {
			continue
		}
		out = append(out, k)
		seen[k] = true
	}
	return out
}

// Header is one ordered pair.
type Header struct {
	Key   string
	Value string
}

// Map converts ordered headers to a map (last key wins). Prefer ApplyTo for order-sensitive clients.
func Map(hs []Header) map[string]string {
	m := make(map[string]string, len(hs))
	for _, h := range hs {
		m[h.Key] = h.Value
	}
	return m
}

// Keys returns ordered key names (for golden tests).
func Keys(hs []Header) []string {
	out := make([]string, len(hs))
	for i, h := range hs {
		out[i] = h.Key
	}
	return out
}

type definition struct {
	Order            []string
	Base             map[string]string
	IdentityKeys     []string // pulled from Bundle.IdentityHeaders
	RequiredIdentity []string
	Forbidden        map[string]bool
}

// Node createBrowserHeaders identity set (openai.ts) plus plan §5 classes.
var identityAll = []string{
	"user-agent",
	"accept-language",
	"sec-ch-ua",
	"sec-ch-ua-full-version-list",
	"sec-ch-ua-mobile",
	"sec-ch-ua-platform",
	"sec-ch-ua-platform-version",
	"sec-ch-viewport-width",
}

var catalog = map[Name]definition{
	DocumentNavigation: {
		// document GET: identity then accept/encoding/sec-fetch
		Order: append(append([]string{}, identityAll...),
			"accept",
			"accept-encoding",
			"upgrade-insecure-requests",
			"sec-fetch-dest",
			"sec-fetch-mode",
			"sec-fetch-site",
			"sec-fetch-user",
		),
		Base: map[string]string{
			"accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
			"accept-encoding":           "gzip, deflate, br, zstd",
			"upgrade-insecure-requests": "1",
			"sec-fetch-dest":            "document",
			"sec-fetch-mode":            "navigate",
			"sec-fetch-site":            "none",
			"sec-fetch-user":            "?1",
		},
		IdentityKeys:     identityAll,
		RequiredIdentity: []string{"user-agent", "accept-language", "sec-ch-ua", "sec-ch-ua-mobile"},
		Forbidden:        map[string]bool{"content-type": true},
	},
	CallbackNavigation: {
		Order: append(append([]string{}, identityAll...),
			"accept",
			"accept-encoding",
			"upgrade-insecure-requests",
			"sec-fetch-dest",
			"sec-fetch-mode",
			"sec-fetch-site",
			"referer",
		),
		Base: map[string]string{
			"accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
			"accept-encoding":           "gzip, deflate, br",
			"upgrade-insecure-requests": "1",
			"sec-fetch-dest":            "document",
			"sec-fetch-mode":            "navigate",
			"sec-fetch-site":            "cross-site",
		},
		IdentityKeys:     identityAll,
		RequiredIdentity: []string{"user-agent", "accept-language"},
		Forbidden:        map[string]bool{},
	},
	SameOriginFetch: {
		Order: append(append([]string{}, identityAll...),
			"accept",
			"accept-encoding",
			"origin",
			"referer",
			"sec-fetch-dest",
			"sec-fetch-mode",
			"sec-fetch-site",
			"content-type",
		),
		Base: map[string]string{
			"accept":          "application/json",
			"accept-encoding": "gzip, deflate, br",
			"sec-fetch-dest":  "empty",
			"sec-fetch-mode":  "cors",
			"sec-fetch-site":  "same-origin",
		},
		IdentityKeys:     identityAll,
		RequiredIdentity: []string{"user-agent", "sec-ch-ua"},
		Forbidden:        map[string]bool{},
	},
	CrossOriginOAuth: {
		Order: append(append([]string{}, identityAll...),
			"accept",
			"accept-encoding",
			"origin",
			"referer",
			"sec-fetch-dest",
			"sec-fetch-mode",
			"sec-fetch-site",
			"content-type",
		),
		Base: map[string]string{
			"accept":          "application/json",
			"accept-encoding": "gzip, deflate, br",
			"sec-fetch-dest":  "empty",
			"sec-fetch-mode":  "cors",
			"sec-fetch-site":  "cross-site",
		},
		IdentityKeys:     identityAll,
		RequiredIdentity: []string{"user-agent"},
		Forbidden:        map[string]bool{},
	},
	// OTP sparse: deliberately fewer CH — match Node/Python "less is correct".
	OTPSparse: {
		Order: []string{
			"user-agent",
			"accept",
			"accept-language",
			"content-type",
			"origin",
			"referer",
			"sec-fetch-dest",
			"sec-fetch-mode",
			"sec-fetch-site",
		},
		Base: map[string]string{
			"accept":         "application/json",
			"content-type":   "application/json",
			"sec-fetch-dest": "empty",
			"sec-fetch-mode": "cors",
			"sec-fetch-site": "same-origin",
		},
		IdentityKeys:     []string{"user-agent", "accept-language"},
		RequiredIdentity: []string{"user-agent"},
		Forbidden: map[string]bool{
			"sec-ch-ua":                   true,
			"sec-ch-ua-full-version-list": true,
			"sec-ch-ua-mobile":            true,
			"sec-ch-ua-platform":          true,
			"sec-ch-ua-platform-version":  true,
			"sec-ch-viewport-width":       true,
			"sec-ch-ua-full-version":      true,
			"sec-ch-ua-arch":              true,
			"sec-ch-ua-bitness":           true,
			"sec-ch-ua-model":             true,
		},
	},
	SentinelReq: {
		Order: []string{
			"user-agent",
			"accept",
			"accept-language",
			"content-type",
			"origin",
			"referer",
			"sec-ch-ua",
			"sec-ch-ua-mobile",
			"sec-ch-ua-platform",
			"sec-fetch-dest",
			"sec-fetch-mode",
			"sec-fetch-site",
		},
		Base: map[string]string{
			"accept":         "*/*",
			"content-type":   "text/plain;charset=UTF-8",
			"sec-fetch-dest": "empty",
			"sec-fetch-mode": "cors",
			"sec-fetch-site": "same-site",
		},
		IdentityKeys: []string{
			"user-agent",
			"accept-language",
			"sec-ch-ua",
			"sec-ch-ua-mobile",
			"sec-ch-ua-platform",
		},
		RequiredIdentity: []string{"user-agent", "sec-ch-ua"},
		Forbidden:        map[string]bool{},
	},
}

func sortStrings(s []string) {
	// tiny insertion sort to avoid extra import churn in hot path tests
	for i := 1; i < len(s); i++ {
		j := i
		for j > 0 && s[j-1] > s[j] {
			s[j-1], s[j] = s[j], s[j-1]
			j--
		}
	}
}

func newTraceHex(nBytes int) string {
	return randomHex(nBytes)
}
