package transport

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// CookieDTO is a portable cookie for encrypted checkpoint (no secrets beyond session cookies).
type CookieDTO struct {
	Name     string `json:"name"`
	Value    string `json:"value"`
	Domain   string `json:"domain,omitempty"`
	Path     string `json:"path,omitempty"`
	Secure   bool   `json:"secure,omitempty"`
	HTTPOnly bool   `json:"http_only,omitempty"`
	// ExpiresUnix is 0 if session cookie.
	ExpiresUnix int64 `json:"expires_unix,omitempty"`
}

// AuthCookieURLs are hosts that matter for OpenAI register resume after OTP park.
var AuthCookieURLs = []string{
	"https://chatgpt.com/",
	"https://auth.openai.com/",
	"https://openai.com/",
	"https://www.openai.com/",
}

// CookieIO is optional: pure-Go clients export/import jar for OTP crash recovery.
type CookieIO interface {
	ExportCookies() ([]byte, error)
	ImportCookies(raw []byte) error
}

// ExportHTTPJar dumps cookies for AuthCookieURLs from a stdlib jar.
func ExportHTTPJar(jar http.CookieJar) ([]byte, error) {
	if jar == nil {
		return json.Marshal([]CookieDTO{})
	}
	seen := map[string]struct{}{}
	var out []CookieDTO
	for _, raw := range AuthCookieURLs {
		u, err := url.Parse(raw)
		if err != nil {
			continue
		}
		for _, c := range jar.Cookies(u) {
			if c == nil || c.Name == "" {
				continue
			}
			key := strings.ToLower(c.Domain) + "|" + c.Path + "|" + c.Name + "|" + c.Value
			if _, ok := seen[key]; ok {
				continue
			}
			seen[key] = struct{}{}
			dto := CookieDTO{
				Name:     c.Name,
				Value:    c.Value,
				Domain:   c.Domain,
				Path:     c.Path,
				Secure:   c.Secure,
				HTTPOnly: c.HttpOnly,
			}
			if c.Path == "" {
				dto.Path = "/"
			}
			if !c.Expires.IsZero() {
				dto.ExpiresUnix = c.Expires.Unix()
			}
			// Prefer host when Domain empty so Import can re-bind.
			if dto.Domain == "" {
				dto.Domain = u.Hostname()
			}
			out = append(out, dto)
		}
	}
	return json.Marshal(out)
}

// ImportHTTPJar loads CookieDTO list into a stdlib jar.
func ImportHTTPJar(jar http.CookieJar, raw []byte) error {
	if jar == nil {
		return fmt.Errorf("transport: nil jar")
	}
	if len(raw) == 0 {
		return nil
	}
	var list []CookieDTO
	if err := json.Unmarshal(raw, &list); err != nil {
		return err
	}
	byHost := map[string][]*http.Cookie{}
	for _, d := range list {
		if d.Name == "" {
			continue
		}
		host := strings.TrimPrefix(strings.ToLower(d.Domain), ".")
		if host == "" {
			continue
		}
		c := &http.Cookie{
			Name:     d.Name,
			Value:    d.Value,
			Domain:   d.Domain,
			Path:     d.Path,
			Secure:   d.Secure,
			HttpOnly: d.HTTPOnly,
		}
		if c.Path == "" {
			c.Path = "/"
		}
		if d.ExpiresUnix > 0 {
			c.Expires = time.Unix(d.ExpiresUnix, 0)
		}
		byHost[host] = append(byHost[host], c)
	}
	for host, cs := range byHost {
		u, err := url.Parse("https://" + host + "/")
		if err != nil {
			continue
		}
		jar.SetCookies(u, cs)
	}
	return nil
}
