// Package sentinel implements OpenAI Sentinel requirements/PoW/realm (Phase F).
package sentinel

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/gpt-register/go-email-protocol/internal/fingerprint"
)

// Requirements is a minimal redacted requirements document.
type Requirements struct {
	Token string `json:"token,omitempty"`
	// Difficulty is filled by ParseRequirements (string or number in JSON).
	Difficulty string `json:"-"`
	// DifficultyN optional numeric for skeleton tests.
	DifficultyN int `json:"-"`
	Flow        string `json:"flow,omitempty"`
	SDKBuild    string `json:"sdk_build,omitempty"`
	SDKHash     string `json:"sdk_hash,omitempty"`
	// Proof of work nested (wire).
	ProofOfWork *ProofOfWork `json:"proofofwork,omitempty"`
	// Turnstile nested (wire); dx present → compute t field.
	Turnstile *Turnstile `json:"turnstile,omitempty"`
	// SO nested (wire); collector_dx present → sessionObserver so material.
	SO  *SessionObserver `json:"so,omitempty"`
	Raw json.RawMessage  `json:"-"`
}

// ProofOfWork is requirements.proofofwork.
type ProofOfWork struct {
	Required   bool   `json:"required"`
	Seed       string `json:"seed,omitempty"`
	Difficulty string `json:"difficulty,omitempty"`
}

// SessionObserver is requirements.so (HAR / sdk sessionObserverToken).
type SessionObserver struct {
	Required    bool   `json:"required"`
	CollectorDX string `json:"collector_dx,omitempty"`
	SnapshotDX  string `json:"snapshot_dx,omitempty"`
}

// Result is the enforcement token material (not logged).
type Result struct {
	HeaderValue string
	// SOHeaderValue is openai-sentinel-so-token JSON {so,c,id,flow} when available.
	SOHeaderValue string
	// P is gAAAAAB… enforcement answer
	P string
	// T is turnstile dx result (optional)
	T string
	// C is requirements token (c field)
	C string
	// SO is raw session-observer token material (optional)
	SO string
	// RequirementsToken gAAAAAC…
	RequirementsToken string
	// TurnstileSource is "sdk" | "vm" | ""
	TurnstileSource string
	// SOSource is "sdk" | "vm" | ""
	SOSource string
	Nonce    string
	Attempts int
}

// Config bounds PoW and pins.
type Config struct {
	MaxAttempts int
	// AllowedSDKHashes if non-empty, unknown hash → protocol_incompatible
	AllowedSDKHashes map[string]bool
	// Timeout for PoW loop
	Timeout time.Duration
	// DeviceID for header id field
	DeviceID string
	// Flow name
	Flow string
	// RequestP is the gAAAAAC token already used (or to use) as requirements request body p.
	// Live path: generate once, POST as p, then pass the same value here for XOR/SDK.
	// If empty, Run generates a fresh RequirementsToken.
	RequestP string
	// SoftTurnstile: if true, turnstile failure does not fail Run (t omitted).
	// Default false = fail-closed when dx present (matches production need).
	SoftTurnstile bool
	// SoftSOStrict: if true, session-observer failure fails Run.
	// Default false (soft) — SO completes browser parity; Node register path omits it.
	SoftSOStrict bool
}

// Engine runs requirements → PoW → token assembly.
type Engine struct {
	Bundle *fingerprint.Bundle
	Cfg    Config
	SID    string
}

// ParseRequirements decodes a JSON requirements body (fixture or live).
func ParseRequirements(raw []byte) (*Requirements, error) {
	var r Requirements
	// Unmarshal known nested fields first (ignore unknown difficulty type via loose map).
	if err := json.Unmarshal(raw, &r); err != nil {
		return nil, fmt.Errorf("sentinel: parse: %w", err)
	}
	var loose map[string]any
	if err := json.Unmarshal(raw, &loose); err != nil {
		return nil, fmt.Errorf("sentinel: parse: %w", err)
	}
	switch d := loose["difficulty"].(type) {
	case string:
		r.Difficulty = d
	case float64:
		r.DifficultyN = int(d)
		if d <= 2 {
			r.Difficulty = "0"
		} else {
			n := int(d)
			if n > 8 {
				n = 8
			}
			r.Difficulty = strings.Repeat("0", n)
		}
	}
	if tok, ok := loose["token"].(string); ok && r.Token == "" {
		r.Token = tok
	}
	if flow, ok := loose["flow"].(string); ok && r.Flow == "" {
		r.Flow = flow
	}
	if h, ok := loose["sdk_hash"].(string); ok && r.SDKHash == "" {
		r.SDKHash = h
	}
	// nested proofofwork difficulty may be string
	if pow, ok := loose["proofofwork"].(map[string]any); ok {
		if r.ProofOfWork == nil {
			r.ProofOfWork = &ProofOfWork{}
		}
		if req, ok := pow["required"].(bool); ok {
			r.ProofOfWork.Required = req
		}
		if seed, ok := pow["seed"].(string); ok {
			r.ProofOfWork.Seed = seed
		}
		switch d := pow["difficulty"].(type) {
		case string:
			r.ProofOfWork.Difficulty = d
		case float64:
			if d <= 2 {
				r.ProofOfWork.Difficulty = "0"
			} else {
				n := int(d)
				if n > 8 {
					n = 8
				}
				r.ProofOfWork.Difficulty = strings.Repeat("0", n)
			}
		}
	}
	if ts, ok := loose["turnstile"].(map[string]any); ok {
		if r.Turnstile == nil {
			r.Turnstile = &Turnstile{}
		}
		if dx, ok := ts["dx"].(string); ok {
			r.Turnstile.DX = dx
		}
	}
	if r.ProofOfWork == nil {
		if r.Difficulty != "" || r.Token != "" || r.DifficultyN > 0 {
			r.ProofOfWork = &ProofOfWork{
				Required:   true,
				Seed:       r.Token,
				Difficulty: r.Difficulty,
			}
			if r.ProofOfWork.Seed == "" {
				r.ProofOfWork.Seed = "seed"
			}
			if r.ProofOfWork.Difficulty == "" {
				r.ProofOfWork.Difficulty = "0"
			}
		}
	}
	r.Raw = append([]byte(nil), raw...)
	return &r, nil
}

// Run parses requirements, checks SDK pin, solves real PoW, builds header JSON.
func (e *Engine) Run(ctx context.Context, raw []byte) (*Result, error) {
	req, err := ParseRequirements(raw)
	if err != nil {
		return nil, err
	}
	if e != nil && e.Cfg.AllowedSDKHashes != nil && req.SDKHash != "" {
		if !e.Cfg.AllowedSDKHashes[req.SDKHash] {
			return nil, &Error{Code: "protocol_incompatible", Message: "sdk_hash not pinned: " + req.SDKHash}
		}
	}
	max := 500_000
	if e != nil && e.Cfg.MaxAttempts > 0 {
		max = e.Cfg.MaxAttempts
	}
	cctx := ctx
	if e != nil && e.Cfg.Timeout > 0 {
		var cancel context.CancelFunc
		cctx, cancel = context.WithTimeout(ctx, e.Cfg.Timeout)
		defer cancel()
	}

	var bundle *fingerprint.Bundle
	sid := ""
	flow := FlowAuthorizeContinue
	deviceID := ""
	if e != nil {
		bundle = e.Bundle
		sid = e.SID
		if e.Cfg.Flow != "" {
			flow = e.Cfg.Flow
		}
		deviceID = e.Cfg.DeviceID
		if req.Flow != "" {
			flow = req.Flow
		}
	}
	if sid == "" {
		sid = NewSID()
	}
	env := EnvFromBundle(bundle)

	// Request p (gAAAAAC…): reuse Cfg.RequestP when live already POSTed it.
	reqTok := ""
	if e != nil {
		reqTok = strings.TrimSpace(e.Cfg.RequestP)
	}
	if reqTok == "" {
		var rerr error
		reqTok, rerr = RequirementsToken(cctx, env, sid, max)
		if rerr != nil {
			return nil, mapPowErr(rerr)
		}
	}

	p := ""
	if req.ProofOfWork != nil && req.ProofOfWork.Required {
		seed := req.ProofOfWork.Seed
		diff := req.ProofOfWork.Difficulty
		if seed == "" {
			seed = "seed"
		}
		if diff == "" {
			diff = "0"
		}
		tok, err := EnforcementToken(cctx, env, sid, seed, diff, max)
		if err != nil {
			return nil, mapPowErr(err)
		}
		p = tok
	}

	// c field: wire uses requirements response token when present (Node).
	cField := req.Token
	if cField == "" {
		cField = reqTok
	}

	// t field: only when turnstile.dx present.
	// XOR/SDK key = request p; requirements.token = response c; pass pow map.
	tField := ""
	tSrc := ""
	if req.Turnstile != nil && strings.TrimSpace(req.Turnstile.DX) != "" {
		soft := e != nil && e.Cfg.SoftTurnstile
		var powMap map[string]any
		if req.ProofOfWork != nil {
			powMap = map[string]any{
				"required":   req.ProofOfWork.Required,
				"seed":       req.ProofOfWork.Seed,
				"difficulty": req.ProofOfWork.Difficulty,
			}
		}
		out, src, terr := ComputeTurnstileDxFull(cctx, env, req.Turnstile.DX, reqTok, cField, powMap)
		if terr != nil {
			terr = MapRuntimeFailure(terr)
			if !soft {
				return nil, terr
			}
		} else {
			tField, tSrc = out, src
		}
	}

	// so field: requirements.so.collector_dx → sessionObserverToken via SDK hook.
	soField := ""
	soSrc := ""
	if req.SO != nil && (req.SO.Required || strings.TrimSpace(req.SO.CollectorDX) != "" || strings.TrimSpace(req.SO.SnapshotDX) != "") {
		out, src, serr := ComputeSessionObserverSO(cctx, env, req.SO, reqTok, cField, flow)
		if serr != nil {
			serr = MapRuntimeFailure(serr)
			strict := e != nil && e.Cfg.SoftSOStrict
			if strict {
				return nil, serr
			}
			// soft: continue without so header
		} else {
			soField, soSrc = out, src
		}
	}

	hdr, err := AssembleHeaderJSON(p, tField, cField, deviceID, flow)
	if err != nil {
		return nil, err
	}
	soHdr, err := AssembleSOHeaderJSON(soField, cField, deviceID, flow)
	if err != nil {
		return nil, err
	}
	return &Result{
		HeaderValue:       hdr,
		SOHeaderValue:     soHdr,
		P:                 p,
		T:                 tField,
		C:                 cField,
		SO:                soField,
		RequirementsToken: reqTok,
		TurnstileSource:   tSrc,
		SOSource:          soSrc,
	}, nil
}

func mapPowErr(err error) error {
	if err == nil {
		return nil
	}
	if fe, ok := err.(*Error); ok {
		return fe
	}
	return &Error{Code: "sentinel_pow_exhausted", Message: err.Error()}
}

// Error is a typed sentinel failure.
type Error struct {
	Code    string
	Message string
}

func (e *Error) Error() string {
	if e == nil {
		return ""
	}
	return e.Code + ": " + e.Message
}
