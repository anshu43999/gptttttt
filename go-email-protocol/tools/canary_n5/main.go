// Command canary_n5 runs 5 sequential pure-go-register live canaries.
// Each iteration mints a fresh bestgo sticky session and invokes the
// prebuilt pure-go-register binary.
package main

import (
	"bufio"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"

	proxypool "github.com/gpt-register/go-email-protocol/internal/proxy"
)

func main() {
	root := findRoot()
	godir := filepath.Join(root, "go-email-protocol")
	out := filepath.Join(root, "output", "pure_go_register_canary", "n5")
	bin := filepath.Join(godir, "bin", "pure-go-register.exe")
	_ = os.MkdirAll(out, 0o755)

	if err := loadEnvFile(filepath.Join(root, "env.db")); err != nil {
		fmt.Fprintln(os.Stderr, "env.db:", err)
	}
	if _, err := os.Stat(bin); err != nil {
		fatalf("binary missing: %s (%v)", bin, err)
	}

	summaryPath := filepath.Join(out, "summary.tsv")
	masterPath := filepath.Join(out, "canary_n5_master.log")
	summary, err := os.Create(summaryPath)
	if err != nil {
		fatalf("%v", err)
	}
	defer summary.Close()
	master, err := os.Create(masterPath)
	if err != nil {
		fatalf("%v", err)
	}
	defer master.Close()
	logBoth := func(format string, args ...any) {
		line := fmt.Sprintf(format, args...)
		fmt.Println(line)
		_, _ = fmt.Fprintln(master, line)
	}

	_, _ = fmt.Fprintln(summary, "run\texit\ttask\temail\ttoken_len\taccount_id\tfail_snippet\tseconds")
	logBoth("START %s", time.Now().Format(time.RFC3339))
	logBoth("binary=%s", bin)
	logBoth("mailbox=outlook_token proxy=bestgo seed mint transport=tlsclient n=5 sequential")

	ok, fail := 0, 0
	reTask := regexp.MustCompile(`(?m)^task=(\S+)`)
	reEmail := regexp.MustCompile(`email=(\S+)`)
	reSuccess := regexp.MustCompile(`SUCCESS access_token_len=(\d+) account_id=(\S+)`)
	reFatal := regexp.MustCompile(`(?m)FATAL:.*|step .* → ERR.*`)

	for i := 1; i <= 5; i++ {
		logBoth("===== RUN %d/5 %s =====", i, time.Now().Format(time.RFC3339))
		session, err := proxypool.MintSeedSession("", fmt.Sprintf("canary_n5_%d", i), []string{"bestgo", "1024"}, "JP", 15)
		if err != nil {
			logBoth("RUN %d mint failed: %v", i, err)
			_, _ = fmt.Fprintf(summary, "%d\t2\t\t\t0\t\tmint:%v\t0\n", i, sanitizeTSV(err.Error()))
			fail++
			continue
		}
		logBoth("proxy_style=%s region=%s id=%d", session.Style, session.Region, session.ResourceID)

		runLogPath := filepath.Join(out, fmt.Sprintf("run_%d.log", i))
		wireDir := filepath.Join(out, fmt.Sprintf("wire_%d", i))
		_ = os.MkdirAll(wireDir, 0o755)

		cmd := exec.Command(bin,
			"-mailbox-provider", "outlook_token",
			"-proxy", session.URL,
			"-browser", "firefox",
			"-out", out,
			"-timeout", "15m",
			"-otp-timeout", "360s",
			"-email-tries", "5",
		)
		cmd.Dir = godir
		cmd.Env = append(os.Environ(), "GPT_REGISTER_WIRE_DIR="+wireDir)

		t0 := time.Now()
		raw, runErr := cmd.CombinedOutput()
		sec := int(time.Since(t0).Seconds())
		_ = os.WriteFile(runLogPath, raw, 0o600)
		ec := 0
		if runErr != nil {
			if ee, ok := runErr.(*exec.ExitError); ok {
				ec = ee.ExitCode()
			} else {
				ec = 1
			}
		}

		text := string(raw)
		task, email, tokenLen, accountID, failSnippet := "", "", 0, "", ""
		if m := reTask.FindStringSubmatch(text); len(m) == 2 {
			task = m[1]
		}
		if m := reEmail.FindStringSubmatch(text); len(m) == 2 {
			email = m[1]
		}
		if m := reSuccess.FindStringSubmatch(text); len(m) == 3 {
			tokenLen, _ = strconv.Atoi(m[1])
			accountID = m[2]
		}
		if ec != 0 {
			if m := reFatal.FindString(text); m != "" {
				failSnippet = sanitizeTSV(trim(m, 160))
			} else {
				failSnippet = sanitizeTSV(trim(runErr.Error(), 160))
			}
			fail++
		} else {
			ok++
		}
		_, _ = fmt.Fprintf(summary, "%d\t%d\t%s\t%s\t%d\t%s\t%s\t%d\n",
			i, ec, task, email, tokenLen, accountID, failSnippet, sec)
		logBoth("RUN %d exit=%d token_len=%d account=%s sec=%d", i, ec, tokenLen, accountID, sec)
		time.Sleep(2 * time.Second)
	}

	logBoth("===== DONE %s ok=%d fail=%d =====", time.Now().Format(time.RFC3339), ok, fail)
	logBoth("summary=%s", summaryPath)
	if ok < 5 {
		os.Exit(1)
	}
}

func findRoot() string {
	wd, _ := os.Getwd()
	// tools/canary_n5 -> go-email-protocol -> root
	cand := filepath.Clean(filepath.Join(wd, "..", ".."))
	if _, err := os.Stat(filepath.Join(cand, "env.db")); err == nil {
		return cand
	}
	cand = filepath.Clean(filepath.Join(wd, ".."))
	if _, err := os.Stat(filepath.Join(cand, "env.db")); err == nil {
		return cand
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
