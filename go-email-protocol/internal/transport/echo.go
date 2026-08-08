package transport

// extractEchoSnapshot pulls stable D5 fields from peet/browserleaks-style JSON.
// Shared by offline tests and //go:build tlsclient ProbeTLSEcho.
func extractEchoSnapshot(profileName string, raw map[string]any, respProto string) EchoSnapshot {
	snap := EchoSnapshot{ProfileName: profileName, RawKeys: mapKeys(raw)}
	if v, ok := raw["http_version"].(string); ok {
		snap.HTTPVersion = v
	}
	if tlsObj, ok := raw["tls"].(map[string]any); ok {
		if h, ok := tlsObj["ja3_hash"].(string); ok {
			snap.JA3Hash = h
		}
		if j, ok := tlsObj["ja4"].(string); ok {
			snap.JA4 = j
		}
		if n, ok := tlsObj["tls_version_negotiated"].(string); ok {
			snap.NegotiatedTLS = n
		}
		if ciphers, ok := tlsObj["ciphers"].([]any); ok {
			snap.CipherCount = len(ciphers)
		}
	}
	if snap.JA3Hash == "" {
		if h, ok := raw["ja3_hash"].(string); ok {
			snap.JA3Hash = h
		}
	}
	if snap.JA4 == "" {
		if j, ok := raw["ja4"].(string); ok {
			snap.JA4 = j
		}
	}
	if snap.JA3Hash == "" {
		if h, ok := raw["ja3"].(string); ok && len(h) == 32 {
			snap.JA3Hash = h
		}
	}
	if snap.HTTPVersion == "" && respProto != "" {
		snap.HTTPVersion = respProto
	}
	return snap
}

func mapKeys(m map[string]any) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}
