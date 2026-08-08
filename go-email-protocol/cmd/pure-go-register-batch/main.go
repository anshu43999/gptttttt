// Command pure-go-register-batch runs N concurrent pure-go-register workers.
//
//	go run ./cmd/pure-go-register-batch -n 10
package main

import (
	"bufio"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

type result struct {
	Worker  int    `json:"worker"`
	OK      bool   `json:"ok"`
	Email   string `json:"email,omitempty"`
	Account string `json:"account_id,omitempty"`
	TokenN  int    `json:"access_token_len,omitempty"`
	Err     string `json:"error,omitempty"`
	LogPath string `json:"log_path"`
	Seconds float64 `json:"seconds"`
}

func main() {
	n := flag.Int("n", 10, "parallel workers")
	db := flag.String("db", filepath.Join("..", "data", "gpt_register.db"), "main DB SQLite path (ignored when Postgres is selected)")
	proxyFile := flag.String("proxy-file", "", "optional proxy file fallback; empty = pure-go-register leases from resource_pool")
	outDir := flag.String("out", filepath.Join("..", "output", "pure_go_register_batch"), "output dir")
	timeout := flag.Duration("timeout", 15*time.Minute, "per-worker timeout (must exceed otp-timeout + protocol)")
	otpTimeout := flag.Duration("otp-timeout", 360*time.Second, "otp timeout (HME often 4-11m under burst)")
	browser := flag.String("browser", "firefox", "browser fingerprint")
	bin := flag.String("bin", "", "path to pure-go-register binary; empty = go run")
	emailTries := flag.Int("email-tries", 20, "per-worker email re-lease attempts")
	mailboxProvider := flag.String("mailbox-provider", "icloud_api", "resource_pool email provider: icloud_api|outlook_token")
	flag.Parse()

	if err := os.MkdirAll(*outDir, 0o755); err != nil {
		fatal(err)
	}
	logDir := filepath.Join(*outDir, "logs_"+time.Now().UTC().Format("20060102_150405"))
	if err := os.MkdirAll(logDir, 0o755); err != nil {
		fatal(err)
	}

	// resolve absolute paths
	abs := func(p string) string {
		a, err := filepath.Abs(p)
		if err != nil {
			return p
		}
		return a
	}
	dbPath := abs(*db)
	proxyPath := ""
	if strings.TrimSpace(*proxyFile) != "" {
		proxyPath = abs(*proxyFile)
	}
	outPath := abs(*outDir)

	fmt.Printf("batch n=%d db=%s out=%s\n", *n, dbPath, outPath)

	var (
		wg      sync.WaitGroup
		mu      sync.Mutex
		results []result
		okN     atomic.Int64
		failN   atomic.Int64
	)

	startAll := time.Now()
	for i := 0; i < *n; i++ {
		wg.Add(1)
		go func(worker int) {
			defer wg.Done()
			// stagger starts to reduce proxy/OpenAI thundering herd
			time.Sleep(time.Duration(worker) * 1500 * time.Millisecond)
			r := runWorker(worker, *bin, dbPath, proxyPath, outPath, logDir, *browser, *timeout, *otpTimeout, *emailTries, *mailboxProvider)
			if r.OK {
				okN.Add(1)
			} else {
				failN.Add(1)
			}
			mu.Lock()
			results = append(results, r)
			mu.Unlock()
			status := "FAIL"
			if r.OK {
				status = "OK"
			}
			fmt.Printf("[%02d] %s %.1fs email=%s err=%s\n", worker, status, r.Seconds, r.Email, trim(r.Err, 120))
		}(i)
	}
	wg.Wait()
	elapsed := time.Since(startAll).Seconds()

	summary := map[string]any{
		"n":       *n,
		"ok":      okN.Load(),
		"fail":    failN.Load(),
		"seconds": elapsed,
		"results": results,
		"at":      time.Now().UTC().Format(time.RFC3339),
	}
	raw, _ := json.MarshalIndent(summary, "", "  ")
	sumPath := filepath.Join(logDir, "summary.json")
	_ = os.WriteFile(sumPath, raw, 0o644)

	fmt.Printf("\n=== BATCH DONE ok=%d fail=%d total=%.1fs summary=%s ===\n", okN.Load(), failN.Load(), elapsed, sumPath)

	// print failure causes grouped
	causes := map[string]int{}
	for _, r := range results {
		if r.OK {
			continue
		}
		causes[classify(r.Err)]++
	}
	if len(causes) > 0 {
		fmt.Println("failure causes:")
		for k, v := range causes {
			fmt.Printf("  %d x %s\n", v, k)
		}
	}
	if failN.Load() > 0 {
		os.Exit(2)
	}
}

func runWorker(worker int, bin, db, proxy, out, logDir, browser string, timeout, otpTimeout time.Duration, emailTries int, mailboxProvider string) result {
	start := time.Now()
	logPath := filepath.Join(logDir, fmt.Sprintf("w%02d.log", worker))
	f, err := os.Create(logPath)
	if err != nil {
		return result{Worker: worker, Err: err.Error(), LogPath: logPath, Seconds: 0}
	}
	defer f.Close()
	w := bufio.NewWriter(f)
	defer w.Flush()

	ctx, cancel := context.WithTimeout(context.Background(), timeout+30*time.Second)
	defer cancel()

	var cmd *exec.Cmd
	args := []string{
		"-db", db,
		"-out", out,
		"-timeout", timeout.String(),
		"-otp-timeout", otpTimeout.String(),
		"-browser", browser,
		"-worker", fmt.Sprintf("%d", worker),
		"-email-tries", fmt.Sprintf("%d", emailTries),
		"-mailbox-provider", mailboxProvider,
	}
	if strings.TrimSpace(proxy) != "" {
		args = append(args, "-proxy-file", proxy)
	}
	if bin != "" {
		cmd = exec.CommandContext(ctx, bin, args...)
	} else {
		// go run from module root (caller cwd should be go-email-protocol)
		cmdArgs := append([]string{"run", "./cmd/pure-go-register"}, args...)
		cmd = exec.CommandContext(ctx, "go", cmdArgs...)
	}
	cmd.Stdout = w
	cmd.Stderr = w
	cmd.Dir = mustAbs(".")

	err = cmd.Run()
	_ = w.Flush()
	sec := time.Since(start).Seconds()

	// parse log for SUCCESS / FATAL
	body, _ := os.ReadFile(logPath)
	text := string(body)
	r := result{Worker: worker, LogPath: logPath, Seconds: sec}
	if strings.Contains(text, "SUCCESS access_token_len=") {
		r.OK = true
		r.Email = findField(text, "email=")
		// SUCCESS line has account_id=
		for _, line := range strings.Split(text, "\n") {
			if strings.Contains(line, "SUCCESS access_token_len=") {
				r.TokenN = atoi(between(line, "access_token_len=", " "))
				r.Account = between(line, "account_id=", " ")
				if r.Account == "" {
					r.Account = between(line, "account_id=", "\n")
				}
			}
			if strings.HasPrefix(line, "email=") {
				r.Email = strings.TrimPrefix(strings.TrimSpace(line), "email=")
				if i := strings.Index(r.Email, " "); i > 0 {
					r.Email = r.Email[:i]
				}
			}
		}
		return r
	}
	r.OK = false
	if err != nil {
		r.Err = err.Error()
	}
	// prefer FATAL line
	for _, line := range strings.Split(text, "\n") {
		if strings.HasPrefix(line, "FATAL:") {
			r.Err = strings.TrimSpace(strings.TrimPrefix(line, "FATAL:"))
		}
		if strings.Contains(line, "step ") && strings.Contains(line, "ERR") {
			r.Err = strings.TrimSpace(line)
		}
		if strings.HasPrefix(line, "email=") && r.Email == "" {
			r.Email = strings.TrimPrefix(strings.TrimSpace(line), "email=")
			if i := strings.Index(r.Email, " "); i > 0 {
				r.Email = r.Email[:i]
			}
		}
	}
	if r.Err == "" {
		r.Err = "unknown failure (see log)"
	}
	return r
}

func classify(err string) string {
	e := strings.ToLower(err)
	switch {
	case strings.Contains(e, "lease race") || strings.Contains(e, "no available icloud"):
		return "mailbox_lease"
	case strings.Contains(e, "otp timeout") || strings.Contains(e, "mailbox: otp"):
		return "otp_timeout"
	case strings.Contains(e, "csrf"):
		return "csrf"
	case strings.Contains(e, "s5 ") || strings.Contains(e, "sentinel"):
		return "sentinel"
	case strings.Contains(e, "s6 "):
		return "s6_authorize_continue"
	case strings.Contains(e, "s7 "):
		return "s7_register"
	case strings.Contains(e, "s10 "):
		return "s10_otp_validate"
	case strings.Contains(e, "s11 "):
		return "s11_create_account"
	case strings.Contains(e, "s12 "):
		return "s12_callback"
	case strings.Contains(e, "s13 ") || strings.Contains(e, "access_token"):
		return "s13_session"
	case strings.Contains(e, "proxy") || strings.Contains(e, "socks") || strings.Contains(e, "timeout") || strings.Contains(e, "connection"):
		return "proxy_network"
	case strings.Contains(e, "context deadline"):
		return "deadline"
	default:
		if len(err) > 80 {
			return err[:80]
		}
		if err == "" {
			return "unknown"
		}
		return err
	}
}

func findField(text, key string) string {
	for _, line := range strings.Split(text, "\n") {
		if strings.HasPrefix(line, key) {
			return strings.TrimSpace(strings.TrimPrefix(line, key))
		}
	}
	return ""
}

func between(s, a, b string) string {
	i := strings.Index(s, a)
	if i < 0 {
		return ""
	}
	s = s[i+len(a):]
	if b == "" {
		return strings.TrimSpace(s)
	}
	j := strings.Index(s, b)
	if j < 0 {
		return strings.TrimSpace(s)
	}
	return strings.TrimSpace(s[:j])
}

func atoi(s string) int {
	n := 0
	for _, c := range s {
		if c < '0' || c > '9' {
			break
		}
		n = n*10 + int(c-'0')
	}
	return n
}

func trim(s string, n int) string {
	s = strings.TrimSpace(s)
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}

func mustAbs(p string) string {
	a, err := filepath.Abs(p)
	if err != nil {
		return p
	}
	return a
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, err)
	os.Exit(1)
}
