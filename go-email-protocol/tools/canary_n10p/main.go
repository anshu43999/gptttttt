// Command canary_n10p runs N concurrent pure-go-register live canaries.
// True parallel: each worker process mints its own sticky SID via -proxy-seed
// and remints on edge_challenge (no fixed -proxy that blocks remint).
package main

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

type runResult struct {
	Worker     int     `json:"worker"`
	Exit       int     `json:"exit"`
	OK         bool    `json:"ok"`
	Task       string  `json:"task,omitempty"`
	Email      string  `json:"email,omitempty"`
	TokenLen   int     `json:"token_len"`
	AccountID  string  `json:"account_id,omitempty"`
	ProxySrc   string  `json:"proxy_source,omitempty"`
	Remints    int     `json:"edge_remints"`
	Fail       string  `json:"fail,omitempty"`
	Seconds    float64 `json:"seconds"`
	LogPath    string  `json:"log_path"`
}

func main() {
	n := flag.Int("n", 10, "parallel workers")
	staggerMS := flag.Int("stagger-ms", 200, "stagger start delay per worker (true parallel: keep small)")
	regions := flag.String("regions", "JP,US,SG,DE,GB", "proxy-seed regions")
	styles := flag.String("styles", "bestgo,1024", "proxy-seed styles")
	edgeRemints := flag.Int("edge-remints", 2, "per-worker edge remint budget")
	flag.Parse()

	root := findRoot()
	godir := filepath.Join(root, "go-email-protocol")
	out := filepath.Join(root, "output", "pure_go_register_canary", fmt.Sprintf("n%dp_seed", *n))
	bin := filepath.Join(godir, "bin", "pure-go-register.exe")
	_ = os.MkdirAll(out, 0o755)

	if err := loadEnvFile(filepath.Join(root, "env.db")); err != nil {
		fmt.Fprintln(os.Stderr, "env.db:", err)
	}
	if _, err := os.Stat(bin); err != nil {
		fatalf("binary missing: %s (%v)", bin, err)
	}

	stamp := time.Now().UTC().Format("20060102_150405")
	summaryPath := filepath.Join(out, "summary_"+stamp+".json")
	tsvPath := filepath.Join(out, "summary_"+stamp+".tsv")
	masterPath := filepath.Join(out, "master_"+stamp+".log")
	master, err := os.Create(masterPath)
	if err != nil {
		fatalf("%v", err)
	}
	defer master.Close()
	tsv, err := os.Create(tsvPath)
	if err != nil {
		fatalf("%v", err)
	}
	defer tsv.Close()
	_, _ = fmt.Fprintln(tsv, "worker\texit\ttask\temail\ttoken_len\taccount_id\tremints\tfail\tseconds")

	logBoth := func(format string, args ...any) {
		line := fmt.Sprintf(format, args...)
		fmt.Println(line)
		_, _ = fmt.Fprintln(master, line)
	}

	logBoth("START %s n=%d parallel stagger_ms=%d mode=proxy-seed", time.Now().Format(time.RFC3339), *n, *staggerMS)
	logBoth("binary=%s out=%s", bin, out)
	logBoth("mailbox=outlook_token styles=%s regions=%s edge_remints=%d transport=tlsclient", *styles, *regions, *edgeRemints)

	var (
		wg      sync.WaitGroup
		mu      sync.Mutex
		results []runResult
		okN     atomic.Int64
		failN   atomic.Int64
	)
	tAll := time.Now()

	for i := range *n {
		wg.Add(1)
		go func(worker int) {
			defer wg.Done()
			if *staggerMS > 0 {
				time.Sleep(time.Duration(worker*(*staggerMS)) * time.Millisecond)
			}
			r := runOne(worker, bin, godir, out, *regions, *styles, *edgeRemints)
			if r.OK {
				okN.Add(1)
			} else {
				failN.Add(1)
			}
			mu.Lock()
			results = append(results, r)
			_, _ = fmt.Fprintf(tsv, "%d\t%d\t%s\t%s\t%d\t%s\t%d\t%s\t%.1f\n",
				r.Worker, r.Exit, r.Task, r.Email, r.TokenLen, r.AccountID, r.Remints, sanitizeTSV(r.Fail), r.Seconds)
			mu.Unlock()
			status := "FAIL"
			if r.OK {
				status = "OK"
			}
			logBoth("[%02d] %s %.1fs token=%d remints=%d email=%s fail=%s",
				worker, status, r.Seconds, r.TokenLen, r.Remints, r.Email, trim(r.Fail, 100))
		}(i)
	}
	wg.Wait()
	elapsed := time.Since(tAll).Seconds()

	sorted := make([]runResult, *n)
	for _, r := range results {
		if r.Worker >= 0 && r.Worker < *n {
			sorted[r.Worker] = r
		}
	}

	causes := map[string]int{}
	for _, r := range sorted {
		if r.OK {
			continue
		}
		causes[classify(r.Fail)]++
	}

	summary := map[string]any{
		"n":           *n,
		"ok":          okN.Load(),
		"fail":        failN.Load(),
		"seconds":     elapsed,
		"mode":        "parallel_proxy_seed",
		"stagger_ms":  *staggerMS,
		"regions":     *regions,
		"styles":      *styles,
		"edge_remints": *edgeRemints,
		"mailbox":     "outlook_token",
		"transport":   "tlsclient",
		"results":     sorted,
		"causes":      causes,
		"at":          time.Now().UTC().Format(time.RFC3339),
	}
	raw, _ := json.MarshalIndent(summary, "", "  ")
	_ = os.WriteFile(summaryPath, raw, 0o644)

	logBoth("===== DONE %s ok=%d fail=%d total=%.1fs =====", time.Now().Format(time.RFC3339), okN.Load(), failN.Load(), elapsed)
	logBoth("summary_json=%s", summaryPath)
	if len(causes) > 0 {
		logBoth("failure causes:")
		for k, v := range causes {
			logBoth("  %d x %s", v, k)
		}
	}
	if failN.Load() > 0 {
		os.Exit(1)
	}
}

func runOne(worker int, bin, godir, out, regions, styles string, edgeRemints int) runResult {
	t0 := time.Now()
	r := runResult{Worker: worker, LogPath: filepath.Join(out, fmt.Sprintf("w%02d.log", worker))}
	wireDir := filepath.Join(out, fmt.Sprintf("wire_w%02d", worker))
	_ = os.MkdirAll(wireDir, 0o755)

	cmd := exec.Command(bin,
		"-mailbox-provider", "outlook_token",
		"-proxy-seed",
		"-proxy-styles", styles,
		"-proxy-regions", regions,
		"-proxy-ttl", "15",
		"-edge-remints", strconv.Itoa(edgeRemints),
		"-browser", "firefox",
		"-out", out,
		"-timeout", "15m",
		"-otp-timeout", "360s",
		"-email-tries", "5",
		"-worker", strconv.Itoa(worker),
	)
	cmd.Dir = godir
	cmd.Env = append(os.Environ(), "GPT_REGISTER_WIRE_DIR="+wireDir)
	raw, runErr := cmd.CombinedOutput()
	_ = os.WriteFile(r.LogPath, raw, 0o600)
	r.Seconds = time.Since(t0).Seconds()
	if runErr != nil {
		if ee, ok := runErr.(*exec.ExitError); ok {
			r.Exit = ee.ExitCode()
		} else {
			r.Exit = 1
		}
	}

	text := string(raw)
	reTask := regexp.MustCompile(`(?m)^task=(\S+)`)
	reEmail := regexp.MustCompile(`email=(\S+)`)
	reSuccess := regexp.MustCompile(`SUCCESS access_token_len=(\d+) account_id=(\S+)`)
	reFatal := regexp.MustCompile(`(?m)FATAL:.*`)
	reStepErr := regexp.MustCompile(`(?m)step .* → ERR.*`)
	reRemint := regexp.MustCompile(`edge_challenge remint`)
	reProxySrc := regexp.MustCompile(`(?m)^proxy_source=(\S+)`)

	if m := reTask.FindStringSubmatch(text); len(m) == 2 {
		r.Task = m[1]
	}
	if m := reEmail.FindStringSubmatch(text); len(m) == 2 {
		r.Email = m[1]
	}
	if m := reProxySrc.FindStringSubmatch(text); len(m) == 2 {
		r.ProxySrc = m[1]
	}
	r.Remints = len(reRemint.FindAllString(text, -1))
	if m := reSuccess.FindStringSubmatch(text); len(m) == 3 {
		r.TokenLen, _ = strconv.Atoi(m[1])
		r.AccountID = m[2]
		r.OK = true
		r.Exit = 0
		return r
	}
	if m := reFatal.FindString(text); m != "" {
		r.Fail = strings.TrimSpace(strings.TrimPrefix(m, "FATAL:"))
	} else if m := reStepErr.FindString(text); m != "" {
		r.Fail = strings.TrimSpace(m)
	} else if runErr != nil {
		r.Fail = runErr.Error()
	} else {
		r.Fail = "unknown failure (see log)"
	}
	if r.Exit == 0 {
		r.Exit = 1
	}
	return r
}

func classify(err string) string {
	e := strings.ToLower(err)
	switch {
	case strings.Contains(e, "edge_challenge"):
		return "edge_challenge"
	case strings.Contains(e, "otp") || strings.Contains(e, "graph_no_openai"):
		return "otp"
	case strings.Contains(e, "mailbox") || strings.Contains(e, "lease") || strings.Contains(e, "no available"):
		return "mailbox_lease"
	case strings.Contains(e, "proxy") || strings.Contains(e, "socks") || strings.Contains(e, "dial"):
		return "proxy_dial"
	case strings.Contains(e, "s11 status 400"):
		return "s11_400"
	case strings.Contains(e, "callback"):
		return "s12_callback"
	case strings.Contains(e, "timeout") || strings.Contains(e, "deadline"):
		return "timeout"
	default:
		if err == "" {
			return "unknown"
		}
		return "other"
	}
}

func findRoot() string {
	wd, _ := os.Getwd()
	for _, cand := range []string{
		filepath.Clean(filepath.Join(wd, "..", "..")),
		filepath.Clean(filepath.Join(wd, "..")),
		wd,
	} {
		if _, err := os.Stat(filepath.Join(cand, "env.db")); err == nil {
			return cand
		}
	}
	return wd
}

func loadEnvFile(path string) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		k, v, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		k = strings.TrimSpace(k)
		v = strings.TrimSpace(v)
		if os.Getenv(k) == "" {
			_ = os.Setenv(k, v)
		}
	}
	return sc.Err()
}

func sanitizeTSV(s string) string {
	s = strings.ReplaceAll(s, "\t", " ")
	s = strings.ReplaceAll(s, "\n", " ")
	s = strings.ReplaceAll(s, "\r", " ")
	return s
}

func trim(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}

func fatalf(format string, args ...any) {
	fmt.Fprintf(os.Stderr, format+"\n", args...)
	os.Exit(2)
}
