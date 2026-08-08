package replay

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"

	"github.com/gpt-register/go-email-protocol/internal/rechallenge"
)

// NewClient compiles one validated registration contract into a zero-network client.
func NewClient(jobID string, contract *rechallenge.RegistrationContract, opts Options) (*Client, error) {
	if err := rechallenge.ValidateContract(contract); err != nil {
		return nil, fmt.Errorf("replay: contract invalid: %w", err)
	}
	jar, err := NewSymbolicJar()
	if err != nil {
		return nil, err
	}
	exchanges := append([]rechallenge.StateExchangeContract(nil), contract.Exchanges...)
	sort.SliceStable(exchanges, func(i, j int) bool {
		return exchanges[i].CaptureSequence < exchanges[j].CaptureSequence
	})
	if err := validateExchangeOrder(exchanges); err != nil {
		return nil, err
	}
	compiler := contractCompiler{contract: contract, exchanges: exchanges, jar: jar, firstCookies: firstCookieEvents(exchanges)}
	script := make([]compiledExchange, 0, len(exchanges))
	for index := range exchanges {
		exchange, err := compiler.compileExchange(&exchanges[index])
		if err != nil {
			return nil, fmt.Errorf("replay: compile capture_sequence=%d: %w", exchanges[index].CaptureSequence, err)
		}
		script = append(script, exchange)
	}
	assignContractCausalLanes(script)
	return newClient(jobID, contract.ContractID, jar, script, opts)
}

type contractCompiler struct {
	contract     *rechallenge.RegistrationContract
	exchanges    []rechallenge.StateExchangeContract
	jar          *SymbolicJar
	firstCookies map[string]cookieOrigin
}

func validateExchangeOrder(exchanges []rechallenge.StateExchangeContract) error {
	stateIndexes := make(map[string]int)
	for index, exchange := range exchanges {
		if exchange.CaptureSequence != index {
			return fmt.Errorf("replay: capture_sequence must be contiguous: position=%d sequence=%d", index, exchange.CaptureSequence)
		}
		state := string(exchange.State)
		if exchange.ExchangeIndex != stateIndexes[state] {
			return fmt.Errorf("replay: state %s exchange_index=%d want=%d", state, exchange.ExchangeIndex, stateIndexes[state])
		}
		stateIndexes[state]++
	}
	return nil
}

func assignContractCausalLanes(script []compiledExchange) {
	previousPrimary := -1
	previousSentinel := -1
	for index := range script {
		exchange := &script[index]
		if exchange.request.sentinelOccurrence != nil {
			exchange.causalLane = causalLaneSentinel
			exchange.dependencies = appendDependency(exchange.dependencies, previousSentinel)
			exchange.dependencies = appendDependency(exchange.dependencies, previousPrimary)
			previousSentinel = index
			continue
		}

		exchange.causalLane = causalLaneRegistration
		exchange.dependencies = appendDependency(exchange.dependencies, previousPrimary)
		if requestRequiresSentinel(exchange.request) {
			exchange.dependencies = appendDependency(exchange.dependencies, previousSentinel)
		}
		previousPrimary = index
	}
}

func requestRequiresSentinel(request compiledRequest) bool {
	for _, name := range []string{"openai-sentinel-token", "openai-sentinel-so-token"} {
		if rule, exists := request.headers[name]; exists && rule.presence == rechallenge.PresenceRequired {
			return true
		}
	}
	return false
}

func appendDependency(dependencies []int, candidate int) []int {
	if candidate < 0 {
		return dependencies
	}
	for _, existing := range dependencies {
		if existing == candidate {
			return dependencies
		}
	}
	return append(dependencies, candidate)
}

func (c *contractCompiler) compileExchange(source *rechallenge.StateExchangeContract) (compiledExchange, error) {
	occurrence := cloneInt(source.SentinelOccurrence)
	position := Position{
		CaptureID: source.Provenance.CaptureID, CaptureSequence: source.CaptureSequence,
		State: string(source.State), ExchangeIndex: source.ExchangeIndex, SentinelOccurrence: occurrence,
	}
	request := compiledRequest{
		method:                 strings.ToUpper(source.Request.Method),
		host:                   strings.ToLower(source.Request.Host),
		path:                   source.Request.Path,
		contentType:            source.Request.ContentType,
		query:                  compileFields(source.Request.Query, true, source.FlowName, false),
		body:                   compileBody(source.Request.Body, source.FlowName),
		headers:                 compileHeaders(source.Request.Headers, c.contract.BrowserIdentity),
		allowUnspecifiedHeaders: false,
		sentinelOccurrence:      occurrence,
		flowName:                source.FlowName,
	}
	for _, event := range source.CookieEvents {
		if event.Hop != 0 {
			return compiledExchange{}, fmt.Errorf("cookie event %s has unsupported hop=%d", event.Name, event.Hop)
		}
		if event.Direction != "send" {
			continue
		}
		origin := c.firstCookies[cookieEventKey(event)]
		request.cookies = append(request.cookies, compiledCookieRule{
			name:      event.Name,
			slot:      event.ValueSlot,
			domain:    event.Domain,
			path:      firstPath(event.Path),
			httpOnly:  event.HTTPOnly,
			secure:    event.Secure,
			sameSite:  parseSameSite(event.SameSite),
			required:  event.Required,
			allowSeed: origin.direction == "send" && origin.sequence == source.CaptureSequence,
		})
	}
	response, err := c.compileResponse(source)
	if err != nil {
		return compiledExchange{}, err
	}
	return compiledExchange{
		position:        position,
		captureSequence: source.CaptureSequence,
		request:         request,
		response:        response,
	}, nil
}

func compileFields(fields []rechallenge.FieldRule, preserveOrder bool, flowName string, jsonEncoded bool) compiledCollection {
	out := compiledCollection{
		fields:     make(map[string]compiledValueRule, len(fields)),
		forbidden: make(map[string]struct{}),
		allowExtra: false,
	}
	ordered := append([]rechallenge.FieldRule(nil), fields...)
	if preserveOrder {
		sort.SliceStable(ordered, func(i, j int) bool { return ordered[i].Order < ordered[j].Order })
	}
	for _, field := range ordered {
		name := field.Name
		if field.Presence == rechallenge.PresenceForbidden {
			out.forbidden[name] = struct{}{}
			continue
		}
		rule := compileFieldValue(field, flowName, jsonEncoded)
		rule.required = field.Presence == rechallenge.PresenceRequired
		out.fields[name] = rule
		if preserveOrder {
			out.order = append(out.order, name)
		}
	}
	return out
}

func compileFieldValue(field rechallenge.FieldRule, flowName string, jsonEncoded bool) compiledValueRule {
	rule := compiledValueRule{kind: field.ValuePolicy, objectKeys: append([]string(nil), field.ObjectKeys...)}
	switch field.ValuePolicy {
	case "dynamic_secret", "person_name_shape":
		if jsonEncoded && field.ValueType == "string" {
			rule.kind = "json_string_nonempty"
		} else {
			rule.kind = "nonempty"
		}
	case "dynamic_json_shape":
		rule.kind = "json_object"
	case "public_parameter":
		rule.kind = "nonempty"
	case "protected_flow":
		rule.kind = "exact"
		if jsonEncoded {
			raw, _ := json.Marshal(flowName)
			rule.literal = string(raw)
		} else {
			rule.literal = flowName
		}
	case "date_shape":
		if jsonEncoded {
			rule.kind = "json_date"
		} else {
			rule.kind = "date"
		}
	case "type:string":
		if jsonEncoded {
			rule.kind = "json_string"
		} else {
			rule.kind = "any"
		}
	case "type:object":
		rule.kind = "json_object"
	case "type:array":
		rule.kind = "json_array"
	case "type:number":
		if jsonEncoded {
			rule.kind = "json_number"
		} else {
			rule.kind = "number"
		}
	case "type:boolean":
		rule.kind = "boolean"
	case "type:null":
		rule.kind = "null"
	}
	return rule
}

func compileBody(body rechallenge.BodyRule, flowName string) compiledBody {
	switch body.Kind {
	case "none", "":
		return compiledBody{kind: "empty"}
	case "json", "form":
		return compiledBody{kind: body.Kind, fields: compileFields(body.Fields, body.Kind == "form", flowName, body.Kind == "json")}
	case "opaque":
		return compiledBody{kind: "raw", raw: compiledValueRule{kind: "nonempty"}}
	default:
		return compiledBody{kind: body.Kind}
	}
}

func compileHeaders(headers []rechallenge.HeaderRule, identity rechallenge.BrowserIdentity) map[string]compiledHeaderRule {
	out := make(map[string]compiledHeaderRule, len(headers))
	for _, header := range headers {
		name := strings.ToLower(header.Name)
		if header.Source == rechallenge.HeaderSourceCookieJar || name == "host" || name == "content-length" {
			continue
		}
		value := compiledValueRule{kind: header.ValuePolicy, literal: header.Expected}
		switch header.ValuePolicy {
		case "exact":
			value.kind = "exact"
		case "secret_json_shape":
			value.kind = "json_object_header"
		case "firefox_ua_major":
			value.kind = "firefox_ua_major"
			value.literal = strconv.Itoa(identity.UAMajor)
		case "locale_catalog":
			value.kind = "locale_prefix"
			value.literal = identity.Locale
		case "public_url_shape":
			value.kind = "public_url"
		case "browser_fetch_metadata":
			value.kind = "fetch_metadata"
		case "dynamic_trace":
			value.kind = "dynamic_trace"
		case "dynamic_identifier":
			value.kind = "nonempty"
		case "transport_managed", "observed", "cookie_jar":
			value.kind = "any"
		case "forbidden":
			value.kind = "any"
		}
		out[name] = compiledHeaderRule{
			presence:     header.Presence,
			value:        value,
			multiplicity: header.Multiplicity,
		}
	}
	return out
}

func (c *contractCompiler) compileResponse(source *rechallenge.StateExchangeContract) (compiledResponse, error) {
	status := source.Response.ObservedStatus
	if status <= 0 && len(source.Response.AllowedStatus) > 0 {
		status = source.Response.AllowedStatus[0]
	}
	outcome := strings.TrimSpace(source.Response.Outcome)
	replayable := status > 0 && outcome != "unknown" && outcome != "capture_transport_incomplete"
	if !replayable {
		return compiledResponse{statusCode: status, replayable: false, outcome: outcome}, nil
	}
	header := make(http.Header)
	if source.Response.ContentType != "" {
		header.Set("Content-Type", source.Response.ContentType)
	}
	redirectMaxHops := 0
	if source.Redirect != nil {
		location, err := c.redirectLocation(source.Redirect)
		if err != nil {
			return compiledResponse{}, err
		}
		header.Set("Location", location)
		status = source.Redirect.Status
		redirectMaxHops = source.Redirect.MaxHops
	}
	for _, event := range source.CookieEvents {
		if event.Hop != 0 {
			return compiledResponse{}, fmt.Errorf("cookie event %s has unsupported hop=%d", event.Name, event.Hop)
		}
		if event.Direction != "set" {
			continue
		}
		cookie, err := c.cookieForEvent(event)
		if err != nil {
			return compiledResponse{}, err
		}
		header.Add("Set-Cookie", cookie.String())
	}
	body, err := c.renderBody(source.Response.BodyTemplate)
	if err != nil {
		return compiledResponse{}, err
	}
	if err := validateCompiledResponse(source.Response, status, body); err != nil {
		return compiledResponse{}, err
	}
	return compiledResponse{statusCode: status, replayable: replayable, outcome: outcome, redirectMaxHops: redirectMaxHops, header: header, body: body}, nil
}

func validateCompiledResponse(contract rechallenge.ResponseContract, status int, body []byte) error {
	allowed := false
	for _, candidate := range contract.AllowedStatus {
		if status == candidate {
			allowed = true
			break
		}
	}
	if !allowed {
		return fmt.Errorf("sanitized response status=%d is not allowed", status)
	}
	paths := append(append([]string(nil), contract.RequiredFields...), contract.Discriminators...)
	if len(paths) == 0 {
		return nil
	}
	var document map[string]any
	if json.Unmarshal(body, &document) != nil {
		return fmt.Errorf("sanitized response must be a JSON object")
	}
	for _, path := range paths {
		if !hasResponsePath(document, strings.Split(path, ".")) {
			return fmt.Errorf("sanitized response missing required path %s", path)
		}
	}
	return nil
}

func hasResponsePath(value any, path []string) bool {
	if len(path) == 0 {
		return true
	}
	object, ok := value.(map[string]any)
	if !ok {
		return false
	}
	next, exists := object[path[0]]
	if !exists {
		return false
	}
	return hasResponsePath(next, path[1:])
}

func (c *contractCompiler) redirectLocation(rule *rechallenge.RedirectRule) (string, error) {
	if rule == nil {
		return "", nil
	}
	if rule.LocationPolicy != "path_template" {
		return "", fmt.Errorf("unsupported redirect location_policy=%q", rule.LocationPolicy)
	}
	if rule.MaxHops <= 0 || rule.MaxHops > defaultMaxRedirectHops {
		return "", fmt.Errorf("redirect max_hops=%d outside replay limit", rule.MaxHops)
	}
	if strings.TrimSpace(rule.FinalHost) == "" || !strings.HasPrefix(rule.LocationPath, "/") {
		return "", fmt.Errorf("redirect requires final_host and absolute location_path")
	}
	return "https://" + strings.ToLower(rule.FinalHost) + rule.LocationPath, nil
}

func (c *contractCompiler) renderBody(template *rechallenge.BodyTemplate) ([]byte, error) {
	if template == nil {
		return nil, nil
	}
	if template.Kind != "json" {
		return nil, fmt.Errorf("unsupported body template kind %q", template.Kind)
	}
	object := make(map[string]any, len(template.Fields))
	for name, value := range template.Fields {
		rendered, err := c.renderValue(value)
		if err != nil {
			return nil, fmt.Errorf("body template field %s: %w", name, err)
		}
		object[name] = rendered
	}
	return json.Marshal(object)
}

func (c *contractCompiler) renderValue(value rechallenge.TemplateValue) (any, error) {
	switch value.Kind {
	case "literal":
		return value.Literal, nil
	case "slot":
		return c.slotValue(value.Slot)
	case "object":
		object := make(map[string]any, len(value.Fields))
		for name, child := range value.Fields {
			rendered, err := c.renderValue(child)
			if err != nil {
				return nil, err
			}
			object[name] = rendered
		}
		return object, nil
	case "array":
		items := make([]any, 0, len(value.Items))
		for _, child := range value.Items {
			rendered, err := c.renderValue(child)
			if err != nil {
				return nil, err
			}
			items = append(items, rendered)
		}
		return items, nil
	default:
		return nil, fmt.Errorf("unsupported template value kind %q", value.Kind)
	}
}

func (c *contractCompiler) slotValue(slot string) (string, error) {
	switch slot {
	case "authorize_url":
		return c.endpointURL("authorize")
	case "callback_url":
		return c.endpointURL("callback")
	case "about_you_url":
		return "https://auth.openai.com/about-you", nil
	case "turnstile_dx", "so_snapshot_dx", "so_collector_dx":
		return "", nil
	case "sentinel_requirements_token":
		return "replay_requirements_token", nil
	case "pow_seed":
		return "replay_pow_seed", nil
	default:
		return c.jar.Value(slot)
	}
}

func (c *contractCompiler) endpointURL(kind string) (string, error) {
	for _, exchange := range c.exchanges {
		if exchange.Request.Kind != kind {
			continue
		}
		var query strings.Builder
		ordered := append([]rechallenge.FieldRule(nil), exchange.Request.Query...)
		sort.SliceStable(ordered, func(i, j int) bool { return ordered[i].Order < ordered[j].Order })
		for index, field := range ordered {
			if index > 0 {
				query.WriteByte('&')
			}
			query.WriteString(url.QueryEscape(field.Name))
			query.WriteByte('=')
			query.WriteString(url.QueryEscape(syntheticFieldValue(field)))
		}
		value := "https://" + exchange.Request.Host + exchange.Request.Path
		if query.Len() > 0 {
			value += "?" + query.String()
		}
		return value, nil
	}
	return "", fmt.Errorf("contract has no endpoint kind %q", kind)
}

func syntheticFieldValue(field rechallenge.FieldRule) string {
	switch field.ValuePolicy {
	case "dynamic_json_shape":
		return `{"replay":true}`
	case "public_parameter":
		switch field.Name {
		case "scope":
			return "openid email profile offline_access model.request model.read organization.read organization.write"
		case "prompt":
			return "login"
		case "screen_hint":
			return "login_or_signup"
		case "response_type":
			return "code"
		case "client_id":
			return "openai-auth-web"
		case "audience":
			return "https://api.openai.com/v1"
		case "redirect_uri":
			return "https://chatgpt.com/api/auth/callback/openai"
		}
		return "replay_" + strings.ReplaceAll(strings.ToLower(field.Name), "-", "_")
	default:
		return "replay_" + strings.ReplaceAll(strings.ToLower(field.Name), "-", "_")
	}
}


type cookieOrigin struct {
	direction string
	sequence  int
}

func firstCookieEvents(exchanges []rechallenge.StateExchangeContract) map[string]cookieOrigin {
	out := make(map[string]cookieOrigin)
	for _, exchange := range exchanges {
		for _, event := range exchange.CookieEvents {
			key := cookieEventKey(event)
			if _, exists := out[key]; !exists {
				out[key] = cookieOrigin{direction: event.Direction, sequence: exchange.CaptureSequence}
			}
		}
	}
	return out
}

func cookieEventKey(event rechallenge.CookieEvent) string {
	return strings.ToLower(event.Name) + "|" + strings.ToLower(event.Domain) + "|" + firstPath(event.Path)
}

func (c *contractCompiler) cookieForEvent(event rechallenge.CookieEvent) (*http.Cookie, error) {
	value, err := c.jar.Value(event.ValueSlot)
	if err != nil {
		return nil, err
	}
	return &http.Cookie{
		Name:     event.Name,
		Value:    value,
		Domain:   event.Domain,
		Path:     firstPath(event.Path),
		Secure:   event.Secure,
		HttpOnly: event.HTTPOnly,
		SameSite: parseSameSite(event.SameSite),
	}, nil
}

func parseSameSite(value string) http.SameSite {
	switch strings.ToLower(value) {
	case "lax":
		return http.SameSiteLaxMode
	case "strict":
		return http.SameSiteStrictMode
	case "none":
		return http.SameSiteNoneMode
	default:
		return http.SameSiteDefaultMode
	}
}

func firstPath(path string) string {
	if strings.HasPrefix(path, "/") {
		return path
	}
	return "/"
}

func cloneInt(value *int) *int {
	if value == nil {
		return nil
	}
	clone := *value
	return &clone
}
