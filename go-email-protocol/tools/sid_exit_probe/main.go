// Command sid_exit_probe mints N bestgo sticky SIDs in parallel and prints
// each session's public exit IP via ipify. Used to check whether "new SID"
// actually yields distinct egress under concurrent load.
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"

	"golang.org/x/net/proxy"

	proxypool "github.com/gpt-register/go-email-protocol/internal/proxy"
)

type row struct {
	Worker int    `json:"worker"`
	SID    string `json:"sid"`
	IP     string `json:"ip,omitempty"`
	MS     int64  `json:"ms"`
	Err    string `json:"err,omitempty"`
	URL    string `json:"url_redacted,omitempty"`
}

func main() {
	n := 10
	out := make([]row, n)
	var wg sync.WaitGroup
	for i := range n {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			s, err := proxypool.MintSeedSession("", fmt.Sprintf("probe_%d_%d", i, time.Now().UnixNano()), []string{"bestgo"}, "JP", 15)
			if err != nil {
				out[i] = row{Worker: i, Err: err.Error()}
				return
			}
			sid := extractSID(s.URL)
			t0 := time.Now()
			ip, e := fetchIP(s.URL)
			r := row{
				Worker: i,
				SID:    sid,
				MS:     time.Since(t0).Milliseconds(),
				URL:    redact(s.URL),
			}
			if e != nil {
				r.Err = e.Error()
			} else {
				r.IP = ip
			}
			out[i] = r
		}(i)
	}
	wg.Wait()

	ips := map[string]int{}
	sids := map[string]int{}
	ok := 0
	for _, r := range out {
		fmt.Printf("w%02d sid=%s ip=%s ms=%d err=%s\n", r.Worker, r.SID, r.IP, r.MS, r.Err)
		if r.SID != "" {
			sids[r.SID]++
		}
		if r.IP != "" {
			ips[r.IP]++
			ok++
		}
	}
	fmt.Printf("unique_sids=%d unique_ips=%d ok=%d/%d\n", len(sids), len(ips), ok, n)
	b, _ := json.MarshalIndent(ips, "", "  ")
	fmt.Println("ip_histogram=", string(b))
}

func extractSID(raw string) string {
	u, err := url.Parse(raw)
	if err != nil || u.User == nil {
		return ""
	}
	user := u.User.Username()
	const mark = "-session-"
	i := strings.Index(user, mark)
	if i < 0 {
		return ""
	}
	rest := user[i+len(mark):]
	if j := strings.Index(rest, "-"); j >= 0 {
		return rest[:j]
	}
	return rest
}

func fetchIP(proxyURL string) (string, error) {
	u, err := url.Parse(proxyURL)
	if err != nil {
		return "", err
	}
	var auth *proxy.Auth
	if u.User != nil {
		p, _ := u.User.Password()
		auth = &proxy.Auth{User: u.User.Username(), Password: p}
	}
	d, err := proxy.SOCKS5("tcp", u.Host, auth, proxy.Direct)
	if err != nil {
		return "", err
	}
	tr := &http.Transport{Dial: d.Dial}
	c := &http.Client{Transport: tr, Timeout: 25 * time.Second}
	resp, err := c.Get("https://api.ipify.org?format=json")
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	b, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", err
	}
	var m map[string]string
	if err := json.Unmarshal(b, &m); err != nil {
		return strings.TrimSpace(string(b)), nil
	}
	return m["ip"], nil
}

func redact(raw string) string {
	u, err := url.Parse(raw)
	if err != nil || u.User == nil {
		return raw
	}
	u.User = url.UserPassword(u.User.Username(), "***")
	return u.String()
}
