//go:build tlsclient

package transport

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	stdhttp "net/http"
	"net/url"
	"strings"
	"sync"

	fhttp "github.com/bogdanfinn/fhttp"
	httpclient "github.com/bogdanfinn/tls-client"
	"github.com/bogdanfinn/tls-client/profiles"
)

// TLSFactory builds per-job bogdanfinn/tls-client instances.
// Supports loopback HTTP CONNECT bridge and authenticated SOCKS5 (pure-Go path).
// Profile selection follows TransportID / Bundle browser (Firefox HAR gold).
type TLSFactory struct{}

func newTLSFactory() (Factory, error) {
	return TLSFactory{}, nil
}

// New implements Factory.
func (TLSFactory) New(jobID string, proxy ProxySnapshot) (Client, error) {
	return TLSFactory{}.NewWithOptions(ClientOptions{JobID: jobID, Proxy: proxy})
}

// NewWithOptions implements OptionsFactory.
func (TLSFactory) NewWithOptions(opts ClientOptions) (Client, error) {
	if strings.TrimSpace(opts.JobID) == "" {
		return nil, fmt.Errorf("transport: job id required")
	}
	if err := ValidateBridgeProxy(opts.Proxy); err != nil {
		return nil, err
	}

	browser := BrowserFromProfileID(opts.TransportID)
	major := 0
	if m, ok := MajorFromProfileID(opts.TransportID); ok {
		major = m
	}
	// BundleJSON may carry identity when TransportID is empty/legacy.
	if browser == "" && len(opts.BundleJSON) > 0 {
		raw := string(opts.BundleJSON)
		low := strings.ToLower(raw)
		if strings.Contains(low, `"browser":"firefox"`) || strings.Contains(low, `"browser": "firefox"`) {
			browser = "firefox"
		} else if strings.Contains(low, `"browser":"edge"`) || strings.Contains(low, `"browser": "edge"`) {
			browser = "edge"
		} else {
			browser = "chrome"
		}
	}
	if browser == "" {
		browser = "firefox" // worker-generated default
	}

	prof, profName, err := clientProfileFor(browser, major)
	if err != nil {
		return nil, err
	}

	proxyURL, connectHeaders, err := tlsProxyURLAndHeaders(opts.Proxy)
	if err != nil {
		return nil, err
	}

	jar := httpclient.NewCookieJar()
	options := []httpclient.HttpClientOption{
		// Shorter per-attempt timeout so doHTTP can retry EOF/timeout on S1/S5.
		httpclient.WithTimeoutSeconds(30),
		httpclient.WithClientProfile(prof),
		httpclient.WithCookieJar(jar),
		httpclient.WithProxyUrl(proxyURL),
	}
	if len(connectHeaders) > 0 {
		options = append(options, httpclient.WithConnectHeaders(connectHeaders))
	}

	hc, err := httpclient.NewHttpClient(httpclient.NewNoopLogger(), options...)
	if err != nil {
		return nil, fmt.Errorf("transport: tls-client: %w", err)
	}

	return &tlsClient{
		jobID:       opts.JobID,
		proxy:       opts.Proxy,
		inner:       hc,
		profileName: profName,
		uaMajor:     major,
		browser:     browser,
	}, nil
}

// tlsProxyURLAndHeaders maps our ProxySnapshot to tls-client proxy URL.
// - socks5://user:pass@host:port  → as-is (capability "direct")
// - http://127.0.0.1:port + Bearer capability → CONNECT bridge with Proxy-Authorization
func tlsProxyURLAndHeaders(p ProxySnapshot) (string, fhttp.Header, error) {
	u, err := url.Parse(strings.TrimSpace(p.BridgeURL))
	if err != nil {
		return "", nil, fmt.Errorf("transport: bad bridge url: %w", err)
	}
	scheme := strings.ToLower(u.Scheme)
	headers := fhttp.Header{}
	switch scheme {
	case "socks5", "socks5h":
		// bogdanfinn accepts socks5:// with embedded userinfo
		return u.String(), headers, nil
	case "http", "https":
		cap := strings.TrimSpace(p.BridgeCapability)
		if cap != "" && !strings.EqualFold(cap, "direct") {
			headers.Set("Proxy-Authorization", "Bearer "+cap)
		}
		return u.String(), headers, nil
	default:
		return "", nil, fmt.Errorf("transport: unsupported bridge scheme %q", scheme)
	}
}

type tlsClient struct {
	mu          sync.Mutex
	jobID       string
	proxy       ProxySnapshot
	inner       httpclient.HttpClient
	profileName string
	uaMajor     int
	browser     string
	closed      bool
}

func (c *tlsClient) JobID() string { return c.jobID }

func (c *tlsClient) Proxy() ProxySnapshot { return c.proxy }

func (c *tlsClient) Do(ctx context.Context, req *stdhttp.Request) (*stdhttp.Response, error) {
	c.mu.Lock()
	closed := c.closed
	inner := c.inner
	c.mu.Unlock()
	if closed || inner == nil {
		return nil, fmt.Errorf("transport: client closed")
	}
	if req == nil {
		return nil, fmt.Errorf("transport: nil request")
	}
	fReq, err := toFHTTPRequest(req)
	if err != nil {
		return nil, err
	}
	if ctx != nil {
		fReq = fReq.WithContext(ctx)
	}
	fResp, err := inner.Do(fReq)
	if err != nil {
		return nil, err
	}
	return toStdResponse(fResp)
}

func (c *tlsClient) Close() error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.closed {
		return nil
	}
	c.closed = true
	c.inner = nil
	return nil
}

// ProfileName returns the tls-client profile label (tests/diagnostics).
func (c *tlsClient) ProfileName() string { return c.profileName }

func (c *tlsClient) ExportCookies() ([]byte, error) {
	c.mu.Lock()
	inner := c.inner
	c.mu.Unlock()
	if inner == nil {
		return json.Marshal([]CookieDTO{})
	}
	// fhttp.CookieJar is not assignable to net/http.CookieJar — use GetCookies only.
	var out []CookieDTO
	seen := map[string]struct{}{}
	for _, rawU := range AuthCookieURLs {
		u, err := url.Parse(rawU)
		if err != nil {
			continue
		}
		for _, ck := range inner.GetCookies(u) {
			if ck == nil || ck.Name == "" {
				continue
			}
			key := ck.Domain + "|" + ck.Path + "|" + ck.Name
			if _, ok := seen[key]; ok {
				continue
			}
			seen[key] = struct{}{}
			dto := CookieDTO{Name: ck.Name, Value: ck.Value, Domain: ck.Domain, Path: ck.Path, Secure: ck.Secure, HTTPOnly: ck.HttpOnly}
			if dto.Path == "" {
				dto.Path = "/"
			}
			if dto.Domain == "" {
				dto.Domain = u.Hostname()
			}
			if !ck.Expires.IsZero() {
				dto.ExpiresUnix = ck.Expires.Unix()
			}
			out = append(out, dto)
		}
	}
	return json.Marshal(out)
}

func (c *tlsClient) ImportCookies(raw []byte) error {
	c.mu.Lock()
	inner := c.inner
	c.mu.Unlock()
	if inner == nil {
		return fmt.Errorf("transport: client closed")
	}
	if len(raw) == 0 {
		return nil
	}
	var list []CookieDTO
	if err := json.Unmarshal(raw, &list); err != nil {
		return err
	}
	byHost := map[string][]*fhttp.Cookie{}
	for _, d := range list {
		if d.Name == "" {
			continue
		}
		host := strings.TrimPrefix(strings.ToLower(d.Domain), ".")
		if host == "" {
			continue
		}
		ck := &fhttp.Cookie{Name: d.Name, Value: d.Value, Domain: d.Domain, Path: d.Path, Secure: d.Secure, HttpOnly: d.HTTPOnly}
		if ck.Path == "" {
			ck.Path = "/"
		}
		byHost[host] = append(byHost[host], ck)
	}
	for host, cs := range byHost {
		u, err := url.Parse("https://" + host + "/")
		if err != nil {
			continue
		}
		inner.SetCookies(u, cs)
	}
	return nil
}

func toFHTTPRequest(req *stdhttp.Request) (*fhttp.Request, error) {
	if req == nil {
		return nil, fmt.Errorf("transport: nil request")
	}
	var body io.Reader
	if req.Body != nil {
		body = req.Body
	}
	fReq, err := fhttp.NewRequest(req.Method, req.URL.String(), body)
	if err != nil {
		return nil, err
	}
	for k, vals := range req.Header {
		for _, v := range vals {
			fReq.Header.Add(k, v)
		}
	}
	return fReq, nil
}

func toStdResponse(resp *fhttp.Response) (*stdhttp.Response, error) {
	if resp == nil {
		return nil, fmt.Errorf("transport: nil response")
	}
	h := make(stdhttp.Header, len(resp.Header))
	for k, vals := range resp.Header {
		for _, v := range vals {
			h.Add(k, v)
		}
	}
	return &stdhttp.Response{
		Status:     resp.Status,
		StatusCode: resp.StatusCode,
		Proto:      resp.Proto,
		ProtoMajor: resp.ProtoMajor,
		ProtoMinor: resp.ProtoMinor,
		Header:     h,
		Body:       resp.Body,
		Request:    nil,
	}, nil
}

// clientProfileFor picks Firefox/Chrome tls-client profile for UA major.
// Firefox max shipped in v1.9.1 is Firefox_135; higher majors pin to 135.
func clientProfileFor(browser string, major int) (profiles.ClientProfile, string, error) {
	switch strings.ToLower(browser) {
	case "firefox", "ff", "gecko":
		return firefoxProfileForMajor(major)
	default:
		return chromeProfileForMajor(major)
	}
}

func firefoxProfileForMajor(major int) (profiles.ClientProfile, string, error) {
	// Pin newest available when major is modern (HAR gold 148–150).
	if major <= 0 || major >= 135 {
		return profiles.Firefox_135, "Firefox_135", nil
	}
	switch {
	case major >= 133:
		return profiles.Firefox_133, "Firefox_133", nil
	case major >= 132:
		return profiles.Firefox_132, "Firefox_132", nil
	case major >= 123:
		return profiles.Firefox_123, "Firefox_123", nil
	case major >= 120:
		return profiles.Firefox_120, "Firefox_120", nil
	case major >= 117:
		return profiles.Firefox_117, "Firefox_117", nil
	case major >= 110:
		return profiles.Firefox_110, "Firefox_110", nil
	case major >= 108:
		return profiles.Firefox_108, "Firefox_108", nil
	case major >= 106:
		return profiles.Firefox_106, "Firefox_106", nil
	case major >= 105:
		return profiles.Firefox_105, "Firefox_105", nil
	case major >= 104:
		return profiles.Firefox_104, "Firefox_104", nil
	default:
		return profiles.Firefox_102, "Firefox_102", nil
	}
}

// chromeProfileForMajor maps Bundle UA major to a locked tls-client profile.
// v1.9.1 ships up to Chrome_133; higher majors pin to 133 until fixture upgrade.
func chromeProfileForMajor(major int) (profiles.ClientProfile, string, error) {
	name, err := ChromeProfileNameForMajor(major)
	if err != nil {
		return profiles.ClientProfile{}, "", err
	}
	switch name {
	case "Chrome_133":
		return profiles.Chrome_133, name, nil
	case "Chrome_131":
		return profiles.Chrome_131, name, nil
	case "Chrome_124":
		return profiles.Chrome_124, name, nil
	case "Chrome_120":
		return profiles.Chrome_120, name, nil
	case "Chrome_117":
		return profiles.Chrome_117, name, nil
	case "Chrome_112":
		return profiles.Chrome_112, name, nil
	case "Chrome_111":
		return profiles.Chrome_111, name, nil
	case "Chrome_110":
		return profiles.Chrome_110, name, nil
	case "Chrome_109":
		return profiles.Chrome_109, name, nil
	case "Chrome_108":
		return profiles.Chrome_108, name, nil
	case "Chrome_107":
		return profiles.Chrome_107, name, nil
	case "Chrome_106":
		return profiles.Chrome_106, name, nil
	case "Chrome_105":
		return profiles.Chrome_105, name, nil
	case "Chrome_104":
		return profiles.Chrome_104, name, nil
	case "Chrome_103":
		return profiles.Chrome_103, name, nil
	default:
		return profiles.Chrome_133, "Chrome_133", nil
	}
}
