// Command pure-go-register runs one pure-Go email protocol registration:
// lease mailbox (icloud_api|outlook_token) → SOCKS proxy → S0..S9 → OTP → S10..S14 → access_token.
//
//	go run ./cmd/pure-go-register \
//	  -db ../data/gpt_register.db \
//	  -out ../output/pure_go_register
//
// env.db / GPT_REGISTER_DB_BACKEND=postgres selects Postgres; -db is then ignored.
//
// Proxy resolution order:
//  1. -proxy (fixed URL)
//  2. -proxy-file
//  3. -proxy-seed (mint sticky SID from proxy_seed; remint on edge CF)
//  4. -proxy-provider exclusive lease (legacy lajiao_credentials)
package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/gpt-register/go-email-protocol/internal/accounts"
	"github.com/gpt-register/go-email-protocol/internal/fingerprint"
	"github.com/gpt-register/go-email-protocol/internal/mailbox"
	"github.com/gpt-register/go-email-protocol/internal/protocol"
	proxypool "github.com/gpt-register/go-email-protocol/internal/proxy"
	"github.com/gpt-register/go-email-protocol/internal/transport"
	mathrand "math/rand/v2"
)

func main() {
	dbPath := flag.String("db", filepath.Join("..", "data", "gpt_register.db"), "main DB SQLite path (ignored when Postgres is selected)")
	proxyFile := flag.String("proxy-file", "", "optional SOCKS list fallback (one per line); empty = lease/mint from resource_pool")
	proxyURL := flag.String("proxy", "", "single socks5 URL (overrides pool/file/seed)")
	proxyProvider := flag.String("proxy-provider", "lajiao_credentials", "resource_pool exclusive proxy provider when not using -proxy-seed")
	proxySeed := flag.Bool("proxy-seed", false, "mint sticky SID from proxy_seed (bestgo/1024); remint on edge_challenge")
	proxyStyles := flag.String("proxy-styles", "bestgo,1024", "comma styles for -proxy-seed")
	proxyRegions := flag.String("proxy-regions", "JP,US,DE,GB,BR", "comma regions for -proxy-seed; must have fingerprint locale; rotated on edge remint")
	proxyTTL := flag.Int("proxy-ttl", 15, "sticky session TTL minutes for -proxy-seed")
	edgeRemints := flag.Int("edge-remints", 2, "max S0 restarts with fresh SID after edge_challenge (proxy-seed mode)")
	noImport := flag.Bool("no-import", false, "skip writing success into accounts tables")
	outDir := flag.String("out", filepath.Join("..", "output", "pure_go_register"), "output directory")
	timeout := flag.Duration("timeout", 12*time.Minute, "overall timeout (must exceed otp-timeout + protocol)")
	otpTimeout := flag.Duration("otp-timeout", 360*time.Second, "OTP poll timeout (HME often 4-11m under burst)")
	browser := flag.String("browser", "firefox", "fingerprint browser: firefox|chrome|edge")
	workerIndex := flag.Int("worker", -1, "legacy file-proxy worker index (-1=random); also seeds region pick")
	emailTries := flag.Int("email-tries", 20, "re-lease new email on already-used / invalid_state")
	mailboxProvider := flag.String("mailbox-provider", "icloud_api", "resource_pool email provider: icloud_api|outlook_token")
	flag.Parse()

	ctx, cancel := context.WithTimeout(context.Background(), *timeout)
	defer cancel()

	if err := os.MkdirAll(*outDir, 0o755); err != nil {
		fatal(err)
	}

	taskID := "purego_" + shortID()
	fmt.Printf("task=%s\n", taskID)

	// Proxy first (shared across email retries unless edge remint rotates SID).
	var (
		err          error
		socks        string
		proxyLease   *proxypool.Lease
		seedSession  *proxypool.SeedSession
		seedStyles   = splitCSV(*proxyStyles)
		seedRegions  = splitCSV(*proxyRegions)
		regionIdx    = 0
		seedMode     = *proxySeed
		fixedProxy   = strings.TrimSpace(*proxyURL) != ""
		edgeBudget   = *edgeRemints
	)
	if len(seedRegions) == 0 {
		seedRegions = []string{"JP"}
	}
	if *workerIndex >= 0 {
		regionIdx = *workerIndex % len(seedRegions)
	}
	socks = strings.TrimSpace(*proxyURL)
	if socks == "" && strings.TrimSpace(*proxyFile) != "" {
		socks, err = pickProxyLine(*proxyFile, *workerIndex)
		if err != nil {
			fatal(err)
		}
		fmt.Printf("proxy_source=file\n")
	}
	if socks == "" && seedMode {
		seedSession, err = mintSeed(*dbPath, taskID, seedStyles, seedRegions[regionIdx%len(seedRegions)], *proxyTTL)
		if err != nil {
			fatal(err)
		}
		socks = seedSession.URL
		fmt.Printf("proxy_source=proxy_seed style=%s region=%s id=%d\n", seedSession.Style, seedSession.Region, seedSession.ResourceID)
	}
	if socks == "" {
		proxyLease, err = proxypool.LeaseFromDBProvider(*dbPath, taskID, *proxyProvider)
		if err != nil {
			fatal(err)
		}
		socks = proxyLease.URL
		fmt.Printf("proxy_source=resource_pool id=%d region=%s exit=%s\n", proxyLease.ID, proxyLease.Region, proxyLease.ExitIP)
	}
	fmt.Printf("proxy=%s\n", redactProxy(socks))
	defer func() {
		if proxyLease != nil && proxyLease.ID > 0 {
			// reusable sticky proxy — always release to available unless marked cooldown below
			_ = proxypool.MarkSuccess(*dbPath, proxyLease.ID)
		}
	}()

	forceBrowser := fingerprint.BrowserFirefox
	switch strings.ToLower(strings.TrimSpace(*browser)) {
	case "chrome":
		forceBrowser = fingerprint.BrowserChrome
	case "edge":
		forceBrowser = fingerprint.BrowserEdge
	case "firefox", "":
		forceBrowser = fingerprint.BrowserFirefox
	}

	password := randomPassword()
	fmt.Printf("password_set=true len=%d\n", len(password))

	var (
		acc    *mailbox.Account
		bundle *fingerprint.Bundle
		client transport.Client
		cur    protocol.Cursor
		eng    *protocol.Engine
	)

	// Email lease + S0..S14 with re-lease on already-used emails / dead OTP mailboxes.
	for try := 1; try <= *emailTries; try++ {
		if acc != nil {
			_ = mailbox.MarkUsed(*dbPath, acc.ID, "used", "email rejected at authorize/continue")
		}
		acc, err = mailbox.LeaseFromDBProvider(*dbPath, taskID+fmt.Sprintf("_%d", try), *mailboxProvider)
		if err != nil {
			fatal(err)
		}
		fmt.Printf("email_try=%d/%d provider=%s email=%s code_url=%v mail_url=%v outlook=%v\n", try, *emailTries, acc.Provider, acc.Email, acc.CodeURL != "", acc.MailURL != "", acc.ClientID != "")

		expectCountry := "JP"
		if seedSession != nil && seedSession.Region != "" {
			expectCountry = seedSession.Region
		} else if proxyLease != nil && proxyLease.ExpectedCountry != "" {
			expectCountry = proxyLease.ExpectedCountry
		}
		bundle, err = fingerprint.Generate(fingerprint.GenerateOptions{
			ForceFamily:     fingerprint.FamilyDesktop,
			ForceBrowser:    forceBrowser,
			ExpectedCountry: expectCountry,
			NoiseEnabled:    true,
			Source:          fingerprint.SourceGenerated,
		})
		if err != nil {
			_ = mailbox.MarkUsed(*dbPath, acc.ID, "available", "fingerprint failed")
			fatal(err)
		}
		fmt.Printf("ua=%s\n", bundle.Device.UserAgent)

		if client != nil {
			_ = client.Close()
		}
		exitIP := ""
		if proxyLease != nil {
			exitIP = proxyLease.ExitIP
		}
		// Prefer bogdanfinn tls-client when built with -tags tlsclient (matches config
		// go_email_protocol_transport: tls). Fall back to stdlib DirectSOCKS otherwise.
		proxySnap := transport.ProxySnapshot{
			BridgeURL:        socks,
			BridgeCapability: "direct",
			ExpectedCountry:  expectCountry,
			ExitIP:           exitIP,
		}
		clientOpts := transport.ClientOptions{
			JobID: taskID,
			Proxy: proxySnap,
		}
		if bundle != nil {
			clientOpts.TransportID = fmt.Sprintf("%s-%d", strings.ToLower(string(forceBrowser)), bundle.Device.UAMajor)
			if raw, mErr := bundle.MarshalCanonical(); mErr == nil {
				clientOpts.BundleJSON = raw
			}
		}
		if f, fErr := transport.NewFactory("tls"); fErr == nil {
			if of, ok := f.(transport.OptionsFactory); ok {
				client, err = of.NewWithOptions(clientOpts)
			} else {
				client, err = f.New(taskID, proxySnap)
			}
			if err == nil {
				fmt.Printf("transport=tls profile_hint=%s\n", clientOpts.TransportID)
			}
		} else {
			client, err = transport.DirectSOCKSFactory{}.NewWithOptions(clientOpts)
			if err == nil {
				fmt.Printf("transport=direct_socks (tls unavailable: %v)\n", fErr)
			}
		}
		if err != nil {
			_ = mailbox.MarkUsed(*dbPath, acc.ID, "available", "transport failed")
			acc = nil
			if proxyLease != nil {
				_ = proxypool.MarkCooldown(*dbPath, proxyLease.ID, err.Error())
				proxyLease = nil
			}
			if strings.TrimSpace(*proxyURL) == "" && strings.TrimSpace(*proxyFile) == "" {
				nl, nerr := proxypool.LeaseFromDBProvider(*dbPath, taskID+"_px", *proxyProvider)
				if nerr != nil {
					fatal(nerr)
				}
				proxyLease = nl
				socks = proxyLease.URL
				fmt.Printf("proxy_re-lease after transport err id=%d\n", proxyLease.ID)
				continue
			}
			fatal(err)
		}

		eng = &protocol.Engine{
			Mode:     protocol.ModeLive,
			Bundle:   bundle,
			Client:   client,
			Email:    acc.Email,
			Password: password,
		}
		cur = protocol.Cursor{State: protocol.S0, Email: acc.Email, Password: password}

		restarts := 0
		var authErr error
		for cur.State != protocol.S9 {
			from := cur.State
			var res protocol.StepResult
			cur, res, authErr = stepWithRetry(ctx, eng, cur, 3)
			logStep(from, res, authErr)
			if authErr != nil && restarts < 1 && (strings.Contains(authErr.Error(), "invalid_state") || strings.Contains(authErr.Error(), "S6 status 409") || strings.Contains(authErr.Error(), "invalid_auth_step")) {
				restarts++
				fmt.Printf("auth restart after %s: %v\n", from, authErr)
				cur = protocol.Cursor{State: protocol.S0, Email: acc.Email, Password: password}
				continue
			}
			// Edge CF: remint sticky SID (+ rotate region) and restart S0 on same email.
			// Never retry the challenged request on the same connection (detector is non-retryable).
			if authErr != nil && isEdgeChallenge(authErr) && seedMode && !fixedProxy && edgeBudget > 0 {
				edgeBudget--
				regionIdx = (regionIdx + 1) % len(seedRegions)
				region := seedRegions[regionIdx]
				fmt.Printf("edge_challenge remint budget_left=%d region=%s err=%v\n", edgeBudget, region, authErr)
				ns, nerr := mintSeed(*dbPath, taskID+fmt.Sprintf("_edge%d", edgeBudget), seedStyles, region, *proxyTTL)
				if nerr != nil {
					fmt.Printf("edge remint failed: %v\n", nerr)
					break
				}
				seedSession = ns
				socks = seedSession.URL
				fmt.Printf("proxy_source=proxy_seed style=%s region=%s id=%d\n", seedSession.Style, seedSession.Region, seedSession.ResourceID)
				fmt.Printf("proxy=%s\n", redactProxy(socks))
				if client != nil {
					_ = client.Close()
					client = nil
				}
				// Rebuild TLS client on new SID; keep same email + password.
				proxySnap := transport.ProxySnapshot{
					BridgeURL:        socks,
					BridgeCapability: "direct",
					ExpectedCountry:  seedSession.Region,
				}
				clientOpts := transport.ClientOptions{JobID: taskID, Proxy: proxySnap}
				if bundle != nil {
					clientOpts.TransportID = fmt.Sprintf("%s-%d", strings.ToLower(string(forceBrowser)), bundle.Device.UAMajor)
					if raw, mErr := bundle.MarshalCanonical(); mErr == nil {
						clientOpts.BundleJSON = raw
					}
				}
				var cerr error
				if f, fErr := transport.NewFactory("tls"); fErr == nil {
					if of, ok := f.(transport.OptionsFactory); ok {
						client, cerr = of.NewWithOptions(clientOpts)
					} else {
						client, cerr = f.New(taskID, proxySnap)
					}
				} else {
					client, cerr = transport.DirectSOCKSFactory{}.NewWithOptions(clientOpts)
				}
				if cerr != nil {
					fmt.Printf("edge remint transport failed: %v\n", cerr)
					break
				}
				eng = &protocol.Engine{
					Mode:     protocol.ModeLive,
					Bundle:   bundle,
					Client:   client,
					Email:    acc.Email,
					Password: password,
				}
				select {
				case <-ctx.Done():
					fatal(ctx.Err())
				case <-time.After(800 * time.Millisecond):
				}
				cur = protocol.Cursor{State: protocol.S0, Email: acc.Email, Password: password}
				continue
			}
			if authErr != nil {
				break
			}
		}
		if authErr != nil {
			// already-used / bad email before OTP → try next lease
			if isEmailAlreadyUsedErr(authErr) {
				fmt.Printf("email rejected, re-lease: %v\n", authErr)
				_ = mailbox.MarkUsed(*dbPath, acc.ID, "used", authErr.Error())
				acc = nil
				continue
			}
			if isProxyDialErr(authErr) {
				fmt.Printf("proxy dead, re-lease: %v\n", authErr)
				_ = mailbox.MarkUsed(*dbPath, acc.ID, "available", "proxy failed before register")
				acc = nil
				if proxyLease != nil {
					_ = proxypool.MarkCooldown(*dbPath, proxyLease.ID, authErr.Error())
					proxyLease = nil
				}
				// seed mode: remint SID; exclusive lease mode: re-lease row
				if seedMode && !fixedProxy && strings.TrimSpace(*proxyFile) == "" {
					regionIdx = (regionIdx + 1) % len(seedRegions)
					ns, nerr := mintSeed(*dbPath, taskID+"_px", seedStyles, seedRegions[regionIdx], *proxyTTL)
					if nerr != nil {
						fatal(nerr)
					}
					seedSession = ns
					socks = seedSession.URL
					fmt.Printf("proxy_source=proxy_seed style=%s region=%s id=%d\n", seedSession.Style, seedSession.Region, seedSession.ResourceID)
					fmt.Printf("proxy=%s\n", redactProxy(socks))
					continue
				}
				if strings.TrimSpace(*proxyURL) == "" && strings.TrimSpace(*proxyFile) == "" {
					nl, nerr := proxypool.LeaseFromDBProvider(*dbPath, taskID+"_px", *proxyProvider)
					if nerr != nil {
						fatal(nerr)
					}
					proxyLease = nl
					socks = proxyLease.URL
					fmt.Printf("proxy_source=resource_pool id=%d region=%s exit=%s\n", proxyLease.ID, proxyLease.Region, proxyLease.ExitIP)
					fmt.Printf("proxy=%s\n", redactProxy(socks))
				}
				continue
			}
			_ = mailbox.MarkUsed(*dbPath, acc.ID, "cooldown", authErr.Error())
			writeFail(*outDir, acc.Email, password, authErr)
			fatal(authErr)
		}
		if cur.State != protocol.S9 {
			continue
		}

		fmt.Printf("device_id=%s waiting_for_otp\n", cur.DeviceID)

		// OTP
		code, otpErr := mailbox.WaitForOTP(ctx, acc, *otpTimeout)
		if otpErr != nil {
			status := mailboxStatusForErr(otpErr)
			fmt.Printf("otp failed status=%s: %v\n", status, otpErr)
			_ = mailbox.MarkUsed(*dbPath, acc.ID, status, otpErr.Error())
			// Dead mailbox / no OpenAI mail → re-lease and restart S0 with a fresh email.
			if isOTPMailboxRetryable(otpErr) && try < *emailTries {
				writeFail(*outDir, acc.Email, password, otpErr)
				acc = nil
				continue
			}
			writeFail(*outDir, acc.Email, password, otpErr)
			fatal(otpErr)
		}
		fmt.Printf("otp=****** (len=%d)\n", len(code))
		// brief settle so auth session + mailbox code are both ready
		select {
		case <-ctx.Done():
			fatal(ctx.Err())
		case <-time.After(2500 * time.Millisecond):
		}
		cur.OTPCode = code
		cur.State = protocol.S10
		s10Recovered := false

		// S10 → S14
		var tailErr error
		for {
			from := cur.State
			var res protocol.StepResult
			cur, res, tailErr = stepWithRetry(ctx, eng, cur, 2)
			logStep(from, res, tailErr)
			if tailErr != nil && from == protocol.S10 && !s10Recovered && (strings.Contains(tailErr.Error(), "S10 status 401") || strings.Contains(tailErr.Error(), "S10 status 409")) {
				// one recovery: re-send OTP + wait new code + retry validate once
				s10Recovered = true
				fmt.Println("S10 recovery: resend OTP once")
				cur.State = protocol.S8
				cur.OTPCode = ""
				cur, res, tailErr = eng.Step(ctx, cur)
				logStep(protocol.S8, res, tailErr)
				if tailErr != nil {
					_ = mailbox.MarkUsed(*dbPath, acc.ID, mailboxStatusForErr(tailErr), tailErr.Error())
					writeFail(*outDir, acc.Email, password, tailErr)
					fatal(tailErr)
				}
				// park then wait new code
				cur.State = protocol.S9
				code2, err2 := mailbox.WaitForOTP(ctx, acc, *otpTimeout)
				if err2 != nil {
					status := mailboxStatusForErr(err2)
					_ = mailbox.MarkUsed(*dbPath, acc.ID, status, err2.Error())
					if isOTPMailboxRetryable(err2) && try < *emailTries {
						writeFail(*outDir, acc.Email, password, err2)
						acc = nil
						break
					}
					writeFail(*outDir, acc.Email, password, err2)
					fatal(err2)
				}
				fmt.Printf("otp_resend=****** (len=%d)\n", len(code2))
				cur.OTPCode = code2
				cur.State = protocol.S10
				continue
			}
			if tailErr != nil {
				// S11 create_account already-exists → burn email and re-lease full S0.
				if isEmailAlreadyUsedErr(tailErr) {
					fmt.Printf("email already exists at %s, re-lease: %v\n", from, tailErr)
					_ = mailbox.MarkUsed(*dbPath, acc.ID, "used", tailErr.Error())
					writeFail(*outDir, acc.Email, password, tailErr)
					acc = nil
					break
				}
				_ = mailbox.MarkUsed(*dbPath, acc.ID, mailboxStatusForErr(tailErr), tailErr.Error())
				writeFail(*outDir, acc.Email, password, tailErr)
				fatal(tailErr)
			}
			if res.Stage == "succeeded" || (cur.State == protocol.S14 && strings.TrimSpace(cur.AccessToken) != "") {
				break
			}
			if from == protocol.S14 {
				break
			}
		}
		if acc == nil {
			// re-lease path from OTP/S11
			continue
		}
		if tailErr != nil {
			// fatal paths already exited; defensive
			continue
		}
		if strings.TrimSpace(cur.AccessToken) != "" {
			break // success
		}
		// missing token without error — treat as soft fail and re-lease if tries remain
		miss := fmt.Errorf("missing access_token after S14")
		_ = mailbox.MarkUsed(*dbPath, acc.ID, "cooldown", miss.Error())
		writeFail(*outDir, acc.Email, password, miss)
		if try < *emailTries {
			acc = nil
			continue
		}
		fatal(miss)
	}
	if acc == nil || strings.TrimSpace(cur.AccessToken) == "" {
		fatal(fmt.Errorf("exhausted email tries without access_token"))
	}
	defer client.Close()


	_ = mailbox.MarkUsed(*dbPath, acc.ID, "used", "")
	exitIP := ""
	proxyRegion := ""
	if proxyLease != nil {
		exitIP = proxyLease.ExitIP
		proxyRegion = proxyLease.Region
	}
	out := map[string]any{
		"email":         acc.Email,
		"password":      password,
		"access_token":  cur.AccessToken,
		"account_id":    cur.AccountID,
		"device_id":     cur.DeviceID,
		"user_agent":    bundle.Device.UserAgent,
		"browser":       forceBrowser,
		"registered_at": time.Now().UTC().Format(time.RFC3339),
		"engine":        "pure-go",
		"task_id":       taskID,
		"proxy_exit_ip": exitIP,
		"proxy_region":  proxyRegion,
	}
	if !*noImport {
		pk, ierr := accounts.ImportRegistered(*dbPath, accounts.Record{
			Email:       acc.Email,
			Password:    password,
			AccessToken: cur.AccessToken,
			AccountID:   cur.AccountID,
			DeviceID:    cur.DeviceID,
			UserAgent:   bundle.Device.UserAgent,
			Browser:     string(forceBrowser),
			TaskID:      taskID,
			ProxyExitIP: exitIP,
			ProxyURL:    redactProxy(socks),
			ProxyRegion: proxyRegion,
			Engine:      "pure-go",
			PlanType:    "free",
		})
		if ierr != nil {
			fmt.Printf("import_warn=%v\n", ierr)
			out["import_error"] = ierr.Error()
		} else {
			out["account_pk"] = pk
			fmt.Printf("imported_account_pk=%d\n", pk)
		}
	}
	raw, _ := json.MarshalIndent(out, "", "  ")
	outPath := filepath.Join(*outDir, sanitize(acc.Email)+"_"+time.Now().UTC().Format("20060102_150405")+".json")
	if err := os.WriteFile(outPath, raw, 0o600); err != nil {
		fatal(err)
	}
	fmt.Printf("SUCCESS access_token_len=%d account_id=%s out=%s\n", len(cur.AccessToken), cur.AccountID, outPath)
}

func pickProxyLine(path string, worker int) (string, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	var lines []string
	for _, line := range strings.Split(string(raw), "\n") {
		line = strings.TrimSpace(strings.TrimPrefix(line, "\ufeff"))
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		lines = append(lines, line)
	}
	if len(lines) == 0 {
		return "", fmt.Errorf("no proxy lines in %s", path)
	}
	if worker >= 0 {
		return lines[worker%len(lines)], nil
	}
	return lines[mathrand.IntN(len(lines))], nil
}

func randomPassword() string {
	const letters = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
	b := make([]byte, 14)
	for i := range b {
		b[i] = letters[mathrand.IntN(len(letters))]
	}
	// ensure complexity
	return string(b) + "Aa1!"
}

func shortID() string {
	var b [6]byte
	_, _ = rand.Read(b[:])
	return hex.EncodeToString(b[:])
}

func redactProxy(u string) string {
	// hide password
	if i := strings.Index(u, "://"); i >= 0 {
		rest := u[i+3:]
		if at := strings.LastIndex(rest, "@"); at > 0 {
			userinfo := rest[:at]
			host := rest[at+1:]
			if c := strings.IndexByte(userinfo, ':'); c >= 0 {
				return u[:i+3] + userinfo[:c] + ":***@" + host
			}
		}
	}
	return u
}

func sanitize(s string) string {
	s = strings.ReplaceAll(s, "@", "_at_")
	s = strings.Map(func(r rune) rune {
		if (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9') || r == '_' || r == '-' || r == '.' {
			return r
		}
		return '_'
	}, s)
	return s
}

func writeFail(dir, email, pass string, err error) {
	out := map[string]any{
		"email":    email,
		"password": pass,
		"error":    err.Error(),
		"at":       time.Now().UTC().Format(time.RFC3339),
		"engine":   "pure-go",
	}
	raw, _ := json.MarshalIndent(out, "", "  ")
	_ = os.WriteFile(filepath.Join(dir, "FAIL_"+sanitize(email)+"_"+time.Now().UTC().Format("150405")+".json"), raw, 0o600)
}

func fatal(err error) {
	fmt.Fprintf(os.Stderr, "FATAL: %v\n", err)
	os.Exit(1)
}


func logStep(from protocol.StateID, res protocol.StepResult, err error) {
	if err != nil {
		fmt.Printf("step %s → ERR status=%d: %v\n", from, res.StatusCode, err)
		return
	}
	fmt.Printf("step %s → %s status=%d stage=%s\n", res.From, res.To, res.StatusCode, res.Stage)
}

func stepWithRetry(ctx context.Context, eng *protocol.Engine, cur protocol.Cursor, maxAttempts int) (protocol.Cursor, protocol.StepResult, error) {
	if maxAttempts < 1 {
		maxAttempts = 1
	}
	var (
		out protocol.Cursor
		res protocol.StepResult
		err error
	)
	for attempt := 1; attempt <= maxAttempts; attempt++ {
		out, res, err = eng.Step(ctx, cur)
		if err == nil {
			return out, res, nil
		}
		if !isTransientNet(err) || attempt == maxAttempts {
			return out, res, err
		}
		fmt.Printf("retry step %s attempt=%d/%d err=%v\n", cur.State, attempt, maxAttempts, err)
		select {
		case <-ctx.Done():
			return out, res, ctx.Err()
		case <-time.After(time.Duration(attempt) * 400 * time.Millisecond):
		}
	}
	return out, res, err
}

func isProxyDialErr(err error) bool {
	if err == nil {
		return false
	}
	s := strings.ToLower(err.Error())
	for _, k := range []string{"socks", "general socks server failure", "proxy", "connection refused", "network is unreachable", "no route to host", "i/o timeout", "tls handshake timeout"} {
		if strings.Contains(s, k) {
			return true
		}
	}
	return false
}

// isEmailAlreadyUsedErr matches OpenAI "account already exists" / early email-used signals.
// Used both pre-OTP (S4/S6) and at S11 create_account.
func isEmailAlreadyUsedErr(err error) bool {
	if err == nil {
		return false
	}
	s := strings.ToLower(err.Error())
	markers := []string{
		"user_already_exists",
		"email_already_used",
		"email already exists",
		"account already exists",
		"already have an account",
		"email used",
		"email-verification",
		"email_already_in_use",
		`"code":"email_taken"`,
	}
	for _, m := range markers {
		if strings.Contains(s, m) {
			return true
		}
	}
	// legacy S6 continue_url / invalid_request paths that previously re-leased
	if strings.Contains(s, "invalid_request") || strings.Contains(s, "s6 continue_url") {
		return true
	}
	return false
}

// isOTPMailboxRetryable: no OpenAI mail / graph empty → burn mailbox and try another email.
// Permanent credential failures still re-lease a different account row.
func isOTPMailboxRetryable(err error) bool {
	if err == nil {
		return false
	}
	s := strings.ToLower(err.Error())
	if strings.Contains(s, "graph_no_openai_code") ||
		strings.Contains(s, "graph_empty_inbox") ||
		strings.Contains(s, "no openai mail") ||
		strings.Contains(s, "otp timeout") ||
		strings.Contains(s, "outlook otp timeout") {
		return true
	}
	// bad refresh token on this row — next lease is a different mailbox
	if strings.Contains(s, "invalid_grant") ||
		strings.Contains(s, "invalid refresh") ||
		strings.Contains(s, "unauthorized_client") ||
		strings.Contains(s, "aadsts") {
		return true
	}
	return false
}

// mailboxStatusForErr mirrors job.mailboxStatusForFailure for CLI MarkUsed.
func mailboxStatusForErr(err error) string {
	if err == nil {
		return "cooldown"
	}
	s := strings.ToLower(err.Error())
	switch {
	case strings.Contains(s, "user_already_exists"),
		strings.Contains(s, "already exists"),
		strings.Contains(s, "already used"),
		strings.Contains(s, "deleted or deactivated"):
		return "used"
	case strings.Contains(s, "invalid_grant"),
		strings.Contains(s, "invalid refresh"),
		strings.Contains(s, "unauthorized_client"),
		strings.Contains(s, "aadsts"):
		return "disabled"
	case strings.Contains(s, "graph_no_openai_code"),
		strings.Contains(s, "no openai mail"),
		strings.Contains(s, "otp timeout"):
		// Soft burn: do not re-lease the same dead mailbox soon.
		return "cooldown"
	default:
		return "cooldown"
	}
}


func mintSeed(dbPath, taskID string, styles []string, region string, ttl int) (*proxypool.SeedSession, error) {
	if ttl < 1 {
		ttl = 15
	}
	if len(styles) == 0 {
		styles = []string{"bestgo", "1024"}
	}
	return proxypool.MintSeedSession(dbPath, taskID, styles, region, ttl)
}

func splitCSV(s string) []string {
	parts := strings.Split(s, ",")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		p = strings.TrimSpace(p)
		if p == "" {
			continue
		}
		out = append(out, p)
	}
	return out
}

func isEdgeChallenge(err error) bool {
	if err == nil {
		return false
	}
	var challenge *protocol.EdgeChallengeError
	if errors.As(err, &challenge) {
		return true
	}
	s := strings.ToLower(err.Error())
	return strings.Contains(s, "edge_challenge_required") ||
		strings.Contains(s, "cf-mitigated") ||
		strings.Contains(s, "challenge-platform")
}

func isTransientNet(err error) bool {
	if err == nil {
		return false
	}
	s := strings.ToLower(err.Error())
	for _, k := range []string{"eof", "timeout", "reset", "connection refused", "broken pipe", "tls handshake", "i/o timeout", "temporary", "unavailable"} {
		if strings.Contains(s, k) {
			// do not retry protocol 4xx business errors
			if strings.Contains(s, "protocol: s") && strings.Contains(s, "status") {
				return false
			}
			return true
		}
	}
	return false
}