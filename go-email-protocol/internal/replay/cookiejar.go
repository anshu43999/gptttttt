package replay

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net/http"
	"net/http/cookiejar"
	"net/url"
	"sort"
	"strings"
	"sync"
)

// SymbolicJar preserves net/http CookieJar domain/path/secure semantics while
// replacing every captured value with a deterministic, non-secret slot value.
type SymbolicJar struct {
	mu      sync.Mutex
	jar     http.CookieJar
	symbols map[string]string
}

// NewSymbolicJar creates an empty real stdlib CookieJar with a symbolic value table.
func NewSymbolicJar() (*SymbolicJar, error) {
	jar, err := cookiejar.New(nil)
	if err != nil {
		return nil, err
	}
	return &SymbolicJar{jar: jar, symbols: make(map[string]string)}, nil
}

// Cookies implements http.CookieJar.
func (j *SymbolicJar) Cookies(u *url.URL) []*http.Cookie {
	if j == nil || j.jar == nil {
		return nil
	}
	j.mu.Lock()
	defer j.mu.Unlock()
	return j.jar.Cookies(u)
}

// SetCookies implements http.CookieJar.
func (j *SymbolicJar) SetCookies(u *url.URL, cookies []*http.Cookie) {
	if j == nil || j.jar == nil {
		return
	}
	j.mu.Lock()
	defer j.mu.Unlock()
	j.jar.SetCookies(u, cookies)
}

// Value returns the deterministic safe value for slot, creating it on demand.
func (j *SymbolicJar) Value(slot string) (string, error) {
	if j == nil {
		return "", fmt.Errorf("replay: nil symbolic jar")
	}
	slot = strings.TrimSpace(slot)
	if slot == "" {
		return "", fmt.Errorf("replay: cookie value slot required")
	}
	j.mu.Lock()
	defer j.mu.Unlock()
	if value := j.symbols[slot]; value != "" {
		return value, nil
	}
	digest := sha256.Sum256([]byte("go-email-protocol/replay/" + slot))
	value := "replay_" + hex.EncodeToString(digest[:12])
	j.symbols[slot] = value
	return value, nil
}

// Matches reports whether value is the deterministic value bound to slot.
func (j *SymbolicJar) Matches(slot, value string) bool {
	expected, err := j.Value(slot)
	return err == nil && value == expected
}

// CookieNames returns sorted cookie names visible to u. Values are never returned.
func (j *SymbolicJar) CookieNames(u *url.URL) []string {
	cookies := j.Cookies(u)
	names := make([]string, 0, len(cookies))
	for _, cookie := range cookies {
		if cookie != nil && cookie.Name != "" {
			names = append(names, cookie.Name)
		}
	}
	sort.Strings(names)
	return names
}

