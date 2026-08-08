package job

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"github.com/gpt-register/go-email-protocol/internal/cryptostore"
	"github.com/gpt-register/go-email-protocol/internal/ledger"
)

// MailatConfig configures the real protocol executor (mailat/codex_register).
// Go owns admission/job/OTP lifecycle; mailat executes the OpenAI register HTTP path.
type MailatConfig struct {
	Enabled   bool
	MailatDir string
	WorkRoot  string
}

func (m *Manager) runMailat(rt *Runtime) {
	cfg := m.runnerCfg.Mailat
	if !cfg.Enabled || strings.TrimSpace(cfg.MailatDir) == "" {
		m.failJob(rt, "mailat_not_configured", true, "mailat runner enabled but MailatDir empty")
		return
	}
	ctx := rt.ctx
	workRoot := cfg.WorkRoot
	if workRoot == "" {
		workRoot = filepath.Join(os.TempDir(), "go-email-protocol-jobs")
	}
	taskDir := filepath.Join(workRoot, rt.JobID)
	if err := os.MkdirAll(taskDir, 0o755); err != nil {
		m.failJob(rt, "workdir_create_failed", true, err.Error())
		return
	}

	_, _ = m.led.BumpVersion(ctx, rt.JobID, func(r *ledger.Record) error {
		if r.Status == ledger.StatusCancelling {
			return errSkip
		}
		r.Status = ledger.StatusRunning
		r.Stage = "mailat_start"
		return nil
	})
	m.notify(rt.JobID)

	proxyURL := strings.TrimSpace(rt.Proxy.BridgeURL)
	mailatCfg := map[string]any{
		"provider":                  "hotmail",
		"defaultProxyUrl":           proxyURL,
		"defaultPassword":           rt.password,
		"loopDelayMs":               30000,
		"gptRegisterExternalEmail":  rt.email,
		"cliproxyApiAutoUploadAuth": false,
	}
	cfgBytes, _ := json.MarshalIndent(mailatCfg, "", "  ")
	if err := os.WriteFile(filepath.Join(taskDir, "config.json"), cfgBytes, 0o600); err != nil {
		m.failJob(rt, "config_write_failed", true, err.Error())
		return
	}

	// Copy sdk.js if present (mailat may expect it beside config).
	sdkSrc := filepath.Join(cfg.MailatDir, "sdk.js")
	if b, err := os.ReadFile(sdkSrc); err == nil {
		_ = os.WriteFile(filepath.Join(taskDir, "sdk.js"), b, 0o644)
	}

	tsx := filepath.Join(cfg.MailatDir, "node_modules", ".bin", "tsx")
	if runtime.GOOS == "windows" {
		tsx += ".cmd"
	}
	entry := filepath.Join(cfg.MailatDir, "src", "index.ts")
	if _, err := os.Stat(tsx); err != nil {
		m.failJob(rt, "tsx_missing", false, fmt.Sprintf("missing tsx: %s", tsx))
		return
	}
	if _, err := os.Stat(entry); err != nil {
		m.failJob(rt, "mailat_entry_missing", false, fmt.Sprintf("missing entry: %s", entry))
		return
	}

	tokenOut := filepath.Join(taskDir, "pool_tokens.txt")
	args := []string{entry, "--at", "--email", rt.email, "--otp", "--gp-token-out", tokenOut}
	// skip_phone is the G1 default for email protocol.
	args = append(args, "--skip-phone")

	cmd := exec.CommandContext(ctx, tsx, args...)
	cmd.Dir = taskDir
	cmd.Env = append(os.Environ(),
		"CODEX_AT_OUT_DIR="+taskDir,
		"CODEX_AUTH_DEBUG=1",
	)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		m.failJob(rt, "pipe_failed", true, err.Error())
		return
	}
	cmd.Stderr = cmd.Stdout
	stdin, err := cmd.StdinPipe()
	if err != nil {
		m.failJob(rt, "stdin_failed", true, err.Error())
		return
	}
	if err := cmd.Start(); err != nil {
		m.failJob(rt, "mailat_start_failed", true, err.Error())
		return
	}

	var output strings.Builder
	reader := bufio.NewReader(stdout)
	otpSent := false
	chID := newID("oc")

	for {
		line, readErr := reader.ReadString('\n')
		if line != "" {
			output.WriteString(line)
			// Keep stage breadcrumbs without secrets.
			trimmed := strings.TrimSpace(line)
			if len(trimmed) > 0 && !strings.Contains(strings.ToLower(trimmed), "password") {
				_, _ = m.led.BumpVersion(context.Background(), rt.JobID, func(r *ledger.Record) error {
					if ledger.Terminal(r.Status) || r.Status == ledger.StatusCancelling {
						return errSkip
					}
					if r.Status == ledger.StatusWaitingForOTP {
						return nil
					}
					r.Status = ledger.StatusRunning
					r.Stage = "mailat_running"
					return nil
				})
			}
			if !otpSent && looksLikeOTPPrompt(line) {
				issued := time.Now().UTC()
				deadline := issued.Add(10 * time.Minute)
				rec2, terr := m.led.BumpVersion(context.Background(), rt.JobID, func(r *ledger.Record) error {
					if r.Status == ledger.StatusCancelling {
						return errSkip
					}
					r.Status = ledger.StatusWaitingForOTP
					r.Stage = "mailat_otp"
					r.ChallengeID = chID
					r.ChallengeIssuedAt = issued
					r.ChallengeDeadline = deadline
					r.RetryAfterMS = 3000
					return nil
				})
				if terr != nil {
					if errors.Is(terr, errSkip) {
						_ = cmd.Process.Kill()
						m.finishCancel(rt.JobID)
						return
					}
				} else {
					rt.ChallengeID = chID
					rt.ChallengeVersion = rec2.StateVersion
					m.notify(rt.JobID)
				}

				// Wait for durable OTP accept (SubmitOTP) then write to mailat stdin.
				select {
				case <-ctx.Done():
					_ = cmd.Process.Kill()
					m.finishCancel(rt.JobID)
					return
				case code := <-rt.otpSignal:
					// Require running status after SubmitOTP transition.
					cur, gerr := m.led.GetByID(context.Background(), rt.JobID)
					if gerr != nil || cur.Status == ledger.StatusCancelling || cur.Status == ledger.StatusCancelled {
						_ = cmd.Process.Kill()
						m.finishCancel(rt.JobID)
						return
					}
					if cur.Status != ledger.StatusRunning && cur.Status != ledger.StatusWaitingForOTP {
						// SubmitOTP should have moved to running; still try if code present.
					}
					if _, werr := io.WriteString(stdin, strings.TrimSpace(code)+"\n"); werr != nil {
						m.failJob(rt, "otp_write_failed", true, werr.Error())
						_ = cmd.Process.Kill()
						return
					}
					otpSent = true
					_, _ = m.led.BumpVersion(context.Background(), rt.JobID, func(r *ledger.Record) error {
						if r.Status == ledger.StatusCancelling {
							return errSkip
						}
						r.Status = ledger.StatusRunning
						r.Stage = "mailat_otp_submitted"
						return nil
					})
					m.notify(rt.JobID)
				}
			}
		}
		if readErr != nil {
			break
		}
	}
	_ = stdin.Close()
	waitErr := cmd.Wait()
	text := output.String()
	_ = os.WriteFile(filepath.Join(taskDir, "mailat.stdout.log"), []byte(text), 0o600)

	if ctx.Err() != nil {
		m.finishCancel(rt.JobID)
		return
	}
	if waitErr != nil {
		msg := waitErr.Error()
		if strings.Contains(text, "unsupported_country") {
			m.failJob(rt, "unsupported_country", true, "OpenAI unsupported_country")
			return
		}
		m.failJob(rt, "mailat_exit_error", true, trimErr(msg+" | "+lastLines(text, 8)))
		return
	}

	parsed := parseMailatStdout(text, taskDir, rt.email)
	token := strings.TrimSpace(parsed["access_token"])
	if token == "" {
		// Also try token out file / auth dumps.
		token = readTokenFallback(taskDir, rt.email)
	}
	if token == "" {
		m.failJob(rt, "missing_access_token", true, "mailat finished without access_token: "+lastLines(text, 12))
		return
	}

	email := parsed["email"]
	if email == "" {
		email = rt.email
	}
	sessionPath := parsed["protocol_session_state_path"]
	var cookies []any
	var origins []any
	if sessionPath != "" {
		if raw, err := os.ReadFile(sessionPath); err == nil {
			var jar map[string]any
			if json.Unmarshal(raw, &jar) == nil {
				if c, ok := jar["cookies"].([]any); ok {
					cookies = c
				}
				if o, ok := jar["origins"].([]any); ok {
					origins = o
				}
			}
		}
	}
	doc := SessionDocument{
		SchemaVersion: 1,
		Email:         email,
		AccessToken:   token,
		AccountID:     parsed["account_id"],
		PlanType:      firstNonEmpty(parsed["plan_type"], "free"),
		ObtainedAt:    time.Now().UTC(),
		Profile:       rt.Profile,
		Cookies:       cookies,
		Origins:       origins,
	}
	redacted := doc
	redacted.AccessToken = ""
	resultBytes, _ := json.Marshal(redacted)
	blob, _ := m.crypto.Seal(cryptostore.Secrets{
		Password:    rt.password,
		Capability:  rt.capability,
		BridgeCap:   rt.bridgeCap,
		AccessToken: token,
	})
	rt.mu.Lock()
	rt.session = &doc
	rt.mu.Unlock()

	_, err = m.led.BumpVersion(context.Background(), rt.JobID, func(r *ledger.Record) error {
		if r.Status == ledger.StatusCancelling {
			return errSkip
		}
		r.Status = ledger.StatusSucceeded
		r.Stage = "mailat_done"
		r.ResultJSON = resultBytes
		r.SecretBlob = blob
		r.RetryAfterMS = 0
		return nil
	})
	if err != nil {
		if errors.Is(err, errSkip) {
			m.finishCancel(rt.JobID)
		}
		return
	}
	m.releaseJob(rt.JobID)
	m.notify(rt.JobID)
}

func (m *Manager) failJob(rt *Runtime, code string, retryable bool, message string) {
	_, _ = m.led.BumpVersion(context.Background(), rt.JobID, func(r *ledger.Record) error {
		if r.Status == ledger.StatusCancelling {
			return errSkip
		}
		r.Status = ledger.StatusFailed
		r.Stage = "failed"
		r.FailureCode = code
		r.Retryable = retryable
		// Store short message in result_json (no secrets).
		r.ResultJSON, _ = json.Marshal(map[string]string{"error": trimErr(message)})
		return nil
	})
	m.releaseJob(rt.JobID)
	m.notify(rt.JobID)
}

func looksLikeOTPPrompt(line string) bool {
	s := strings.ToLower(line)
	return strings.Contains(line, "提交邮箱验证码") ||
		strings.Contains(line, "请输入邮箱验证码") ||
		strings.Contains(s, "manualemailotp") ||
		strings.Contains(s, "enter email") && strings.Contains(s, "code") ||
		strings.Contains(s, "verification code")
}

func parseMailatStdout(text, taskDir, email string) map[string]string {
	out := map[string]string{"email": email}
	for _, line := range strings.Split(text, "\n") {
		trimmed := strings.TrimSpace(line)
		if strings.HasPrefix(trimmed, "[access_token]") {
			out["access_token"] = strings.TrimSpace(strings.TrimPrefix(trimmed, "[access_token]"))
		} else if strings.HasPrefix(trimmed, "[access_token_file]") {
			out["access_token_file"] = strings.TrimSpace(strings.TrimPrefix(trimmed, "[access_token_file]"))
		} else if strings.Contains(trimmed, "注册成功") && strings.Contains(trimmed, "邮箱：") {
			part := strings.SplitN(trimmed, "邮箱：", 2)
			if len(part) == 2 {
				out["email"] = strings.Fields(part[1])[0]
			}
		}
	}
	safe := strings.Map(func(r rune) rune {
		switch r {
		case '<', '>', ':', '"', '/', '\\', '|', '?', '*':
			return '_'
		default:
			if r < 0x20 {
				return '_'
			}
			return r
		}
	}, strings.ToLower(out["email"]))
	session := filepath.Join(taskDir, "email_sessions", safe+".json")
	if st, err := os.Stat(session); err == nil && !st.IsDir() {
		out["protocol_session_state_path"] = session
	}
	return out
}

func readTokenFallback(taskDir, email string) string {
	// auth/at/*.json dumps
	authDir := filepath.Join(taskDir, "auth", "at")
	entries, err := os.ReadDir(authDir)
	if err == nil {
		for _, e := range entries {
			if e.IsDir() {
				continue
			}
			raw, err := os.ReadFile(filepath.Join(authDir, e.Name()))
			if err != nil {
				continue
			}
			var m map[string]any
			if json.Unmarshal(raw, &m) != nil {
				continue
			}
			for _, k := range []string{"access_token", "accessToken", "token"} {
				if v, ok := m[k].(string); ok && strings.Count(v, ".") >= 2 {
					return v
				}
			}
		}
	}
	// pool_tokens.txt lines
	if raw, err := os.ReadFile(filepath.Join(taskDir, "pool_tokens.txt")); err == nil {
		for _, line := range strings.Split(string(raw), "\n") {
			line = strings.TrimSpace(line)
			if strings.Count(line, ".") >= 2 && len(line) > 40 {
				return line
			}
		}
	}
	_ = email
	return ""
}

func lastLines(text string, n int) string {
	lines := strings.Split(text, "\n")
	if len(lines) <= n {
		return strings.TrimSpace(text)
	}
	return strings.TrimSpace(strings.Join(lines[len(lines)-n:], "\n"))
}

func trimErr(s string) string {
	s = strings.TrimSpace(s)
	if len(s) > 500 {
		return s[:500]
	}
	return s
}

func firstNonEmpty(vals ...string) string {
	for _, v := range vals {
		if strings.TrimSpace(v) != "" {
			return v
		}
	}
	return ""
}
