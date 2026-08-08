package protocol

import (
	"context"
	"encoding/json"
	"fmt"
	mathrand "math/rand/v2"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/gpt-register/go-email-protocol/internal/headerpreset"
	"github.com/gpt-register/go-email-protocol/internal/sentinel"
)

// LiveSession holds success fields captured on S11–S14 (memory only).
type LiveSession struct {
	AccessToken string
	AccountID   string
	PlanType    string
}

// LiveStepTail handles S11–S14. Called from LiveStep default path.
func (e *Engine) liveStepTail(ctx context.Context, cur Cursor, res StepResult, baseChat, baseAuth string) (Cursor, StepResult, error) {
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
	case S11:
		// Node completeAboutYou: fresh oauth_create_account sentinel + random name/birthdate.
		if tok, soTok, err := e.mintSentinel(ctx, cur, sentinel.FlowOAuthCreateAccount); err != nil {
			return cur, res, err
		} else {
			if tok != "" {
				cur.SentinelToken = tok
			}
			if soTok != "" {
				cur.SentinelSOToken = soTok
			}
		}
		prof := randomAboutYouProfile()
		payload, err := json.Marshal(prof)
		if err != nil {
			return cur, res, err
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, baseAuth+"/api/accounts/create_account", strings.NewReader(string(payload)))
		if err != nil {
			return cur, res, err
		}
		if err := e.applyPreset(req, headerpreset.SameOriginFetch, map[string]string{
			"content-type": "application/json",
			"origin":       baseAuth,
			"referer":      baseAuth + "/about-you",
		}); err != nil {
			return cur, res, err
		}
		applySentinelHeaders(req, cur)
		resp, err := e.doHTTP(ctx, S11, req)
		if err != nil {
			res.Ambiguous = true
			res.FailureCode = "ambiguous_after_send"
			return cur, res, err
		}
		defer resp.Body.Close()
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
		res.StatusCode = resp.StatusCode
		if u := extractJSONString(body, "continue_url"); u != "" {
			cur.ContinueURL = u
		} else if u := extractJSONString(body, "url"); u != "" {
			cur.ContinueURL = u
		}
		if resp.StatusCode >= 400 {
			code, retryable, msg := classifyHTTPFailure("S11", resp.StatusCode, body)
			if resp.StatusCode >= 500 {
				res.Ambiguous = true
				// Allow one SO re-mint retry at job layer; mark distinct from fatal.
				if code == "server_error" {
					code = "create_account_server_error"
				}
			}
			res.FailureCode = code
			res.Retryable = retryable
			return cur, res, fmt.Errorf("%s", msg)
		}
		if cur.ContinueURL == "" {
			return cur, res, fmt.Errorf("protocol: S11 missing continue_url body=%s", trimBody(body, 240))
		}
		return advance(cur)

	case S12:
		target := cur.ContinueURL
		if target == "" {
			return cur, res, fmt.Errorf("protocol: S12 missing continue_url from create_account")
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, target, nil)
		if err != nil {
			return cur, res, err
		}
		if err := e.applyPreset(req, headerpreset.CallbackNavigation, map[string]string{
			"referer": baseAuth + "/",
		}); err != nil {
			return cur, res, err
		}
		resp, err := e.doHTTP(ctx, S12, req)
		if err != nil {
			return cur, res, err
		}
		defer resp.Body.Close()
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<20))
		res.StatusCode = resp.StatusCode
		return advance(cur)

	case S13:
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, baseChat+"/api/auth/session", nil)
		if err != nil {
			return cur, res, err
		}
		if err := e.applyPreset(req, headerpreset.SameOriginFetch, map[string]string{
			"referer": baseChat + "/",
			"origin":  baseChat,
		}); err != nil {
			return cur, res, err
		}
		resp, err := e.doHTTP(ctx, S13, req)
		if err != nil {
			return cur, res, err
		}
		defer resp.Body.Close()
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
		res.StatusCode = resp.StatusCode
		if tok := extractJSONString(body, "accessToken"); tok != "" {
			cur.AccessToken = tok
		} else if tok := extractJSONString(body, "access_token"); tok != "" {
			cur.AccessToken = tok
		}
		if id := extractJSONString(body, "id"); id != "" {
			cur.AccountID = id
		} else if id := extractNestedAccountID(body); id != "" {
			cur.AccountID = id
		}
		return advance(cur)

	case S14:
		if strings.TrimSpace(cur.AccessToken) == "" {
			return cur, res, fmt.Errorf("protocol: S14 missing access_token (session document incomplete)")
		}
		res.To = S14
		res.Stage = "succeeded"
		return cur, res, nil

	default:
		return cur, res, fmt.Errorf("protocol: live step %s not implemented yet", cur.State)
	}
}

func extractNestedAccountID(body []byte) string {
	// very light: "user":{"id":"..."}
	s := string(body)
	i := strings.Index(s, `"user"`)
	if i < 0 {
		return ""
	}
	return extractJSONString([]byte(s[i:]), "id")
}


func randomAboutYouProfile() map[string]string {
	firstNames := []string{"Ethan", "Noah", "Liam", "Mason", "Lucas", "Logan", "Owen", "Ryan", "Leo", "Adam", "Ella", "Ava", "Mia", "Luna", "Chloe", "Grace", "Ruby", "Nora", "Ivy", "Sofia"}
	lastNames := []string{"Smith", "Brown", "Taylor", "Walker", "Wilson", "Clark", "Hall", "Young", "Allen", "King", "Scott", "Green", "Baker", "Adams", "Turner"}
	age := 25 + mathrand.IntN(10) // 25..34
	year := time.Now().Year() - age
	month := 1 + mathrand.IntN(12)
	day := 1 + mathrand.IntN(28)
	return map[string]string{
		"name":      firstNames[mathrand.IntN(len(firstNames))] + " " + lastNames[mathrand.IntN(len(lastNames))],
		"birthdate": fmt.Sprintf("%04d-%02d-%02d", year, month, day),
	}
}
