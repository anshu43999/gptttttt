package rechallenge

import (
	"sort"
	"strings"
)

const (
	HeaderSourceApp            = "app"
	HeaderSourceCookieJar      = "cookie_jar"
	HeaderSourceTransport      = "transport"
	HeaderSourceTelemetry      = "telemetry"
	HeaderSourceDynamicRuntime = "dynamic_runtime"

	PresenceRequired  = "required"
	PresenceOptional  = "optional"
	PresenceForbidden = "forbidden"
	PresenceObserved  = "observed"
	PresenceDynamic   = "dynamic"
)

var forbiddenFirefoxHeaders = []string{
	"sec-ch-ua",
	"sec-ch-ua-full-version-list",
	"sec-ch-ua-mobile",
	"sec-ch-ua-platform",
	"sec-ch-ua-platform-version",
	"sec-ch-viewport-width",
}

func normalizeHeaderRules(headers []harNameValue, requestKind string) []HeaderRule {
	counts := make(map[string]int)
	firstValue := make(map[string]string)
	order := make(map[string]int)
	for index, header := range headers {
		name := strings.ToLower(strings.TrimSpace(header.Name))
		if name == "" {
			continue
		}
		counts[name]++
		if _, ok := order[name]; !ok {
			order[name] = index
			firstValue[name] = header.Value
		}
	}
	rules := make([]HeaderRule, 0, len(counts)+len(forbiddenFirefoxHeaders))
	orderedNames := make([]string, 0, len(counts))
	for name := range counts { orderedNames = append(orderedNames, name) }
	sort.Slice(orderedNames, func(i, j int) bool { return order[orderedNames[i]] < order[orderedNames[j]] })
	for _, name := range orderedNames {
		source := classifyHeaderSource(name)
		presence := headerPresence(name, source, requestKind)
		valuePolicy, expected := headerValuePolicy(name, source, firstValue[name])
		multiplicity := "single"
		if counts[name] > 1 { multiplicity = "multiple" }
		rules = append(rules, HeaderRule{
			Name: name,
			Source: source,
			Presence: presence,
			ValuePolicy: valuePolicy,
			Expected: expected,
			OrderPolicy: "observed",
			Order: order[name],
			Multiplicity: multiplicity,
			Provenance: headerProvenance(source),
		})
	}
	for _, name := range forbiddenFirefoxHeaders {
		if counts[name] != 0 {
			continue
		}
		rules = append(rules, HeaderRule{
			Name: name,
			Source: HeaderSourceApp,
			Presence: PresenceForbidden,
			ValuePolicy: "forbidden",
			OrderPolicy: "not_applicable",
			Order: -1,
			Multiplicity: "none",
			Provenance: []string{"har_absence", "policy:firefox_no_client_hints"},
		})
	}
	return rules
}

func classifyHeaderSource(name string) string {
	name = strings.ToLower(name)
	switch {
	case name == "cookie":
		return HeaderSourceCookieJar
	case name == "host" || name == "connection" || name == "content-length" || name == "te" || name == "transfer-encoding" || name == "accept-encoding":
		return HeaderSourceTransport
	case name == "traceparent" || name == "tracestate" || strings.HasPrefix(name, "x-datadog-"):
		return HeaderSourceTelemetry
	case name == "x-access-flow-invocation-id" || name == "x-request-id" || name == "x-client-observation-id":
		return HeaderSourceDynamicRuntime
	default:
		return HeaderSourceApp
	}
}

func headerPresence(name, source, requestKind string) string {
	if source == HeaderSourceCookieJar || source == HeaderSourceTransport {
		return PresenceObserved
	}
	if source == HeaderSourceTelemetry || source == HeaderSourceDynamicRuntime {
		return PresenceDynamic
	}
	switch name {
	case "user-agent", "accept", "content-type", "origin", "referer":
		return PresenceRequired
	case "openai-sentinel-token", "openai-sentinel-so-token":
		if requestKind == "create_account" { return PresenceRequired }
		return PresenceObserved
	default:
		return PresenceObserved
	}
}

func headerValuePolicy(name, source, observed string) (string, string) {
	switch source {
	case HeaderSourceCookieJar:
		return "cookie_jar", ""
	case HeaderSourceTransport:
		return "transport_managed", ""
	case HeaderSourceTelemetry:
		return "dynamic_trace", ""
	case HeaderSourceDynamicRuntime:
		return "dynamic_identifier", ""
	}
	switch name {
	case "openai-sentinel-token", "openai-sentinel-so-token":
		return "secret_json_shape", ""
	case "user-agent":
		return "firefox_ua_major", ""
	case "accept-language":
		return "locale_catalog", ""
	case "content-type":
		return "exact", publicContentType(observed)
	case "origin", "referer":
		return "public_url_shape", ""
	case "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest", "sec-fetch-user":
		return "browser_fetch_metadata", ""
	default:
		return "observed", ""
	}
}

func headerProvenance(source string) []string {
	switch source {
	case HeaderSourceApp:
		return []string{"har_request", "policy:app_builder"}
	case HeaderSourceCookieJar:
		return []string{"har_request", "runtime:http_cookie_jar"}
	case HeaderSourceTransport:
		return []string{"har_request", "runtime:transport_client"}
	case HeaderSourceTelemetry:
		return []string{"har_request", "runtime:telemetry"}
	case HeaderSourceDynamicRuntime:
		return []string{"har_request", "runtime:dynamic"}
	default:
		return []string{"har_request"}
	}
}

func publicContentType(value string) string {
	value = strings.TrimSpace(value)
	if index := strings.Index(value, "; boundary="); index >= 0 {
		return value[:index]
	}
	return value
}
