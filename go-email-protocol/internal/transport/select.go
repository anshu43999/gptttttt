package transport

import (
	"fmt"
	"strings"
)

// FactoryName selects the per-job HTTP implementation.
type FactoryName string

const (
	// FactoryFake is the G1 default (no live TLS).
	FactoryFake FactoryName = "fake"
	// FactoryTLS is bogdanfinn/tls-client (Phase D). Only used when explicitly selected.
	FactoryTLS FactoryName = "tls"
	// FactoryDirect dials SOCKS5 directly (pure-Go CLI/worker path; BridgeURL=socks5://...).
	FactoryDirect FactoryName = "direct"
)

// NewFactory returns a Factory. Default is always Fake until Phase E gate.
// name: "fake" | "tls" | ""
func NewFactory(name string) (Factory, error) {
	switch FactoryName(strings.ToLower(strings.TrimSpace(name))) {
	case "", FactoryFake:
		return FakeFactory{}, nil
	case FactoryTLS:
		f, err := newTLSFactory()
		if err != nil {
			return nil, err
		}
		return f, nil
	case FactoryDirect:
		return DirectSOCKSFactory{}, nil
	default:
		return nil, fmt.Errorf("transport: unknown factory %q (want fake|tls|direct)", name)
	}
}
