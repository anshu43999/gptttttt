package transport

// EchoSnapshot is the stable subset we assert across D5 runs (not full JA3 equality).
type EchoSnapshot struct {
	ProfileName   string   `json:"profile_name"`
	HTTPVersion   string   `json:"http_version"`
	JA3Hash       string   `json:"ja3_hash,omitempty"`
	JA4           string   `json:"ja4,omitempty"`
	NegotiatedTLS string   `json:"tls_version_negotiated,omitempty"`
	CipherCount   int      `json:"cipher_count,omitempty"`
	RawKeys       []string `json:"raw_keys,omitempty"`
}

// DefaultTLSEchoURL is a public ClientHello/H2 observer used for D5 drift checks.
// Override with GPT_REGISTER_TLS_ECHO_URL when the public endpoint is down.
const DefaultTLSEchoURL = "https://tls.peet.ws/api/all"
