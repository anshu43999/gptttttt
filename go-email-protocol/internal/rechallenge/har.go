package rechallenge

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/gpt-register/go-email-protocol/internal/protocol"
)

type IngestOptions struct {
	CaptureID         string
	ExpectedRole      CaptureRole
	ExpectedSHA256    string
	RedactionPolicyID string
}

type Capture struct {
	Manifest  CaptureManifest
	Exchanges []StateExchangeContract
}

type harNameValue struct {
	Name  string `json:"name"`
	Value string `json:"value"`
}

type harCookie struct {
	Name     string `json:"name"`
	Value    string `json:"value"`
	Path     string `json:"path"`
	Domain   string `json:"domain"`
	HTTPOnly bool   `json:"httpOnly"`
	Secure   bool   `json:"secure"`
	SameSite string `json:"sameSite"`
}

type harPostData struct {
	MimeType string         `json:"mimeType"`
	Text     string         `json:"text"`
	Params   []harNameValue `json:"params"`
}

type harContent struct {
	MimeType string `json:"mimeType"`
	Text     string `json:"text"`
	Encoding string `json:"encoding"`
}

type harRequest struct {
	Method   string         `json:"method"`
	URL      string         `json:"url"`
	Headers  []harNameValue `json:"headers"`
	Cookies  []harCookie    `json:"cookies"`
	PostData *harPostData   `json:"postData"`
}

type harResponse struct {
	Status      int            `json:"status"`
	RedirectURL string         `json:"redirectURL"`
	Headers     []harNameValue `json:"headers"`
	Cookies     []harCookie    `json:"cookies"`
	Content     harContent     `json:"content"`
}

type harEntry struct {
	StartedDateTime string      `json:"startedDateTime"`
	Request         harRequest  `json:"request"`
	Response        harResponse `json:"response"`
}

type harCreator struct {
	Name    string `json:"name"`
	Version string `json:"version"`
}

type roleCollector struct {
	registrationSignals map[string]int
	checkoutSignals     map[string]int
	sentinelOccurrences int
	evidence            map[string]RoleEvidence
}

func IngestHAR(ctx context.Context, path string, options IngestOptions) (*Capture, error) {
	if ctx == nil {
		ctx = context.Background()
	}
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()

	hasher := sha256.New()
	stream := io.TeeReader(f, hasher)
	decoder := json.NewDecoder(stream)
	collector := &roleCollector{registrationSignals: make(map[string]int), checkoutSignals: make(map[string]int), evidence: make(map[string]RoleEvidence)}
	capture := &Capture{}
	stateIndexes := make(map[protocol.StateID]int)
	sentinelOccurrence := 0
	captureSequence := 0
	seenCallback := false
	var creator harCreator
	var earliest time.Time
	var browser string
	var uaMajor int
	var locale string

	err = streamHAREntries(ctx, decoder, &creator, func(harIndex int, entry *harEntry) error {
		if entry == nil {
			return nil
		}
		parsedURL, err := url.Parse(entry.Request.URL)
		if err != nil || parsedURL.Hostname() == "" {
			return nil
		}
		flow, requestP := "", ""
		if strings.EqualFold(entry.Request.Method, http.MethodPost) && parsedURL.Path == "/backend-api/sentinel/req" && (parsedURL.Hostname() == "sentinel.openai.com" || parsedURL.Hostname() == "chatgpt.com") {
			flow, requestP, err = sentinelRequestValues(entry.Request.PostData)
			if err != nil {
				return contractError(CodeWireContractDrift, "ingest", "sentinel", "request.body", fmt.Sprintf("har_entry:%d", harIndex), err)
			}
		}
		collector.observe(harIndex, entry.Request.Method, parsedURL.Hostname(), parsedURL.Path, flow)
		if browser == "" {
			browser, uaMajor = browserIdentity(headerValue(entry.Request.Headers, "user-agent"))
		}
		if locale == "" {
			locale = primaryLocale(headerValue(entry.Request.Headers, "accept-language"))
		}
		if started, parseErr := time.Parse(time.RFC3339Nano, entry.StartedDateTime); parseErr == nil && (earliest.IsZero() || started.Before(earliest)) {
			earliest = started
		}

		state, kind, include := mapObservedEndpoint(entry.Request.Method, parsedURL.Hostname(), parsedURL.Path, seenCallback)
		if !include {
			return nil
		}
		if state == protocol.S12 && parsedURL.Path == "/api/auth/callback/openai" {
			seenCallback = true
		}
		exchangeIndex := stateIndexes[state]
		stateIndexes[state]++
		responseContract, responseErr := normalizeHARResponse(kind, &entry.Response)
		if responseErr != nil {
			return contractError(CodeWireContractDrift, "normalize", "response", "response.content", fmt.Sprintf("har_entry:%d", harIndex), responseErr)
		}
		exchange := StateExchangeContract{
			State:           state,
			ExchangeIndex:   exchangeIndex,
			CaptureSequence: captureSequence,
			Provenance: ExchangeProvenance{
				SourceKind: "har",
				HARIndex:   harIndex,
				StartedAt:  entry.StartedDateTime,
				Observed:   true,
			},
			Request:  normalizeHARRequest(kind, parsedURL, &entry.Request),
			Response: responseContract,
			Redirect: normalizeRedirect(&entry.Response),
		}
		exchange.CookieEvents = normalizeCookieEvents(parsedURL, &entry.Request, &entry.Response)
		if state == protocol.T1 {
			occurrence := sentinelOccurrence
			sentinelOccurrence++
			exchange.SentinelOccurrence = &occurrence
			exchange.FlowName = flow
			source, fingerprint, observationErr := sentinelObservation(requestP, &entry.Response)
			if observationErr != nil {
				return contractError(CodeWireContractDrift, "ingest", "sentinel", "request.p", fmt.Sprintf("har_entry:%d", harIndex), observationErr)
			}
			exchange.ObservedScriptSource = source
			exchange.RequirementsFingerprint = fingerprint
		}
		capture.Exchanges = append(capture.Exchanges, exchange)
		captureSequence++
		return nil
	})
	if err != nil {
		return nil, err
	}
	var trailing any
	if err := decoder.Decode(&trailing); err != io.EOF {
		if err == nil {
			return nil, errors.New("rechallenge: HAR contains a trailing JSON value")
		}
		return nil, fmt.Errorf("rechallenge: HAR trailing content: %w", err)
	}
	if _, err := io.Copy(io.Discard, stream); err != nil {
		return nil, fmt.Errorf("rechallenge: finish HAR source hash: %w", err)
	}
	digest := hex.EncodeToString(hasher.Sum(nil))
	if expected := strings.TrimPrefix(strings.ToLower(strings.TrimSpace(options.ExpectedSHA256)), "sha256:"); expected != "" && expected != digest {
		return nil, contractError(CodeSourceHashMismatch, "ingest", "provenance", "source_sha256", options.CaptureID, fmt.Errorf("got %s want %s", digest, expected))
	}
	captureID := strings.TrimSpace(options.CaptureID)
	if captureID == "" {
		captureID = "capture-" + digest[:12]
	}
	role, evidence := collector.classify()
	if options.ExpectedRole != "" && role != options.ExpectedRole {
		return nil, contractError(CodeCaptureRoleMismatch, "classify", "capture", "role", captureID, fmt.Errorf("observed %s expected %s", role, options.ExpectedRole))
	}
	if browser == "" {
		browser, uaMajor = browserIdentity(creator.Name + "/" + creator.Version)
	}
	policy := strings.TrimSpace(options.RedactionPolicyID)
	if policy == "" {
		if role == RoleCheckout { policy = "checkout-v1" } else { policy = "registration-v1" }
	}
	capturedAt, timezone := captureTimeIdentity(earliest)
	capture.Manifest = CaptureManifest{
		SchemaVersion: CurrentSchemaVersion,
		CaptureID: captureID,
		Role: role,
		RoleEvidence: evidence,
		CapturedAt: capturedAt,
		SourceSHA256: "sha256:" + digest,
		Browser: browser,
		UAMajor: uaMajor,
		Locale: locale,
		Timezone: timezone,
		SourceKind: "har",
		RedactionPolicyID: policy,
	}
	for i := range capture.Exchanges {
		capture.Exchanges[i].Provenance.CaptureID = captureID
	}
	return capture, nil
}

func streamHAREntries(ctx context.Context, decoder *json.Decoder, creator *harCreator, visit func(int, *harEntry) error) error {
	token, err := decoder.Token()
	if err != nil {
		return err
	}
	if delimiter, ok := token.(json.Delim); !ok || delimiter != '{' {
		return errors.New("rechallenge: HAR root must be an object")
	}
	foundLog := false
	for decoder.More() {
		keyToken, err := decoder.Token()
		if err != nil {
			return err
		}
		key, _ := keyToken.(string)
		if key != "log" {
			if err := skipJSONValue(decoder); err != nil {
				return err
			}
			continue
		}
		foundLog = true
		if err := streamHARLog(ctx, decoder, creator, visit); err != nil {
			return err
		}
	}
	if _, err := decoder.Token(); err != nil {
		return err
	}
	if !foundLog {
		return errors.New("rechallenge: HAR missing log object")
	}
	return nil
}

func streamHARLog(ctx context.Context, decoder *json.Decoder, creator *harCreator, visit func(int, *harEntry) error) error {
	token, err := decoder.Token()
	if err != nil {
		return err
	}
	if delimiter, ok := token.(json.Delim); !ok || delimiter != '{' {
		return errors.New("rechallenge: HAR log must be an object")
	}
	foundEntries := false
	for decoder.More() {
		keyToken, err := decoder.Token()
		if err != nil {
			return err
		}
		key, _ := keyToken.(string)
		switch key {
		case "creator":
			if err := decoder.Decode(creator); err != nil {
				return err
			}
		case "entries":
			foundEntries = true
			start, err := decoder.Token()
			if err != nil {
				return err
			}
			if delimiter, ok := start.(json.Delim); !ok || delimiter != '[' {
				return errors.New("rechallenge: HAR log.entries must be an array")
			}
			index := 0
			for decoder.More() {
				select {
				case <-ctx.Done():
					return ctx.Err()
				default:
				}
				var entry harEntry
				if err := decoder.Decode(&entry); err != nil {
					return fmt.Errorf("rechallenge: decode HAR entry %d: %w", index, err)
				}
				if err := visit(index, &entry); err != nil {
					return err
				}
				index++
			}
			if _, err := decoder.Token(); err != nil {
				return err
			}
		default:
			if err := skipJSONValue(decoder); err != nil {
				return err
			}
		}
	}
	if _, err := decoder.Token(); err != nil {
		return err
	}
	if !foundEntries {
		return errors.New("rechallenge: HAR missing log.entries")
	}
	return nil
}

func skipJSONValue(decoder *json.Decoder) error {
	token, err := decoder.Token()
	if err != nil {
		return err
	}
	delimiter, ok := token.(json.Delim)
	if !ok {
		return nil
	}
	if delimiter != '{' && delimiter != '[' {
		return nil
	}
	for decoder.More() {
		if delimiter == '{' {
			if _, err := decoder.Token(); err != nil {
				return err
			}
		}
		if err := skipJSONValue(decoder); err != nil {
			return err
		}
	}
	_, err = decoder.Token()
	return err
}

func (collector *roleCollector) observe(index int, method, host, path, flow string) {
	method = strings.ToUpper(method)
	host = strings.ToLower(host)
	lowerPath := strings.ToLower(path)
	registrationSignal := ""
	switch {
	case method == http.MethodGet && host == "chatgpt.com" && lowerPath == "/api/auth/providers":
		registrationSignal = "providers"
	case method == http.MethodGet && host == "chatgpt.com" && lowerPath == "/api/auth/csrf":
		registrationSignal = "csrf"
	case method == http.MethodPost && host == "chatgpt.com" && lowerPath == "/api/auth/signin/openai":
		registrationSignal = "signin"
	case method == http.MethodGet && host == "auth.openai.com" && lowerPath == "/api/accounts/authorize":
		registrationSignal = "authorize"
	case method == http.MethodPost && host == "auth.openai.com" && lowerPath == "/api/accounts/email-otp/validate":
		registrationSignal = "otp_validate"
	case method == http.MethodPost && host == "auth.openai.com" && lowerPath == "/api/accounts/create_account":
		registrationSignal = "create_account"
	case method == http.MethodGet && host == "chatgpt.com" && lowerPath == "/api/auth/callback/openai":
		registrationSignal = "callback"
	case method == http.MethodPost && host == "sentinel.openai.com" && lowerPath == "/backend-api/sentinel/req" && flow == "oauth_create_account":
		collector.sentinelOccurrences++
		collector.add(RoleEvidence{Kind: "sentinel_flow", Host: host, Path: path, Flow: flow})
	}
	if registrationSignal != "" {
		collector.recordSignal(collector.registrationSignals, registrationSignal, index)
		collector.add(RoleEvidence{Kind: "registration_endpoint", Host: host, Path: path})
	}
	if method == http.MethodGet && host == "chatgpt.com" && strings.HasPrefix(lowerPath, "/checkout/") {
		collector.recordSignal(collector.checkoutSignals, "checkout_page", index)
		collector.add(RoleEvidence{Kind: "checkout_endpoint", Host: host, Path: path})
	} else if method == http.MethodPost && host == "chatgpt.com" && strings.Contains(lowerPath, "/payments/checkout/") {
		collector.recordSignal(collector.checkoutSignals, "checkout_payment", index)
		collector.add(RoleEvidence{Kind: "checkout_endpoint", Host: host, Path: path})
	} else if host == "chatgpt.com" && strings.Contains(lowerPath, "checkout_pricing") {
		collector.add(RoleEvidence{Kind: "checkout_context", Host: host, Path: path})
	}
	if method == http.MethodPost && host == "chatgpt.com" && lowerPath == "/backend-api/sentinel/req" && flow == "checkout_session_approval" {
		collector.recordSignal(collector.checkoutSignals, "checkout_sentinel", index)
		collector.add(RoleEvidence{Kind: "sentinel_flow", Host: host, Path: path, Flow: flow})
	}
}

func (collector *roleCollector) recordSignal(signals map[string]int, name string, index int) {
	if _, exists := signals[name]; !exists {
		signals[name] = index
	}
}
func (collector *roleCollector) add(evidence RoleEvidence) {
	evidence.Path = sanitizeEvidencePath(evidence.Path)
	key := evidence.Kind + "|" + evidence.Host + "|" + evidence.Path + "|" + evidence.Flow
	collector.evidence[key] = evidence
}

func sanitizeEvidencePath(path string) string {
	if strings.HasPrefix(strings.ToLower(path), "/checkout/") {
		return "/checkout/{checkout_session}"
	}
	return path
}

func (collector *roleCollector) classify() (CaptureRole, []RoleEvidence) {
	evidence := make([]RoleEvidence, 0, len(collector.evidence))
	for _, item := range collector.evidence {
		evidence = append(evidence, item)
	}
	sort.Slice(evidence, func(i, j int) bool {
		return evidence[i].Kind+evidence[i].Host+evidence[i].Path+evidence[i].Flow < evidence[j].Kind+evidence[j].Host+evidence[j].Path+evidence[j].Flow
	})
	registrationOrder := []string{"providers", "csrf", "signin", "authorize", "otp_validate", "create_account", "callback"}
	registration := collector.sentinelOccurrences == 3 && signalsStrictlyOrdered(collector.registrationSignals, registrationOrder)
	_, checkoutSentinel := collector.checkoutSignals["checkout_sentinel"]
	_, checkoutPage := collector.checkoutSignals["checkout_page"]
	_, checkoutPayment := collector.checkoutSignals["checkout_payment"]
	checkout := checkoutSentinel && (checkoutPage || checkoutPayment)
	if registration && !checkout {
		return RoleRegistration, evidence
	}
	if checkout && !registration {
		return RoleCheckout, evidence
	}
	return RoleUnknown, evidence
}

func signalsStrictlyOrdered(signals map[string]int, names []string) bool {
	previous := -1
	for _, name := range names {
		index, exists := signals[name]
		if !exists || index <= previous {
			return false
		}
		previous = index
	}
	return true
}

func mapObservedEndpoint(method, host, path string, seenCallback bool) (protocol.StateID, string, bool) {
	method = strings.ToUpper(method)
	host = strings.ToLower(host)
	switch {
	case method == http.MethodGet && host == "chatgpt.com" && path == "/api/auth/providers":
		return protocol.S1, "auth_providers", true
	case method == http.MethodGet && host == "chatgpt.com" && path == "/api/auth/csrf":
		return protocol.S2, "csrf", true
	case method == http.MethodPost && host == "chatgpt.com" && path == "/api/auth/signin/openai":
		return protocol.S3, "signin", true
	case method == http.MethodGet && host == "auth.openai.com" && path == "/api/accounts/authorize":
		return protocol.S4, "authorize", true
	case method == http.MethodGet && host == "auth.openai.com" && path == "/email-verification":
		return protocol.S4, "redirect_hop", true
	case method == http.MethodPost && host == "auth.openai.com" && path == "/api/accounts/email-otp/validate":
		return protocol.S10, "otp_validate", true
	case method == http.MethodPost && host == "sentinel.openai.com" && path == "/backend-api/sentinel/req":
		return protocol.T1, "sentinel_req", true
	case method == http.MethodPost && host == "auth.openai.com" && path == "/api/accounts/create_account":
		return protocol.S11, "create_account", true
	case method == http.MethodGet && host == "chatgpt.com" && path == "/api/auth/callback/openai":
		return protocol.S12, "callback", true
	case method == http.MethodGet && host == "chatgpt.com" && path == "/" && seenCallback:
		return protocol.S12, "redirect_hop", true
	default:
		return "", "", false
	}
}

func sentinelRequestValues(postData *harPostData) (flow, requestP string, err error) {
	if postData == nil || strings.TrimSpace(postData.Text) == "" {
		return "", "", errors.New("missing Sentinel request body")
	}
	var body map[string]json.RawMessage
	if err := json.Unmarshal([]byte(postData.Text), &body); err != nil {
		return "", "", fmt.Errorf("invalid Sentinel request JSON: %w", err)
	}
	if err := json.Unmarshal(body["flow"], &flow); err != nil || strings.TrimSpace(flow) == "" {
		return "", "", errors.New("missing Sentinel flow")
	}
	if err := json.Unmarshal(body["p"], &requestP); err != nil || strings.TrimSpace(requestP) == "" {
		return "", "", errors.New("missing Sentinel requirements token")
	}
	return flow, requestP, nil
}

func sentinelObservation(requestP string, response *harResponse) (string, string, error) {
	payload, err := decodeSentinelPayload(requestP)
	if err != nil {
		return "", "", err
	}
	if len(payload) != 25 {
		return "", "", fmt.Errorf("Sentinel payload length %d, want 25", len(payload))
	}
	source, ok := payload[5].(string)
	if !ok {
		return "", "", errors.New("Sentinel payload index 5 is not a script source")
	}
	if source != "https://sentinel.openai.com/backend-api/sentinel/sdk.js" && source != "https://sentinel.openai.com/sentinel/20260219f9f6/sdk.js" {
		return "", "", errors.New("Sentinel payload index 5 is not a trusted release source")
	}
	if payload[6] != nil {
		return "", "", errors.New("Sentinel payload index 6 violates Firefox null policy")
	}
	types := make([]string, len(payload))
	for index, item := range payload {
		types[index] = jsonType(item)
	}
	responseRaw, err := decodeHARContent(response)
	if err != nil {
		return "", "", err
	}
	shape := map[string]any{
		"payload_length": len(payload),
		"payload_types": types,
		"response_semantics": responseSemanticShape(responseRaw),
	}
	raw, err := json.Marshal(shape)
	if err != nil {
		return "", "", err
	}
	sum := sha256.Sum256(raw)
	return source, "sha256:" + hex.EncodeToString(sum[:]), nil
}

func decodeSentinelPayload(requestP string) ([]any, error) {
	if !strings.HasPrefix(requestP, "gAAAAAC") {
		return nil, errors.New("Sentinel requirements token has an unknown prefix")
	}
	encoded := strings.TrimPrefix(requestP, "gAAAAAC")
	if marker := strings.Index(encoded, "~"); marker >= 0 {
		encoded = encoded[:marker]
	}
	raw, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil {
		return nil, errors.New("Sentinel requirements token is not valid base64")
	}
	var payload []any
	if err := json.Unmarshal(raw, &payload); err != nil {
		return nil, errors.New("Sentinel requirements token payload is not valid JSON")
	}
	return payload, nil
}

func decodeHARContent(response *harResponse) ([]byte, error) {
	if response == nil || response.Content.Text == "" {
		return nil, nil
	}
	if response.Content.Encoding == "" {
		return []byte(response.Content.Text), nil
	}
	if !strings.EqualFold(response.Content.Encoding, "base64") {
		return nil, fmt.Errorf("unsupported HAR content encoding %q", response.Content.Encoding)
	}
	raw, err := base64.StdEncoding.DecodeString(response.Content.Text)
	if err != nil {
		return nil, errors.New("invalid base64 HAR response content")
	}
	return raw, nil
}

func responseSemanticShape(raw []byte) []string {
	if len(raw) == 0 {
		return []string{"unknown"}
	}
	var value any
	if json.Unmarshal(raw, &value) != nil {
		return []string{"unknown"}
	}
	var out []string
	var walk func(string, any)
	walk = func(prefix string, value any) {
		switch typed := value.(type) {
		case map[string]any:
			keys := make([]string, 0, len(typed))
			for key := range typed { keys = append(keys, key) }
			sort.Strings(keys)
			for _, key := range keys {
				path := key
				if prefix != "" { path = prefix + "." + key }
				walk(path, typed[key])
			}
		case bool:
			out = append(out, fmt.Sprintf("%s:boolean=%t", prefix, typed))
		case string:
			if strings.HasSuffix(prefix, "difficulty") {
				out = append(out, prefix+":string="+typed)
			} else {
				out = append(out, prefix+":string")
			}
		default:
			out = append(out, prefix+":"+jsonType(value))
		}
	}
	walk("", value)
	sort.Strings(out)
	return out
}

func jsonType(value any) string {
	switch value.(type) {
	case nil: return "null"
	case bool: return "boolean"
	case float64, json.Number: return "number"
	case string: return "string"
	case []any: return "array"
	case map[string]any: return "object"
	default: return "unknown"
	}
}

func browserIdentity(userAgent string) (string, int) {
	match := regexp.MustCompile(`(?i)Firefox/(\d+)`).FindStringSubmatch(userAgent)
	if len(match) == 2 {
		major, _ := strconv.Atoi(match[1])
		return "firefox", major
	}
	match = regexp.MustCompile(`(?i)(?:Chrome|Chromium)/(\d+)`).FindStringSubmatch(userAgent)
	if len(match) == 2 {
		major, _ := strconv.Atoi(match[1])
		return "chrome", major
	}
	return strings.ToLower(strings.TrimSpace(strings.Split(userAgent, "/")[0])), 0
}

func primaryLocale(acceptLanguage string) string {
	first := strings.TrimSpace(strings.Split(acceptLanguage, ",")[0])
	return strings.TrimSpace(strings.Split(first, ";")[0])
}

func captureTimeIdentity(captured time.Time) (string, string) {
	if captured.IsZero() {
		return "", ""
	}
	_, offset := captured.Zone()
	sign := "+"
	if offset < 0 { sign = "-"; offset = -offset }
	timezone := fmt.Sprintf("UTC%s%02d:%02d", sign, offset/3600, (offset%3600)/60)
	return captured.Format(time.RFC3339Nano), timezone
}

func headerValue(headers []harNameValue, name string) string {
	for _, header := range headers {
		if strings.EqualFold(header.Name, name) {
			return header.Value
		}
	}
	return ""
}
