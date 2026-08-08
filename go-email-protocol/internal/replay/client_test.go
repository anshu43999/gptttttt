package replay

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	mathrand "math/rand/v2"
	"net/http"
	"net/url"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"github.com/gpt-register/go-email-protocol/internal/fingerprint"
	"github.com/gpt-register/go-email-protocol/internal/protocol"
	"github.com/gpt-register/go-email-protocol/internal/rechallenge"
)

func TestObservedContractsStopAtNonReplayableEvidence(t *testing.T) {
	for _, test := range []struct {
		capture string
		sequence int
		state string
		occurrence *int
	}{
		{capture: "d17-firefox150-ptbr", sequence: 7, state: string(protocol.S11)},
		{capture: "d24-firefox150-jajp", sequence: 8, state: string(protocol.T1), occurrence: intPtr(1)},
	} {
		t.Run(test.capture, func(t *testing.T) {
			contract := loadRegistrationContract(t, test.capture)
			client, err := NewClient("job-"+test.capture, contract, Options{})
			if err != nil {
				t.Fatal(err)
			}
			defer client.Close()

			redirectCookies := make(map[string]bool)
			var got error
			for client.Stats().Remaining > 0 {
				exchange := exchangeAtSequence(t, contract, client.Stats().Consumed)
				req := contractRequest(t, exchange, contract.BrowserIdentity, nil)
			resp, callErr := client.Do(context.Background(), req)
			if callErr != nil {
				got = callErr
				break
			}
			_, _ = io.Copy(io.Discard, resp.Body)
			_ = resp.Body.Close()
			if exchange.Redirect != nil {
				assertRedirectCookiePropagation(t, client, contract, exchange, redirectCookies)
			}
			}
			var mismatch *Mismatch
			if !errors.As(got, &mismatch) {
				t.Fatalf("got %T %v, want typed non-replayable mismatch", got, got)
			}
			if mismatch.Position.CaptureSequence != test.sequence || mismatch.Position.State != test.state || mismatch.Field != "response.replayability" || mismatch.Expected != "observed replayable status" || mismatch.Actual != "capture_transport_incomplete" {
				t.Fatalf("unexpected non-replayable blocker: %+v", mismatch)
			}
			if (mismatch.Position.SentinelOccurrence == nil) != (test.occurrence == nil) || test.occurrence != nil && *mismatch.Position.SentinelOccurrence != *test.occurrence {
				t.Fatalf("unexpected occurrence provenance: %+v", mismatch.Position)
			}
			if !redirectCookies[string(protocol.S4)] {
				t.Fatalf("%s did not prove S4 redirect CookieJar propagation before blocker", test.capture)
			}
			stats := client.Stats()
			if stats.NetworkFallbacks != 0 || stats.Consumed != test.sequence || stats.ScriptedResponses != test.sequence {
				t.Fatalf("unexpected consumption before blocker: %+v", stats)
			}
		})
	}
}

func intPtr(value int) *int { return &value }

func TestS12RedirectUsesRealJarAndPropagatesSymbolicCookies(t *testing.T) {
	for _, capture := range []string{"d17-firefox150-ptbr", "d24-firefox150-jajp"} {
		t.Run(capture, func(t *testing.T) {
			contract := loadRegistrationContract(t, capture)
			var callback, home rechallenge.StateExchangeContract
			for _, exchange := range contract.Exchanges {
				if exchange.Request.Kind == "callback" {
					callback = exchange
			}
				if exchange.Request.Kind == "redirect_hop" && exchange.State == protocol.S12 {
					home = exchange
				}
			}
			if callback.Request.Path == "" || home.Request.Path == "" {
				t.Fatal("S12 callback or redirect hop absent")
			}
			callback.CaptureSequence, callback.ExchangeIndex = 0, 0
			home.CaptureSequence, home.ExchangeIndex = 1, 1
			exchanges := []rechallenge.StateExchangeContract{callback, home}
			jar, err := NewSymbolicJar()
			if err != nil {
				t.Fatal(err)
			}
			compiler := contractCompiler{contract: contract, exchanges: exchanges, jar: jar, firstCookies: firstCookieEvents(exchanges)}
			script := make([]compiledExchange, 0, len(exchanges))
			for index := range exchanges {
				compiled, err := compiler.compileExchange(&exchanges[index])
				if err != nil {
					t.Fatal(err)
				}
				script = append(script, compiled)
			}
			client, err := newClient("s12-"+capture, contract.ContractID, jar, script, Options{})
			if err != nil {
				t.Fatal(err)
			}
			resp, err := client.Do(context.Background(), contractRequest(t, &callback, contract.BrowserIdentity, nil))
			if err != nil {
				t.Fatal(err)
			}
			_ = resp.Body.Close()
			if err := client.AssertComplete(); err != nil {
				t.Fatal(err)
			}
			proved := make(map[string]bool)
			assertRedirectCookiePropagation(t, client, &rechallenge.RegistrationContract{Exchanges: exchanges}, &callback, proved)
			if !proved[string(protocol.S12)] {
				t.Fatal("S12 callback Set-Cookie was not propagated by real jar")
			}
			stats := client.Stats()
			if stats.Redirects != 1 || stats.NetworkFallbacks != 0 {
				t.Fatalf("unexpected S12 stats: %+v", stats)
			}
		})
	}
}

func TestS12RedirectAndSentinelUseIndependentCausalLanes(t *testing.T) {
	contract := loadRegistrationContract(t, "d24-firefox150-jajp")
	var callback, sentinel, home rechallenge.StateExchangeContract
	for _, exchange := range contract.Exchanges {
		switch {
		case exchange.Request.Kind == "callback":
			callback = exchange
		case exchange.SentinelOccurrence != nil && *exchange.SentinelOccurrence == 2:
			sentinel = exchange
		case exchange.Request.Kind == "redirect_hop" && exchange.State == protocol.S12:
			home = exchange
		}
	}
	if callback.Request.Path == "" || sentinel.Request.Path == "" || home.Request.Path == "" {
		t.Fatal("callback, third Sentinel occurrence, or redirect hop absent")
	}

	exchanges := []rechallenge.StateExchangeContract{callback, sentinel, home}
	jar, err := NewSymbolicJar()
	if err != nil {
		t.Fatal(err)
	}
	compiler := contractCompiler{contract: contract, exchanges: exchanges, jar: jar, firstCookies: firstCookieEvents(exchanges)}
	script := make([]compiledExchange, 0, len(exchanges))
	for index := range exchanges {
		compiled, compileErr := compiler.compileExchange(&exchanges[index])
		if compileErr != nil {
			t.Fatal(compileErr)
		}
		script = append(script, compiled)
	}
	assignContractCausalLanes(script)
	client, err := newClient("s12-causal-lanes", contract.ContractID, jar, script, Options{})
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	client.sentinelSeen = 2

	resp, err := client.Do(context.Background(), contractRequest(t, &callback, contract.BrowserIdentity, nil))
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if stats := client.Stats(); stats.Consumed != 2 || stats.Redirects != 1 || stats.NetworkFallbacks != 0 {
		t.Fatalf("callback redirect did not bypass only the eligible auxiliary lane: %+v", stats)
	}
	if client.consumed[1] || !client.consumed[2] {
		t.Fatalf("capture order was reordered instead of tracked causally: consumed=%v", client.consumed)
	}

	resp, err = client.Do(context.Background(), contractRequest(t, &sentinel, contract.BrowserIdentity, nil))
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if err := client.AssertComplete(); err != nil {
		t.Fatal(err)
	}
	if stats := client.Stats(); stats.Consumed != 3 || stats.ScriptedResponses != 3 || stats.NetworkFallbacks != 0 {
		t.Fatalf("causal lanes did not complete exactly once: %+v", stats)
	}
}

func TestWrongSentinelOccurrenceCannotConsumeResponse(t *testing.T) {
	contract := loadRegistrationContract(t, "d24-firefox150-jajp")
	client, err := NewClient("wrong-occurrence", contract, Options{})
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()

	var sentinelIndexes []int
	for index := range client.script {
		if client.script[index].request.sentinelOccurrence != nil {
			sentinelIndexes = append(sentinelIndexes, index)
		}
	}
	if len(sentinelIndexes) != 3 {
		t.Fatalf("sentinel occurrence count=%d", len(sentinelIndexes))
	}
	client.mu.Lock()
	client.script[sentinelIndexes[0]].request.sentinelOccurrence, client.script[sentinelIndexes[1]].request.sentinelOccurrence =
		client.script[sentinelIndexes[1]].request.sentinelOccurrence, client.script[sentinelIndexes[0]].request.sentinelOccurrence
	client.mu.Unlock()

	var got error
	for client.Stats().Remaining > 0 {
		exchange := exchangeAtSequence(t, contract, client.Stats().Consumed)
		resp, callErr := client.Do(context.Background(), contractRequest(t, exchange, contract.BrowserIdentity, nil))
		if callErr != nil {
			got = callErr
			break
		}
		_ = resp.Body.Close()
	}
	var mismatch *Mismatch
	if !errors.As(got, &mismatch) {
		t.Fatalf("got %T %v, want typed mismatch", got, got)
	}
	if mismatch.Code() != CodeWireContractDrift || mismatch.Field != "sentinel.occurrence" {
		t.Fatalf("mismatch=%+v", mismatch)
	}
	if stats := client.Stats(); stats.NetworkFallbacks != 0 || stats.ScriptedResponses != stats.Consumed {
		t.Fatalf("unexpected stats after mismatch: %+v", stats)
	}
}

func TestProtectedRequestMutationsFailClosed(t *testing.T) {
	contract := loadRegistrationContract(t, "d24-firefox150-jajp")
	tests := []struct {
		name       string
		targetKind string
		field      string
		mutate     func(*http.Request)
	}{
		{
			name:       "header",
			targetKind: "auth_providers",
			field:      "request.headers.user-agent",
			mutate: func(req *http.Request) {
				req.Header.Set("User-Agent", "Mozilla/5.0 Firefox/149.0")
			},
		},
		{
			name:       "query",
			targetKind: "signin",
			field:      "request.query.keys",
			mutate: func(req *http.Request) {
				query := req.URL.Query()
				for key := range query {
					query.Del(key)
					break
				}
				req.URL.RawQuery = query.Encode()
			},
		},
		{
			name:       "body",
			targetKind: "otp_validate",
			field:      "request.body.keys",
			mutate: func(req *http.Request) {
				req.Body = io.NopCloser(strings.NewReader(`{}`))
				req.ContentLength = 2
			},
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			client, err := NewClient("mutation-"+test.name, contract, Options{})
			if err != nil {
				t.Fatal(err)
			}
			defer client.Close()
			var got error
			for client.Stats().Remaining > 0 {
				exchange := exchangeAtSequence(t, contract, client.Stats().Consumed)
				req := contractRequest(t, exchange, contract.BrowserIdentity, nil)
				if exchange.Request.Kind == test.targetKind {
					test.mutate(req)
				}
				resp, callErr := client.Do(context.Background(), req)
				if callErr != nil {
					got = callErr
					break
				}
				_ = resp.Body.Close()
			}
			var mismatch *Mismatch
			if !errors.As(got, &mismatch) {
				t.Fatalf("got %T %v, want typed mismatch", got, got)
			}
			if mismatch.Field != test.field || mismatch.Code() != CodeWireContractDrift {
				t.Fatalf("mismatch=%+v want field=%s", mismatch, test.field)
			}
			if client.Stats().NetworkFallbacks != 0 {
				t.Fatal("mutation reached a network fallback")
			}
		})
	}
}

func TestRedirectLimitFailsClosedAtTenHops(t *testing.T) {
	jar, err := NewSymbolicJar()
	if err != nil {
		t.Fatal(err)
	}
	const redirects = 11
	script := make([]compiledExchange, 0, redirects+1)
	for index := 0; index <= redirects; index++ {
		header := make(http.Header)
		status := http.StatusOK
		if index < redirects {
			status = http.StatusFound
			header.Set("Location", fmt.Sprintf("https://redirect.test/%d", index+1))
		}
		script = append(script, compiledExchange{
			position:        Position{State: "S4", ExchangeIndex: index},
			captureSequence: index,
			request: compiledRequest{
				method: "GET", host: "redirect.test", path: fmt.Sprintf("/%d", index),
				query: compiledCollection{fields: map[string]compiledValueRule{}, forbidden: map[string]struct{}{}},
				body: compiledBody{kind: "empty"}, headers: map[string]compiledHeaderRule{}, allowUnspecifiedHeaders: true,
			},
			response: compiledResponse{statusCode: status, header: header},
		})
	}
	client, err := newClient("redirect-limit", "contract-test", jar, script, Options{MaxRedirectHops: 10})
	if err != nil {
		t.Fatal(err)
	}
	resp, err := client.Do(context.Background(), mustRequest(t, http.MethodGet, "https://redirect.test/0", nil))
	if resp != nil {
		_ = resp.Body.Close()
	}
	var clientErr *ClientError
	if !errors.As(err, &clientErr) || clientErr.Code() != CodeRedirectLimit {
		t.Fatalf("got %T %v, want redirect limit", err, err)
	}
	stats := client.Stats()
	if stats.Redirects != 10 || stats.NetworkFallbacks != 0 {
		t.Fatalf("stats=%+v", stats)
	}
	otherJar, err := NewSymbolicJar()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := newClient("redirect-over-limit", "contract-test", otherJar, script, Options{MaxRedirectHops: 11}); err == nil {
		t.Fatal("redirect limit above ten accepted")
	}
	if _, err := newClient("redirect-unknown-fault", "contract-test", otherJar, script, Options{Faults: []Fault{{CaptureSequence: 999}}}); err == nil {
		t.Fatal("fault for unknown capture sequence accepted")
	}
}

func TestMismatchErrorsNeverExposeProtectedValues(t *testing.T) {
	jar, err := NewSymbolicJar()
	if err != nil {
		t.Fatal(err)
	}
	script := []compiledExchange{{
		position: Position{State: "S10", ExchangeIndex: 0}, captureSequence: 0,
		request: compiledRequest{
			method: "POST", host: "safe.test", path: "/validate",
			query: compiledCollection{
				fields: map[string]compiledValueRule{"token": {kind: "nonempty", required: true}},
				forbidden: map[string]struct{}{},
			},
			body: compiledBody{kind: "raw", raw: compiledValueRule{kind: "any"}},
			headers: map[string]compiledHeaderRule{}, allowUnspecifiedHeaders: true,
		},
		response: compiledResponse{statusCode: http.StatusOK, header: make(http.Header)},
	}}
	client, err := newClient("redaction", "contract-test", jar, script, Options{})
	if err != nil {
		t.Fatal(err)
	}
	req := mustRequest(t, http.MethodPost, "https://safe.test/validate?token=TOP_QUERY_SECRET&extra=1", strings.NewReader("TOP_BODY_SECRET"))
	req.Header.Set("X-Protected", "TOP_HEADER_SECRET")
	_, err = client.Do(context.Background(), req)
	var mismatch *Mismatch
	if !errors.As(err, &mismatch) {
		t.Fatalf("got %T %v, want mismatch", err, err)
	}
	message := err.Error()
	for _, secret := range []string{"TOP_QUERY_SECRET", "TOP_BODY_SECRET", "TOP_HEADER_SECRET"} {
		if strings.Contains(message, secret) {
			t.Fatalf("mismatch leaked protected value %q: %s", secret, message)
		}
	}
}

func TestAmbiguousPostsAreConsumedOnceAndNeverAutoReplayed(t *testing.T) {
	for _, state := range []protocol.StateID{protocol.S7, protocol.S10, protocol.S11} {
		t.Run(string(state), func(t *testing.T) {
			client := ambiguousStateClient(t, state)
			bundle := ambiguityBundle(t)
			engine := &protocol.Engine{
				Mode:     protocol.ModeLive,
				Bundle:   bundle,
				Client:   client,
				Email:    "replay@example.invalid",
				Password: "replay-password",
			}
			cursor := protocol.Cursor{State: state, DeviceID: "replay-device", OTPCode: "123456"}
			_, result, err := engine.Step(context.Background(), cursor)
			if err == nil {
				t.Fatal("scripted after-send failure was accepted")
			}
			if !result.Ambiguous || result.FailureCode != "ambiguous_after_send" {
				t.Fatalf("result=%+v err=%v", result, err)
			}
			wantCalls := 1
			if state == protocol.S7 || state == protocol.S11 {
				wantCalls = 2
			}
			stats := client.Stats()
			if stats.Requests != wantCalls || stats.Consumed != wantCalls || stats.ScriptedResponses != wantCalls-1 {
				t.Fatalf("state=%s auto-replay or wrong consumption: %+v", state, stats)
			}
			if stats.NetworkFallbacks != 0 {
				t.Fatal("ambiguous request reached network fallback")
			}
		})
	}
}

func ambiguousStateClient(t *testing.T, state protocol.StateID) *Client {
	t.Helper()
	jar, err := NewSymbolicJar()
	if err != nil {
		t.Fatal(err)
	}
	var script []compiledExchange
	if state == protocol.S7 || state == protocol.S11 {
		script = append(script, compiledExchange{
			position:        Position{State: string(protocol.T1), ExchangeIndex: 0},
			captureSequence: 0,
			request: compiledRequest{
				method: "POST", host: "sentinel.openai.com", path: "/backend-api/sentinel/req",
				query: compiledCollection{fields: map[string]compiledValueRule{}, forbidden: map[string]struct{}{}},
				body: compiledBody{kind: "raw", raw: compiledValueRule{kind: "any"}},
				headers: map[string]compiledHeaderRule{}, allowUnspecifiedHeaders: true,
			},
			response: compiledResponse{
				statusCode: http.StatusOK,
				header:     http.Header{"Content-Type": []string{"application/json"}},
				body:       []byte(`{"token":"replay","proofofwork":{"required":false},"turnstile":{"required":false},"so":{"required":false}}`),
			},
		})
	}
	sequence := len(script)
	method, host, path := http.MethodPost, "auth.openai.com", ""
	switch state {
	case protocol.S7:
		path = "/api/accounts/user/register"
	case protocol.S10:
		path = "/api/accounts/email-otp/validate"
	case protocol.S11:
		path = "/api/accounts/create_account"
	default:
		t.Fatalf("unsupported ambiguous state %s", state)
	}
	script = append(script, compiledExchange{
		position:        Position{State: string(state), ExchangeIndex: 0},
		captureSequence: sequence,
		request: compiledRequest{
			method: method, host: host, path: path,
			query: compiledCollection{fields: map[string]compiledValueRule{}, forbidden: map[string]struct{}{}},
			body: compiledBody{kind: "raw", raw: compiledValueRule{kind: "any"}},
			headers: map[string]compiledHeaderRule{}, allowUnspecifiedHeaders: true,
		},
		response: compiledResponse{statusCode: http.StatusOK, header: make(http.Header)},
	})
	client, err := newClient("ambiguous-"+string(state), "contract-test", jar, script, Options{Faults: []Fault{{CaptureSequence: sequence, AfterSend: true}}})
	if err != nil {
		t.Fatal(err)
	}
	return client
}

func ambiguityBundle(t *testing.T) *fingerprint.Bundle {
	t.Helper()
	bundle, err := fingerprint.Generate(fingerprint.GenerateOptions{
		RNG:             mathrand.New(mathrand.NewPCG(101, 202)),
		ForceFamily:     fingerprint.FamilyDesktop,
		ForceBrowser:    fingerprint.BrowserFirefox,
		ExpectedCountry: "JP",
	})
	if err != nil {
		t.Fatal(err)
	}
	return bundle
}

func loadRegistrationContract(t *testing.T, capture string) *rechallenge.RegistrationContract {
	t.Helper()
	path := filepath.Join("..", "..", "testdata", "rechallenge", "registration", capture, "contract.json")
	contract, err := rechallenge.LoadContract(path)
	if err != nil {
		t.Fatal(err)
	}
	return contract
}

func cloneContract(t *testing.T, contract *rechallenge.RegistrationContract) *rechallenge.RegistrationContract {
	t.Helper()
	raw, err := json.Marshal(contract)
	if err != nil {
		t.Fatal(err)
	}
	var clone rechallenge.RegistrationContract
	if err := json.Unmarshal(raw, &clone); err != nil {
		t.Fatal(err)
	}
	return &clone
}

func exchangeAtSequence(t *testing.T, contract *rechallenge.RegistrationContract, sequence int) *rechallenge.StateExchangeContract {
	t.Helper()
	for index := range contract.Exchanges {
		if contract.Exchanges[index].CaptureSequence == sequence {
			return &contract.Exchanges[index]
		}
	}
	t.Fatalf("capture_sequence=%d not found", sequence)
	return nil
}

func contractRequest(t *testing.T, exchange *rechallenge.StateExchangeContract, identity rechallenge.BrowserIdentity, mutate func(*http.Request)) *http.Request {
	t.Helper()
	fields := append([]rechallenge.FieldRule(nil), exchange.Request.Query...)
	sort.SliceStable(fields, func(i, j int) bool { return fields[i].Order < fields[j].Order })
	var query strings.Builder
	for index, field := range fields {
		if index > 0 {
			query.WriteByte('&')
		}
		query.WriteString(url.QueryEscape(field.Name))
		query.WriteByte('=')
		query.WriteString(url.QueryEscape(syntheticFieldValue(field)))
	}
	target := "https://" + exchange.Request.Host + exchange.Request.Path
	if query.Len() > 0 {
		target += "?" + query.String()
	}
	body := contractBody(t, exchange)
	req := mustRequest(t, exchange.Request.Method, target, body)
	if exchange.Request.ContentType != "" {
		req.Header.Set("Content-Type", exchange.Request.ContentType)
	}
	for _, rule := range exchange.Request.Headers {
		if rule.Presence != rechallenge.PresenceRequired {
			continue
		}
		switch strings.ToLower(rule.Name) {
		case "user-agent":
			req.Header.Set(rule.Name, fmt.Sprintf("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:%d.0) Gecko/20100101 Firefox/%d.0", identity.UAMajor, identity.UAMajor))
		case "accept":
			req.Header.Set(rule.Name, "application/json")
		case "content-type":
			req.Header.Set(rule.Name, rule.Expected)
		case "origin", "referer":
			req.Header.Set(rule.Name, "https://"+exchange.Request.Host+"/")
		case "openai-sentinel-token":
			req.Header.Set(rule.Name, `{"p":"replay","t":"","c":"replay","id":"replay","flow":"oauth_create_account"}`)
		case "openai-sentinel-so-token":
			req.Header.Set(rule.Name, `{"so":"replay","c":"replay","id":"replay","flow":"oauth_create_account"}`)
		default:
			req.Header.Set(rule.Name, "replay")
		}
	}
	if mutate != nil {
		mutate(req)
	}
	return req
}

func contractBody(t *testing.T, exchange *rechallenge.StateExchangeContract) io.Reader {
	t.Helper()
	body := exchange.Request.Body
	switch body.Kind {
	case "none", "":
		return nil
	case "json":
		object := make(map[string]any, len(body.Fields))
		for _, field := range body.Fields {
			object[field.Name] = testFieldValue(field, exchange.FlowName)
		}
		raw, err := json.Marshal(object)
		if err != nil {
			t.Fatal(err)
		}
		return strings.NewReader(string(raw))
	case "form":
		var encoded strings.Builder
		for index, field := range body.Fields {
			if index > 0 {
				encoded.WriteByte('&')
			}
			encoded.WriteString(url.QueryEscape(field.Name))
			encoded.WriteByte('=')
			encoded.WriteString(url.QueryEscape(fmt.Sprint(testFieldValue(field, exchange.FlowName))))
		}
		return strings.NewReader(encoded.String())
	case "opaque":
		return strings.NewReader("replay")
	default:
		t.Fatalf("unsupported body kind %q", body.Kind)
		return nil
	}
}

func testFieldValue(field rechallenge.FieldRule, flow string) any {
	if field.ValuePolicy == "protected_flow" {
		return flow
	}
	if field.ValuePolicy == "date_shape" {
		return "1990-01-02"
	}
	switch field.ValueType {
	case "object":
		object := make(map[string]any, len(field.ObjectKeys))
		for _, key := range field.ObjectKeys {
			object[key] = "replay"
		}
		return object
	case "array":
		return []any{"replay"}
	case "boolean":
		return true
	case "number":
		return 1
	case "null":
		return nil
	default:
		return "replay"
	}
}

func assertRedirectCookiePropagation(t *testing.T, client *Client, contract *rechallenge.RegistrationContract, exchange *rechallenge.StateExchangeContract, proved map[string]bool) {
	t.Helper()
	if exchange.Redirect == nil {
		return
	}
	nextSequence := exchange.CaptureSequence + 1
	next := exchangeAtSequence(t, contract, nextSequence)
	nextURL, err := url.Parse("https://" + next.Request.Host + next.Request.Path)
	if err != nil {
		t.Fatal(err)
	}
	for _, event := range exchange.CookieEvents {
		if event.Direction != "set" {
			continue
		}
		for _, cookie := range client.Jar().Cookies(nextURL) {
			if cookie.Name == event.Name && client.Jar().Matches(event.ValueSlot, cookie.Value) {
				proved[string(exchange.State)] = true
				return
			}
		}
	}
}

func mustRequest(t *testing.T, method, target string, body io.Reader) *http.Request {
	t.Helper()
	req, err := http.NewRequest(method, target, body)
	if err != nil {
		t.Fatal(err)
	}
	return req
}
