package fixture

import (
	"encoding/json"
	"fmt"
	"regexp"
	"strings"
)

// Forbidden value patterns that must never appear as raw secrets in fixtures.
// Field *names* such as "password" or "access_token" are allowed as keys.
var (
	reJWT              = regexp.MustCompile(`\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b`)
	reBearer           = regexp.MustCompile(`(?i)\bBearer\s+[A-Za-z0-9\-._~+/]+=*`)
	reProxyUserinfo    = regexp.MustCompile(`(?i)(https?|socks5?)://[^/\s:]+:[^/\s@]+@`)
	reCookiePair       = regexp.MustCompile(`(?i)"(set-cookie|set_cookie|cookie|cookie_value)"\s*:\s*"[^"]+="`)
	reOTPValue         = regexp.MustCompile(`(?i)"(otp|code|email_otp|verification_code)"\s*:\s*"\d{4,8}"`)
	reAccessTokenValue = regexp.MustCompile(`(?i)"(access_token|accessToken|refresh_token|refreshToken)"\s*:\s*"[^"]{8,}"`)
	reAuthHeaderValue  = regexp.MustCompile(`(?i)"authorization"\s*:\s*"[^"]{8,}"`)
	rePasswordValue    = regexp.MustCompile(`(?i)"password"\s*:\s*"[^"]+"`)
	reCapabilityValue  = regexp.MustCompile(`(?i)"(capability|bridge_capability|proxy_password|proxy_userinfo)"\s*:\s*"[^"]+"`)
	reOpaqueToken      = regexp.MustCompile(`^[A-Za-z0-9+/=_\-.]{32,}$`)
	reDigitsOTP        = regexp.MustCompile(`^\d{4,8}$`)
)

// Allowed redaction markers in fixtures. Matching is exact whole-string equality
// after trim (case-insensitive for marker text). Substring contains is forbidden:
// values like "[REDACTED] SuperSecret1!" or "myhashvalue" must still fail.
var allowedMarkers = []string{
	"[REDACTED]",
	"[HASHED]",
	"<redacted>",
	"<hashed>",
	"sha256:", // bare policy token only; not "sha256:deadbeef..."
	"omit",
	"redacted_marker",
	"hash",
}

// secretKeys are JSON object keys whose string values must be markers or type labels.
// Matching is case-insensitive on the key name.
var secretKeys = map[string]bool{
	"password":              true,
	"otp":                   true,
	"code":                  true,
	"authorization":         true,
	"access_token":          true,
	"accesstoken":           true,
	"refresh_token":         true,
	"refreshtoken":          true,
	"cookie":                true,
	"set-cookie":            true,
	"set_cookie":            true,
	"capability":            true,
	"bridge_capability":     true,
	"proxy_password":        true,
	"proxy_userinfo":        true,
	"proxy_url":             true,
	"upstream_url":          true,
	"csrf_token_value":      true,
	"token_value":           true,
	"value":                 true,
	"cookie_value":          true,
	// Bare token / CSRF / sentinel keys (G0 audit blockers).
	"token":                 true,
	"csrftoken":             true,
	"csrf_token":            true,
	"openai-sentinel-token": true,
	"openai_sentinel_token": true,
	"sentinel_token":        true,
	"sentineltoken":         true,
}

// RedactionError describes a secret that must not land in fixtures.
type RedactionError struct {
	Path    string
	Reason  string
	Snippet string
}

func (e *RedactionError) Error() string {
	return fmt.Sprintf("redaction violation at %s: %s (%s)", e.Path, e.Reason, truncate(e.Snippet, 80))
}

// ValidateRedactedJSON rejects fixtures that embed raw secret values.
func ValidateRedactedJSON(path string, raw []byte) error {
	checks := []struct {
		name string
		re   *regexp.Regexp
	}{
		{"jwt_like_token", reJWT},
		{"bearer_token", reBearer},
		{"proxy_userinfo_url", reProxyUserinfo},
		{"cookie_header_value", reCookiePair},
		{"otp_code_value", reOTPValue},
		{"access_token_value", reAccessTokenValue},
		{"authorization_value", reAuthHeaderValue},
		{"password_value", rePasswordValue},
		{"capability_value", reCapabilityValue},
	}
	s := string(raw)
	for _, c := range checks {
		if loc := c.re.FindStringIndex(s); loc != nil {
			snippet := s[loc[0]:loc[1]]
			// Regex matches often include the key quote wrapper. If the captured
			// value portion is an allowed marker, accept it.
			if isAllowedMarker(snippet) || regexMatchIsMarkerValue(snippet) {
				continue
			}
			return &RedactionError{Path: path, Reason: c.name, Snippet: snippet}
		}
	}

	var v any
	if err := json.Unmarshal(raw, &v); err != nil {
		return fmt.Errorf("fixture %s: invalid json: %w", path, err)
	}
	return walkSecrets(path, "", v)
}

func walkSecrets(filePath, jsonPath string, v any) error {
	switch t := v.(type) {
	case map[string]any:
		for k, child := range t {
			p := jsonPath + "." + k
			if jsonPath == "" {
				p = k
			}
			if err := checkKeyValue(filePath, p, k, child); err != nil {
				return err
			}
			if err := walkSecrets(filePath, p, child); err != nil {
				return err
			}
		}
	case []any:
		for i, child := range t {
			p := fmt.Sprintf("%s[%d]", jsonPath, i)
			if err := walkSecrets(filePath, p, child); err != nil {
				return err
			}
		}
	}
	return nil
}

func checkKeyValue(filePath, jsonPath, key string, val any) error {
	s, ok := val.(string)
	if !ok || s == "" {
		return nil
	}
	if isAllowedMarker(s) {
		return nil
	}
	lk := strings.ToLower(key)
	// Normalize hyphen/underscore variants already covered by explicit map entries.
	if secretKeys[lk] || strings.HasSuffix(lk, "_password") || strings.HasSuffix(lk, "_token") || strings.HasSuffix(lk, "-token") {
		if looksLikeSecretPayload(s) {
			return &RedactionError{
				Path:    filePath + ":" + jsonPath,
				Reason:  "secret_value_for_key_" + key,
				Snippet: s,
			}
		}
	}
	return nil
}

func looksLikeSecretPayload(s string) bool {
	if isAllowedMarker(s) {
		return false
	}
	switch strings.ToLower(s) {
	case "string", "number", "boolean", "object", "array", "null",
		"secret", "redacted", "template", "none", "json", "form", "empty",
		"omit", "hash", "hashed", "redacted_marker", "true", "false":
		return false
	}
	if reDigitsOTP.MatchString(s) {
		return true
	}
	// Cookie header / name=value pairs are secrets even when short.
	if strings.Contains(s, "=") && (strings.Contains(s, ";") || strings.Contains(strings.ToLower(s), "session=") || strings.Contains(s, "Path=")) {
		return true
	}
	if reJWT.MatchString(s) || reBearer.MatchString(s) || reProxyUserinfo.MatchString(s) {
		return true
	}
	// Short structural notes / field type labels stay allowed; longer opaque payloads do not.
	if len(s) <= 12 && !strings.Contains(s, "://") && !strings.Contains(s, "eyJ") {
		return false
	}
	// Any non-marker payload on a secret key is rejected (password/otp/cookie/token/etc.).
	if strings.Contains(s, "://") || strings.Contains(s, "eyJ") || len(s) >= 8 {
		return true
	}
	if len(s) >= 32 && reOpaqueToken.MatchString(s) {
		return true
	}
	return false
}

// isAllowedMarker reports whether s is exactly one of the allowed redaction markers
// after trim. Matching is case-insensitive equality, never substring Contains.
// "sha256:" is accepted only as the bare policy token (optionally with trailing
// whitespace already trimmed); longer digests like "sha256:deadbeef" are rejected.
func isAllowedMarker(s string) bool {
	ls := strings.ToLower(strings.TrimSpace(s))
	if ls == "" {
		return false
	}
	for _, m := range allowedMarkers {
		if ls == strings.ToLower(m) {
			return true
		}
	}
	return false
}

// regexMatchIsMarkerValue reports whether a raw regex snippet ends with a JSON
// string value that is an allowed redaction marker (e.g. `"password":"[REDACTED]"`).
func regexMatchIsMarkerValue(snippet string) bool {
	// Prefer the last quoted JSON string in the match as the value.
	end := strings.LastIndex(snippet, `"`)
	if end <= 0 {
		return false
	}
	start := strings.LastIndex(snippet[:end], `"`)
	if start < 0 || start+1 >= end {
		return false
	}
	return isAllowedMarker(snippet[start+1 : end])
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}

// MustRedactString replaces a secret string with a redacted marker (recorder use).
func MustRedactString(_ string) string {
	return "[REDACTED]"
}

// HashPlaceholder returns a non-reversible cookie/value marker.
func HashPlaceholder() string {
	return "[HASHED]"
}

// Observation is an in-memory fixture draft produced offline from plan/oracle JSON
// or a capture pipeline. Values that look like secrets must already be markers or
// will be rejected by validation.
type Observation struct {
	Fixture Fixture
	// Raw is optional JSON bytes to run through ValidateRedactedJSON. When empty,
	// Fixture is marshaled first.
	Raw []byte
}

// RecordFromObservation redacts known secret string fields on a copy of the
// observation, then validates structure and redaction. It never opens the network.
// On success it returns a validated Fixture ready for catalogue inclusion.
func RecordFromObservation(obs Observation) (*Fixture, error) {
	f := obs.Fixture
	// Apply marker policies on cookie value policies and known secret body types.
	for i := range f.Cookies {
		switch f.Cookies[i].ValuePolicy {
		case "omit", "hash", "redacted_marker":
		case "":
			f.Cookies[i].ValuePolicy = "omit"
		default:
			// leave for ValidateFixture to reject
		}
	}
	raw := obs.Raw
	if len(raw) == 0 {
		var err error
		raw, err = json.Marshal(&f)
		if err != nil {
			return nil, fmt.Errorf("record: marshal: %w", err)
		}
	}
	// Walk JSON and replace non-marker secret-key values with [REDACTED].
	var v any
	if err := json.Unmarshal(raw, &v); err != nil {
		return nil, fmt.Errorf("record: invalid json: %w", err)
	}
	redactInPlace("", v)
	out, err := json.Marshal(v)
	if err != nil {
		return nil, fmt.Errorf("record: remashal: %w", err)
	}
	if err := ValidateRedactedJSON("observation", out); err != nil {
		return nil, err
	}
	var got Fixture
	if err := json.Unmarshal(out, &got); err != nil {
		return nil, fmt.Errorf("record: decode fixture: %w", err)
	}
	if err := ValidateFixture(&got, "observation"); err != nil {
		return nil, err
	}
	return &got, nil
}

// redactInPlace replaces secret-key string values that are not allowed markers
// with MustRedactString output. Structural field names are preserved.
func redactInPlace(jsonPath string, v any) {
	switch t := v.(type) {
	case map[string]any:
		for k, child := range t {
			if s, ok := child.(string); ok && s != "" {
				lk := strings.ToLower(k)
				if secretKeys[lk] || strings.HasSuffix(lk, "_password") || strings.HasSuffix(lk, "_token") || strings.HasSuffix(lk, "-token") {
					if !isAllowedMarker(s) && looksLikeSecretPayload(s) {
						t[k] = MustRedactString(s)
						continue
					}
				}
			}
			redactInPlace(jsonPath+"."+k, child)
		}
	case []any:
		for i, child := range t {
			redactInPlace(fmt.Sprintf("%s[%d]", jsonPath, i), child)
		}
	}
}
