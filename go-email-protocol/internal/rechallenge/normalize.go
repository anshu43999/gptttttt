package rechallenge

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"unicode"
)

type NormalizeOptions struct {
	CaptureID          string
	ExpectedRole       CaptureRole
	ExpectedSHA256     string
	RedactionPolicyID  string
	SentinelReleaseID  string
	TransportProfileID string
	PolicyID           string
	ParentContractID   string
}

func NormalizeHAR(ctx context.Context, path string, options NormalizeOptions) (*NormalizedCapture, error) {
	capture, err := IngestHAR(ctx, path, IngestOptions{
		CaptureID: options.CaptureID,
		ExpectedRole: options.ExpectedRole,
		ExpectedSHA256: options.ExpectedSHA256,
		RedactionPolicyID: options.RedactionPolicyID,
	})
	if err != nil {
		return nil, err
	}
	contract, err := NormalizeCapture(capture, options)
	if err != nil {
		return nil, err
	}
	manifest := capture.Manifest
	return &NormalizedCapture{Manifest: &manifest, Contract: contract}, nil
}

func NormalizeCapture(capture *Capture, options NormalizeOptions) (*RegistrationContract, error) {
	if capture == nil {
		return nil, fmt.Errorf("rechallenge: nil capture")
	}
	if capture.Manifest.Role != RoleRegistration {
		return nil, contractError(CodeCaptureRoleMismatch, "normalize", "capture", "role", capture.Manifest.CaptureID, fmt.Errorf("observed role %s", capture.Manifest.Role))
	}
	if options.ExpectedRole != "" && options.ExpectedRole != capture.Manifest.Role {
		return nil, contractError(CodeCaptureRoleMismatch, "normalize", "capture", "role", capture.Manifest.CaptureID, fmt.Errorf("observed %s expected %s", capture.Manifest.Role, options.ExpectedRole))
	}
	sentinelRelease := strings.TrimSpace(options.SentinelReleaseID)
	if sentinelRelease == "" { sentinelRelease = "sentinel-20260219f9f6-r1" }
	transportProfile := strings.TrimSpace(options.TransportProfileID)
	if transportProfile == "" { transportProfile = "firefox-150-win-h2-r1" }
	policyID := strings.TrimSpace(options.PolicyID)
	if policyID == "" { policyID = "registration-contract-v1" }
	contract := &RegistrationContract{
		SchemaVersion: CurrentSchemaVersion,
		ParentContractID: options.ParentContractID,
		Captures: []CaptureManifest{capture.Manifest},
		Flow: "oauth_create_account",
		BrowserIdentity: BrowserIdentity{Browser: capture.Manifest.Browser, UAMajor: capture.Manifest.UAMajor, Locale: capture.Manifest.Locale},
		Exchanges: append([]StateExchangeContract(nil), capture.Exchanges...),
		SentinelReleaseID: sentinelRelease,
		TransportProfileID: transportProfile,
		PolicyID: policyID,
	}
	if err := FinalizeContract(contract); err != nil {
		return nil, err
	}
	return contract, nil
}

func normalizeHARRequest(kind string, parsedURL *url.URL, request *harRequest) RequestContract {
	body := normalizeBodyRule(request.PostData)
	contentType := publicContentType(headerValue(request.Headers, "content-type"))
	if contentType == "" && request.PostData != nil {
		contentType = publicContentType(request.PostData.MimeType)
	}
	return RequestContract{
		Kind: kind,
		Method: strings.ToUpper(request.Method),
		Host: strings.ToLower(parsedURL.Hostname()),
		Path: parsedURL.EscapedPath(),
		Query: normalizeQueryRules(parsedURL.RawQuery),
		ContentType: contentType,
		Body: body,
		Headers: normalizeHeaderRules(request.Headers, kind),
	}
}

func normalizeQueryRules(rawQuery string) []FieldRule {
	if rawQuery == "" {
		return nil
	}
	seen := make(map[string]bool)
	rules := make([]FieldRule, 0)
	for _, pair := range strings.Split(rawQuery, "&") {
		name := pair
		if index := strings.IndexByte(pair, '='); index >= 0 { name = pair[:index] }
		name, _ = url.QueryUnescape(name)
		if name == "" || seen[name] { continue }
		seen[name] = true
		rules = append(rules, FieldRule{
			Name: name,
			Presence: PresenceRequired,
			ValuePolicy: queryValuePolicy(name),
			ValueType: "string",
			Order: len(rules),
			Provenance: []string{"har_request_query"},
		})
	}
	return rules
}

func queryValuePolicy(name string) string {
	switch strings.ToLower(name) {
	case "state", "code", "login_hint", "device_id", "ext-oai-did", "auth_session_logging_id", "sid", "nonce", "code_verifier":
		return "dynamic_secret"
	case "ccaps", "ext-passkey-client-capabilities":
		return "dynamic_json_shape"
	default:
		return "public_parameter"
	}
}

func normalizeBodyRule(postData *harPostData) BodyRule {
	if postData == nil || (strings.TrimSpace(postData.Text) == "" && len(postData.Params) == 0) {
		return BodyRule{Kind: "none"}
	}
	if len(postData.Params) > 0 {
		fields := make([]FieldRule, 0, len(postData.Params))
		seen := make(map[string]bool)
		for _, parameter := range postData.Params {
			if parameter.Name == "" || seen[parameter.Name] { continue }
			seen[parameter.Name] = true
			fields = append(fields, bodyFieldRule(parameter.Name, json.RawMessage(strconvJSON(parameter.Value))))
		}
		return BodyRule{Kind: "form", Fields: fields}
	}
	var object map[string]json.RawMessage
	if json.Unmarshal([]byte(postData.Text), &object) == nil {
		fields := make([]FieldRule, 0, len(object))
		for name, raw := range object { fields = append(fields, bodyFieldRule(name, raw)) }
		sort.Slice(fields, func(i, j int) bool { return fields[i].Name < fields[j].Name })
		return BodyRule{Kind: "json", Fields: fields}
	}
	if strings.Contains(strings.ToLower(postData.MimeType), "form") {
		values, _ := url.ParseQuery(postData.Text)
		fields := make([]FieldRule, 0, len(values))
		for name := range values { fields = append(fields, bodyFieldRule(name, nil)) }
		sort.Slice(fields, func(i, j int) bool { return fields[i].Name < fields[j].Name })
		return BodyRule{Kind: "form", Fields: fields}
	}
	return BodyRule{Kind: "opaque"}
}

func strconvJSON(value string) string {
	raw, _ := json.Marshal(value)
	return string(raw)
}

func bodyFieldRule(name string, raw json.RawMessage) FieldRule {
	valueType := rawJSONType(raw)
	objectKeys := []string(nil)
	if valueType == "object" {
		var object map[string]json.RawMessage
		if json.Unmarshal(raw, &object) == nil {
			for key := range object { objectKeys = append(objectKeys, key) }
			sort.Strings(objectKeys)
		}
	}
	return FieldRule{
		Name: name,
		Presence: PresenceRequired,
		ValuePolicy: bodyValuePolicy(name, valueType),
		ValueType: valueType,
		ObjectKeys: objectKeys,
		Provenance: []string{"har_request_body"},
	}
}

func rawJSONType(raw json.RawMessage) string {
	trimmed := strings.TrimSpace(string(raw))
	if trimmed == "" { return "string" }
	switch trimmed[0] {
	case '{': return "object"
	case '[': return "array"
	case '"': return "string"
	case 't', 'f': return "boolean"
	case 'n': return "null"
	default: return "number"
	}
}

func bodyValuePolicy(name, valueType string) string {
	switch strings.ToLower(name) {
	case "password", "otp", "code", "email", "username", "csrf", "csrftoken", "csrftokenvalue", "p", "id", "token", "state", "nonce":
		return "dynamic_secret"
	case "flow":
		return "protected_flow"
	case "name":
		return "person_name_shape"
	case "birthdate":
		return "date_shape"
	default:
		return "type:" + valueType
	}
}

func normalizeHARResponse(kind string, response *harResponse) (ResponseContract, error) {
	allowed := []int(nil)
	outcome := "capture_transport_incomplete"
	if response.Status > 0 {
		allowed = []int{response.Status}
		outcome = "success"
		if response.Status >= 300 && response.Status < 400 {
			outcome = "redirect"
		} else if response.Status < 200 || response.Status >= 400 {
			outcome = "error"
		}
	}
	raw, err := decodeHARContent(response)
	if err != nil {
		return ResponseContract{}, err
	}
	required, discriminators, template, observed := observedResponseRules(kind, raw)
	if requiresJSONResponse(kind) && !observed && response.Status > 0 {
		outcome = "unknown"
	}
	return ResponseContract{
		AllowedStatus: allowed,
		ObservedStatus: response.Status,
		ContentType: publicContentType(firstNonEmpty(headerValue(response.Headers, "content-type"), response.Content.MimeType)),
		RequiredFields: required,
		Discriminators: discriminators,
		Outcome: outcome,
		BodyTemplate: template,
	}, nil
}

func requiresJSONResponse(kind string) bool {
	switch kind {
	case "csrf", "signin", "otp_validate", "sentinel_req", "create_account":
		return true
	default:
		return false
	}
}

func observedResponseRules(kind string, raw []byte) ([]string, []string, *BodyTemplate, bool) {
	if len(raw) == 0 {
		return nil, nil, nil, false
	}
	var document map[string]any
	if json.Unmarshal(raw, &document) != nil {
		return nil, nil, nil, false
	}
	slot := func(name string) TemplateValue { return TemplateValue{Kind: "slot", Slot: name} }
	literal := func(value any) TemplateValue { return TemplateValue{Kind: "literal", Literal: value} }
	object := func(fields map[string]TemplateValue) TemplateValue { return TemplateValue{Kind: "object", Fields: fields} }
	fields := make(map[string]TemplateValue)
	var required []string
	var discriminators []string
	addSlot := func(field, slotName string, discriminator bool) {
		if value, ok := document[field].(string); ok && value != "" {
			fields[field] = slot(slotName)
			required = append(required, field)
			if discriminator { discriminators = append(discriminators, field) }
		}
	}
	switch kind {
	case "csrf":
		addSlot("csrfToken", "csrf_token", true)
	case "signin":
		addSlot("url", "authorize_url", true)
	case "otp_validate":
		addSlot("continue_url", "about_you_url", true)
		if len(fields) == 0 { addSlot("url", "about_you_url", true) }
	case "create_account":
		addSlot("continue_url", "callback_url", true)
		if len(fields) == 0 { addSlot("url", "callback_url", true) }
	case "sentinel_req":
		addSlot("token", "sentinel_requirements_token", true)
		for _, objectName := range []string{"proofofwork", "turnstile", "so"} {
			observedObject, ok := document[objectName].(map[string]any)
			if !ok { continue }
			objectFields := make(map[string]TemplateValue)
			if value, ok := observedObject["required"].(bool); ok {
				objectFields["required"] = literal(value)
				required = append(required, objectName+".required")
				discriminators = append(discriminators, objectName+".required")
			}
			if value, ok := observedObject["difficulty"].(string); ok && len(value) <= 16 {
				objectFields["difficulty"] = literal(value)
				required = append(required, objectName+".difficulty")
			}
			slots := map[string]string{"seed": "pow_seed", "dx": objectName + "_dx", "snapshot_dx": "so_snapshot_dx", "collector_dx": "so_collector_dx"}
			for field, slotName := range slots {
				if value, ok := observedObject[field].(string); ok && value != "" {
					objectFields[field] = slot(slotName)
					required = append(required, objectName+"."+field)
				}
			}
			if len(objectFields) != 0 { fields[objectName] = object(objectFields) }
		}
	default:
		return nil, nil, nil, true
	}
	if len(fields) == 0 {
		return nil, nil, nil, false
	}
	sort.Strings(required)
	sort.Strings(discriminators)
	return required, discriminators, &BodyTemplate{Kind: "json", Fields: fields}, true
}

func normalizeRedirect(response *harResponse) *RedirectRule {
	if response.Status < 300 || response.Status >= 400 {
		return nil
	}
	location := firstNonEmpty(headerValue(response.Headers, "location"), response.RedirectURL)
	rule := &RedirectRule{Status: response.Status, LocationPolicy: "path_template", MaxHops: 10}
	if parsed, err := url.Parse(location); err == nil {
		rule.LocationPath = parsed.EscapedPath()
		rule.FinalHost = strings.ToLower(parsed.Hostname())
	}
	return rule
}

func normalizeCookieEvents(parsedURL *url.URL, request *harRequest, response *harResponse) []CookieEvent {
	events := make([]CookieEvent, 0, len(request.Cookies)+len(response.Cookies))
	seen := make(map[string]bool)
	add := func(event CookieEvent) {
		key := event.Direction+"|"+strings.ToLower(event.Name)+"|"+event.Domain+"|"+event.Path
		if event.Name == "" || seen[key] { return }
		seen[key] = true
		events = append(events, event)
	}
	for _, cookie := range request.Cookies {
		add(cookieEvent("send", parsedURL.Hostname(), cookie, "har_request_cookie"))
	}
	if len(request.Cookies) == 0 {
		for _, name := range cookieHeaderNames(headerValue(request.Headers, "cookie")) {
			add(CookieEvent{Direction: "send", Name: name, Domain: parsedURL.Hostname(), Path: "/", ValueSlot: cookieValueSlot(name), Required: false, Provenance: []string{"har_request_header", "runtime:http_cookie_jar"}})
		}
	}
	for _, cookie := range response.Cookies {
		add(cookieEvent("set", parsedURL.Hostname(), cookie, "har_response_cookie"))
	}
	if len(response.Cookies) == 0 {
		headers := make(http.Header)
		for _, header := range response.Headers {
			if strings.EqualFold(header.Name, "set-cookie") { headers.Add("Set-Cookie", header.Value) }
		}
		parsedResponse := &http.Response{Header: headers}
		for _, cookie := range parsedResponse.Cookies() {
			add(CookieEvent{
				Direction: "set", Name: cookie.Name, Domain: firstNonEmpty(cookie.Domain, parsedURL.Hostname()), Path: firstNonEmpty(cookie.Path, "/"),
				HTTPOnly: cookie.HttpOnly, Secure: cookie.Secure, SameSite: sameSiteName(cookie.SameSite), ValueSlot: cookieValueSlot(cookie.Name), Required: requiredCookie(cookie.Name),
				Provenance: []string{"har_response_set_cookie", "runtime:http_cookie_jar"},
			})
		}
	}
	return events
}

func cookieEvent(direction, fallbackDomain string, cookie harCookie, provenance string) CookieEvent {
	return CookieEvent{
		Direction: direction, Name: cookie.Name, Domain: firstNonEmpty(cookie.Domain, fallbackDomain), Path: firstNonEmpty(cookie.Path, "/"),
		HTTPOnly: cookie.HTTPOnly, Secure: cookie.Secure, SameSite: cookie.SameSite, ValueSlot: cookieValueSlot(cookie.Name), Required: requiredCookie(cookie.Name),
		Provenance: []string{provenance, "runtime:http_cookie_jar"},
	}
}

func cookieHeaderNames(raw string) []string {
	var names []string
	for _, pair := range strings.Split(raw, ";") {
		name := strings.TrimSpace(strings.SplitN(pair, "=", 2)[0])
		if name != "" { names = append(names, name) }
	}
	return names
}

func cookieValueSlot(name string) string {
	lower := strings.ToLower(name)
	switch {
	case lower == "oai-did": return "device_id"
	case strings.Contains(lower, "csrf"): return "csrf_cookie"
	case strings.Contains(lower, "session"): return "session_cookie"
	case strings.Contains(lower, "cf_clearance"): return "edge_clearance"
	}
	var builder strings.Builder
	separator := false
	for _, r := range lower {
		if unicode.IsLetter(r) || unicode.IsDigit(r) {
			builder.WriteRune(r)
			separator = false
		} else if builder.Len() > 0 && !separator {
			builder.WriteByte('_')
			separator = true
		}
	}
	value := strings.Trim(builder.String(), "_")
	if value == "" { return "cookie_slot" }
	return value
}

func requiredCookie(name string) bool {
	lower := strings.ToLower(name)
	return lower == "oai-did" || strings.Contains(lower, "csrf") || strings.Contains(lower, "next-auth.session")
}

func sameSiteName(value http.SameSite) string {
	switch value {
	case http.SameSiteLaxMode: return "Lax"
	case http.SameSiteStrictMode: return "Strict"
	case http.SameSiteNoneMode: return "None"
	default: return ""
	}
}

func firstNonEmpty(values ...string) string {
	for _, value := range values { if strings.TrimSpace(value) != "" { return value } }
	return ""
}
