package replay

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"

	"github.com/gpt-register/go-email-protocol/internal/transport"
)

const defaultMaxRedirectHops = 10

// Fault deterministically fails one captured exchange after its request has
// matched and been consumed. It is used to exercise ambiguous-after-send paths.
type Fault struct {
	CaptureSequence int
	AfterSend       bool
}

// Options configures a closed, deterministic replay session.
type Options struct {
	MaxRedirectHops int
	Faults          []Fault
}

// Stats is a redaction-safe replay counter snapshot.
type Stats struct {
	Requests          int `json:"requests"`
	ScriptedResponses int `json:"scripted_responses"`
	Consumed          int `json:"consumed"`
	Remaining         int `json:"remaining"`
	Redirects         int `json:"redirects"`
	NetworkFallbacks  int `json:"network_fallbacks"`
}

// Client implements transport.Client using only an in-memory RoundTripper.
// Its http.Client is retained deliberately so redirects and CookieJar behavior
// are the same mechanisms used by the copied live protocol client.
type Client struct {
	mu              sync.Mutex
	jobID           string
	contractID      string
	proxy           transport.ProxySnapshot
	jar             *SymbolicJar
	httpClient      *http.Client
	script          []compiledExchange
	consumed        []bool
	consumedCount   int
	cursor          int
	closed          bool
	requests        int
	responses       int
	redirects       int
	sentinelSeen    int
	redirectLimit   int
	maxRedirectHops int
	faults          map[int]Fault
}

func newClient(jobID, contractID string, jar *SymbolicJar, script []compiledExchange, opts Options) (*Client, error) {
	if strings.TrimSpace(jobID) == "" {
		return nil, fmt.Errorf("replay: job id required")
	}
	if jar == nil {
		return nil, fmt.Errorf("replay: symbolic jar required")
	}
	maxHops := opts.MaxRedirectHops
	if maxHops <= 0 {
		maxHops = defaultMaxRedirectHops
	}
	if maxHops > defaultMaxRedirectHops {
		return nil, fmt.Errorf("replay: max redirect hops=%d exceeds hard limit=%d", maxHops, defaultMaxRedirectHops)
	}
	sequences := make(map[int]struct{}, len(script))
	for _, exchange := range script {
		sequences[exchange.captureSequence] = struct{}{}
	}
	faults := make(map[int]Fault, len(opts.Faults))
	for _, fault := range opts.Faults {
		if _, exists := faults[fault.CaptureSequence]; exists {
			return nil, fmt.Errorf("replay: duplicate fault capture_sequence=%d", fault.CaptureSequence)
		}
		if _, exists := sequences[fault.CaptureSequence]; !exists {
			return nil, fmt.Errorf("replay: fault references unknown capture_sequence=%d", fault.CaptureSequence)
		}
		faults[fault.CaptureSequence] = fault
	}
	ensureCausalDependencies(script)
	if err := validateCausalDependencies(script); err != nil {
		return nil, err
	}
	client := &Client{
		jobID:           jobID,
		contractID:      contractID,
		jar:             jar,
		script:          script,
		consumed:        make([]bool, len(script)),
		maxRedirectHops: maxHops,
		faults:          faults,
	}
	client.httpClient = &http.Client{
		Transport: roundTripper{client: client},
		Jar:       jar,
		CheckRedirect: func(_ *http.Request, via []*http.Request) error {
			client.mu.Lock()
			limit := client.redirectLimit
			if limit <= 0 {
				limit = client.maxRedirectHops
			}
			position := client.nextPositionLocked()
			if len(via) > limit {
				client.mu.Unlock()
				return &ClientError{FailureCode: CodeRedirectLimit, Position: position, Detail: fmt.Sprintf("maximum scripted redirect hops=%d", limit)}
			}
			client.redirects++
			client.mu.Unlock()
			return nil
		},
	}
	return client, nil
}

func ensureCausalDependencies(script []compiledExchange) {
	for index := range script {
		if script[index].causalLane != "" {
			continue
		}
		script[index].causalLane = causalLaneCapture
		if index > 0 {
			script[index].dependencies = appendDependency(script[index].dependencies, index-1)
		}
	}
}

func validateCausalDependencies(script []compiledExchange) error {
	for index, exchange := range script {
		for _, dependency := range exchange.dependencies {
			if dependency < 0 || dependency >= index {
				return fmt.Errorf("replay: invalid causal dependency exchange=%d dependency=%d", index, dependency)
			}
		}
	}
	return nil
}

// JobID implements transport.Client.
func (c *Client) JobID() string {
	if c == nil {
		return ""
	}
	return c.jobID
}

// Proxy implements transport.Client. Replay never has an egress proxy.
func (c *Client) Proxy() transport.ProxySnapshot {
	if c == nil {
		return transport.ProxySnapshot{}
	}
	return c.proxy
}

// Do implements transport.Client. There is no fallback transport to invoke.
func (c *Client) Do(ctx context.Context, req *http.Request) (*http.Response, error) {
	if c == nil {
		return nil, &ClientError{FailureCode: CodeReplayClosed, Detail: "nil client"}
	}
	c.mu.Lock()
	closed := c.closed
	c.mu.Unlock()
	if closed {
		return nil, &ClientError{FailureCode: CodeReplayClosed}
	}
	if req == nil {
		return nil, &Mismatch{ContractID: c.contractID, Position: c.nextPosition(), Field: "request", Expected: "non-nil", Actual: "nil"}
	}
	if err := c.prepareInitialCookies(req); err != nil {
		return nil, err
	}
	if ctx == nil {
		ctx = context.Background()
	}
	resp, err := c.httpClient.Do(req.WithContext(ctx))
	if err == nil {
		return resp, nil
	}
	if resp != nil && resp.Body != nil {
		_ = resp.Body.Close()
	}
	var mismatch *Mismatch
	if errors.As(err, &mismatch) {
		return nil, mismatch
	}
	var clientErr *ClientError
	if errors.As(err, &clientErr) {
		return nil, clientErr
	}
	var transportErr *TransportFailure
	if errors.As(err, &transportErr) {
		return nil, transportErr
	}
	if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
		if cause := context.Cause(ctx); cause != nil {
			return nil, cause
		}
	}
	return nil, &ClientError{FailureCode: CodeWireContractDrift, Position: c.nextPosition(), Detail: "unexpected scripted HTTP client failure"}
}

func (c *Client) prepareInitialCookies(req *http.Request) error {
	if req == nil || req.URL == nil || c.jar == nil {
		return nil
	}
	c.mu.Lock()
	index := c.selectExchangeLocked(req)
	if index < 0 {
		c.mu.Unlock()
		return nil
	}
	rules := append([]compiledCookieRule(nil), c.script[index].request.cookies...)
	position := c.script[index].position
	c.mu.Unlock()
	existing := make(map[string]bool)
	for _, cookie := range c.jar.Cookies(req.URL) {
		if cookie != nil {
			existing[cookie.Name] = true
		}
	}
	for _, rule := range rules {
		if !rule.allowSeed || existing[rule.name] {
			continue
		}
		value, err := c.jar.Value(rule.slot)
		if err != nil {
			return &Mismatch{ContractID: c.contractID, Position: position, Field: "request.cookies." + rule.name, Expected: "valid symbolic slot", Actual: "invalid slot"}
		}
		seedURL := *req.URL
		if host := strings.TrimPrefix(strings.ToLower(rule.domain), "."); host != "" {
			seedURL.Host = host
		}
		seedURL.Path = rule.path
		c.jar.SetCookies(&seedURL, []*http.Cookie{{
			Name: rule.name, Value: value, Domain: rule.domain, Path: rule.path,
			Secure: rule.secure, HttpOnly: rule.httpOnly, SameSite: rule.sameSite,
		}})
		existing[rule.name] = true
	}
	return nil
}

// Close implements transport.Client.
func (c *Client) Close() error {
	if c == nil {
		return nil
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	c.closed = true
	return nil
}

// Jar returns the real symbolic CookieJar used by the replay http.Client.
func (c *Client) Jar() *SymbolicJar {
	if c == nil {
		return nil
	}
	return c.jar
}

// Stats returns counters proving that replay has no network fallback surface.
func (c *Client) Stats() Stats {
	if c == nil {
		return Stats{}
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	return Stats{
		Requests:          c.requests,
		ScriptedResponses: c.responses,
		Consumed:          c.consumedCount,
		Remaining:         len(c.script) - c.consumedCount,
		Redirects:         c.redirects,
		NetworkFallbacks:  0,
	}
}

// AssertComplete fails closed if the engine did not consume the entire capture.
func (c *Client) AssertComplete() error {
	if c == nil {
		return &ClientError{FailureCode: CodeReplayClosed, Detail: "nil client"}
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.consumedCount == len(c.script) {
		return nil
	}
	next := c.nextPositionLocked()
	return &Mismatch{
		ContractID: c.contractID,
		Position:   next,
		Field:      "script.completion",
		Expected:   "all exchanges consumed",
		Actual:     fmt.Sprintf("remaining=%d", len(c.script)-c.consumedCount),
	}
}

func (c *Client) nextPosition() Position {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.nextPositionLocked()
}

func (c *Client) nextPositionLocked() Position {
	if index := c.firstUnconsumedIndexLocked(); index >= 0 {
		return c.script[index].position
	}
	return Position{}
}

func (c *Client) firstUnconsumedIndexLocked() int {
	for index := c.cursor; index < len(c.script); index++ {
		if !c.consumed[index] {
			return index
		}
	}
	return -1
}

type roundTripper struct {
	client *Client
}

func (r roundTripper) RoundTrip(req *http.Request) (*http.Response, error) {
	if r.client == nil {
		return nil, &ClientError{FailureCode: CodeReplayClosed, Detail: "nil round tripper client"}
	}
	return r.client.roundTrip(req)
}

func (c *Client) roundTrip(req *http.Request) (*http.Response, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.requests++
	if c.closed {
		return nil, &ClientError{FailureCode: CodeReplayClosed}
	}
	index := c.selectExchangeLocked(req)
	if index < 0 {
		actual := "nil"
		if req != nil && req.URL != nil {
			actual = req.Method + " " + req.URL.Hostname() + req.URL.EscapedPath()
		}
		return nil, &Mismatch{ContractID: c.contractID, Position: c.nextPositionLocked(), Field: "script.order", Expected: "causally eligible request", Actual: actual}
	}
	exchange := c.script[index]
	body, err := readAndRestoreBody(req)
	if err != nil {
		return nil, &Mismatch{ContractID: c.contractID, Position: exchange.position, Field: "request.body", Expected: "readable", Actual: "read failure"}
	}
	actualOccurrence := c.sentinelSeen
	if mismatch := matchRequest(c.contractID, exchange, req, body, actualOccurrence, c.jar); mismatch != nil {
		return nil, mismatch
	}
	nonReplayable := exchange.response.statusCode <= 0 || (!exchange.response.replayable && exchange.response.outcome != "")
	if nonReplayable {
		outcome := exchange.response.outcome
		if outcome == "" {
			outcome = "unknown"
		}
		return nil, &Mismatch{
			ContractID: c.contractID,
			Position:   exchange.position,
			Field:      "response.replayability",
			Expected:   "observed replayable status",
			Actual:     outcome,
			Detail:     "HAR response status is unavailable or response semantics are unknown",
		}
	}
	if fault, ok := c.faults[exchange.captureSequence]; ok && !fault.AfterSend {
		return nil, &TransportFailure{Position: exchange.position, Sent: false}
	}
	if exchange.request.sentinelOccurrence != nil {
		c.sentinelSeen++
	}
	c.markConsumedLocked(index)
	if fault, ok := c.faults[exchange.captureSequence]; ok && fault.AfterSend {
		return nil, &TransportFailure{Position: exchange.position, Sent: true}
	}
	c.redirectLimit = exchange.response.redirectMaxHops
	response := exchange.response.clone(req)
	c.responses++
	return response, nil
 }

func (c *Client) selectExchangeLocked(req *http.Request) int {
	eligible := c.eligibleIndexesLocked()
	if len(eligible) == 0 {
		return -1
	}
	for _, index := range eligible {
		if requestMatchesEndpoint(req, c.script[index].request) {
			return index
		}
	}
	lane := requestCausalLane(req)
	for _, index := range eligible {
		if c.script[index].causalLane == lane {
			return index
		}
	}
	return eligible[0]
}

func (c *Client) eligibleIndexesLocked() []int {
	eligible := make([]int, 0, 2)
	for index := range c.script {
		if c.consumed[index] || !c.dependenciesConsumedLocked(c.script[index].dependencies) {
			continue
		}
		eligible = append(eligible, index)
	}
	return eligible
}

func (c *Client) dependenciesConsumedLocked(dependencies []int) bool {
	for _, dependency := range dependencies {
		if dependency < 0 || dependency >= len(c.consumed) || !c.consumed[dependency] {
			return false
		}
	}
	return true
}

func (c *Client) markConsumedLocked(index int) {
	if index < 0 || index >= len(c.consumed) || c.consumed[index] {
		return
	}
	c.consumed[index] = true
	c.consumedCount++
	for c.cursor < len(c.consumed) && c.consumed[c.cursor] {
		c.cursor++
	}
}

func requestMatchesEndpoint(req *http.Request, rule compiledRequest) bool {
	return req != nil && req.URL != nil && strings.EqualFold(req.Method, rule.method) &&
		strings.EqualFold(req.URL.Hostname(), rule.host) && req.URL.EscapedPath() == rule.path
}

func requestCausalLane(req *http.Request) string {
	if req != nil && req.URL != nil && strings.EqualFold(req.URL.Hostname(), "sentinel.openai.com") && req.URL.EscapedPath() == "/backend-api/sentinel/req" {
		return causalLaneSentinel
	}
	return causalLaneRegistration
}

func readAndRestoreBody(req *http.Request) ([]byte, error) {
	if req == nil || req.Body == nil || req.Body == http.NoBody {
		return nil, nil
	}
	body, err := io.ReadAll(req.Body)
	if err != nil {
		return nil, err
	}
	_ = req.Body.Close()
	req.Body = io.NopCloser(bytes.NewReader(body))
	return body, nil
}
