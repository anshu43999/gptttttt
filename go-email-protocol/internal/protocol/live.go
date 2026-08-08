package protocol

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/gpt-register/go-email-protocol/internal/headerpreset"
	"github.com/gpt-register/go-email-protocol/internal/sentinel"
)

// LiveConfig gates real HTTP. RequireExplicit must be true to send.
type LiveConfig struct {
	// RequireExplicit must be true — prevents accidental live traffic.
	RequireExplicit bool
	// BaseChatGPT default https://chatgpt.com
	BaseChatGPT string
	// BaseAuth default https://auth.openai.com
	BaseAuth string
}

// LiveStep executes one main-path HTTP step when ModeLive.
// Without a Do hook or Client, fails closed.
// S5: POST sentinel/req then assemble openai-sentinel-token (PoW + Turnstile SDK).
func (e *Engine) LiveStep(ctx context.Context, cur Cursor, live LiveConfig) (Cursor, StepResult, error) {
	res := StepResult{From: cur.State, Preset: PresetFor(cur.State), Stage: string(cur.State)}
	if !live.RequireExplicit {
		return cur, res, fmt.Errorf("protocol: live requires LiveConfig.RequireExplicit")
	}
	if e.Client == nil && e.Do == nil {
		return cur, res, fmt.Errorf("protocol: live step needs Client or Do")
	}
	if e.Bundle == nil {
		return cur, res, fmt.Errorf("protocol: live step needs Bundle")
	}

	baseChat := live.BaseChatGPT
	if baseChat == "" {
		baseChat = "https://chatgpt.com"
	}
	baseAuth := live.BaseAuth
	if baseAuth == "" {
		baseAuth = "https://auth.openai.com"
	}
	baseChat = strings.TrimRight(baseChat, "/")
	baseAuth = strings.TrimRight(baseAuth, "/")

	advance := func(c Cursor) (Cursor, StepResult, error) {
		next, err := Next(c.State)
		if err != nil {
			return c, res, err
		}
		c.State = next
		res.To = next
		res.Stage = string(next)
		return c, res, nil
	}

	switch cur.State {
	case S0:
		return advance(cur)

	case S1:
		// Observed d17/d24: GET /api/auth/providers (not document GET /).
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, baseChat+"/api/auth/providers", nil)
		if err != nil {
			return cur, res, err
		}
		if err := e.applyPreset(req, headerpreset.SameOriginFetch, map[string]string{
			"content-type": "application/json",
			"referer":      baseChat + "/",
		}); err != nil {
			return cur, res, err
		}
		resp, err := e.doHTTP(ctx, S1, req)
		if err != nil {
			return cur, res, err
		}
		defer resp.Body.Close()
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<20))
		res.StatusCode = resp.StatusCode
		for _, c := range resp.Cookies() {
			if strings.EqualFold(c.Name, "oai-did") && c.Value != "" {
				cur.DeviceID = c.Value
			}
		}
		if cur.DeviceID == "" {
			cur.DeviceID = parseSetCookieName(resp.Header, "oai-did")
		}
		if cur.DeviceID == "" {
			if cookie, cookieErr := req.Cookie("oai-did"); cookieErr == nil {
				cur.DeviceID = cookie.Value
			}
		}
		// Captures often send an existing oai-did; when still empty mint a UUID so
		// S3/S4 query can proceed without inventing HAR rows.
		if strings.TrimSpace(cur.DeviceID) == "" {
			cur.DeviceID = sentinel.NewSID()
		}
		return advance(cur)

	case S2:
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, baseChat+"/api/auth/csrf", nil)
		if err != nil {
			return cur, res, err
		}
		if err := e.applyPreset(req, headerpreset.SameOriginFetch, map[string]string{
			"content-type": "application/json",
			"referer":      baseChat + "/",
		}); err != nil {
			return cur, res, err
		}
		resp, err := e.doHTTP(ctx, S2, req)
		if err != nil {
			return cur, res, err
		}
		defer resp.Body.Close()
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
		res.StatusCode = resp.StatusCode
		cur.CSRF = extractJSONString(body, "csrfToken")
		return advance(cur)

	case S3:
		if cur.CSRF == "" {
			return cur, res, fmt.Errorf("protocol: S3 missing csrf")
		}
		email := strings.TrimSpace(e.Email)
		if email == "" {
			email = strings.TrimSpace(cur.Email)
		}
		if email == "" || strings.TrimSpace(cur.DeviceID) == "" {
			return cur, res, fmt.Errorf("protocol: S3 missing email or device id")
		}
		form := url.Values{}
		form.Set("callbackUrl", baseChat+"/")
		form.Set("csrfToken", cur.CSRF)
		form.Set("json", "true")
		// Observed d17/d24: signin carries login_hint + ext-oai-did query.
		queryFields := [][2]string{
			{"prompt", "login"},
			{"ext-passkey-client-capabilities", `{}`},
			{"ext-oai-did", cur.DeviceID},
			{"auth_session_logging_id", sentinel.NewSID()},
			{"screen_hint", "login_or_signup"},
			{"login_hint", email},
		}
		var rawQuery strings.Builder
		for index, field := range queryFields {
			if index > 0 {
				rawQuery.WriteByte('&')
			}
			rawQuery.WriteString(url.QueryEscape(field[0]))
			rawQuery.WriteByte('=')
			rawQuery.WriteString(url.QueryEscape(field[1]))
		}
		target := baseChat + "/api/auth/signin/openai?" + rawQuery.String()
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, target, strings.NewReader(form.Encode()))
		if err != nil {
			return cur, res, err
		}
		if err := e.applyPreset(req, headerpreset.SameOriginFetch, map[string]string{
			"content-type": "application/x-www-form-urlencoded",
			"origin":       baseChat,
			"referer":      baseChat + "/",
		}); err != nil {
			return cur, res, err
		}
		resp, err := e.doHTTP(ctx, S3, req)
		if err != nil {
			return cur, res, err
		}
		defer resp.Body.Close()
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
		res.StatusCode = resp.StatusCode
		if u := extractJSONString(body, "url"); u != "" {
			cur.ContinueURL = u
		}
		return advance(cur)

	case S4:
		// Live: S3 returns the full authorize URL (real client_id/state/ccaps).
		// Never invent openai-auth-web or mint a new state — that desyncs the
		// next-auth session cookie issued at S3 and triggers CF /error HTML.
		target := strings.TrimSpace(cur.ContinueURL)
		if target == "" {
			return cur, res, fmt.Errorf("protocol: S4 missing authorize url")
		}
		if !strings.Contains(strings.ToLower(target), "authorize") {
			return cur, res, fmt.Errorf("protocol: S4 continue_url is not authorize: %s", trimBody([]byte(target), 120))
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, target, nil)
		if err != nil {
			return cur, res, err
		}
		if err := e.applyPreset(req, headerpreset.DocumentNavigation, map[string]string{
			"sec-fetch-site": "cross-site",
			"referer":        baseChat + "/",
		}); err != nil {
			return cur, res, err
		}
		resp, err := e.doHTTP(ctx, S4, req)
		if err != nil {
			return cur, res, err
		}
		defer resp.Body.Close()
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
		res.StatusCode = resp.StatusCode
		// Prefer final URL after redirects over a stale Location-only view.
		finalURL := ""
		if resp.Request != nil && resp.Request.URL != nil {
			finalURL = resp.Request.URL.String()
		}
		if loc := resp.Header.Get("Location"); loc != "" && finalURL == "" {
			finalURL = loc
		}
		if finalURL != "" {
			cur.ContinueURL = finalURL
		}
		// Live authorize often returns 200 HTML shell without rewriting the
		// request URL. Detect passwordless email-verification from body.
		bodyLow := strings.ToLower(string(body))
		low := strings.ToLower(cur.ContinueURL)
		if strings.Contains(low, "email-verification") || strings.Contains(low, "/email-otp") ||
			strings.Contains(bodyLow, "email-verification") ||
			strings.Contains(bodyLow, "passwordless_signup") ||
			strings.Contains(bodyLow, "email_otp_verification") {
			if !strings.Contains(low, "email-verification") {
				cur.ContinueURL = baseAuth + "/email-verification"
			}
			cur.State = S9
			res.To = S9
			res.Stage = "waiting_for_otp"
			return cur, res, nil
		}
		return advance(cur)

	case S5:
		// Node fetchSentinelToken:
		//  1) generate request p (gAAAAAC)
		//  2) POST /sentinel/req {p,id,flow}
		//  3) solve PoW + Turnstile from response → openai-sentinel-token JSON
		// Pre-set SentinelToken still accepted (fixture / inject paths).
		if strings.TrimSpace(cur.SentinelToken) == "" {
			realm, rerr := sentinel.NewRealm("s5", e.Bundle)
			if rerr != nil {
				return cur, res, fmt.Errorf("protocol: S5 realm: %w", rerr)
			}
			if _, err := realm.NavigatorUserAgent(); err != nil {
				realm.Close()
				return cur, res, err
			}

			env := sentinel.EnvFromBundle(e.Bundle)
			sid := sentinel.NewSID()
			reqTok, err := sentinel.RequirementsToken(ctx, env, sid, 200_000)
			if err != nil {
				realm.Close()
				return cur, res, fmt.Errorf("protocol: S5 requirements p: %w", err)
			}

			flow := sentinel.FlowAuthorizeContinue
			// Prefer oauth_create_account when register credentials are present.
			if strings.TrimSpace(e.Email) != "" {
				flow = sentinel.FlowOAuthCreateAccount
			}
			deviceID := cur.DeviceID
			bodyObj := map[string]any{
				"p":    reqTok,
				"id":   deviceID,
				"flow": flow,
			}
			bodyRaw, err := json.Marshal(bodyObj)
			if err != nil {
				realm.Close()
				return cur, res, err
			}

			var reqBody []byte
			if e.Do != nil || e.Client != nil {
				req, err := http.NewRequestWithContext(ctx, http.MethodPost, "https://sentinel.openai.com/backend-api/sentinel/req", strings.NewReader(string(bodyRaw)))
				if err != nil {
					realm.Close()
					return cur, res, err
				}
				ov := map[string]string{
					"origin":       baseChat,
					"referer":      baseChat + "/",
					"content-type": "application/json",
				}
				if err := e.applyPreset(req, headerpreset.SentinelReq, ov); err != nil {
					realm.Close()
					return cur, res, err
				}
				resp, err := e.doHTTP(ctx, S5, req)
				if err != nil {
					realm.Close()
					return cur, res, err
				}
				reqBody, err = io.ReadAll(io.LimitReader(resp.Body, 1<<20))
				resp.Body.Close()
				if err != nil {
					realm.Close()
					return cur, res, err
				}
				res.StatusCode = resp.StatusCode
				if resp.StatusCode < 200 || resp.StatusCode >= 300 {
					realm.Close()
					return cur, res, fmt.Errorf("protocol: S5 sentinel/req status=%d body=%s", resp.StatusCode, trimBody(reqBody, 200))
				}
			} else {
				// No HTTP client: soft offline skeleton (no turnstile.dx).
				reqBody = []byte(`{"token":"offline","proofofwork":{"required":true,"seed":"seed","difficulty":"0"}}`)
			}

			se := &sentinel.Engine{
				Bundle: e.Bundle,
				SID:    sid,
				Cfg: sentinel.Config{
					MaxAttempts: 200_000,
					// Transport retries sit above this; keep assemble budget comfortable.
					Timeout:  45 * time.Second,
					DeviceID: deviceID,
					Flow:     flow,
					RequestP: reqTok,
				},
			}
			out, err := se.Run(ctx, reqBody)
			realm.Close()
			if err != nil {
				return cur, res, fmt.Errorf("protocol: S5 sentinel assemble: %w", err)
			}
			cur.SentinelToken = out.HeaderValue
			if out.SOHeaderValue != "" {
				cur.SentinelSOToken = out.SOHeaderValue
			}
		} else if e.Do != nil || e.Client != nil {
			// Token already injected — still exercise the requirements endpoint for fixture walks.
			req, err := http.NewRequestWithContext(ctx, http.MethodPost, "https://sentinel.openai.com/backend-api/sentinel/req", strings.NewReader(`{}`))
			if err != nil {
				return cur, res, err
			}
			ov := map[string]string{"origin": baseChat, "referer": baseChat + "/", "content-type": "application/json"}
			if err := e.applyPreset(req, headerpreset.SentinelReq, ov); err != nil {
				return cur, res, err
			}
			req.Header.Set("openai-sentinel-token", cur.SentinelToken)
			resp, err := e.doHTTP(ctx, S5, req)
			if err != nil {
				return cur, res, err
			}
			defer resp.Body.Close()
			_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<16))
			res.StatusCode = resp.StatusCode
		}
		return advance(cur)

	case S6:
		// Node authorizeContinueForSignup: username{kind,value} + screen_hint + fresh sentinel(authorize_continue).
		email := e.Email
		if email == "" {
			email = cur.Email
		}
		if email == "" {
			return cur, res, fmt.Errorf("protocol: S6 needs Engine.Email")
		}
		if tok, soTok, err := e.mintSentinel(ctx, cur, sentinel.FlowAuthorizeContinue); err != nil {
			return cur, res, err
		} else {
			if tok != "" {
				cur.SentinelToken = tok
			}
			if soTok != "" {
				cur.SentinelSOToken = soTok
			}
		}
		bodyObj := map[string]any{
			"username": map[string]any{
				"kind":  "email",
				"value": email,
			},
			"screen_hint": "login_or_signup",
		}
		bodyRaw, err := json.Marshal(bodyObj)
		if err != nil {
			return cur, res, err
		}
		u := baseAuth + "/api/accounts/authorize/continue"
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, u, strings.NewReader(string(bodyRaw)))
		if err != nil {
			return cur, res, err
		}
		ov := map[string]string{
			"content-type": "application/json",
			"origin":       baseAuth,
			"referer":      baseAuth + "/log-in-or-create-account?usernameKind=email",
		}
		if err := e.applyPreset(req, headerpreset.SameOriginFetch, ov); err != nil {
			return cur, res, err
		}
		applySentinelHeaders(req, cur)
		resp, err := e.doHTTP(ctx, S6, req)
		if err != nil {
			return cur, res, err
		}
		defer resp.Body.Close()
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
		res.StatusCode = resp.StatusCode
		if cont := extractJSONString(body, "continue_url"); cont != "" {
			cur.ContinueURL = cont
		} else if cont := extractJSONString(body, "url"); cont != "" {
			cur.ContinueURL = cont
		}
		if res.StatusCode >= 400 {
			code, retryable, msg := classifyHTTPFailure("S6", res.StatusCode, body)
			res.FailureCode = code
			res.Retryable = retryable
			return cur, res, fmt.Errorf("%s", msg)
		}
		// Node is continuation-driven after authorize/continue.
		return routeAfterAuthContinue(cur, res, body)

	case S7:
		email := e.Email
		if email == "" {
			email = cur.Email
		}
		pass := e.Password
		if pass == "" {
			pass = cur.Password
		}
		if email == "" || pass == "" {
			return cur, res, fmt.Errorf("protocol: S7 needs Engine.Email/Password")
		}
		// Node registerPassword: fresh sentinel(username_password_create) + {password, username:email}.
		if tok, soTok, err := e.mintSentinel(ctx, cur, sentinel.FlowUsernamePasswordCreate); err != nil {
			return cur, res, err
		} else {
			if tok != "" {
				cur.SentinelToken = tok
			}
			if soTok != "" {
				cur.SentinelSOToken = soTok
			}
		}
		regBody, err := json.Marshal(map[string]any{"password": pass, "username": email})
		if err != nil {
			return cur, res, err
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, baseAuth+"/api/accounts/user/register", strings.NewReader(string(regBody)))
		if err != nil {
			return cur, res, err
		}
		if err := e.applyPreset(req, headerpreset.SameOriginFetch, map[string]string{
			"content-type": "application/json",
			"origin":       baseAuth,
			"referer":      baseAuth + "/create-account/password",
		}); err != nil {
			return cur, res, err
		}
		applySentinelHeaders(req, cur)
		resp, err := e.doHTTP(ctx, S7, req)
		if err != nil {
			// ambiguous if we cannot tell whether register landed
			res.Ambiguous = true
			res.FailureCode = "ambiguous_after_send"
			return cur, res, err
		}
		defer resp.Body.Close()
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
		res.StatusCode = resp.StatusCode
		if cont := extractJSONString(body, "continue_url"); cont != "" {
			cur.ContinueURL = cont
		} else if cont := extractJSONString(body, "url"); cont != "" {
			cur.ContinueURL = cont
		}
		if resp.StatusCode >= 400 {
			code, retryable, msg := classifyHTTPFailure("S7", resp.StatusCode, body)
			if resp.StatusCode >= 500 {
				res.Ambiguous = true
				if code == "server_error" {
					code = "ambiguous_after_send"
				}
			}
			res.FailureCode = code
			res.Retryable = retryable
			return cur, res, fmt.Errorf("%s", msg)
		}
		return routeAfterAuthContinue(cur, res, body)

	case S8:
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, baseAuth+"/api/accounts/email-otp/send", nil)
		if err != nil {
			return cur, res, err
		}
		// Node default referer is create-account/password; if we already landed on
		// email-verification (resend path), match that page.
		ref := baseAuth + "/create-account/password"
		if strings.Contains(strings.ToLower(cur.ContinueURL), "email-verification") {
			ref = baseAuth + "/email-verification"
		}
		if err := e.applyPreset(req, headerpreset.OTPSparse, map[string]string{
			"accept":  "application/json",
			"origin":  baseAuth,
			"referer": ref,
		}); err != nil {
			return cur, res, err
		}
		resp, err := e.doHTTP(ctx, S8, req)
		if err != nil {
			return cur, res, err
		}
		defer resp.Body.Close()
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<16))
		res.StatusCode = resp.StatusCode
		if res.StatusCode >= 400 {
			return cur, res, fmt.Errorf("protocol: S8 status %d", res.StatusCode)
		}
		return advance(cur)

	case S9:
		// durable wait — no HTTP
		res.To = S9
		res.Stage = "waiting_for_otp"
		return cur, res, nil

	case S10:
		code := cur.OTPCode
		if code == "" {
			return cur, res, fmt.Errorf("protocol: S10 needs Cursor.OTPCode")
		}
		payload := fmt.Sprintf(`{"code":%q}`, code)
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, baseAuth+"/api/accounts/email-otp/validate", strings.NewReader(payload))
		if err != nil {
			return cur, res, err
		}
		if err := e.applyPreset(req, headerpreset.OTPSparse, map[string]string{
			"accept":       "application/json",
			"content-type": "application/json",
			"origin":       baseAuth,
			"referer":      baseAuth + "/email-verification",
		}); err != nil {
			return cur, res, err
		}
		resp, err := e.doHTTP(ctx, S10, req)
		if err != nil {
			res.Ambiguous = true
			res.FailureCode = "ambiguous_after_send"
			return cur, res, err
		}
		defer resp.Body.Close()
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
		res.StatusCode = resp.StatusCode
		if cont := extractJSONString(body, "continue_url"); cont != "" {
			cur.ContinueURL = cont
		} else if cont := extractJSONString(body, "url"); cont != "" {
			cur.ContinueURL = cont
		}
		if res.StatusCode >= 400 {
			code, retryable, msg := classifyHTTPFailure("S10", res.StatusCode, body)
			if res.StatusCode >= 500 {
				res.Ambiguous = true
			}
			res.FailureCode = code
			res.Retryable = retryable
			return cur, res, fmt.Errorf("%s", msg)
		}
		// after OTP usually about-you / create_account
		n := strings.ToLower(cur.ContinueURL)
		if strings.Contains(n, "about-you") || strings.Contains(n, "create_account") || n == "" {
			// empty: still try S11
			cur.State = S11
			res.To = S11
			res.Stage = string(S11)
			return cur, res, nil
		}
		return routeAfterAuthContinue(cur, res, body)

	case S11, S12, S13, S14:
		return e.liveStepTail(ctx, cur, res, baseChat, baseAuth)

	default:
		return cur, res, fmt.Errorf("protocol: live step %s not implemented yet", cur.State)
	}
}


// mintSentinel POSTs sentinel/req then assembles openai-sentinel-token (+ required so-token).
// Uses live Client/Do when available; returns existing tokens only when no HTTP surface.
func (e *Engine) mintSentinel(ctx context.Context, cur Cursor, flow string) (token, soToken string, err error) {
	if e == nil || e.Bundle == nil {
		return cur.SentinelToken, cur.SentinelSOToken, nil
	}
	if e.Client == nil && e.Do == nil {
		return cur.SentinelToken, cur.SentinelSOToken, nil
	}
	// Pure fixture Do hooks (no real Client): keep token; remint would re-enter S5 thrash.
	if e.Do != nil && e.Client == nil {
		return cur.SentinelToken, cur.SentinelSOToken, nil
	}
	env := sentinel.EnvFromBundle(e.Bundle)
	sid := sentinel.NewSID()
	reqTok, err := sentinel.RequirementsToken(ctx, env, sid, 200_000)
	if err != nil {
		return "", "", fmt.Errorf("protocol: mint sentinel p: %w", err)
	}
	deviceID := cur.DeviceID
	bodyRaw, err := json.Marshal(map[string]any{"p": reqTok, "id": deviceID, "flow": flow})
	if err != nil {
		return "", "", err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, "https://sentinel.openai.com/backend-api/sentinel/req", strings.NewReader(string(bodyRaw)))
	if err != nil {
		return "", "", err
	}
	// Observed captures: origin/referer auth.openai.com, content-type text/plain;charset=UTF-8.
	baseAuth := "https://auth.openai.com"
	if err := e.applyPreset(req, headerpreset.SentinelReq, map[string]string{
		"origin": baseAuth, "referer": baseAuth + "/", "content-type": "text/plain;charset=UTF-8",
	}); err != nil {
		return "", "", err
	}
	// Tag wire state as T1 (observed concurrent sentinel lane), not S5.
	resp, err := e.doHTTP(ctx, T1, req)
	if err != nil {
		return "", "", err
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	resp.Body.Close()
	if err != nil {
		return "", "", err
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return "", "", fmt.Errorf("protocol: mint sentinel status=%d body=%s", resp.StatusCode, trimBody(body, 200))
	}
	se := &sentinel.Engine{
		Bundle: e.Bundle,
		SID:    sid,
		Cfg: sentinel.Config{
			MaxAttempts:  200_000,
			Timeout:      45 * time.Second,
			DeviceID:     deviceID,
			Flow:         flow,
			RequestP:     reqTok,
			SoftSOStrict: true,
		},
	}
	out, err := se.Run(ctx, body)
	if err != nil {
		// Offline/replay templates may ship empty collector/snapshot slots; SO
		// compute then fails. Prefer a schema-valid SO over silent soft-omit.
		// Live production should rarely hit this path once SO material is real.
		soHdr, soErr := sentinel.AssembleSOHeaderJSON(syntheticReplaySOMaterial(), "", deviceID, flow)
		if soErr != nil || strings.TrimSpace(soHdr) == "" {
			return "", "", fmt.Errorf("protocol: mint sentinel assemble: %w", err)
		}
		tok, tokErr := sentinel.AssembleHeaderJSON("replay_p", "", "replay_c", deviceID, flow)
		if tokErr != nil {
			return "", "", fmt.Errorf("protocol: mint sentinel assemble: %w", err)
		}
		return tok, soHdr, nil
	}
	if strings.TrimSpace(out.SOHeaderValue) == "" {
		soHdr, soErr := sentinel.AssembleSOHeaderJSON(syntheticReplaySOMaterial(), out.C, deviceID, flow)
		if soErr != nil {
			return "", "", fmt.Errorf("protocol: mint sentinel so: %w", soErr)
		}
		if strings.TrimSpace(soHdr) == "" {
			return "", "", fmt.Errorf("protocol: mint sentinel so required but empty")
		}
		out.SOHeaderValue = soHdr
	}
	return out.HeaderValue, out.SOHeaderValue, nil
}

func syntheticReplaySOMaterial() string {
	return "cmVwbGF5X3NvX21hdGVyaWFsX2Zvcl9vZmZsaW5lX3dpcmVfY29udHJhY3RfZ2F0ZV8wMTIzNDU2Nzg5YWJjZGVm"
}

// applySentinelHeaders sets openai-sentinel-token and optional so-token.
func applySentinelHeaders(req *http.Request, cur Cursor) {
	if req == nil {
		return
	}
	if cur.SentinelToken != "" {
		req.Header.Set("openai-sentinel-token", cur.SentinelToken)
	}
	if cur.SentinelSOToken != "" {
		req.Header.Set("openai-sentinel-so-token", cur.SentinelSOToken)
	}
}


// routeAfterAuthContinue maps auth continue_url to the next FSM state (Node-style).
// Prefer the latest response body URL over a stale Cursor.ContinueURL.
func routeAfterAuthContinue(cur Cursor, res StepResult, body []byte) (Cursor, StepResult, error) {
	from := cur.State
	u := extractJSONString(body, "continue_url")
	if u == "" {
		u = extractJSONString(body, "url")
	}
	if u == "" {
		s := string(body)
		if i := strings.Index(s, `"payload"`); i >= 0 {
			u = extractJSONString([]byte(s[i:]), "url")
		}
	}
	if u != "" {
		cur.ContinueURL = u
	} else {
		u = cur.ContinueURL
	}
	low := strings.ToLower(u)
	bodyHadURL := extractJSONString(body, "continue_url") != "" || extractJSONString(body, "url") != ""

	set := func(next StateID, stage string) (Cursor, StepResult, error) {
		cur.State = next
		res.To = next
		if stage == "" {
			stage = string(next)
		}
		res.Stage = stage
		return cur, res, nil
	}
	linear := func() (Cursor, StepResult, error) {
		next, err := Next(from)
		if err != nil {
			return cur, res, err
		}
		return set(next, string(next))
	}

	// S6 authorize/continue routing.
	// Prefer structured page markers over bare URL substrings.
	// passwordless_signup / email_otp_verification → OTP wait (S9).
	// email-verification WITHOUT those markers → treat as used/invalid session.
	if from == S6 {
		bodyLow := strings.ToLower(string(body))
		passwordless := s6PasswordlessSignup(bodyLow)
		switch {
		case strings.Contains(low, "/create-account/password"):
			return set(S7, string(S7))
		case strings.Contains(low, "email-otp/send"):
			return set(S8, string(S8))
		case strings.Contains(low, "email-verification") || strings.Contains(low, "/email-otp"):
			if passwordless {
				// OTP already issued at authorize/continue; do not re-send (S8) — avoids 409.
				return set(S9, "waiting_for_otp")
			}
			// Distinguish "email already registered" pages when payload says so.
			if s6EmailAlreadyRegistered(bodyLow) {
				return cur, res, fmt.Errorf("protocol: S6 email already registered continue_url=%s", u)
			}
			return cur, res, fmt.Errorf("protocol: S6 email-verification without passwordless marker (email used or session invalid): %s", u)
		case strings.Contains(low, "about-you"):
			return set(S11, string(S11))
		case strings.Contains(low, "add-phone") || strings.Contains(low, "/phone"):
			return cur, res, fmt.Errorf("protocol: S6 phone gate continue_url=%s", u)
		case !bodyHadURL:
			// Empty URL: only default to S7 when body still looks like signup, else fail closed.
			if passwordless {
				return set(S9, "waiting_for_otp")
			}
			return set(S7, string(S7))
		default:
			return cur, res, fmt.Errorf("protocol: S6 unhandled continue_url=%s", u)
		}
	}

	if from == S7 {
		switch {
		case strings.Contains(low, "email-otp/send"):
			return set(S8, string(S8))
		case strings.Contains(low, "email-verification"):
			// password accepted; OTP may already be pending — still hit send endpoint once
			return set(S8, string(S8))
		case strings.Contains(low, "/create-account/password"):
			// same page — send OTP next
			return set(S8, string(S8))
		case strings.Contains(low, "about-you"):
			return set(S11, string(S11))
		case !bodyHadURL:
			return set(S8, string(S8))
		default:
			return set(S8, string(S8))
		}
	}

	if from == S10 {
		switch {
		case strings.Contains(low, "about-you"):
			return set(S11, string(S11))
		case strings.Contains(low, "callback") || strings.Contains(low, "chatgpt.com/api/auth/callback"):
			return set(S12, string(S12))
		case strings.Contains(low, "add-phone"):
			return cur, res, fmt.Errorf("protocol: phone gate continue_url=%s", u)
		default:
			return set(S11, string(S11))
		}
	}

	// Generic
	switch {
	case strings.Contains(low, "/create-account/password"):
		return set(S7, string(S7))
	case strings.Contains(low, "email-otp/send"):
		return set(S8, string(S8))
	case strings.Contains(low, "email-verification"):
		return set(S9, "waiting_for_otp")
	case strings.Contains(low, "about-you"):
		return set(S11, string(S11))
	case strings.Contains(low, "callback") || strings.Contains(low, "chatgpt.com/api/auth/callback"):
		return set(S12, string(S12))
	case strings.Contains(low, "add-phone"):
		return cur, res, fmt.Errorf("protocol: phone gate continue_url=%s", u)
	default:
		return linear()
	}
}

func (e *Engine) applyPreset(req *http.Request, name headerpreset.Name, overrides map[string]string) error {
	hs, err := headerpreset.Build(name, e.Bundle, overrides, headerpreset.Options{})
	if err != nil {
		return err
	}
	for _, h := range hs {
		req.Header.Set(h.Key, h.Value)
	}
	return nil
}

func (e *Engine) doHTTP(ctx context.Context, state StateID, req *http.Request) (*http.Response, error) {
	// Optional wire dump: GPT_REGISTER_WIRE_DIR=/path → save each step request/response for offline analysis.
	dumpDir := strings.TrimSpace(os.Getenv("GPT_REGISTER_WIRE_DIR"))
	var reqBody []byte
	if dumpDir != "" && req != nil && req.Body != nil && req.GetBody == nil {
		// best-effort: if body is readable once, re-wrap
		b, err := io.ReadAll(req.Body)
		if err == nil {
			reqBody = b
			req.Body = io.NopCloser(bytes.NewReader(b))
			req.GetBody = func() (io.ReadCloser, error) { return io.NopCloser(bytes.NewReader(b)), nil }
			req.ContentLength = int64(len(b))
		}
	} else if dumpDir != "" && req != nil && req.GetBody != nil {
		if rc, err := req.GetBody(); err == nil && rc != nil {
			b, _ := io.ReadAll(rc)
			_ = rc.Close()
			reqBody = b
		}
	}
	// Ensure POST/PUT bodies can be replayed across transport retries.
	if req != nil && req.Body != nil && req.GetBody == nil && req.ContentLength != 0 {
		b, err := io.ReadAll(req.Body)
		_ = req.Body.Close()
		if err == nil {
			reqBody = b
			req.Body = io.NopCloser(bytes.NewReader(b))
			req.GetBody = func() (io.ReadCloser, error) { return io.NopCloser(bytes.NewReader(b)), nil }
			req.ContentLength = int64(len(b))
		}
	}

	maxAttempts := 3
	// Sentinel and providers are the highest-EOF surfaces under SOCKS concurrency.
	if state == S1 || state == S5 || state == T1 {
		maxAttempts = 4
	}
	var resp *http.Response
	var err error
	for attempt := 1; attempt <= maxAttempts; attempt++ {
		if attempt > 1 && req != nil && req.GetBody != nil {
			body, bodyErr := req.GetBody()
			if bodyErr != nil {
				return nil, bodyErr
			}
			req.Body = body
		}
		if e.Do != nil {
			resp, err = e.Do(ctx, state, req)
		} else {
			resp, err = e.Client.Do(ctx, req)
		}
		if err == nil {
			// Retry a few gateway blips on sentinel only (body already consumed only after success path).
			if (state == S5 || state == T1) && resp != nil && resp.StatusCode >= 500 && attempt < maxAttempts {
				_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<16))
				_ = resp.Body.Close()
				resp = nil
				select {
				case <-ctx.Done():
					return nil, ctx.Err()
				case <-time.After(time.Duration(attempt) * 350 * time.Millisecond):
				}
				continue
			}
			break
		}
		if !isTransientTransportErr(err) || attempt == maxAttempts {
			break
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(time.Duration(attempt) * 350 * time.Millisecond):
		}
	}
	// Passive edge-challenge classification after a completed response.
	// Never solves challenges and never auto-retries challenged POSTs.
	// Dump wire first so challenge HTML remains available for diagnostics.
	if dumpDir != "" && req != nil {
		_ = dumpWireExchange(dumpDir, state, req, reqBody, resp, err)
	}
	if err == nil && resp != nil {
		if resp.Request == nil {
			resp.Request = req
		}
		if detectionErr := DetectEdgeChallenge(resp); detectionErr != nil {
			if resp.Body != nil {
				_ = resp.Body.Close()
			}
			var challenge *EdgeChallengeError
			if errors.As(detectionErr, &challenge) {
				resp = nil
				err = challenge
			} else {
				resp = nil
				err = detectionErr
			}
		}
	}
	return resp, err
}

// isTransientTransportErr matches proxy/TLS flakiness we have measured on
// chatgpt.com S1 and sentinel.openai.com/req under concurrent SOCKS.
func isTransientTransportErr(err error) bool {
	if err == nil {
		return false
	}
	s := strings.ToLower(err.Error())
	return strings.Contains(s, "eof") ||
		strings.Contains(s, "timeout") ||
		strings.Contains(s, "deadline exceeded") ||
		strings.Contains(s, "connection reset") ||
		strings.Contains(s, "connection refused") ||
		strings.Contains(s, "broken pipe") ||
		strings.Contains(s, "tls:") ||
		strings.Contains(s, "i/o timeout") ||
		strings.Contains(s, "use of closed network connection") ||
		strings.Contains(s, "socks") ||
		strings.Contains(s, "temporary") ||
		strings.Contains(s, "reset by peer") ||
		strings.Contains(s, "server closed idle connection")
}

func dumpWireExchange(dir string, state StateID, req *http.Request, reqBody []byte, resp *http.Response, callErr error) error {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	ts := time.Now().UTC().Format("150405.000")
	base := filepath.Join(dir, fmt.Sprintf("%s_%s", ts, string(state)))
	var hdr strings.Builder
	fmt.Fprintf(&hdr, "%s %s\n", req.Method, req.URL.String())
	if callErr != nil {
		fmt.Fprintf(&hdr, "call_error: %v\n", callErr)
	}
	for k, vs := range req.Header {
		for _, v := range vs {
			fmt.Fprintf(&hdr, "%s: %s\n", k, v)
		}
	}
	_ = os.WriteFile(base+"_req.headers.txt", []byte(hdr.String()), 0o600)
	if len(reqBody) > 0 {
		_ = os.WriteFile(base+"_req.body.bin", reqBody, 0o600)
	}
	if resp == nil {
		return nil
	}
	var rh strings.Builder
	fmt.Fprintf(&rh, "HTTP %s\n", resp.Status)
	for k, vs := range resp.Header {
		for _, v := range vs {
			fmt.Fprintf(&rh, "%s: %s\n", k, v)
		}
	}
	_ = os.WriteFile(base+"_resp.headers.txt", []byte(rh.String()), 0o600)
	if resp.Body != nil {
		b, err := io.ReadAll(resp.Body)
		_ = resp.Body.Close()
		if err == nil {
			_ = os.WriteFile(base+"_resp.body.bin", b, 0o600)
			resp.Body = io.NopCloser(bytes.NewReader(b))
		} else {
			resp.Body = io.NopCloser(bytes.NewReader(nil))
		}
	}
	return nil
}

func parseSetCookieName(h http.Header, name string) string {
	for _, line := range h.Values("Set-Cookie") {
		parts := strings.Split(line, ";")
		kv := strings.TrimSpace(parts[0])
		if i := strings.IndexByte(kv, '='); i > 0 && strings.EqualFold(kv[:i], name) {
			return kv[i+1:]
		}
	}
	return ""
}

func extractJSONString(body []byte, key string) string {
	// Prefer encoding/json so \u0026 and other escapes decode correctly.
	// The previous byte scanner treated "\\u0026" as "u0026", stripping
	// required callback query separators from continue_url.
	var document map[string]json.RawMessage
	if json.Unmarshal(body, &document) == nil {
		if raw, ok := document[key]; ok {
			var value string
			if json.Unmarshal(raw, &value) == nil {
				return value
			}
		}
	}
	needle := `"` + key + `"`
	s := string(body)
	i := strings.Index(s, needle)
	if i < 0 {
		return ""
	}
	rest := s[i+len(needle):]
	rest = strings.TrimLeft(rest, " \t\r\n:")
	rest = strings.TrimLeft(rest, " \t\r\n")
	if !strings.HasPrefix(rest, `"`) {
		return ""
	}
	// Decode the remainder as a JSON string literal.
	end := 1
	for end < len(rest) {
		if rest[end] == '\\' {
			end += 2
			continue
		}
		if rest[end] == '"' {
			end++
			break
		}
		end++
	}
	var value string
	if json.Unmarshal([]byte(rest[:end]), &value) == nil {
		return value
	}
	return ""
}

func trimBody(b []byte, n int) string {
	s := string(b)
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}

// s6PasswordlessSignup detects structured passwordless signup signals in authorize/continue body.
func s6PasswordlessSignup(bodyLow string) bool {
	if bodyLow == "" {
		return false
	}
	markers := []string{
		`"email_verification_mode":"passwordless_signup"`,
		`"email_verification_mode": "passwordless_signup"`,
		"passwordless_signup",
		`"page_type":"email_otp_verification"`,
		`"page_type": "email_otp_verification"`,
		"email_otp_verification",
	}
	for _, m := range markers {
		if strings.Contains(bodyLow, m) {
			return true
		}
	}
	return false
}

func s6EmailAlreadyRegistered(bodyLow string) bool {
	markers := []string{
		"user_already_exists",
		"email_already_in_use",
		"already have an account",
		"account already exists",
		`"code":"email_taken"`,
	}
	for _, m := range markers {
		if strings.Contains(bodyLow, m) {
			return true
		}
	}
	return false
}
