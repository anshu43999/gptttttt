package transport

import (
	"fmt"
	"net/url"
	"strings"
)

// ClientOptions is the G2 constructor input (fingerprint + profile + bridge).
// G1 FakeFactory ignores Profile/BundleJSON.
type ClientOptions struct {
	JobID       string
	Proxy       ProxySnapshot
	TransportID string
	// BundleJSON is the frozen FingerprintBundle v2 bytes (optional for fake).
	BundleJSON []byte
	// Profile is the locked TransportProfile (optional for fake).
	Profile *ProfileV1
}

// Factory builds per-job clients.
type Factory interface {
	New(jobID string, proxy ProxySnapshot) (Client, error)
}

// OptionsFactory can consume full ClientOptions (G2).
type OptionsFactory interface {
	Factory
	NewWithOptions(opts ClientOptions) (Client, error)
}

// FakeFactory always returns FakeClient.
type FakeFactory struct{}

// New implements Factory.
func (FakeFactory) New(jobID string, proxy ProxySnapshot) (Client, error) {
	return NewFake(jobID, proxy), nil
}

// NewWithOptions implements OptionsFactory.
func (f FakeFactory) NewWithOptions(opts ClientOptions) (Client, error) {
	if opts.JobID == "" {
		return nil, fmt.Errorf("transport: job id required")
	}
	if err := ValidateBridgeProxy(opts.Proxy); err != nil {
		return nil, err
	}
	return NewFake(opts.JobID, opts.Proxy), nil
}

// ValidateBridgeProxy enforces bridge-only egress (fail closed).
// Accepts:
//   - http(s)://127.0.0.1:<port> loopback CONNECT bridge (mailat / tls-client path)
//   - socks5://user:pass@host:port with capability "direct" (pure-Go DirectSOCKS)
func ValidateBridgeProxy(p ProxySnapshot) error {
	if strings.TrimSpace(p.BridgeURL) == "" {
		return fmt.Errorf("transport: bridge_required")
	}
	u, err := url.Parse(p.BridgeURL)
	if err != nil {
		return fmt.Errorf("transport: bad bridge url: %w", err)
	}
	scheme := strings.ToLower(u.Scheme)
	switch scheme {
	case "http", "https":
		host := strings.ToLower(u.Hostname())
		if host != "127.0.0.1" && host != "localhost" && host != "::1" {
			return fmt.Errorf("transport: bridge url must be loopback, got %s", host)
		}
	case "socks5", "socks5h":
		if u.Hostname() == "" || u.Port() == "" {
			return fmt.Errorf("transport: socks bridge needs host:port")
		}
		// capability still required (use "direct" for pure-Go)
	default:
		return fmt.Errorf("transport: bridge scheme must be http(s) or socks5")
	}
	if strings.TrimSpace(p.BridgeCapability) == "" {
		return fmt.Errorf("transport: bridge capability required")
	}
	return nil
}
