// Package plusverify implements concurrent ChatGPT Plus/subscription checks.
//
// Endpoint target matches Python platforms/chatgpt/payment.py:
//
//	GET https://chatgpt.com/backend-api/wham/usage
//	Authorization: Bearer <access_token>
//	User-Agent: codex_cli_rs/0.76.0 (Debian 13.0.0; x86_64) WindowsTerminal
//	Chatgpt-Account-Id: <optional>
package plusverify

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"
)

const (
	whamUsageURL = "https://chatgpt.com/backend-api/wham/usage"
	userAgent    = "codex_cli_rs/0.76.0 (Debian 13.0.0; x86_64) WindowsTerminal"
	// DefaultWorkers is the multi-worker concurrency for bulk Plus checks.
	DefaultWorkers = 32
	MaxWorkers     = 100
)

// Item is one account to verify.
type Item struct {
	Key       string `json:"key"`
	AccountID string `json:"account_id,omitempty"`
	// AccessToken is required. Prefer live access_token over chatgpt_access_token_initial.
	AccessToken string `json:"access_token"`
	// ProxyURL is optional http/https/socks5 proxy. Prefer loopback bridge when available.
	ProxyURL string `json:"proxy,omitempty"`
	// Proxies are alternate bridges/proxies used on network failure (换代理/换 sid 重试).
	// Tried in order after ProxyURL. Duplicates and empty entries are skipped.
	Proxies []string `json:"proxies,omitempty"`
}

// Result is one account's verification outcome.
type Result struct {
	Key       string `json:"key"`
	OK        bool   `json:"ok"`
	Paid      bool   `json:"paid"`
	PlanType  string `json:"plan_type,omitempty"`
	Source    string `json:"source,omitempty"`
	Message   string `json:"message,omitempty"`
	ErrorCode string `json:"error_code,omitempty"`
	Status    int    `json:"status_code,omitempty"`
}

// BatchRequest is the bulk Plus verify payload.
type BatchRequest struct {
	Items   []Item `json:"items"`
	Workers int    `json:"workers,omitempty"`
	// TimeoutMS per-item HTTP timeout (default 15000).
	TimeoutMS int `json:"timeout_ms,omitempty"`
}

// BatchResponse aggregates results in input order.
type BatchResponse struct {
	OK       bool     `json:"ok"`
	Checked  int      `json:"checked"`
	Paid     int      `json:"paid"`
	Failed   int      `json:"failed"`
	Workers  int      `json:"workers"`
	Results  []Result `json:"results"`
	Duration string   `json:"duration"`
}

// Service runs concurrent Plus checks.
type Service struct {
	// optional shared base client (no proxy). Per-proxy clients are built on demand.
	base *http.Client
}

// New constructs a Service.
func New() *Service {
	return &Service{
		base: &http.Client{Timeout: 20 * time.Second},
	}
}

// VerifyBatch runs multi-worker Plus checks. Results preserve input order.
func (s *Service) VerifyBatch(ctx context.Context, req BatchRequest) BatchResponse {
	start := time.Now()
	workers := req.Workers
	if workers <= 0 {
		workers = DefaultWorkers
	}
	if workers > MaxWorkers {
		workers = MaxWorkers
	}
	if workers > len(req.Items) && len(req.Items) > 0 {
		workers = len(req.Items)
	}
	timeout := 15 * time.Second
	if req.TimeoutMS > 0 {
		timeout = time.Duration(req.TimeoutMS) * time.Millisecond
	}

	results := make([]Result, len(req.Items))
	if len(req.Items) == 0 {
		return BatchResponse{OK: true, Workers: workers, Results: results, Duration: time.Since(start).String()}
	}

	type job struct {
		idx  int
		item Item
	}
	jobs := make(chan job, len(req.Items))
	for i, item := range req.Items {
		jobs <- job{idx: i, item: item}
	}
	close(jobs)

	var wg sync.WaitGroup
	for range workers {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := range jobs {
				if ctx.Err() != nil {
					results[j.idx] = Result{
						Key: j.item.Key, OK: false, Message: "cancelled", ErrorCode: "cancelled", Status: 499,
					}
					continue
				}
				results[j.idx] = s.verifyOne(ctx, j.item, timeout)
			}
		}()
	}
	wg.Wait()

	paid, failed := 0, 0
	allOK := true
	for _, r := range results {
		if r.Paid {
			paid++
		}
		if !r.OK {
			failed++
			allOK = false
		}
	}
	return BatchResponse{
		OK:       allOK,
		Checked:  len(results),
		Paid:     paid,
		Failed:   failed,
		Workers:  workers,
		Results:  results,
		Duration: time.Since(start).String(),
	}
}

func (s *Service) verifyOne(ctx context.Context, item Item, timeout time.Duration) Result {
	key := strings.TrimSpace(item.Key)
	token := strings.TrimSpace(item.AccessToken)
	if key == "" {
		return Result{Key: key, OK: false, Message: "empty key", ErrorCode: "invalid_item", Status: 400}
	}
	if token == "" {
		return Result{Key: key, OK: false, Message: "缺少 access_token，无法自动校验 Plus", ErrorCode: "missing_access_token", Status: 400}
	}

	proxies := uniqueProxyList(item.ProxyURL, item.Proxies)
	if len(proxies) == 0 {
		proxies = []string{""} // direct
	}

	// Network failures: retest with next proxy / rotated sid bridge.
	// Up to 3 attempts total across the proxy list (initial + 2 retries).
	const maxAttempts = 3
	var last Result
	for attempt := 1; attempt <= maxAttempts; attempt++ {
		if ctx.Err() != nil {
			return Result{Key: key, OK: false, Message: "cancelled", ErrorCode: "cancelled", Status: 499}
		}
		proxy := proxies[(attempt-1)%len(proxies)]
		tryItem := item
		tryItem.ProxyURL = proxy
		last = s.verifyOneAttempt(ctx, tryItem, timeout)
		if last.OK {
			if attempt > 1 {
				last.Message = strings.TrimSpace(last.Message + fmt.Sprintf(" (ok after %d network retries)", attempt-1))
			}
			return last
		}
		if !isTransientNetworkFailure(last) || attempt == maxAttempts {
			if attempt > 1 && isTransientNetworkFailure(last) {
				last.Message = fmt.Sprintf("%s (retried %d times, rotated proxy/sid)", last.Message, attempt-1)
			}
			return last
		}
		// Brief backoff before retest with next proxy: 300ms, 600ms.
		select {
		case <-ctx.Done():
			return Result{Key: key, OK: false, Message: "cancelled", ErrorCode: "cancelled", Status: 499}
		case <-time.After(time.Duration(attempt) * 300 * time.Millisecond):
		}
	}
	return last
}

func uniqueProxyList(primary string, alts []string) []string {
	seen := map[string]struct{}{}
	out := make([]string, 0, 1+len(alts))
	add := func(v string) {
		v = strings.TrimSpace(v)
		if _, ok := seen[v]; ok {
			return
		}
		seen[v] = struct{}{}
		out = append(out, v)
	}
	add(primary)
	for _, v := range alts {
		add(v)
	}
	return out
}

func isTransientNetworkFailure(r Result) bool {
	if r.OK {
		return false
	}
	switch r.ErrorCode {
	case "proxy_failed", "http_error", "bad_response":
		// fall through to message heuristics for http_error
	case "auth_failed", "missing_access_token", "invalid_item", "proxy_invalid", "request_build_failed", "cancelled":
		return false
	default:
		// unknown codes: only retry if message looks networky
	}
	if r.Status == 429 || r.Status == 502 || r.Status == 503 || r.Status == 504 {
		return true
	}
	msg := strings.ToLower(r.Message)
	needles := []string{
		"timeout",
		"timed out",
		"connection refused",
		"connection reset",
		"connection timeout",
		"i/o timeout",
		"tls handshake timeout",
		"upstream connect",
		"disconnect",
		"reset before headers",
		"eof",
		"broken pipe",
		"no such host",
		"temporary failure",
		"network is unreachable",
		"proxy_failed",
	}
	for _, n := range needles {
		if strings.Contains(msg, n) {
			return true
		}
	}
	return r.ErrorCode == "proxy_failed" || r.ErrorCode == "bad_response"
}

func (s *Service) verifyOneAttempt(ctx context.Context, item Item, timeout time.Duration) Result {
	key := strings.TrimSpace(item.Key)
	token := strings.TrimSpace(item.AccessToken)

	client, err := s.clientForProxy(item.ProxyURL, timeout)
	if err != nil {
		return Result{Key: key, OK: false, Message: err.Error(), ErrorCode: "proxy_invalid", Status: 400}
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, whamUsageURL, nil)
	if err != nil {
		return Result{Key: key, OK: false, Message: err.Error(), ErrorCode: "request_build_failed", Status: 500}
	}
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("User-Agent", userAgent)
	req.Header.Set("Accept", "application/json")
	if aid := strings.TrimSpace(item.AccountID); aid != "" {
		req.Header.Set("Chatgpt-Account-Id", aid)
	}

	resp, err := client.Do(req)
	if err != nil {
		return Result{Key: key, OK: false, Message: err.Error(), ErrorCode: "proxy_failed", Status: 502}
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))

	if resp.StatusCode == 401 || resp.StatusCode == 403 {
		return Result{
			Key: key, OK: false, Status: resp.StatusCode,
			Message: "Plus 校验失败: access_token 无效或无权限", ErrorCode: "auth_failed",
		}
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		msg := strings.TrimSpace(string(body))
		if len(msg) > 240 {
			msg = msg[:240]
		}
		if msg == "" {
			msg = fmt.Sprintf("HTTP %d", resp.StatusCode)
		}
		return Result{Key: key, OK: false, Status: resp.StatusCode, Message: msg, ErrorCode: "http_error"}
	}

	var data map[string]any
	if err := json.Unmarshal(body, &data); err != nil {
		return Result{Key: key, OK: false, Message: "wham/usage 响应非 JSON", ErrorCode: "bad_response", Status: 502}
	}
	plan := normalizePlan(fmt.Sprint(data["plan_type"]))
	paid := plan != "free"
	return Result{
		Key:      key,
		OK:       true,
		Paid:     paid,
		PlanType: plan,
		Source:   "backend-api/wham/usage",
		Status:   200,
	}
}

func normalizePlan(plan string) string {
	raw := strings.ToLower(strings.TrimSpace(plan))
	if raw == "" {
		return "free"
	}
	switch raw {
	case "plus", "pro", "premium", "paid", "team", "business", "enterprise":
		return raw
	case "free", "free_plan", "none":
		return "free"
	default:
		// keep unknown labels as-is so UI can still show them
		return raw
	}
}

func (s *Service) clientForProxy(proxyURL string, timeout time.Duration) (*http.Client, error) {
	proxyURL = strings.TrimSpace(proxyURL)
	if proxyURL == "" {
		return &http.Client{Timeout: timeout}, nil
	}
	// Normalize socks5 -> socks5h for remote DNS when possible is not required by net/http;
	// Go supports socks5 via proxy URL scheme only with golang.org/x/net/proxy.
	// For maximum compatibility without extra deps, require http/https CONNECT proxies
	// (local bridge) — matches Go email-protocol bridge model.
	u, err := url.Parse(proxyURL)
	if err != nil {
		return nil, fmt.Errorf("invalid proxy: %w", err)
	}
	scheme := strings.ToLower(u.Scheme)
	switch scheme {
	case "http", "https":
		// ok
	case "socks5", "socks5h":
		// stdlib transport does not natively dial socks5 without x/net/proxy.
		// Accept the URL and try via HTTP proxy env fallback only if host is loopback bridge
		// that actually speaks HTTP CONNECT. Otherwise return a clear error.
		if host := u.Hostname(); host != "127.0.0.1" && host != "localhost" && host != "::1" {
			return nil, fmt.Errorf("socks5 proxy 需要本地 HTTP CONNECT bridge；当前=%s", proxyURL)
		}
		// rewrite to http for local bridge ports that may have been labeled socks5
		u.Scheme = "http"
	default:
		return nil, fmt.Errorf("unsupported proxy scheme %q (use http://127.0.0.1:<bridge>)", scheme)
	}
	transport := &http.Transport{
		Proxy: http.ProxyURL(u),
		DialContext: (&net.Dialer{
			Timeout:   10 * time.Second,
			KeepAlive: 30 * time.Second,
		}).DialContext,
		MaxIdleConns:          100,
		MaxIdleConnsPerHost:   16,
		IdleConnTimeout:       90 * time.Second,
		TLSHandshakeTimeout:   10 * time.Second,
		ExpectContinueTimeout: 1 * time.Second,
		ForceAttemptHTTP2:     true,
	}
	return &http.Client{Timeout: timeout, Transport: transport}, nil
}
