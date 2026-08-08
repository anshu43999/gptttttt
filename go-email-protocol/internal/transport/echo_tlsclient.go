//go:build tlsclient

package transport

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	stdhttp "net/http"
	"strings"

	httpclient "github.com/bogdanfinn/tls-client"
)

// NewTLSEchoClient builds a no-proxy tls-client for fingerprint echo probes (D5).
// Bridge validation is intentionally skipped: this is diagnostics, not job egress.
func NewTLSEchoClient(uaMajor int) (Client, string, error) {
	prof, name, err := chromeProfileForMajor(uaMajor)
	if err != nil {
		return nil, "", err
	}
	jar := httpclient.NewCookieJar()
	options := []httpclient.HttpClientOption{
		httpclient.WithTimeoutSeconds(60),
		httpclient.WithClientProfile(prof),
		httpclient.WithCookieJar(jar),
	}
	hc, err := httpclient.NewHttpClient(httpclient.NewNoopLogger(), options...)
	if err != nil {
		return nil, "", fmt.Errorf("transport: tls echo client: %w", err)
	}
	return &tlsClient{
		jobID:       "tls-echo",
		proxy:       ProxySnapshot{BridgeURL: "http://127.0.0.1:0", BridgeCapability: "echo"},
		inner:       hc,
		profileName: name,
		uaMajor:     uaMajor,
	}, name, nil
}

// ProbeTLSEcho GETs echoURL (or DefaultTLSEchoURL) and extracts a stable snapshot.
func ProbeTLSEcho(ctx context.Context, uaMajor int, echoURL string) (EchoSnapshot, error) {
	if strings.TrimSpace(echoURL) == "" {
		echoURL = DefaultTLSEchoURL
	}
	cli, profName, err := NewTLSEchoClient(uaMajor)
	if err != nil {
		return EchoSnapshot{}, err
	}
	defer cli.Close()

	req, err := stdhttp.NewRequestWithContext(ctx, stdhttp.MethodGet, echoURL, nil)
	if err != nil {
		return EchoSnapshot{}, err
	}
	req.Header.Set("Accept", "application/json")
	req.Header.Set("User-Agent", fmt.Sprintf(
		"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/%d.0.0.0 Safari/537.36",
		clampChromeMajor(uaMajor),
	))

	resp, err := cli.Do(ctx, req)
	if err != nil {
		return EchoSnapshot{}, fmt.Errorf("transport: tls echo request: %w", err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return EchoSnapshot{}, err
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return EchoSnapshot{}, fmt.Errorf("transport: tls echo status %d body=%s", resp.StatusCode, truncate(string(body), 200))
	}

	var raw map[string]any
	if err := json.Unmarshal(body, &raw); err != nil {
		return EchoSnapshot{}, fmt.Errorf("transport: tls echo json: %w", err)
	}
	return extractEchoSnapshot(profName, raw, resp.Proto), nil
}

func clampChromeMajor(major int) int {
	if major <= 0 {
		return 133
	}
	if major > 133 {
		return 133
	}
	return major
}
