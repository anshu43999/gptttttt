package transport

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/cookiejar"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/andybalholm/brotli"
	"github.com/klauspost/compress/gzip"
	"github.com/klauspost/compress/zstd"
	"golang.org/x/net/proxy"
)

// DirectSOCKSFactory builds clients that dial via authenticated SOCKS5/SOCKS5h
// without requiring a Python loopback HTTP bridge.
type DirectSOCKSFactory struct{}

// NewWithOptions returns a Client for jobID using SOCKS proxy URL in ProxySnapshot.BridgeURL.
func (DirectSOCKSFactory) NewWithOptions(opts ClientOptions) (Client, error) {
	if strings.TrimSpace(opts.JobID) == "" {
		return nil, fmt.Errorf("transport: job id required")
	}
	raw := strings.TrimSpace(opts.Proxy.BridgeURL)
	if raw == "" {
		return nil, fmt.Errorf("transport: socks proxy url required")
	}
	u, err := url.Parse(raw)
	if err != nil {
		return nil, fmt.Errorf("transport: bad socks url: %w", err)
	}
	scheme := strings.ToLower(u.Scheme)
	if scheme != "socks5" && scheme != "socks5h" {
		return nil, fmt.Errorf("transport: direct socks requires socks5/socks5h, got %s", scheme)
	}
	if u.Hostname() == "" || u.Port() == "" {
		return nil, fmt.Errorf("transport: socks host/port required")
	}

	var auth *proxy.Auth
	if u.User != nil {
		pass, _ := u.User.Password()
		auth = &proxy.Auth{User: u.User.Username(), Password: pass}
	}
	addr := net.JoinHostPort(u.Hostname(), u.Port())
	dialer, err := proxy.SOCKS5("tcp", addr, auth, proxy.Direct)
	if err != nil {
		return nil, fmt.Errorf("transport: socks dialer: %w", err)
	}
	contextDialer, ok := dialer.(proxy.ContextDialer)
	if !ok {
		contextDialer = contextDialerShim{d: dialer}
	}

	// Disable Transport auto-gzip so we control multi-codec decode (br/zstd/gzip).
	tr := &http.Transport{
		Proxy: nil,
		DialContext: func(ctx context.Context, network, address string) (net.Conn, error) {
			return contextDialer.DialContext(ctx, network, address)
		},
		ForceAttemptHTTP2:     true,
		MaxIdleConns:          32,
		IdleConnTimeout:       90 * time.Second,
		TLSHandshakeTimeout:   20 * time.Second,
		ExpectContinueTimeout: 1 * time.Second,
		DisableCompression:    true,
	}
	jar, err := cookiejar.New(nil)
	if err != nil {
		return nil, err
	}
	hc := &http.Client{
		Transport: tr,
		Jar:       jar,
		Timeout:   60 * time.Second,
		CheckRedirect: func(req *http.Request, via []*http.Request) error {
			if len(via) >= 10 {
				return fmt.Errorf("stopped after 10 redirects")
			}
			return nil
		},
	}
	return &directSOCKSClient{jobID: opts.JobID, proxy: opts.Proxy, hc: hc}, nil
}

// New implements Factory using BridgeURL as socks URL.
func (f DirectSOCKSFactory) New(jobID string, proxySnap ProxySnapshot) (Client, error) {
	return f.NewWithOptions(ClientOptions{JobID: jobID, Proxy: proxySnap})
}

type directSOCKSClient struct {
	mu     sync.Mutex
	jobID  string
	proxy  ProxySnapshot
	hc     *http.Client
	closed bool
}

func (c *directSOCKSClient) JobID() string        { return c.jobID }
func (c *directSOCKSClient) Proxy() ProxySnapshot { return c.proxy }

func (c *directSOCKSClient) Do(ctx context.Context, req *http.Request) (*http.Response, error) {
	c.mu.Lock()
	closed := c.closed
	c.mu.Unlock()
	if closed {
		return nil, fmt.Errorf("transport: client closed")
	}
	req = req.WithContext(ctx)
	resp, err := c.hc.Do(req)
	if err != nil {
		return nil, err
	}
	if err := decodeContentEncoding(resp); err != nil {
		resp.Body.Close()
		return nil, err
	}
	return resp, nil
}

func (c *directSOCKSClient) Close() error {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.closed = true
	if c.hc != nil {
		if tr, ok := c.hc.Transport.(*http.Transport); ok {
			tr.CloseIdleConnections()
		}
	}
	return nil
}

func (c *directSOCKSClient) ExportCookies() ([]byte, error) {
	c.mu.Lock()
	hc := c.hc
	c.mu.Unlock()
	if hc == nil || hc.Jar == nil {
		return ExportHTTPJar(nil)
	}
	return ExportHTTPJar(hc.Jar)
}

func (c *directSOCKSClient) ImportCookies(raw []byte) error {
	c.mu.Lock()
	hc := c.hc
	c.mu.Unlock()
	if hc == nil || hc.Jar == nil {
		return fmt.Errorf("transport: no jar")
	}
	return ImportHTTPJar(hc.Jar, raw)
}

// decodeContentEncoding expands br/gzip/zstd/deflate bodies and strips encoding headers.
func decodeContentEncoding(resp *http.Response) error {
	if resp == nil || resp.Body == nil {
		return nil
	}
	ce := strings.ToLower(strings.TrimSpace(resp.Header.Get("Content-Encoding")))
	if ce == "" || ce == "identity" {
		return nil
	}
	// Support single codec (servers almost always send one).
	var (
		r   io.Reader
		err error
	)
	switch ce {
	case "gzip", "x-gzip":
		r, err = gzip.NewReader(resp.Body)
	case "br":
		r = brotli.NewReader(resp.Body)
	case "zstd":
		zr, zerr := zstd.NewReader(resp.Body)
		if zerr != nil {
			return zerr
		}
		// zstd reader needs Close; wrap
		data, rerr := io.ReadAll(zr)
		zr.Close()
		resp.Body.Close()
		if rerr != nil {
			return rerr
		}
		resp.Body = io.NopCloser(bytes.NewReader(data))
		resp.Header.Del("Content-Encoding")
		resp.Header.Del("Content-Length")
		resp.ContentLength = int64(len(data))
		resp.Uncompressed = true
		return nil
	default:
		// unknown: leave as-is
		return nil
	}
	if err != nil {
		return err
	}
	data, err := io.ReadAll(r)
	// close original body
	_ = resp.Body.Close()
	if err != nil {
		return err
	}
	// gzip.Reader may need Close
	if gc, ok := r.(io.Closer); ok {
		_ = gc.Close()
	}
	resp.Body = io.NopCloser(bytes.NewReader(data))
	resp.Header.Del("Content-Encoding")
	resp.Header.Del("Content-Length")
	resp.ContentLength = int64(len(data))
	resp.Uncompressed = true
	return nil
}

type contextDialerShim struct{ d proxy.Dialer }

func (s contextDialerShim) DialContext(ctx context.Context, network, address string) (net.Conn, error) {
	type res struct {
		c   net.Conn
		err error
	}
	ch := make(chan res, 1)
	go func() {
		c, err := s.d.Dial(network, address)
		ch <- res{c, err}
	}()
	select {
	case <-ctx.Done():
		return nil, ctx.Err()
	case r := <-ch:
		return r.c, r.err
	}
}
