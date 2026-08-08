package job

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/gpt-register/go-email-protocol/internal/admission"
	"github.com/gpt-register/go-email-protocol/internal/cryptostore"
	"github.com/gpt-register/go-email-protocol/internal/fingerprint"
	"github.com/gpt-register/go-email-protocol/internal/ledger"
	"github.com/gpt-register/go-email-protocol/internal/protocol"
	proxypool "github.com/gpt-register/go-email-protocol/internal/proxy"
	"github.com/gpt-register/go-email-protocol/internal/session"
	"github.com/gpt-register/go-email-protocol/internal/transport"
)

// Runtime is the isolated per-job execution context.
type Runtime struct {
	JobID   string
	Attempt int
	Profile json.RawMessage
	// Bundle is the parsed/frozen FingerprintBundle v2 (nil only if prepare failed — should not run).
	Bundle *fingerprint.Bundle
	Jar    *session.Jar
	Client transport.Client
	Proxy  transport.ProxySnapshot

	ChallengeID      string
	ChallengeVersion int64

	cancel context.CancelFunc
	ctx    context.Context

	password   string
	capability string
	bridgeCap  string
	otpCode    string
	email      string
	// optional Graph mailbox credentials for in-worker OTP (software path)
	mailboxClientID     string
	mailboxRefreshToken string
	otpTimeout          time.Duration

	// live protocol engine parked at S9 for ModeLive OTP continuation
	liveEng *protocol.Engine
	liveCur protocol.Cursor

	mu        sync.Mutex
	status    string
	stage     string
	version   int64
	session   *SessionDocument
	closed    bool
	otpSignal chan string
}

// Manager owns jobs, ledger, admission, and synthetic runners.
type Manager struct {
	led       *ledger.Ledger
	adm       *admission.Controller
	crypto    *cryptostore.Store
	factory   transport.Factory
	runnerCfg RunnerConfig

	mu       sync.Mutex
	runtimes map[string]*Runtime
	waiters  map[string][]chan struct{}
	closed   bool
	bulkMu         sync.Mutex
	batches        map[string]*bulkState
	businessDBPath string
	wg       sync.WaitGroup
}

// NewManager constructs a job manager.
func NewManager(led *ledger.Ledger, adm *admission.Controller, crypto *cryptostore.Store, factory transport.Factory, cfg RunnerConfig) *Manager {
	if factory == nil {
		factory = transport.FakeFactory{}
	}
	if cfg.ToOTPDelay <= 0 {
		cfg.ToOTPDelay = 20 * time.Millisecond
	}
	if cfg.ToSuccessDelay <= 0 {
		cfg.ToSuccessDelay = 10 * time.Millisecond
	}
	return &Manager{
		led:       led,
		adm:       adm,
		crypto:    crypto,
		factory:   factory,
		runnerCfg: cfg,
		runtimes:       make(map[string]*Runtime),
		waiters:        make(map[string][]chan struct{}),
		batches:        make(map[string]*bulkState),
		businessDBPath: strings.TrimSpace(cfg.BusinessDBPath),
	}
}

// Ledger returns the durable ledger.
func (m *Manager) Ledger() *ledger.Ledger { return m.led }

// Admission returns the controller.
func (m *Manager) Admission() *admission.Controller { return m.adm }

// waitForAdmissionSeat provides bounded, cancellable backpressure for both
// initial creates and OTP resumes. A global-cap rejection is scheduling state,
// not a registration failure; proxy/mailbox identity is never changed here.
func (m *Manager) waitForAdmissionSeat(ctx context.Context, seat admission.Seat) error {
	queued := false
	defer func() {
		if queued {
			m.adm.Dequeue()
		}
	}()
	for attempt := 0; ; attempt++ {
		if m.isClosed() {
			return context.Canceled
		}
		if err := m.adm.TryAdmit(seat); err == nil {
			return nil
		}
		if !queued {
			if err := m.adm.TryQueue(); err != nil {
				return fmt.Errorf("admission queue full: %w", err)
			}
			queued = true
		}
		// Short exponential backoff prevents a stampede after a burst of OTPs.
		delay := 50 * time.Millisecond
		for i := 0; i < attempt && delay < time.Second; i++ {
			delay *= 2
		}
		if delay > time.Second {
			delay = time.Second
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(delay):
		}
	}
}

// acquireRuntimeSeat restores a parked live session to the protocol hot path
// without changing its sticky proxy, mailbox, Engine, or cookie jar.
func (m *Manager) acquireRuntimeSeat(ctx context.Context, rt *Runtime) error {
	if rt == nil {
		return fmt.Errorf("admission: nil runtime")
	}
	if m.adm.Has(rt.JobID) {
		return nil
	}
	rec, err := m.led.GetByID(context.Background(), rt.JobID)
	if err != nil {
		return err
	}
	return m.waitForAdmissionSeat(ctx, admission.Seat{
		JobID:    rt.JobID,
		EmailKey: rec.EmailResourceKey,
		ProxyKey: rec.ProxyResourceKey,
		Domain:   domainOf(rec.Email),
	})
}

func (m *Manager) startAdmittedRuntime(rec *ledger.Record, cap string, req CreateRequest, profile json.RawMessage, bundle *fingerprint.Bundle) (*StatusView, error) {
	if rec == nil {
		return nil, fmt.Errorf("admission: nil ledger record")
	}
	_, err := m.led.BumpVersion(context.Background(), rec.JobID, func(r *ledger.Record) error {
		if r.Status != ledger.StatusQueued {
			return errSkip
		}
		r.Status = ledger.StatusRunning
		r.Stage = "admission"
		return nil
	})
	if err != nil {
		m.adm.Release(rec.JobID)
		return nil, err
	}
	current, err := m.led.GetByID(context.Background(), rec.JobID)
	if err != nil {
		m.adm.Release(rec.JobID)
		return nil, err
	}
	rt, err := m.spawnRuntime(current, cap, req.Password, req.ResourceGrant.Bridge.Capability, profile, bundle)
	if err != nil {
		m.adm.Release(rec.JobID)
		_, _ = m.led.BumpVersion(context.Background(), rec.JobID, func(r *ledger.Record) error {
			if r.Status != ledger.StatusRunning {
				return errSkip
			}
			r.Status = ledger.StatusFailed
			r.Stage = "runtime_spawn_failed"
			r.FailureCode = "runtime_spawn_failed"
			r.Retryable = true
			return nil
		})
		return nil, err
	}
	rt.mu.Lock()
	rt.mailboxClientID = strings.TrimSpace(req.MailboxClientID)
	rt.mailboxRefreshToken = strings.TrimSpace(req.MailboxRefreshToken)
	if req.OTPTimeoutSeconds > 0 {
		rt.otpTimeout = time.Duration(req.OTPTimeoutSeconds) * time.Second
	}
	rt.mu.Unlock()
	m.goRun(func() { m.runJob(rt) })
	return m.recordToView(current, cap), nil
}

func (m *Manager) queueInitialAdmission(rec *ledger.Record, cap string, req CreateRequest, profile json.RawMessage, bundle *fingerprint.Bundle) {
	ctx := context.Background()
	if !rec.DeadlineAt.IsZero() {
		var cancel context.CancelFunc
		ctx, cancel = context.WithDeadline(ctx, rec.DeadlineAt)
		defer cancel()
	}
	seat := admission.Seat{
		JobID:    rec.JobID,
		EmailKey: rec.EmailResourceKey,
		ProxyKey: rec.ProxyResourceKey,
		Domain:   domainOf(rec.Email),
	}
	if err := m.waitForAdmissionSeat(ctx, seat); err != nil {
		_, _ = m.led.BumpVersion(context.Background(), rec.JobID, func(r *ledger.Record) error {
			if r.Status != ledger.StatusQueued {
				return errSkip
			}
			r.Status = ledger.StatusFailed
			r.Stage = "admission_queue_timeout"
			r.FailureCode = "admission_queue_timeout"
			r.Retryable = true
			return nil
		})
		m.notify(rec.JobID)
		return
	}
	if _, err := m.startAdmittedRuntime(rec, cap, req, profile, bundle); err != nil && !errors.Is(err, errSkip) {
		_, _ = m.led.BumpVersion(context.Background(), rec.JobID, func(r *ledger.Record) error {
			if ledger.Terminal(r.Status) {
				return errSkip
			}
			r.Status = ledger.StatusFailed
			r.Stage = "admission_start_failed"
			r.FailureCode = "admission_start_failed"
			r.Retryable = true
			return nil
		})
	}
	m.notify(rec.JobID)
}

// Create admits or idempotently returns an existing job.
func (m *Manager) Create(ctx context.Context, req CreateRequest) (*StatusView, error) {
	if err := validateCreate(req); err != nil {
		return nil, err
	}

	existing, err := m.led.GetByTaskAttempt(ctx, req.TaskID, req.AttemptID)
	if err == nil && existing != nil {
		if existing.RequestFingerprint != req.RequestFingerprint {
			return nil, &ConflictError{Code: "fingerprint_mismatch", Message: "same task/attempt different request_fingerprint"}
		}
		if existing.BridgeGeneration != req.ResourceGrant.Bridge.Generation {
			return nil, &ConflictError{Code: "bridge_generation_mismatch", Message: "same fingerprint different bridge.generation"}
		}
		view := m.recordToView(existing, "")
		if rt := m.getRuntime(existing.JobID); rt != nil {
			view.JobCapability = rt.capability
		}
		return view, nil
	}
	if err != nil && !errors.Is(err, ledger.ErrNotFound) {
		return nil, err
	}

	bundle, profileJSON, err := m.prepareProfile(req)
	if err != nil {
		return nil, err
	}

	jobID := newID("jr")
	cap := newSecret()
	blob, err := m.crypto.Seal(cryptostore.Secrets{
		Password:            req.Password,
		Capability:          cap,
		BridgeCap:           req.ResourceGrant.Bridge.Capability,
		MailboxClientID:     strings.TrimSpace(req.MailboxClientID),
		MailboxRefreshToken: strings.TrimSpace(req.MailboxRefreshToken),
	})
	if err != nil {
		return nil, err
	}
	profileID := profileIDFromBundle(bundle, profileJSON)
	deadline := req.DeadlineAt
	if deadline.IsZero() {
		deadline = time.Now().UTC().Add(15 * time.Minute)
	}

	// Durable insert first so concurrent same (task,attempt) loses on UNIQUE and
	// replays the existing job instead of competing for email/proxy seats with a
	// provisional job_id (audit: concurrent idempotent create must not 429).
	rec, err := m.led.Create(ctx, ledger.CreateInput{
		JobID:              jobID,
		TaskID:             req.TaskID,
		AttemptID:          req.AttemptID,
		IdempotencyKey:     req.IdempotencyKey,
		RequestFingerprint: req.RequestFingerprint,
		Capability:         cap,
		Email:              req.Email,
		Password:           req.Password,
		EmailKey:           req.ResourceGrant.EmailKey,
		ProxyKey:           req.ResourceGrant.ProxyKey,
		BridgeURL:          req.ResourceGrant.Bridge.URL,
		BridgeGeneration:   req.ResourceGrant.Bridge.Generation,
		LeaseFence:         req.ResourceGrant.LeaseFence,
		ExitIP:             req.ResourceGrant.ExitIP,
		ProfileJSON:        profileJSON,
		ProfileID:          profileID,
		SkipPhone:          req.SkipPhone,
		DeadlineAt:         deadline,
		SecretBlob:         blob,
		Status:             ledger.StatusQueued,
		Stage:              "admission",
	})
	if err != nil {
		if ledger.IsUniqueViolation(err) {
			existing, e2 := m.led.GetByTaskAttempt(ctx, req.TaskID, req.AttemptID)
			if e2 != nil {
				return nil, e2
			}
			if existing.RequestFingerprint != req.RequestFingerprint {
				return nil, &ConflictError{Code: "fingerprint_mismatch", Message: "same task/attempt different request_fingerprint"}
			}
			if existing.BridgeGeneration != req.ResourceGrant.Bridge.Generation {
				return nil, &ConflictError{Code: "bridge_generation_mismatch", Message: "same fingerprint different bridge.generation"}
			}
			view := m.recordToView(existing, "")
			if rt := m.getRuntime(existing.JobID); rt != nil {
				view.JobCapability = rt.capability
			}
			return view, nil
		}
		return nil, err
	}

	seat := admission.Seat{
		JobID:    rec.JobID,
		EmailKey: req.ResourceGrant.EmailKey,
		ProxyKey: req.ResourceGrant.ProxyKey,
		Domain:   domainOf(req.Email),
	}
	if err := m.adm.TryAdmit(seat); err != nil {
		var rejected *admission.RejectError
		if errors.As(err, &rejected) && rejected.Reason == admission.ReasonGlobal {
			// Global capacity is transient scheduler pressure. Keep the durable job
			// queued and admit it asynchronously; do not force Python to rotate a
			// valid mailbox/proxy or report a false registration failure.
			_, _ = m.led.BumpVersion(context.Background(), rec.JobID, func(r *ledger.Record) error {
				r.Status = ledger.StatusQueued
				r.Stage = "admission_queued"
				r.RetryAfterMS = 50
				return nil
			})
			m.goRun(func() { m.queueInitialAdmission(rec, cap, req, profileJSON, bundle) })
			queued, getErr := m.led.GetByID(context.Background(), rec.JobID)
			if getErr != nil {
				return nil, getErr
			}
			return m.recordToView(queued, cap), nil
		}

		// Mailbox/proxy rejections preserve their existing retry classification:
		// the caller may obtain a distinct resource, while a global cap queues.
		rejMsg := err.Error()
		resultBytes, _ := json.Marshal(map[string]any{
			"error":     rejMsg,
			"retryable": true,
		})
		_, _ = m.led.BumpVersion(context.Background(), rec.JobID, func(r *ledger.Record) error {
			r.Status = ledger.StatusFailed
			r.Stage = "admission_rejected"
			r.FailureCode = "admission_rejected"
			r.Retryable = true
			r.ResultJSON = resultBytes
			return nil
		})
		return nil, err
	}

	return m.startAdmittedRuntime(rec, cap, req, profileJSON, bundle)
}

func (m *Manager) spawnRuntime(rec *ledger.Record, capability, password, bridgeCap string, profile json.RawMessage, bundle *fingerprint.Bundle) (*Runtime, error) {
	jar, err := session.NewJar(rec.JobID)
	if err != nil {
		return nil, err
	}
	proxy := transport.ProxySnapshot{
		ProxyKey:         rec.ProxyResourceKey,
		BridgeURL:        rec.BridgeURL,
		BridgeGeneration: rec.BridgeGeneration,
		BridgeCapability: bridgeCap,
		LeaseFence:       rec.LeaseFence,
		Style:            proxypool.StyleFromProxyURL(rec.BridgeURL),
	}
	// Prefer country from frozen fingerprint affinity (bulk sets ExpectedCountry on grant).
	if bundle != nil && strings.TrimSpace(bundle.ProxyAffinity.ExpectedCountry) != "" {
		proxy.ExpectedCountry = strings.TrimSpace(bundle.ProxyAffinity.ExpectedCountry)
	}
	// Prefer OptionsFactory when available (Phase D tls-client); Fake still works.
	var client transport.Client
	if of, ok := m.factory.(transport.OptionsFactory); ok {
		opts := transport.ClientOptions{
			JobID:       rec.JobID,
			Proxy:       proxy,
			BundleJSON:  []byte(profile),
			TransportID: "",
		}
		if bundle != nil {
			opts.TransportID = bundle.TransportProfileID
		}
		client, err = of.NewWithOptions(opts)
	} else {
		client, err = m.factory.New(rec.JobID, proxy)
	}
	if err != nil {
		return nil, err
	}
	// If profile JSON was ledger-only (recover path), parse bundle when missing.
	if bundle == nil && len(profile) > 0 {
		if b, perr := fingerprint.ParseJSON(profile); perr == nil {
			if aerr := b.AssertReady(); aerr == nil {
				bundle = b
			}
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	if !rec.DeadlineAt.IsZero() {
		dctx, dcancel := context.WithDeadline(ctx, rec.DeadlineAt)
		ctx = dctx
		old := cancel
		cancel = func() {
			dcancel()
			old()
		}
	}
	rt := &Runtime{
		JobID:      rec.JobID,
		Attempt:    rec.AttemptID,
		Profile:    profile,
		Bundle:     bundle,
		Jar:        jar,
		Client:     client,
		Proxy:      proxy,
		cancel:     cancel,
		ctx:        ctx,
		password:   password,
		capability: capability,
		bridgeCap:  bridgeCap,
		email:      rec.Email,
		status:     rec.Status,
		stage:      rec.Stage,
		version:    rec.StateVersion,
		otpSignal:  make(chan string, 1),
	}
	if u, err := url.Parse("https://chatgpt.com/"); err == nil {
		jar.SetNamed(u, "job_marker", rec.JobID)
	}
	m.mu.Lock()
	m.runtimes[rec.JobID] = rt
	m.mu.Unlock()
	return rt, nil
}

// Get returns status if capability matches.
func (m *Manager) Get(ctx context.Context, jobID, capability string, waitMS int) (*StatusView, error) {
	rec, err := m.led.GetByID(ctx, jobID)
	if err != nil {
		return nil, err
	}
	if !m.led.VerifyCapability(rec, capability) {
		return nil, ErrUnauthorized
	}
	if waitMS > 0 && !ledger.Terminal(rec.Status) {
		ch := make(chan struct{}, 1)
		m.mu.Lock()
		m.waiters[jobID] = append(m.waiters[jobID], ch)
		m.mu.Unlock()
		timer := time.NewTimer(time.Duration(waitMS) * time.Millisecond)
		select {
		case <-ch:
			timer.Stop()
		case <-timer.C:
		case <-ctx.Done():
			timer.Stop()
		}
		rec, err = m.led.GetByID(ctx, jobID)
		if err != nil {
			return nil, err
		}
	}
	view := m.recordToView(rec, "")
	if rec.Status == ledger.StatusSucceeded {
		if rt := m.getRuntime(jobID); rt != nil && rt.session != nil {
			view.Session = rt.session
		} else if len(rec.ResultJSON) > 0 {
			var doc SessionDocument
			if json.Unmarshal(rec.ResultJSON, &doc) == nil {
				if m.crypto != nil && len(rec.SecretBlob) > 0 {
					if sec, err := m.crypto.Open(rec.SecretBlob); err == nil && sec.AccessToken != "" {
						doc.AccessToken = sec.AccessToken
					}
				}
				view.Session = &doc
			}
		}
	}
	return view, nil
}

// SubmitOTP handles OTP submission with version/challenge checks.
func (m *Manager) SubmitOTP(ctx context.Context, jobID, capability string, body OTPSubmit) (*StatusView, error) {
	rec, err := m.led.GetByID(ctx, jobID)
	if err != nil {
		return nil, err
	}
	if !m.led.VerifyCapability(rec, capability) {
		return nil, ErrUnauthorized
	}
	if rec.Status != ledger.StatusWaitingForOTP {
		return nil, &ConflictError{Code: "not_waiting_for_otp", Message: "job is not waiting_for_otp"}
	}
	if body.ChallengeID == "" || body.ChallengeID != rec.ChallengeID {
		return nil, &ConflictError{Code: "challenge_mismatch", Message: "challenge_id mismatch"}
	}
	if body.StateVersion != rec.StateVersion {
		return nil, &ConflictError{Code: "state_version_conflict", Message: "stale state_version"}
	}
	if body.Code == "" {
		return nil, &ValidationError{Message: "code required"}
	}
	if !rec.ChallengeDeadline.IsZero() && time.Now().After(rec.ChallengeDeadline) {
		return nil, &ConflictError{Code: "challenge_expired", Message: "challenge deadline passed"}
	}

	rt := m.getRuntime(jobID)
	if rt == nil {
		return nil, &ConflictError{Code: "no_runtime", Message: "job runtime not available"}
	}
	// Durable transition first; only then wake the runner. Signaling before
	// Transition let a failed CAS still consume the waiter and advance incorrectly.
	rec2, err := m.led.Transition(ctx, jobID, rec.StateVersion, func(r *ledger.Record) error {
		r.Status = ledger.StatusRunning
		r.Stage = "otp_accepted"
		r.RetryAfterMS = 500
		return nil
	})
	if err != nil {
		if errors.Is(err, ledger.ErrVersionConflict) {
			return nil, &ConflictError{Code: "state_version_conflict", Message: "stale state_version"}
		}
		return nil, err
	}
	rt.mu.Lock()
	rt.otpCode = body.Code // memory only; never ledger/log
	rt.mu.Unlock()
	select {
	case rt.otpSignal <- body.Code:
	default:
	}
	m.notify(jobID)
	return m.recordToView(rec2, ""), nil
}

// Cancel starts cancellation (idempotent).
func (m *Manager) Cancel(ctx context.Context, jobID, capability string) (*StatusView, error) {
	rec, err := m.led.GetByID(ctx, jobID)
	if err != nil {
		return nil, err
	}
	if !m.led.VerifyCapability(rec, capability) {
		return nil, ErrUnauthorized
	}
	if ledger.Terminal(rec.Status) {
		return m.recordToView(rec, ""), nil
	}
	if rec.Status == ledger.StatusCancelling {
		return m.recordToView(rec, ""), nil
	}
	rec2, err := m.led.BumpVersion(ctx, jobID, func(r *ledger.Record) error {
		r.Status = ledger.StatusCancelling
		r.Stage = "cancelling"
		r.RetryAfterMS = 200
		return nil
	})
	if err != nil {
		return nil, err
	}
	if rt := m.getRuntime(jobID); rt != nil {
		rt.cancel()
	}
	m.notify(jobID)
	m.goRun(func() { m.finishCancel(jobID) })
	return m.recordToView(rec2, ""), nil
}

func (m *Manager) finishCancel(jobID string) {
	time.Sleep(5 * time.Millisecond)
	if m.isClosed() {
		m.releaseJob(jobID)
		return
	}
	ctx := context.Background()
	rec, err := m.led.GetByID(ctx, jobID)
	if err != nil {
		m.releaseJob(jobID)
		return
	}
	if ledger.Terminal(rec.Status) {
		m.releaseJob(jobID)
		return
	}
	_, _ = m.led.BumpVersion(ctx, jobID, func(r *ledger.Record) error {
		r.Status = ledger.StatusCancelled
		r.Stage = "cancelled"
		r.RetryAfterMS = 0
		return nil
	})
	m.releaseJob(jobID)
	m.notify(jobID)
}

// RecoverNonTerminal reloads nonterminal jobs from ledger after restart.
func (m *Manager) RecoverNonTerminal(ctx context.Context) error {
	list, err := m.led.ListNonTerminal(ctx)
	if err != nil {
		return err
	}
	for _, rec := range list {
		if !rec.DeadlineAt.IsZero() && time.Now().After(rec.DeadlineAt) {
			_, _ = m.led.BumpVersion(ctx, rec.JobID, func(r *ledger.Record) error {
				r.Status = ledger.StatusReconcileRequired
				r.Stage = "deadline_exceeded"
				r.FailureCode = "deadline_exceeded"
				return nil
			})
			continue
		}
		// A parked OTP runtime deliberately owns no protocol seat. Re-acquiring it
		// during recovery would recreate the original capacity leak before Graph has
		// produced a code. Running/queued jobs still need a seat before execution.
		if rec.Status != ledger.StatusWaitingForOTP && rec.Status != ledger.StatusCancelling {
			seat := admission.Seat{
				JobID:    rec.JobID,
				EmailKey: rec.EmailResourceKey,
				ProxyKey: rec.ProxyResourceKey,
				Domain:   domainOf(rec.Email),
			}
			if err := m.adm.TryAdmit(seat); err != nil {
				rejMsg := err.Error()
				resultBytes, _ := json.Marshal(map[string]any{"error": rejMsg, "retryable": true})
				_, _ = m.led.BumpVersion(ctx, rec.JobID, func(r *ledger.Record) error {
					r.Status = ledger.StatusReconcileRequired
					r.Stage = "admission_recovery_failed"
					r.FailureCode = "admission_recovery_failed"
					r.ResultJSON = resultBytes
					return nil
				})
				continue
			}
		}
		var password, cap, bridgeCap, mailboxClientID, mailboxRefreshToken string
		if m.crypto != nil && len(rec.SecretBlob) > 0 {
			sec, err := m.crypto.Open(rec.SecretBlob)
			if err != nil {
				_, _ = m.led.BumpVersion(ctx, rec.JobID, func(r *ledger.Record) error {
					r.Status = ledger.StatusReconcileRequired
					r.Stage = "secret_blob_unreadable"
					r.FailureCode = "secret_blob_unreadable"
					return nil
				})
				m.adm.Release(rec.JobID)
				continue
			}
			password = sec.Password
			cap = sec.Capability
			bridgeCap = sec.BridgeCap
			mailboxClientID = sec.MailboxClientID
			mailboxRefreshToken = sec.MailboxRefreshToken
			if cap == "" || ledger.HashSecret(cap) != rec.CapabilityHash {
				_, _ = m.led.BumpVersion(ctx, rec.JobID, func(r *ledger.Record) error {
					r.Status = ledger.StatusReconcileRequired
					r.Stage = "capability_mismatch"
					r.FailureCode = "capability_mismatch"
					return nil
				})
				m.adm.Release(rec.JobID)
				continue
			}
		} else {
			_, _ = m.led.BumpVersion(ctx, rec.JobID, func(r *ledger.Record) error {
				r.Status = ledger.StatusReconcileRequired
				r.Stage = "missing_secret_blob"
				r.FailureCode = "missing_secret_blob"
				return nil
			})
			m.adm.Release(rec.JobID)
			continue
		}
		rt, err := m.spawnRuntime(rec, cap, password, bridgeCap, json.RawMessage(rec.ProfileJSON), nil)
		if err != nil {
			_, _ = m.led.BumpVersion(ctx, rec.JobID, func(r *ledger.Record) error {
				r.Status = ledger.StatusReconcileRequired
				r.Stage = "runtime_spawn_failed"
				r.FailureCode = "runtime_spawn_failed"
				return nil
			})
			m.adm.Release(rec.JobID)
			continue
		}
		rt.mu.Lock()
		rt.mailboxClientID = strings.TrimSpace(mailboxClientID)
		rt.mailboxRefreshToken = strings.TrimSpace(mailboxRefreshToken)
		rt.mu.Unlock()
		switch rec.Status {
		case ledger.StatusCancelling:
			m.goRun(func() { m.finishCancel(rec.JobID) })
		case ledger.StatusWaitingForOTP:
			rt.ChallengeID = rec.ChallengeID
			rt.ChallengeVersion = rec.StateVersion
			// Prefer pure-Go live resume when OTP checkpoint exists; else synthetic waiter.
			m.goRun(func() {
				if m.restoreLiveOTPCheckpoint(rt) {
					m.runLiveFromOTP(rt)
					return
				}
				m.runSyntheticFromOTP(rt)
			})
		case ledger.StatusRunning:
			// Post-OTP accepted stages must not mint a new challenge.
			if rec.Stage == "synthetic_s10" || rec.Stage == "synthetic_done" {
				m.goRun(func() { m.runSyntheticComplete(rt) })
			} else {
				m.goRun(func() { m.runSynthetic(rt) })
			}
		default:
			m.goRun(func() { m.runSynthetic(rt) })
		}
	}
	return nil
}

// Runtime returns isolated runtime for tests (nil if missing).
func (m *Manager) Runtime(jobID string) *Runtime {
	return m.getRuntime(jobID)
}

// Close cancels runtimes, waits for runners, then returns.
// Caller may then close the ledger safely.
func (m *Manager) Close() {
	m.mu.Lock()
	if m.closed {
		m.mu.Unlock()
		return
	}
	m.closed = true
	ids := make([]string, 0, len(m.runtimes))
	for id := range m.runtimes {
		ids = append(ids, id)
	}
	m.mu.Unlock()
	for _, id := range ids {
		m.releaseJob(id)
	}
	m.wg.Wait()
}

func (m *Manager) isClosed() bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.closed
}

func (m *Manager) goRun(fn func()) {
	m.mu.Lock()
	if m.closed {
		m.mu.Unlock()
		return
	}
	m.wg.Add(1)
	m.mu.Unlock()
	go func() {
		defer m.wg.Done()
		fn()
	}()
}

func (m *Manager) getRuntime(jobID string) *Runtime {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.runtimes[jobID]
}

func (m *Manager) releaseJob(jobID string) {
	m.mu.Lock()
	rt := m.runtimes[jobID]
	delete(m.runtimes, jobID)
	m.mu.Unlock()
	if rt != nil {
		rt.mu.Lock()
		if !rt.closed {
			rt.closed = true
			if rt.cancel != nil {
				rt.cancel()
			}
			if rt.Client != nil {
				_ = rt.Client.Close()
			}
		}
		rt.mu.Unlock()
	}
	m.adm.Release(jobID)
}

func (m *Manager) notify(jobID string) {
	m.mu.Lock()
	ws := m.waiters[jobID]
	delete(m.waiters, jobID)
	m.mu.Unlock()
	for _, ch := range ws {
		select {
		case ch <- struct{}{}:
		default:
		}
	}
}

func (m *Manager) recordToView(rec *ledger.Record, capability string) *StatusView {
	v := &StatusView{
		JobID:                        rec.JobID,
		JobCapability:                capability,
		Status:                       rec.Status,
		StateVersion:                 rec.StateVersion,
		Stage:                        rec.Stage,
		RetryAfterMS:                 rec.RetryAfterMS,
		FailureCode:                  rec.FailureCode,
		Retryable:                    rec.Retryable,
		RegistrationMayHaveSucceeded: rec.RegistrationMayHaveSucceeded,
	}
	if rec.Status == ledger.StatusFailed && len(rec.ResultJSON) > 0 {
		var failure struct {
			Error string `json:"error"`
		}
		if json.Unmarshal(rec.ResultJSON, &failure) == nil {
			v.Message = strings.TrimSpace(failure.Error)
		}
	}
	if rec.Status == ledger.StatusWaitingForOTP && rec.ChallengeID != "" {
		v.Challenge = &Challenge{
			ChallengeID:  rec.ChallengeID,
			StateVersion: rec.StateVersion,
			IssuedAt:     rec.ChallengeIssuedAt,
			DeadlineAt:   rec.ChallengeDeadline,
			RetryAfterMS: rec.RetryAfterMS,
		}
	}
	return v
}

func (m *Manager) runJob(rt *Runtime) {
	if m.runnerCfg.Mailat.Enabled {
		m.runMailat(rt)
		return
	}
	switch strings.ToLower(strings.TrimSpace(m.runnerCfg.ProtocolMode)) {
	case "engine", "fixture", "live":
		m.runProtocolEngine(rt)
		return
	default:
		m.runSynthetic(rt)
	}
}

func (m *Manager) runSynthetic(rt *Runtime) {
	ctx := rt.ctx
	_, err := m.led.BumpVersion(ctx, rt.JobID, func(r *ledger.Record) error {
		if r.Status == ledger.StatusCancelling {
			return errSkip
		}
		r.Status = ledger.StatusRunning
		r.Stage = "synthetic_s1"
		r.RetryAfterMS = int(m.runnerCfg.ToOTPDelay / time.Millisecond)
		if r.RetryAfterMS <= 0 {
			r.RetryAfterMS = 20
		}
		return nil
	})
	if err != nil {
		if errors.Is(err, errSkip) {
			m.finishCancel(rt.JobID)
		}
		return
	}
	m.notify(rt.JobID)

	if m.runnerCfg.FailInject != "" {
		_, _ = m.led.BumpVersion(ctx, rt.JobID, func(r *ledger.Record) error {
			r.Status = ledger.StatusFailed
			r.Stage = "failed"
			r.FailureCode = m.runnerCfg.FailInject
			r.Retryable = true
			return nil
		})
		m.releaseJob(rt.JobID)
		m.notify(rt.JobID)
		return
	}

	if m.runnerCfg.HoldInRunning {
		select {
		case <-ctx.Done():
			m.finishCancel(rt.JobID)
			return
		case <-rt.otpSignal:
		}
	}

	select {
	case <-ctx.Done():
		m.finishCancel(rt.JobID)
		return
	case <-time.After(m.runnerCfg.ToOTPDelay):
	}

	cur, err := m.led.GetByID(context.Background(), rt.JobID)
	if err != nil {
		return
	}
	if cur.Status == ledger.StatusCancelling || cur.Status == ledger.StatusCancelled {
		m.finishCancel(rt.JobID)
		return
	}

	chID := newID("oc")
	issued := time.Now().UTC()
	deadline := issued.Add(10 * time.Minute)
	rec2, err := m.led.BumpVersion(context.Background(), rt.JobID, func(r *ledger.Record) error {
		if r.Status == ledger.StatusCancelling {
			return errSkip
		}
		r.Status = ledger.StatusWaitingForOTP
		r.Stage = "synthetic_s9"
		r.ChallengeID = chID
		r.ChallengeIssuedAt = issued
		r.ChallengeDeadline = deadline
		r.RetryAfterMS = 3000
		return nil
	})
	if err != nil {
		if errors.Is(err, errSkip) {
			m.finishCancel(rt.JobID)
		}
		return
	}
	rt.ChallengeID = chID
	rt.ChallengeVersion = rec2.StateVersion
	m.notify(rt.JobID)
	m.runSyntheticFromOTP(rt)
}

func (m *Manager) runSyntheticFromOTP(rt *Runtime) {
	ctx := rt.ctx
	// Wait for a single OTP submission signal (or cancel).
	select {
	case <-ctx.Done():
		m.finishCancel(rt.JobID)
		return
	case <-rt.otpSignal:
	}

	cur, err := m.led.GetByID(context.Background(), rt.JobID)
	if err != nil {
		return
	}
	if cur.Status == ledger.StatusCancelling || cur.Status == ledger.StatusCancelled {
		m.finishCancel(rt.JobID)
		return
	}
	// Require durable OTP accept (SubmitOTP Transition to running) before success.
	if cur.Status != ledger.StatusRunning {
		return
	}
	m.runSyntheticComplete(rt)
}

// runSyntheticComplete finishes a job after OTP was durably accepted (status running).
// Used by the live OTP waiter and by crash recovery for stage synthetic_s10.
func (m *Manager) runSyntheticComplete(rt *Runtime) {
	ctx := rt.ctx
	select {
	case <-ctx.Done():
		m.finishCancel(rt.JobID)
		return
	case <-time.After(m.runnerCfg.ToSuccessDelay):
	}

	cur, err := m.led.GetByID(context.Background(), rt.JobID)
	if err != nil {
		return
	}
	if ledger.Terminal(cur.Status) || cur.Status == ledger.StatusCancelling {
		if cur.Status == ledger.StatusCancelling {
			m.finishCancel(rt.JobID)
		}
		return
	}
	if cur.Status != ledger.StatusRunning {
		return
	}

	token := "test-" + rt.JobID
	doc := SessionDocument{
		SchemaVersion: 1,
		Email:         rt.email,
		AccessToken:   token,
		AccountID:     "acct_" + rt.JobID,
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
	rt.mu.Unlock()

	_, err = m.led.BumpVersion(context.Background(), rt.JobID, func(r *ledger.Record) error {
		if r.Status == ledger.StatusCancelling {
			return errSkip
		}
		if r.Status != ledger.StatusRunning {
			return errSkip
		}
		r.Status = ledger.StatusSucceeded
		r.Stage = "synthetic_done"
		r.ResultJSON = resultBytes
		r.SecretBlob = blob
		r.RetryAfterMS = 0
		return nil
	})
	if err != nil {
		if errors.Is(err, errSkip) {
			// cancelled or status moved; only finish cancel if still cancelling
			if cur2, e2 := m.led.GetByID(context.Background(), rt.JobID); e2 == nil && cur2.Status == ledger.StatusCancelling {
				m.finishCancel(rt.JobID)
			}
		}
		return
	}
	// Close transport + drop seat; session token remains in sealed secret_blob for Get.
	m.releaseJob(rt.JobID)
	m.notify(rt.JobID)
}

var errSkip = errors.New("skip transition")

// ErrUnauthorized is capability auth failure.
var ErrUnauthorized = errors.New("unauthorized")

// ConflictError is HTTP 409.
type ConflictError struct {
	Code    string
	Message string
}

func (e *ConflictError) Error() string { return e.Message }

// ValidationError is HTTP 400.
type ValidationError struct {
	Code    string
	Message string
}

func (e *ValidationError) Error() string {
	if e == nil {
		return ""
	}
	if e.Code != "" {
		return e.Code + ": " + e.Message
	}
	return e.Message
}

func validateCreate(req CreateRequest) error {
	if strings.TrimSpace(req.TaskID) == "" {
		return &ValidationError{Message: "task_id required"}
	}
	if req.AttemptID <= 0 {
		return &ValidationError{Message: "attempt_id required"}
	}
	if strings.TrimSpace(req.IdempotencyKey) == "" {
		return &ValidationError{Message: "idempotency_key required"}
	}
	if strings.TrimSpace(req.RequestFingerprint) == "" {
		return &ValidationError{Message: "request_fingerprint required"}
	}
	if strings.TrimSpace(req.Email) == "" || strings.TrimSpace(req.Password) == "" {
		return &ValidationError{Message: "email and password required"}
	}
	if strings.TrimSpace(req.ResourceGrant.EmailKey) == "" || strings.TrimSpace(req.ResourceGrant.ProxyKey) == "" {
		return &ValidationError{Message: "resource_grant email_key and proxy_key required"}
	}
	b := req.ResourceGrant.Bridge
	if strings.TrimSpace(b.URL) == "" || b.Generation == 0 {
		return &ValidationError{Message: "bridge url and generation required"}
	}
	if strings.TrimSpace(b.Capability) == "" {
		return &ValidationError{Message: "bridge.capability required"}
	}
	u, err := url.Parse(b.URL)
	if err != nil {
		return &ValidationError{Message: "bridge.url invalid"}
	}
	scheme := strings.ToLower(u.Scheme)
	switch scheme {
	case "http", "https":
		host := strings.ToLower(u.Hostname())
		if host != "127.0.0.1" && host != "localhost" && host != "::1" {
			return &ValidationError{Message: "bridge.url http(s) must be loopback"}
		}
		if u.Port() == "" {
			return &ValidationError{Message: "bridge.url must include port"}
		}
	case "socks5", "socks5h":
		// pure-Go direct dial path: BridgeURL is the SOCKS proxy itself.
		if u.Hostname() == "" || u.Port() == "" {
			return &ValidationError{Message: "socks bridge.url needs host:port"}
		}
	default:
		return &ValidationError{Message: "bridge.url scheme must be http(s) or socks5"}
	}
	return nil
}

func newID(prefix string) string {
	var b [12]byte
	_, _ = rand.Read(b[:])
	return prefix + "_" + hex.EncodeToString(b[:])
}

func newSecret() string {
	var b [32]byte
	_, _ = rand.Read(b[:])
	return hex.EncodeToString(b[:])
}

func domainOf(email string) string {
	i := strings.LastIndex(email, "@")
	if i < 0 {
		return ""
	}
	return strings.ToLower(email[i+1:])
}

func profileIDFrom(p json.RawMessage) string {
	if len(p) == 0 {
		return ""
	}
	var m map[string]any
	if json.Unmarshal(p, &m) != nil {
		return ""
	}
	for _, k := range []string{"bundle_id", "id"} {
		if id, ok := m[k].(string); ok && id != "" {
			return id
		}
	}
	if ident, ok := m["identity"].(map[string]any); ok {
		if id, ok := ident["profile_uuid"].(string); ok {
			return id
		}
	}
	return ""
}

// IsolationProbe exposes jar/proxy/profile for cross-talk tests.
func (rt *Runtime) IsolationProbe() (jobID, proxyKey, profileMarker, jarMarker string) {
	if rt == nil {
		return "", "", "", ""
	}
	u, _ := url.Parse("https://chatgpt.com/")
	marker := ""
	if rt.Jar != nil {
		marker = rt.Jar.GetNamed(u, "job_marker")
	}
	prof := ""
	if rt.Bundle != nil {
		prof = rt.Bundle.BundleID
		if prof == "" {
			prof = rt.Bundle.Identity.ProfileUUID
		}
	} else if len(rt.Profile) > 0 {
		prof = profileIDFrom(rt.Profile)
	}
	return rt.JobID, rt.Proxy.ProxyKey, prof, marker
}

// IdentityHeaders returns frozen browser headers for this job (nil if no bundle).
func (rt *Runtime) IdentityHeaders() map[string]string {
	if rt == nil || rt.Bundle == nil {
		return nil
	}
	return rt.Bundle.IdentityHeaders()
}

func (rt *Runtime) String() string {
	return fmt.Sprintf("Runtime{job=%s status=%s}", rt.JobID, rt.status)
}
