package job

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/gpt-register/go-email-protocol/internal/cryptostore"
	"github.com/gpt-register/go-email-protocol/internal/ledger"
	"github.com/gpt-register/go-email-protocol/internal/mailbox"
	"github.com/gpt-register/go-email-protocol/internal/protocol"
	proxypool "github.com/gpt-register/go-email-protocol/internal/proxy"
	"github.com/gpt-register/go-email-protocol/internal/session"
	"github.com/gpt-register/go-email-protocol/internal/transport"
)

// runProtocolEngine walks S0→S9 via protocol.Engine, parks waiting_for_otp,
// then after OTP:
//   - mode live: continues S10→S14 on the same Engine/Client/cookies
//   - mode engine/fixture/synthetic: G1 synthetic success (test path)

func classifyProtocolErr(err error) (code string, retryable bool) {
	if err == nil {
		return "protocol_step_failed", true
	}
	// Prefer structured FailureCode embedded in error message prefixes from live steps.
	s := strings.ToLower(err.Error())
	switch {
	case strings.Contains(s, "edge_challenge_required"), strings.Contains(s, "challenge-platform"),
		strings.Contains(s, "cf-mitigated"), strings.Contains(s, "cf_challenge"),
		strings.Contains(s, "just a moment"), strings.Contains(s, "cf-browser-verification"):
		// CF/edge: retryable only at session layer with a new sticky SID (not same connection).
		return "edge_challenge_required", true
	case strings.Contains(s, "session_invalid"), strings.Contains(s, "invalid_state"), strings.Contains(s, "no longer valid"):
		return "session_invalid", true
	case strings.Contains(s, "create_account_server_error"), strings.Contains(s, "s11 status 5"):
		return "create_account_server_error", true
	case strings.Contains(s, "already registered"), strings.Contains(s, "user_already_exists"), strings.Contains(s, "email used"):
		return "email_already_used", false
	case strings.Contains(s, "otp_wrong_code"), strings.Contains(s, "wrong code"):
		return "otp_wrong_code", true
	case isTransientTransportErrString(s):
		return "proxy_or_network", true
	case strings.Contains(s, "http_429"), strings.Contains(s, "status 429"):
		return "http_429", true
	default:
		return "protocol_step_failed", true
	}
}

// isTransientTransportErrString matches Clash/fake-ip/proxy TLS blips seen in L0 capacity.
// These must remint SID rather than terminal-fail as protocol_step_failed / ambiguous_after_send.
func isTransientTransportErrString(s string) bool {
	s = strings.ToLower(s)
	if s == "" {
		return false
	}
	keys := []string{
		"socks", "proxy", "i/o timeout", "deadline exceeded", "unexpected eof",
		": eof", // net/http often ends with ": EOF" (sentinel POST, etc.)
		"connection reset", "tls:", "wsarecv", "wsasend", "broken pipe",
		"server gave http response to https",
		"http: server gave http response to https client",
		"connection attempt failed",
		"forcibly closed",
		"goaway",
		"198.18.",
		"network is unreachable",
		"no route to host",
		"connection refused",
		"tls handshake",
		"use of closed network connection",
	}
	for _, k := range keys {
		if strings.Contains(s, k) {
			return true
		}
	}
	return false
}

func isTransientTransportErr(err error) bool {
	if err == nil {
		return false
	}
	return isTransientTransportErrString(err.Error())
}

// stepFailureCode prefers StepResult.FailureCode when set.
func stepFailureCode(res protocol.StepResult, err error) (code string, retryable bool) {
	if err != nil {
		// Typed edge challenge from doHTTP always wins over generic FailureCode.
		var challenge *protocol.EdgeChallengeError
		if errors.As(err, &challenge) {
			return "edge_challenge_required", true
		}
		if strings.Contains(strings.ToLower(err.Error()), "edge_challenge_required") {
			return "edge_challenge_required", true
		}
		// Transport blips often arrive with FailureCode=ambiguous_after_send / protocol_step_failed
		// and Retryable=false zero-value. Reclassify so remint can fire.
		if isTransientTransportErr(err) {
			return "proxy_or_network", true
		}
	}
	if res.FailureCode != "" {
		retryable = res.Retryable
		if res.FailureCode == "edge_challenge_required" {
			return res.FailureCode, true
		}
		if res.FailureCode == "proxy_or_network" {
			return res.FailureCode, true
		}
		// ambiguous_after_send from lost response: if error text is transport, already handled above.
		if !retryable {
			// default retryable unless explicitly false and known permanent
			if res.FailureCode == "email_already_used" {
				return res.FailureCode, false
			}
			retryable = true
		}
		return res.FailureCode, retryable
	}
	return classifyProtocolErr(err)
}
func (m *Manager) runProtocolEngine(rt *Runtime) {
	ctx := rt.ctx
	mode := protocol.ModeSynthetic
	switch strings.ToLower(strings.TrimSpace(m.runnerCfg.ProtocolMode)) {
	case "fixture":
		mode = protocol.ModeFixture
	case "live":
		mode = protocol.ModeLive
	case "engine":
		mode = protocol.ModeSynthetic
	}
	eng := &protocol.Engine{
		Mode:     mode,
		Bundle:   rt.Bundle,
		Client:   rt.Client,
		Email:    rt.email,
		Password: rt.password,
	}

	_, err := m.led.BumpVersion(ctx, rt.JobID, func(r *ledger.Record) error {
		if r.Status == ledger.StatusCancelling {
			return errSkip
		}
		r.Status = ledger.StatusRunning
		r.Stage = "protocol_s0"
		return nil
	})
	if err != nil {
		if err == errSkip {
			m.finishCancel(rt.JobID)
		}
		return
	}

	if m.runnerCfg.FailInject != "" {
		m.failJob(rt, m.runnerCfg.FailInject, true, "fail inject")
		return
	}
	if m.runnerCfg.HoldInRunning {
		select {
		case <-ctx.Done():
			m.finishCancel(rt.JobID)
		}
		return
	}

	cur := protocol.Cursor{State: protocol.S0, Email: rt.email, Password: rt.password}
	sessionRestarts := 0
	maxSessionRestarts := m.runnerCfg.SessionRemints
	if maxSessionRestarts <= 0 {
		maxSessionRestarts = 2 // canary pure-go-register -edge-remints default
	}
	for cur.State != protocol.S9 {
		select {
		case <-ctx.Done():
			m.finishCancel(rt.JobID)
			return
		default:
		}
		var res protocol.StepResult
		var stepErr error
		cur, res, stepErr = eng.Step(ctx, cur)
		stage := res.Stage
		if stage == "" {
			stage = string(cur.State)
		}
		_, _ = m.led.BumpVersion(context.Background(), rt.JobID, func(r *ledger.Record) error {
			if r.Status == ledger.StatusCancelling {
				return errSkip
			}
			r.Status = ledger.StatusRunning
			r.Stage = "protocol_" + stage
			return nil
		})
		if stepErr != nil {
			code, retryable := stepFailureCode(res, stepErr)
			if code == "" {
				code, retryable = classifyProtocolErr(stepErr)
			}
			errLow := strings.ToLower(stepErr.Error())
			// Full S0 restart budget (canary uses 2):
			// - edge/CF: remint sticky SID + rotate region/style
			// - proxy_or_network / EOF / timeout / HTTP-to-HTTPS / wsarecv: same
			// - session_invalid: restart session (rotate if network-ish)
			transportish := code == "proxy_or_network" || isTransientTransportErrString(errLow)
			needRestart := sessionRestarts < maxSessionRestarts && (code == "session_invalid" || code == "cf_challenge" ||
				code == "edge_challenge_required" || transportish ||
				(retryable && (strings.Contains(errLow, "edge_challenge") ||
					strings.Contains(errLow, "eof") ||
					strings.Contains(errLow, "timeout") ||
					strings.Contains(errLow, "deadline exceeded") ||
					strings.Contains(errLow, "connection reset") ||
					strings.Contains(errLow, "wsarecv") ||
					strings.Contains(errLow, "broken pipe") ||
					strings.Contains(errLow, "server gave http response to https"))))
			if needRestart {
				sessionRestarts++
				_, _ = m.led.BumpVersion(context.Background(), rt.JobID, func(r *ledger.Record) error {
					r.Status = ledger.StatusRunning
					r.Stage = "protocol_s0_restart_" + code
					return nil
				})
				rotate := code == "edge_challenge_required" || transportish ||
					strings.Contains(errLow, "edge_challenge") ||
					strings.Contains(errLow, "eof") ||
					strings.Contains(errLow, "timeout") ||
					strings.Contains(errLow, "deadline exceeded") ||
					strings.Contains(errLow, "connection reset") ||
					strings.Contains(errLow, "wsarecv") ||
					strings.Contains(errLow, "server gave http response to https")
				if rotate {
					if rerr := m.rotateRuntimeProxy(rt); rerr != nil {
						_, _ = m.led.BumpVersion(context.Background(), rt.JobID, func(r *ledger.Record) error {
							r.Stage = "protocol_s0_restart_proxy_keep"
							return nil
						})
					}
				}
				if jar, jerr := session.NewJar(rt.JobID); jerr == nil {
					rt.mu.Lock()
					rt.Jar = jar
					rt.mu.Unlock()
				}
				eng = &protocol.Engine{
					Mode:     mode,
					Bundle:   rt.Bundle,
					Client:   rt.Client,
					Email:    rt.email,
					Password: rt.password,
				}
				select {
				case <-ctx.Done():
					m.finishCancel(rt.JobID)
					return
				case <-time.After(300 * time.Millisecond):
				}
				cur = protocol.Cursor{State: protocol.S0, Email: rt.email, Password: rt.password}
				continue
			}
			m.failJob(rt, code, retryable, stepErr.Error())
			return
		}
		if res.From == res.To && cur.State == protocol.S9 {
			break
		}
	}

	// Stash live engine/cursor for OTP continuation (same TLS session + cookies).
	rt.mu.Lock()
	rt.liveEng = eng
	rt.liveCur = cur
	rt.mu.Unlock()

	// Durable checkpoint: cookies + cursor so worker restart can resume S10+ without full re-auth.
	if mode == protocol.ModeLive {
		_ = m.sealLiveOTPCheckpoint(rt, cur)
	}

	// Park in waiting_for_otp (same durable challenge as synthetic).
	chID := newID("ch")
	_, err = m.led.BumpVersion(context.Background(), rt.JobID, func(r *ledger.Record) error {
		if r.Status == ledger.StatusCancelling {
			return errSkip
		}
		r.Status = ledger.StatusWaitingForOTP
		r.Stage = "waiting_for_otp"
		r.ChallengeID = chID
		r.ChallengeIssuedAt = time.Now().UTC()
		r.ChallengeDeadline = time.Now().UTC().Add(15 * time.Minute)
		r.RetryAfterMS = 1000
		return nil
	})
	if err != nil {
		if err == errSkip {
			m.finishCancel(rt.JobID)
		}
		return
	}
	rt.mu.Lock()
	rt.ChallengeID = chID
	rt.ChallengeVersion = 0
	rt.mu.Unlock()
	if rec, e := m.led.GetByID(context.Background(), rt.JobID); e == nil {
		rt.mu.Lock()
		rt.ChallengeVersion = rec.StateVersion
		rt.version = rec.StateVersion
		rt.mu.Unlock()
	}
	// The live session is checkpointed and remains runtime-owned, but waiting for
	// mail is I/O idle time. Release only admission capacity, never the client,
	// proxy lease, cookie jar, or Engine.
	if mode == protocol.ModeLive {
		m.adm.Release(rt.JobID)
	}
	m.notify(rt.JobID)

	if mode == protocol.ModeLive {
		m.runLiveFromOTP(rt)
		return
	}
	m.runSyntheticFromOTP(rt)
}

// rotateRuntimeProxy mints a new sticky SID from proxy_seed (multi-region + style rotation)
// and rebuilds the TLS client. Same mailbox/email; only egress identity changes.
func (m *Manager) rotateRuntimeProxy(rt *Runtime) error {
	if m == nil || rt == nil {
		return fmt.Errorf("nil runtime")
	}
	if strings.TrimSpace(m.businessDBPath) == "" {
		return fmt.Errorf("business db not configured")
	}
	styles := []string{"bestgo", "1024"}
	// Prefer multi-region rotation (canary: JP,US,DE,GB,BR). Fall back to current country.
	regions := proxypool.DefaultSeedRegions()
	rt.mu.Lock()
	curRegion := strings.TrimSpace(rt.Proxy.ExpectedCountry)
	curStyle := strings.TrimSpace(rt.Proxy.Style)
	if curStyle == "" {
		curStyle = proxypool.StyleFromProxyURL(rt.Proxy.BridgeURL)
	}
	taskHint := fmt.Sprintf("%s-edge-retry-%d", rt.JobID, time.Now().UnixNano()%1_000_000)
	rt.mu.Unlock()
	region := proxypool.NextSeedRegion(regions, curRegion)
	if region == "" {
		region = curRegion
	}
	if region == "" {
		region = "JP"
	}
	preferStyle := proxypool.NextSeedStyle(styles, curStyle)
	seed, err := proxypool.MintSeedSessionPrefer(m.businessDBPath, taskHint, styles, preferStyle, region, 15)
	if err != nil {
		return err
	}
	sum := sha256.Sum256([]byte(taskHint + "\x00" + seed.ResourceKey))
	proxyKey := "sha256:" + hex.EncodeToString(sum[:])
	style := seed.Style
	if style == "" {
		style = preferStyle
	}
	proxy := transport.ProxySnapshot{
		ProxyKey:         proxyKey,
		BridgeURL:        seed.URL,
		BridgeCapability: "direct",
		BridgeGeneration: 1,
		ExpectedCountry:  seed.Region,
		Style:            style,
	}
	var client transport.Client
	var cerr error
	if of, ok := m.factory.(transport.OptionsFactory); ok {
		opts := transport.ClientOptions{JobID: rt.JobID, Proxy: proxy}
		if rt.Bundle != nil {
			opts.TransportID = rt.Bundle.TransportProfileID
			if b, merr := json.Marshal(rt.Bundle); merr == nil {
				opts.BundleJSON = b
			}
		}
		client, cerr = of.NewWithOptions(opts)
	} else {
		client, cerr = m.factory.New(rt.JobID, proxy)
	}
	if cerr != nil {
		return cerr
	}
	rt.mu.Lock()
	old := rt.Client
	rt.Client = client
	rt.Proxy = proxy
	rt.mu.Unlock()
	if old != nil {
		_ = old.Close()
	}
	return nil
}

// runLiveFromOTP waits for OTP (in-worker Graph when mailbox creds present, else SubmitOTP),
// then walks S10→S14 on the parked Engine.
func (m *Manager) runLiveFromOTP(rt *Runtime) {
	ctx := rt.ctx

	// Prefer in-process Graph OTP when outlook/hotmail credentials were sealed at Create.
	// Software path used to block on Python otp_callback; CLI pure-go already used mailbox.WaitForOTP.
	rt.mu.Lock()
	clientID := strings.TrimSpace(rt.mailboxClientID)
	refresh := strings.TrimSpace(rt.mailboxRefreshToken)
	email := strings.TrimSpace(rt.email)
	otpTO := rt.otpTimeout
	rt.mu.Unlock()
	if clientID != "" && refresh != "" && email != "" {
		if otpTO <= 0 {
			otpTO = 120 * time.Second
		}
		acc := &mailbox.Account{
			Email:        email,
			Provider:     mailbox.ProviderOutlookToken,
			ClientID:     clientID,
			RefreshToken: refresh,
		}
		proxyURL := ""
		if rt != nil {
			proxyURL = strings.TrimSpace(rt.Proxy.BridgeURL)
		}
		// Success path returns as soon as code arrives (~30s typical).
		// Dead path: Wait early-aborts ~65s when no OpenAI mail → S8 resend once → short second wait.
		// If OpenAI mail exists but code lags, Wait keeps going (no early abort).
		firstWait := 70 * time.Second
		if firstWait > otpTO*3/5 {
			firstWait = otpTO * 3/5
		}
		if firstWait < 55*time.Second {
			firstWait = 55 * time.Second
		}
		if firstWait > otpTO {
			firstWait = otpTO
		}
		code, err := mailbox.WaitForOTPProxy(ctx, acc, firstWait, proxyURL)
		// Resend on any OTP timeout, including "no openai mail, early abort".
		if err != nil && ctx.Err() == nil && strings.Contains(strings.ToLower(err.Error()), "otp timeout") {
			rt.mu.Lock()
			eng := rt.liveEng
			cur := rt.liveCur
			rt.mu.Unlock()
			if eng != nil {
				cur.State = protocol.S8
				if _, _, rerr := eng.Step(ctx, cur); rerr == nil {
					_, _ = m.led.BumpVersion(context.Background(), rt.JobID, func(r *ledger.Record) error {
						if r.Status != ledger.StatusWaitingForOTP {
							return nil
						}
						r.Stage = "otp_resend_s8"
						return nil
					})
				}
			}
			// After resend: short window only. Success still returns immediately.
			remain := 60 * time.Second
			if otpTO > firstWait {
				if left := otpTO - firstWait; left < remain {
					remain = left
				}
			}
			if remain < 45*time.Second {
				remain = 45 * time.Second
			}
			code, err = mailbox.WaitForOTPProxy(ctx, acc, remain, proxyURL)
		}
		if err != nil {
			m.failJob(rt, "otp_timeout", true, err.Error())
			return
		}
		// Mirror SubmitOTP durable transition so recovery/ledger stay consistent.
		curRec, gerr := m.led.GetByID(context.Background(), rt.JobID)
		if gerr != nil {
			return
		}
		if curRec.Status == ledger.StatusCancelling || curRec.Status == ledger.StatusCancelled {
			m.finishCancel(rt.JobID)
			return
		}
		if curRec.Status != ledger.StatusWaitingForOTP {
			// Already moved (external SubmitOTP race) — fall through if running.
		} else {
			_, terr := m.led.Transition(context.Background(), rt.JobID, curRec.StateVersion, func(r *ledger.Record) error {
				r.Status = ledger.StatusRunning
				r.Stage = "otp_accepted_go"
				r.RetryAfterMS = 500
				return nil
			})
			if terr != nil {
				m.failJob(rt, "otp_accept_failed", true, terr.Error())
				return
			}
		}
		rt.mu.Lock()
		rt.otpCode = code
		rt.mu.Unlock()
	} else {
		// Legacy: wait for Python/external SubmitOTP.
		select {
		case <-ctx.Done():
			m.finishCancel(rt.JobID)
			return
		case <-rt.otpSignal:
		}
	}

	curRec, err := m.led.GetByID(context.Background(), rt.JobID)
	if err != nil {
		return
	}
	if curRec.Status == ledger.StatusCancelling || curRec.Status == ledger.StatusCancelled {
		m.finishCancel(rt.JobID)
		return
	}
	if curRec.Status != ledger.StatusRunning {
		// SubmitOTP / go-otp should have transitioned waiting_for_otp → running
		return
	}

	// S10+ is protocol hot-path work again. Reacquire the original seat without
	// changing its sticky proxy/session; resume waits rather than becoming a fake
	// business failure when the global pool is momentarily full.
	if err := m.acquireRuntimeSeat(ctx, rt); err != nil {
		if ctx.Err() != nil {
			m.finishCancel(rt.JobID)
		} else {
			m.failJob(rt, "admission_resume_failed", true, err.Error())
		}
		return
	}
	rt.mu.Lock()
	eng := rt.liveEng
	cur := rt.liveCur
	code := rt.otpCode
	rt.mu.Unlock()
	if eng == nil {
		m.failJob(rt, "protocol_live_missing_engine", false, "live engine not parked before OTP")
		return
	}
	if strings.TrimSpace(code) == "" {
		m.failJob(rt, "otp_empty", true, "empty OTP code")
		return
	}

	cur.OTPCode = code
	cur.State = protocol.S10
	s10Recovered := false
	postOTPTransportRetried := false
	_, _ = m.led.BumpVersion(context.Background(), rt.JobID, func(r *ledger.Record) error {
		if r.Status == ledger.StatusCancelling {
			return errSkip
		}
		r.Status = ledger.StatusRunning
		r.Stage = "protocol_S10"
		return nil
	})

	for {
		select {
		case <-ctx.Done():
			m.finishCancel(rt.JobID)
			return
		default:
		}
		from := cur.State
		var res protocol.StepResult
		var stepErr error
		cur, res, stepErr = eng.Step(ctx, cur)
		stage := res.Stage
		if stage == "" {
			stage = string(cur.State)
		}
		_, _ = m.led.BumpVersion(context.Background(), rt.JobID, func(r *ledger.Record) error {
			if r.Status == ledger.StatusCancelling {
				return errSkip
			}
			r.Status = ledger.StatusRunning
			r.Stage = "protocol_" + stage
			return nil
		})
		if stepErr != nil {
			failCode, retryable := stepFailureCode(res, stepErr)
			// One S10 recovery: wrong/stale OTP → resend + re-park waiting_for_otp for a new code.
			errLow := strings.ToLower(stepErr.Error())
			isWrongOTP := from == protocol.S10 && !s10Recovered && (failCode == "otp_wrong_code" ||
				strings.Contains(errLow, "s10 status 401") ||
				strings.Contains(errLow, "s10 status 409") ||
				strings.Contains(errLow, "wrong code") ||
				strings.Contains(errLow, "wrong_email_otp"))
			if isWrongOTP {
				s10Recovered = true
				cur.OTPCode = ""
				cur.State = protocol.S8
				_, _ = m.led.BumpVersion(context.Background(), rt.JobID, func(r *ledger.Record) error {
					r.Status = ledger.StatusRunning
					r.Stage = "protocol_S8_resend"
					return nil
				})
				var resSend protocol.StepResult
				cur, resSend, stepErr = eng.Step(ctx, cur)
				if stepErr != nil {
					code2, retryable2 := stepFailureCode(resSend, stepErr)
					m.failJob(rt, code2, retryable2, stepErr.Error())
					return
				}
				// Re-issue durable challenge so Python can SubmitOTP again.
				chID := newID("ch")
				_, errPark := m.led.BumpVersion(context.Background(), rt.JobID, func(r *ledger.Record) error {
					if r.Status == ledger.StatusCancelling {
						return errSkip
					}
					r.Status = ledger.StatusWaitingForOTP
					r.Stage = "waiting_for_otp"
					r.ChallengeID = chID
					r.ChallengeIssuedAt = time.Now().UTC()
					r.ChallengeDeadline = time.Now().UTC().Add(15 * time.Minute)
					r.RetryAfterMS = 1000
					return nil
				})
				if errPark != nil {
					if errPark == errSkip {
						m.finishCancel(rt.JobID)
						return
					}
					m.failJob(rt, "otp_repark_failed", true, errPark.Error())
					return
				}
				rt.mu.Lock()
				rt.liveCur = cur
				rt.otpCode = ""
				// Drain stale signal so we wait for a fresh SubmitOTP.
				select {
				case <-rt.otpSignal:
				default:
				}
				rt.mu.Unlock()
				// The resend has made this runtime mail-idle again. Keep its sticky
				// session alive but let another pre-OTP job use the protocol seat.
				m.adm.Release(rt.JobID)
				m.notify(rt.JobID)
				// Re-fetch OTP: Go Graph when mailbox creds present, else external SubmitOTP.
				rt.mu.Lock()
				clientID2 := strings.TrimSpace(rt.mailboxClientID)
				refresh2 := strings.TrimSpace(rt.mailboxRefreshToken)
				email2 := strings.TrimSpace(rt.email)
				otpTO2 := rt.otpTimeout
				rt.mu.Unlock()
				if clientID2 != "" && refresh2 != "" && email2 != "" {
					if otpTO2 <= 0 {
						otpTO2 = 360 * time.Second
					}
					acc2 := &mailbox.Account{
						Email:        email2,
						Provider:     mailbox.ProviderOutlookToken,
						ClientID:     clientID2,
						RefreshToken: refresh2,
					}
					proxyURL2 := strings.TrimSpace(rt.Proxy.BridgeURL)
					code2, errOTP := mailbox.WaitForOTPProxy(ctx, acc2, otpTO2, proxyURL2)
					if errOTP != nil {
						m.failJob(rt, "otp_timeout", true, errOTP.Error())
						return
					}
					curPark, _ := m.led.GetByID(context.Background(), rt.JobID)
					if curPark != nil && curPark.Status == ledger.StatusWaitingForOTP {
						_, _ = m.led.Transition(context.Background(), rt.JobID, curPark.StateVersion, func(r *ledger.Record) error {
							r.Status = ledger.StatusRunning
							r.Stage = "otp_accepted_go"
							r.RetryAfterMS = 500
							return nil
						})
					}
					rt.mu.Lock()
					rt.otpCode = code2
					rt.mu.Unlock()
				} else {
					select {
					case <-ctx.Done():
						m.finishCancel(rt.JobID)
						return
					case <-rt.otpSignal:
					}
				}
				curRec2, err2 := m.led.GetByID(context.Background(), rt.JobID)
				if err2 != nil {
					return
				}
				if curRec2.Status == ledger.StatusCancelling || curRec2.Status == ledger.StatusCancelled {
					m.finishCancel(rt.JobID)
					return
				}
				if curRec2.Status != ledger.StatusRunning {
					return
				}
				if err := m.acquireRuntimeSeat(ctx, rt); err != nil {
					if ctx.Err() != nil {
						m.finishCancel(rt.JobID)
					} else {
						m.failJob(rt, "admission_resume_failed", true, err.Error())
					}
					return
				}
				rt.mu.Lock()
				code = rt.otpCode
				rt.mu.Unlock()
				if strings.TrimSpace(code) == "" {
					m.failJob(rt, "otp_empty", true, "empty OTP code after S10 recovery")
					return
				}
				cur.OTPCode = code
				cur.State = protocol.S10
				_, _ = m.led.BumpVersion(context.Background(), rt.JobID, func(r *ledger.Record) error {
					r.Status = ledger.StatusRunning
					r.Stage = "protocol_S10"
					return nil
				})
				continue
			}
			// One SO re-mint + retry create_account on S11 5xx.
			if retryable && from == protocol.S11 && failCode == "create_account_server_error" {
				cur.SentinelToken = ""
				cur.SentinelSOToken = ""
				cur.State = protocol.S11
				_, _ = m.led.BumpVersion(context.Background(), rt.JobID, func(r *ledger.Record) error {
					r.Status = ledger.StatusRunning
					r.Stage = "protocol_S11_remint"
					return nil
				})
				var res2 protocol.StepResult
				cur, res2, stepErr = eng.Step(ctx, cur)
				if stepErr == nil {
					res = res2
					// fall through to success checks below
				} else {
					failCode, retryable = stepFailureCode(res2, stepErr)
					m.failJob(rt, failCode, retryable, stepErr.Error())
					return
				}
			} else if !postOTPTransportRetried && from != protocol.S11 &&
				(failCode == "proxy_or_network" || isTransientTransportErrString(errLow)) {
				// Post-OTP transport blip on non-create steps: rebuild client once.
				// Never auto-retry S11 create_account (ambiguous_after_send risk).
				postOTPTransportRetried = true
				if rerr := m.rotateRuntimeProxy(rt); rerr == nil {
					rt.mu.Lock()
					eng.Client = rt.Client
					rt.mu.Unlock()
					_, _ = m.led.BumpVersion(context.Background(), rt.JobID, func(r *ledger.Record) error {
						r.Status = ledger.StatusRunning
						r.Stage = "protocol_post_otp_transport_retry"
						return nil
					})
					cur.State = from
					continue
				}
				m.failJob(rt, failCode, retryable, stepErr.Error())
				return
			} else {
				m.failJob(rt, failCode, retryable, stepErr.Error())
				return
			}
		}
		if res.Stage == "succeeded" || (cur.State == protocol.S14 && strings.TrimSpace(cur.AccessToken) != "") {
			break
		}
		if from == protocol.S14 {
			break
		}
	}

	token := strings.TrimSpace(cur.AccessToken)
	if token == "" {
		m.failJob(rt, "missing_access_token", false, "S14 completed without access_token")
		return
	}

	doc := SessionDocument{
		SchemaVersion: 1,
		Email:         rt.email,
		AccessToken:   token,
		AccountID:     cur.AccountID,
		PlanType:      "free",
		ObtainedAt:    time.Now().UTC(),
		Profile:       rt.Profile,
		Cookies:       []any{},
		Origins:       []any{},
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
	rt.liveCur = cur
	rt.mu.Unlock()

	_, err = m.led.BumpVersion(context.Background(), rt.JobID, func(r *ledger.Record) error {
		if r.Status == ledger.StatusCancelling {
			return errSkip
		}
		if r.Status != ledger.StatusRunning {
			return errSkip
		}
		r.Status = ledger.StatusSucceeded
		r.Stage = "protocol_done"
		r.ResultJSON = resultBytes
		r.SecretBlob = blob
		r.RetryAfterMS = 0
		return nil
	})
	if err != nil {
		if errors.Is(err, errSkip) {
			if cur2, e := m.led.GetByID(context.Background(), rt.JobID); e == nil && cur2.Status == ledger.StatusCancelling {
				m.finishCancel(rt.JobID)
			}
			return
		}
		m.failJob(rt, "ledger_succeed_failed", true, err.Error())
		return
	}
	m.releaseJob(rt.JobID)
	m.notify(rt.JobID)
}
