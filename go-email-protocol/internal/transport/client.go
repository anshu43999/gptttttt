// Package transport holds TransportProfile stubs and per-job client interfaces.
// G1 uses interface + fake only; no real OpenAI / tls-client.
package transport

import (
	"context"
	"io"
	"net/http"
	"sync"
)

// ProxySnapshot is the immutable proxy/bridge grant for a job.
type ProxySnapshot struct {
	ProxyKey         string
	BridgeID         string
	BridgeURL        string
	BridgeGeneration int64
	// BridgeCapability is secret; keep in memory only, never log.
	BridgeCapability string
	ExitIP           string
	ExpectedCountry  string
	// Style is proxy_seed style (bestgo/1024/…) for remint rotation; optional.
	Style      string
	LeaseFence int64
}

// Client is the per-job protocol HTTP client handle.
type Client interface {
	// JobID returns owning job.
	JobID() string
	// Proxy returns the immutable proxy snapshot.
	Proxy() ProxySnapshot
	// Do performs a request (fake or real).
	Do(ctx context.Context, req *http.Request) (*http.Response, error)
	// Close releases idle connections.
	Close() error
}

// FakeClient is an isolated in-memory transport for G1 tests.
type FakeClient struct {
	mu      sync.Mutex
	jobID   string
	proxy   ProxySnapshot
	closed  bool
	calls   int
	lastURL string
	// OnDo optional hook.
	OnDo func(ctx context.Context, req *http.Request) (*http.Response, error)
}

// NewFake creates a job-local fake transport.
func NewFake(jobID string, proxy ProxySnapshot) *FakeClient {
	return &FakeClient{jobID: jobID, proxy: proxy}
}

// JobID implements Client.
func (f *FakeClient) JobID() string { return f.jobID }

// Proxy implements Client.
func (f *FakeClient) Proxy() ProxySnapshot {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.proxy
}

// Do implements Client.
func (f *FakeClient) Do(ctx context.Context, req *http.Request) (*http.Response, error) {
	f.mu.Lock()
	f.calls++
	if req != nil && req.URL != nil {
		f.lastURL = req.URL.String()
	}
	hook := f.OnDo
	closed := f.closed
	f.mu.Unlock()
	if closed {
		return nil, io.ErrClosedPipe
	}
	if hook != nil {
		return hook(ctx, req)
	}
	// Default: empty 200.
	return &http.Response{
		StatusCode: http.StatusOK,
		Header:     make(http.Header),
		Body:       http.NoBody,
		Request:    req,
	}, nil
}

// Close implements Client.
func (f *FakeClient) Close() error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.closed = true
	return nil
}

// Calls returns request count.
func (f *FakeClient) Calls() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.calls
}

// Closed reports whether Close was called.
func (f *FakeClient) Closed() bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.closed
}

// LastURL returns last request URL.
func (f *FakeClient) LastURL() string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.lastURL
}
