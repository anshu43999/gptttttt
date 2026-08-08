// Package protocol: FSM runner for Phase E (fixture-first; live opt-in).
package protocol

import (
	"context"
	"fmt"
	"net/http"

	"github.com/gpt-register/go-email-protocol/internal/fingerprint"
	"github.com/gpt-register/go-email-protocol/internal/headerpreset"
	"github.com/gpt-register/go-email-protocol/internal/transport"
)

// Mode selects execution backend.
type Mode string

const (
	// ModeSynthetic advances local stages without HTTP (G1 tests).
	ModeSynthetic Mode = "synthetic"
	// ModeFixture replays recorded request/response shapes (Phase E tests).
	ModeFixture Mode = "fixture"
	// ModeLive performs real HTTP via transport.Client (Phase E+; opt-in).
	ModeLive Mode = "live"
)

// Cursor is the durable FSM position.
type Cursor struct {
	State   StateID
	Attempt int
	// ContinueURL from last authorize/continue when applicable.
	ContinueURL string
	// DeviceID is server oai-did (from wire only).
	DeviceID string
	// CSRF token if captured.
	CSRF string
	// SentinelToken is openai-sentinel-token material (memory only).
	SentinelToken string
	// SentinelSOToken is openai-sentinel-so-token JSON (memory only; HAR create_account).
	SentinelSOToken string
	// OTPCode set by OTP submit path before S10.
	OTPCode string
	// Email/Password optional job credentials (prefer Engine fields).
	Email    string
	Password string
	// AccessToken/AccountID filled on S13 (memory; sealed by job layer).
	AccessToken string
	AccountID   string
}

// StepResult is one transition outcome.
type StepResult struct {
	From        StateID
	To          StateID
	Preset      headerpreset.Name
	StatusCode  int
	Ambiguous   bool
	FailureCode string
	Retryable   bool
	// Stage for ledger stage string.
	Stage string
}

// Engine drives S0–S14. Live HTTP only when ModeLive and Client/Do non-nil.
type Engine struct {
	Mode   Mode
	Bundle *fingerprint.Bundle
	Client transport.Client
	// Email/Password for register steps (S7+).
	Email    string
	Password string
	// Do is optional override for tests (fixture injection).
	Do func(ctx context.Context, state StateID, req *http.Request) (*http.Response, error)
}

// Next computes the successor for a main-path state (no side effects).
func Next(id StateID) (StateID, error) {
	switch id {
	case S0:
		return S1, nil
	case S1:
		return S2, nil
	case S2:
		return S3, nil
	case S3:
		return S4, nil
	case S4:
		return S5, nil
	case S5:
		return S6, nil
	case S6:
		return S7, nil
	case S7:
		return S8, nil
	case S8:
		return S9, nil
	case S9:
		return S10, nil
	case S10:
		return S11, nil
	case S11:
		return S12, nil
	case S12:
		return S13, nil
	case S13:
		return S14, nil
	case S14:
		return "", fmt.Errorf("protocol: terminal %s", id)
	default:
		return "", fmt.Errorf("protocol: no linear next for %s", id)
	}
}

// PresetFor returns the header preset for a main state (plan §5 / §7).
func PresetFor(id StateID) headerpreset.Name {
	switch id {
	case S1, S4, S12:
		return headerpreset.DocumentNavigation
	case S2, S3, S6, S7, S11, S13:
		return headerpreset.SameOriginFetch
	case S8, S10:
		return headerpreset.OTPSparse
	case S5:
		return headerpreset.SentinelReq
	default:
		return headerpreset.SameOriginFetch
	}
}

// Step advances one state. Synthetic/fixture do not require network.
func (e *Engine) Step(ctx context.Context, cur Cursor) (Cursor, StepResult, error) {
	if e == nil {
		return cur, StepResult{}, fmt.Errorf("protocol: nil engine")
	}
	if !IsKnown(cur.State) && cur.State != "" {
		return cur, StepResult{}, fmt.Errorf("protocol: unknown state %s", cur.State)
	}
	if cur.State == "" {
		cur.State = S0
	}
	res := StepResult{From: cur.State, Preset: PresetFor(cur.State), Stage: string(cur.State)}

	switch e.Mode {
	case ModeLive:
		return e.LiveStep(ctx, cur, LiveConfig{RequireExplicit: true})
	case ModeFixture, ModeSynthetic, "":
		// Linear advance for main path; S9 is waiting (caller injects OTP externally).
		if cur.State == S9 {
			res.To = S9
			res.Stage = "waiting_for_otp"
			return cur, res, nil
		}
		if cur.State == S14 {
			res.To = S14
			res.Stage = "succeeded"
			return cur, res, nil
		}
		next, err := Next(cur.State)
		if err != nil {
			return cur, res, err
		}
		// Optional: build headers to prove preset+bundle wiring (no send).
		if e.Bundle != nil && res.Preset != "" {
			if _, err := headerpreset.Build(res.Preset, e.Bundle, nil, headerpreset.Options{}); err != nil {
				return cur, res, fmt.Errorf("protocol: preset %s: %w", res.Preset, err)
			}
		}
		cur.State = next
		res.To = next
		res.Stage = string(next)
		return cur, res, nil
	default:
		return cur, res, fmt.Errorf("protocol: unknown mode %q", e.Mode)
	}
}

// RunToOTP walks S0→S9 for synthetic/fixture tests.
func (e *Engine) RunToOTP(ctx context.Context, cur Cursor) (Cursor, []StepResult, error) {
	var steps []StepResult
	if cur.State == "" {
		cur.State = S0
	}
	for cur.State != S9 && cur.State != S14 {
		var res StepResult
		var err error
		cur, res, err = e.Step(ctx, cur)
		steps = append(steps, res)
		if err != nil {
			return cur, steps, err
		}
		if res.From == res.To && cur.State == S9 {
			break
		}
		if len(steps) > 32 {
			return cur, steps, fmt.Errorf("protocol: step overflow")
		}
	}
	return cur, steps, nil
}
