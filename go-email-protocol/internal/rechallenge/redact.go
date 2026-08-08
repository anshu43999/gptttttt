package rechallenge

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"regexp"
	"strings"

	"github.com/gpt-register/go-email-protocol/internal/fixture"
)

var (
	rawEmailPattern = regexp.MustCompile(`(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b`)
	rawJWTLikePattern = regexp.MustCompile(`\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b`)
	rawBearerPattern = regexp.MustCompile(`(?i)\bBearer\s+[A-Za-z0-9._~+/-]{8,}=*`)
	rawProxyPattern = regexp.MustCompile(`(?i)(https?|socks5?)://[^/\s:@]+:[^/\s@]+@`)
	rawCookiePairPattern = regexp.MustCompile(`(?i)\b(session|token|auth|csrf|oai-did|__secure-[^=\s;]+)=[^;\s]{3,}`)
	rawOTPPattern = regexp.MustCompile(`(?i)"(?:otp|email_otp|verification_code|code)"\s*:\s*"?\d{4,8}"?`)
	rawCheckoutTokenPattern = regexp.MustCompile(`\b(?:cs|pi|seti)_(?:live|test)_[A-Za-z0-9]{12,}\b`)
	symbolicSlotPattern = regexp.MustCompile(`^[a-z][a-z0-9_]{1,63}$`)
)

var sensitiveNames = map[string]bool{
	"password": true, "otp": true, "code": true, "email": true, "login_hint": true,
	"authorization": true, "cookie": true, "set-cookie": true, "csrf": true, "csrftoken": true,
	"csrf_token": true, "access_token": true, "accesstoken": true, "refresh_token": true,
	"id_token": true, "token": true, "openai-sentinel-token": true,
	"openai-sentinel-so-token": true, "sentinel_token": true, "sentinel_so_token": true,
	"oauth_state": true, "oauth_code": true, "code_verifier": true, "nonce": true,
	"x-access-flow-invocation-id": true, "traceparent": true, "tracestate": true,
	"x-datadog-parent-id": true, "x-datadog-trace-id": true,
	"cf_clearance": true, "challenge_token": true, "proxy_url": true,
	"proxy_password": true, "proxy_userinfo": true, "capability": true, "bridge_capability": true,
}

func ValidateRedactedJSON(path string, raw []byte) error {
	checks := []struct {
		name string
		re *regexp.Regexp
	}{
		{"raw_email", rawEmailPattern},
		{"jwt_like_token", rawJWTLikePattern},
		{"bearer_token", rawBearerPattern},
		{"proxy_userinfo", rawProxyPattern},
		{"checkout_session_token", rawCheckoutTokenPattern},
		{"cookie_pair", rawCookiePairPattern},
		{"otp_value", rawOTPPattern},
	}
	for _, check := range checks {
		if check.re.Find(raw) != nil {
			return contractError(CodeContractRedactionViolation, "redact", "secret", path, "normalized_json", errors.New(check.name+" detected"))
		}
	}
	var document any
	decoder := json.NewDecoder(bytes.NewReader(raw))
	if err := decoder.Decode(&document); err != nil {
		return fmt.Errorf("rechallenge: redaction input is not JSON: %w", err)
	}
	var trailing any
	if err := decoder.Decode(&trailing); err != io.EOF {
		if err == nil {
			return errors.New("rechallenge: trailing JSON value")
		}
		return fmt.Errorf("rechallenge: trailing content: %w", err)
	}
	if err := walkContractSecrets(path, "$", "", false, document); err != nil {
		return err
	}
	if err := fixture.ValidateRedactedJSON(path, raw); err != nil {
		return contractError(CodeContractRedactionViolation, "redact", "fixture_guard", path, "fixture.ValidateRedactedJSON", errors.New("sensitive JSON rejected"))
	}
	return nil
}

func walkContractSecrets(file, path, key string, inheritedSensitive bool, value any) error {
	sensitive := inheritedSensitive || isSensitiveName(key)
	switch typed := value.(type) {
	case map[string]any:
		if name, ok := typed["name"].(string); ok && isSensitiveName(name) {
			if expected, ok := typed["expected"].(string); ok && strings.TrimSpace(expected) != "" {
				return redactionValueError(file, path+".expected", expected)
			}
		}
		for childKey, child := range typed {
			childSensitive := sensitive || isSensitiveName(childKey)
			if childKey == "kind" {
				childSensitive = false
			}
			if childKey == "slot" {
				if slot, ok := child.(string); !ok || !symbolicSlotPattern.MatchString(slot) {
					return redactionValueError(file, path+".slot", fmt.Sprint(child))
				}
				childSensitive = false
			}
			if key == "fields" && isSensitiveName(childKey) {
				childSensitive = true
			}
			if err := walkContractSecrets(file, path+"."+childKey, childKey, childSensitive, child); err != nil {
				return err
			}
		}
	case []any:
		for i, child := range typed {
			if err := walkContractSecrets(file, fmt.Sprintf("%s[%d]", path, i), key, sensitive, child); err != nil {
				return err
			}
		}
	case string:
		if sensitive && !safeSymbolicString(typed) {
			return redactionValueError(file, path, typed)
		}
	}
	return nil
}

func isSensitiveName(name string) bool {
	name = strings.ToLower(strings.TrimSpace(name))
	if sensitiveNames[name] {
		return true
	}
	return strings.HasSuffix(name, "_password") || strings.HasSuffix(name, "_token") || strings.HasSuffix(name, "-token") || strings.Contains(name, "cookie_value")
}

func safeSymbolicString(value string) bool {
	value = strings.TrimSpace(value)
	if value == "" {
		return true
	}
	lower := strings.ToLower(value)
	switch lower {
	case "[redacted]", "[hashed]", "<redacted>", "<hashed>", "omit", "hash", "secret", "redacted", "template", "dynamic_secret", "secret_json_shape", "cookie_jar", "string", "number", "boolean", "object", "array", "null":
		return true
	}
	if strings.HasPrefix(value, "${") && strings.HasSuffix(value, "}") {
		return true
	}
	if matched, _ := regexp.MatchString(`^S(?:[0-9]|1[0-5])$`, value); matched {
		return true
	}
	return false
}

func redactionValueError(file, path, _ string) error {
	return contractError(CodeContractRedactionViolation, "redact", "secret", path, file, errors.New("raw sensitive value detected"))
}
