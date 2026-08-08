//go:build !tlsclient

package transport

import "fmt"

// newTLSFactory is the default build: tls-client not linked.
// Enable with -tags tlsclient after `go get github.com/bogdanfinn/tls-client`.
func newTLSFactory() (Factory, error) {
	return nil, fmt.Errorf("transport: tls factory requires build tag tlsclient and module github.com/bogdanfinn/tls-client (Phase D); default remains fake")
}
