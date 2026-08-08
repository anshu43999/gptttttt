// Package session provides per-job cookie jar isolation helpers.
package session

import (
	"net/http"
	"net/http/cookiejar"
	"net/url"
	"sync"
)

// Jar is an isolated cookie jar owned by a single job.
type Jar struct {
	mu  sync.Mutex
	jar http.CookieJar
	id  string
}

// NewJar creates a job-local cookie jar. Each job must call this independently.
func NewJar(jobID string) (*Jar, error) {
	j, err := cookiejar.New(nil)
	if err != nil {
		return nil, err
	}
	return &Jar{jar: j, id: jobID}, nil
}

// JobID returns the owning job id.
func (j *Jar) JobID() string {
	if j == nil {
		return ""
	}
	return j.id
}

// SetCookies implements http.CookieJar.
func (j *Jar) SetCookies(u *url.URL, cookies []*http.Cookie) {
	if j == nil {
		return
	}
	j.mu.Lock()
	defer j.mu.Unlock()
	j.jar.SetCookies(u, cookies)
}

// Cookies implements http.CookieJar.
func (j *Jar) Cookies(u *url.URL) []*http.Cookie {
	if j == nil {
		return nil
	}
	j.mu.Lock()
	defer j.mu.Unlock()
	return j.jar.Cookies(u)
}

// Snapshot returns a copy of cookies for a URL (for isolation tests).
func (j *Jar) Snapshot(u *url.URL) []http.Cookie {
	cs := j.Cookies(u)
	out := make([]http.Cookie, 0, len(cs))
	for _, c := range cs {
		if c != nil {
			out = append(out, *c)
		}
	}
	return out
}

// SetNamed sets a named cookie on the jar for u.
func (j *Jar) SetNamed(u *url.URL, name, value string) {
	j.SetCookies(u, []*http.Cookie{{Name: name, Value: value, Path: "/"}})
}

// GetNamed returns cookie value by name for u, or empty.
func (j *Jar) GetNamed(u *url.URL, name string) string {
	for _, c := range j.Cookies(u) {
		if c.Name == name {
			return c.Value
		}
	}
	return ""
}
