package replay

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"time"
)

func matchRequest(contractID string, exchange compiledExchange, req *http.Request, body []byte, actualOccurrence int, jar *SymbolicJar) *Mismatch {
	mismatch := func(field, expected, actual string) *Mismatch {
		return &Mismatch{
			ContractID: contractID,
			Position:   exchange.position,
			Field:      field,
			Expected:   expected,
			Actual:     actual,
		}
	}
	if req == nil {
		return mismatch("request", "non-nil", "nil")
	}
	if req.URL == nil {
		return mismatch("request.url", "non-nil", "nil")
	}
	rule := exchange.request
	if !strings.EqualFold(req.Method, rule.method) {
		return mismatch("request.method", strings.ToUpper(rule.method), strings.ToUpper(req.Method))
	}
	if !strings.EqualFold(req.URL.Hostname(), rule.host) {
		return mismatch("request.host", strings.ToLower(rule.host), strings.ToLower(req.URL.Hostname()))
	}
	actualPath := req.URL.EscapedPath()
	if actualPath != rule.path {
		return mismatch("request.path", rule.path, actualPath)
	}
	if rule.contentType != "" {
		expected := normalizeContentType(rule.contentType)
		actual := normalizeContentType(req.Header.Get("Content-Type"))
		if actual != expected {
			return mismatch("request.content_type", expected, actual)
		}
	}

	query, queryOrder, err := parseQuery(req.URL.RawQuery)
	if err != nil {
		return mismatch("request.query", "valid encoded query", "invalid encoding")
	}
	if issue := matchCollection(rule.query, query, queryOrder, jar); issue != nil {
		return mismatch("request.query."+issue.field, issue.expected, issue.actual)
	}

	if issue := matchBody(rule.body, body, req.Header.Get("Content-Type"), jar); issue != nil {
		return mismatch("request.body."+issue.field, issue.expected, issue.actual)
	}
	if issue := matchHeaders(rule, req.Header, jar); issue != nil {
		return mismatch("request.headers."+issue.field, issue.expected, issue.actual)
	}
	if issue := matchCookies(rule, req, jar); issue != nil {
		return mismatch("request.cookies."+issue.field, issue.expected, issue.actual)
	}

	if rule.sentinelOccurrence != nil {
		if actualOccurrence != *rule.sentinelOccurrence {
			return mismatch("sentinel.occurrence", fmt.Sprintf("%d", *rule.sentinelOccurrence), fmt.Sprintf("%d", actualOccurrence))
		}
		var payload map[string]json.RawMessage
		if err := json.Unmarshal(body, &payload); err != nil {
			return mismatch("sentinel.body", "JSON object", "invalid JSON")
		}
		var flow string
		if raw := payload["flow"]; len(raw) > 0 {
			_ = json.Unmarshal(raw, &flow)
		}
		if flow != rule.flowName {
			return mismatch("sentinel.flow", rule.flowName, flow)
		}
	}
	return nil
}

type matchIssue struct {
	field    string
	expected string
	actual   string
}

func matchCollection(rule compiledCollection, actual map[string][]string, order []string, jar *SymbolicJar) *matchIssue {
	for name := range rule.forbidden {
		if _, exists := actual[name]; exists {
			return &matchIssue{field: "keys", expected: "forbidden key absent: " + name, actual: "forbidden key present: " + name}
		}
	}
	for name, valueRule := range rule.fields {
		values, exists := actual[name]
		if valueRule.required && !exists {
			return &matchIssue{field: "keys", expected: "required key present: " + name, actual: "missing key: " + name}
		}
		if !exists {
			continue
		}
		if len(values) != 1 {
			return &matchIssue{field: name, expected: "single value", actual: fmt.Sprintf("multiplicity=%d", len(values))}
		}
		if !matchesValue(valueRule, values[0], jar) {
			return &matchIssue{field: name, expected: valueDescription(valueRule), actual: "value policy mismatch"}
		}
	}
	if !rule.allowExtra {
		for name := range actual {
			if _, known := rule.fields[name]; !known {
				return &matchIssue{field: "keys", expected: "contract key set", actual: "unexpected key: " + name}
			}
		}
	}
	if len(rule.order) > 0 && !equalStrings(rule.order, order) {
		return &matchIssue{field: "order", expected: strings.Join(rule.order, ","), actual: strings.Join(order, ",")}
	}
	return nil
}

func matchBody(rule compiledBody, body []byte, contentType string, jar *SymbolicJar) *matchIssue {
	kind := rule.kind
	if kind == "" {
		kind = "empty"
	}
	switch kind {
	case "empty":
		if len(body) != 0 && !rule.allowEmpty {
			return &matchIssue{field: "kind", expected: "empty", actual: "non-empty"}
		}
		return nil
	case "json":
		var object map[string]json.RawMessage
		if err := json.Unmarshal(body, &object); err != nil {
			return &matchIssue{field: "kind", expected: "JSON object", actual: "invalid JSON object"}
		}
		values := make(map[string][]string, len(object))
		for key, raw := range object {
			values[key] = []string{string(raw)}
		}
		return matchCollection(rule.fields, values, nil, jar)
	case "form":
		values, err := url.ParseQuery(string(body))
		if err != nil {
			return &matchIssue{field: "kind", expected: "URL-encoded form", actual: "invalid form"}
		}
		return matchCollection(rule.fields, map[string][]string(values), rawFormOrder(string(body)), jar)
	case "raw":
		if !matchesValue(rule.raw, string(body), jar) {
			return &matchIssue{field: "value", expected: valueDescription(rule.raw), actual: "value policy mismatch"}
		}
		return nil
	default:
		return &matchIssue{field: "kind", expected: kind, actual: mediaBodyKind(contentType, body)}
	}
}

func matchHeaders(rule compiledRequest, header http.Header, jar *SymbolicJar) *matchIssue {
	actual := make(map[string][]string, len(header))
	for name, values := range header {
		actual[strings.ToLower(name)] = values
	}
	for name, headerRule := range rule.headers {
		values, exists := actual[name]
		switch headerRule.presence {
		case "forbidden":
			if exists {
				return &matchIssue{field: name, expected: "absent", actual: "present"}
			}
			continue
		case "required":
			if !exists || len(values) == 0 || strings.TrimSpace(values[0]) == "" {
				return &matchIssue{field: name, expected: "present", actual: "missing"}
			}
		}
		if !exists {
			continue
		}
		if headerRule.multiplicity == "single" && len(values) != 1 {
			return &matchIssue{field: name, expected: "single value", actual: fmt.Sprintf("multiplicity=%d", len(values))}
		}
		for _, value := range values {
			if !matchesValue(headerRule.value, value, jar) {
				return &matchIssue{field: name, expected: valueDescription(headerRule.value), actual: "value policy mismatch"}
			}
		}
	}
	if !rule.allowUnspecifiedHeaders {
		for name := range actual {
			if name == "cookie" {
				continue
			}
			if _, known := rule.headers[name]; !known {
				return &matchIssue{field: "keys", expected: "contract header set", actual: "unexpected header: " + name}
			}
		}
	}
	return nil
}

func matchCookies(rule compiledRequest, req *http.Request, jar *SymbolicJar) *matchIssue {
	for _, cookieRule := range rule.cookies {
		cookie, err := req.Cookie(cookieRule.name)
		if err != nil {
			if cookieRule.required {
				return &matchIssue{field: cookieRule.name, expected: "present", actual: "missing"}
			}
			continue
		}
		if cookieRule.slot != "" && !jar.Matches(cookieRule.slot, cookie.Value) {
			return &matchIssue{field: cookieRule.name, expected: "symbolic slot " + cookieRule.slot, actual: "value policy mismatch"}
		}
	}
	return nil
}

func matchesValue(rule compiledValueRule, actual string, jar *SymbolicJar) bool {
	switch rule.kind {
	case "", "any", "observed":
		return true
	case "nonempty", "dynamic", "redacted", "dynamic_trace":
		return strings.TrimSpace(actual) != ""
	case "exact":
		return actual == rule.literal
	case "slot":
		return jar != nil && jar.Matches(rule.slot, actual)
	case "json", "secret_json_shape", "json_object", "json_object_header":
		var object map[string]json.RawMessage
		if json.Unmarshal([]byte(actual), &object) != nil || object == nil {
			return false
		}
		return len(rule.objectKeys) == 0 || equalStrings(sortedKeys(object), rule.objectKeys)
	case "json_array":
		var value []json.RawMessage
		return json.Unmarshal([]byte(actual), &value) == nil && value != nil
	case "json_string":
		var value string
		return json.Unmarshal([]byte(actual), &value) == nil
	case "json_string_nonempty":
		var value string
		return json.Unmarshal([]byte(actual), &value) == nil && strings.TrimSpace(value) != ""
	case "json_date":
		var value string
		if json.Unmarshal([]byte(actual), &value) != nil {
			return false
		}
		_, err := time.Parse("2006-01-02", value)
		return err == nil
	case "date":
		_, err := time.Parse("2006-01-02", actual)
		return err == nil
	case "json_number", "number":
		var value json.Number
		return json.Unmarshal([]byte(actual), &value) == nil
	case "boolean":
		return actual == "true" || actual == "false"
	case "null":
		return strings.TrimSpace(actual) == "null"
	case "firefox_ua_major":
		return strings.Contains(actual, "Firefox/"+rule.literal+".") && strings.Contains(actual, "rv:"+rule.literal+".")
	case "locale_prefix":
		return rule.literal != "" && strings.HasPrefix(strings.ToLower(actual), strings.ToLower(rule.literal))
	case "public_url":
		parsed, err := url.Parse(actual)
		if err != nil || parsed.Scheme != "https" {
			return false
		}
		switch strings.ToLower(parsed.Hostname()) {
		case "chatgpt.com", "auth.openai.com", "sentinel.openai.com":
			return true
		default:
			return false
		}
	case "fetch_metadata":
		switch actual {
		case "none", "same-origin", "same-site", "cross-site", "cors", "navigate", "no-cors", "document", "empty", "?1":
			return true
		default:
			return false
		}
	default:
		return false
	}
}

func valueDescription(rule compiledValueRule) string {
	switch rule.kind {
	case "exact":
		return "exact static value"
	case "slot":
		return "symbolic slot " + rule.slot
	case "":
		return "any value"
	default:
		return rule.kind + " value"
	}
}

func parseQuery(raw string) (map[string][]string, []string, error) {
	values, err := url.ParseQuery(raw)
	if err != nil {
		return nil, nil, err
	}
	return map[string][]string(values), rawFormOrder(raw), nil
}

func rawFormOrder(raw string) []string {
	if raw == "" {
		return nil
	}
	parts := strings.Split(raw, "&")
	order := make([]string, 0, len(parts))
	for _, part := range parts {
		name := part
		if index := strings.IndexByte(name, '='); index >= 0 {
			name = name[:index]
		}
		if decoded, err := url.QueryUnescape(name); err == nil {
			name = decoded
		}
		order = append(order, name)
	}
	return order
}

func normalizeContentType(value string) string {
	return strings.ToLower(strings.ReplaceAll(strings.TrimSpace(value), " ", ""))
}

func mediaBodyKind(contentType string, body []byte) string {
	contentType = normalizeContentType(contentType)
	switch {
	case len(body) == 0:
		return "empty"
	case strings.Contains(contentType, "json"):
		return "json"
	case strings.Contains(contentType, "x-www-form-urlencoded"):
		return "form"
	default:
		return "raw"
	}
}

func equalStrings(left, right []string) bool {
	if len(left) != len(right) {
		return false
	}
	for index := range left {
		if left[index] != right[index] {
			return false
		}
	}
	return true
}

func sortedKeys[V any](values map[string]V) []string {
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}
