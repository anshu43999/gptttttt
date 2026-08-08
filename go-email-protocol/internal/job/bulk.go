package job

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math/big"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/gpt-register/go-email-protocol/internal/accounts"
	"github.com/gpt-register/go-email-protocol/internal/mailbox"
	proxypool "github.com/gpt-register/go-email-protocol/internal/proxy"
	"github.com/gpt-register/go-email-protocol/internal/taskstore"
)

// BulkCreateRequest is a Go-owned registration batch. The caller supplies only
// policy; the daemon leases mailbox/proxy resources, creates live protocol jobs,
// waits for their terminal state, writes dashboard task rows, and imports accounts.
type BulkCreateRequest struct {
	BatchID           string   `json:"batch_id"`
	Count             int      `json:"count"`
	MaxConcurrent     int      `json:"max_concurrent"`
	MailboxProvider   string   `json:"mailbox_provider"`
	ProxyStyles       []string `json:"proxy_styles"`
	// ProxyRegion is a single region or comma list (JP,US,DE,GB,BR). Prefer ProxyRegions.
	ProxyRegion string `json:"proxy_region"`
	// ProxyRegions multi-region rotation pool (canary parity).
	ProxyRegions []string `json:"proxy_regions"`
	ProxyTTLSeconds   int `json:"proxy_ttl_seconds"`
	OTPTimeoutSeconds int `json:"otp_timeout_seconds"`
	TimeoutSeconds    int `json:"timeout_seconds"`
	// EmailTries re-lease mailbox on already-used / dead OTP (canary -email-tries).
	EmailTries int  `json:"email_tries"`
	SkipPhone  bool `json:"skip_phone"`
}

// BulkStatus contains no mailbox, proxy, token, or capability secrets.
type BulkStatus struct {
	BatchID        string    `json:"batch_id"`
	TaskIDs        []string  `json:"task_ids,omitempty"`
	Count          int       `json:"count"`
	MaxConcurrent  int       `json:"max_concurrent"`
	Queued         int       `json:"queued"`
	Running        int       `json:"running"`
	WaitingForOTP  int       `json:"waiting_for_otp"`
	ProtocolActive int       `json:"protocol_active"`
	Succeeded      int       `json:"succeeded"`
	Failed         int       `json:"failed"`
	Cancelled      int       `json:"cancelled"`
	Done           bool      `json:"done"`
	CreatedAt      time.Time `json:"created_at"`
	FinishedAt     time.Time `json:"finished_at,omitempty"`
}

type bulkState struct {
	req      BulkCreateRequest
	ctx      context.Context
	cancel   context.CancelFunc
	taskIDs  []string
	created  time.Time
	finished time.Time

	mu             sync.Mutex
	queued         int
	running        int
	waitingForOTP  int
	protocolActive int
	succeeded      int
	failed         int
	cancelled      int
	done           map[string]bool
	jobs           map[string]bulkJob
	phases         map[string]string // taskID -> protocol|otp|done
	// otpWG tracks detached OTP waiters so finishBulk waits for continuous refill.
	otpWG sync.WaitGroup
}

type bulkJob struct {
	jobID      string
	capability string
}

func (m *Manager) StartBulk(req BulkCreateRequest) (*BulkStatus, error) {
	if err := validateBulkRequest(&req); err != nil {
		return nil, err
	}
	if m.businessDBPath == "" {
		return nil, fmt.Errorf("bulk: business database path is not configured")
	}
	tasks, err := taskstore.CreateRegistrationTasks(m.businessDBPath, req.BatchID, req.Count)
	if err != nil {
		return nil, err
	}
	ctx, cancel := context.WithCancel(context.Background())
	state := &bulkState{
		req: req, ctx: ctx, cancel: cancel, taskIDs: tasks, created: time.Now().UTC(),
		queued: len(tasks), done: make(map[string]bool, len(tasks)), jobs: make(map[string]bulkJob, len(tasks)),
		phases: make(map[string]string, len(tasks)),
	}
	m.bulkMu.Lock()
	if m.batches == nil {
		m.batches = make(map[string]*bulkState)
	}
	if _, exists := m.batches[req.BatchID]; exists {
		m.bulkMu.Unlock()
		cancel()
		return nil, &ConflictError{Code: "batch_exists", Message: "batch_id already exists"}
	}
	m.batches[req.BatchID] = state
	m.bulkMu.Unlock()
	m.goRun(func() { m.runBulk(state) })
	return bulkSnapshot(state, true), nil
}

func (m *Manager) GetBulk(batchID string) (*BulkStatus, error) {
	m.bulkMu.Lock()
	state := m.batches[batchID]
	m.bulkMu.Unlock()
	if state == nil {
		return nil, fmt.Errorf("bulk: batch not found")
	}
	return bulkSnapshot(state, true), nil
}

func (m *Manager) CancelBulk(batchID string) (*BulkStatus, error) {
	m.bulkMu.Lock()
	state := m.batches[batchID]
	m.bulkMu.Unlock()
	if state == nil {
		return nil, fmt.Errorf("bulk: batch not found")
	}
	state.cancel()
	state.mu.Lock()
	jobs := make([]bulkJob, 0, len(state.jobs))
	for _, entry := range state.jobs {
		jobs = append(jobs, entry)
	}
	state.mu.Unlock()
	for _, entry := range jobs {
		if entry.jobID != "" && entry.capability != "" {
			_, _ = m.Cancel(context.Background(), entry.jobID, entry.capability)
		}
	}
	return bulkSnapshot(state, true), nil
}

func (m *Manager) runBulk(state *bulkState) {
	// Protocol workers only. OTP wait is detached so a free seat is refilled
	// immediately instead of holding a worker for the whole OTP tail.
	workers := state.req.MaxConcurrent
	queue := make(chan string)
	var wg sync.WaitGroup
	for range workers {
		wg.Add(1)
		m.goRun(func() {
			defer wg.Done()
			for taskID := range queue {
				m.runBulkTask(state, taskID)
			}
		})
	}
	for _, taskID := range state.taskIDs {
		select {
		case <-state.ctx.Done():
			close(queue)
			wg.Wait()
			state.otpWG.Wait()
			m.cancelUndoneBulkTasks(state)
			m.finishBulk(state)
			return
		case queue <- taskID:
		}
	}
	close(queue)
	wg.Wait()
	// Protocol workers drained; OTP waiters may still be finishing.
	state.otpWG.Wait()
	if state.ctx.Err() != nil {
		m.cancelUndoneBulkTasks(state)
	}
	m.finishBulk(state)
}

func (m *Manager) runBulkTask(state *bulkState, taskID string) {
	if state.ctx.Err() != nil {
		m.completeBulkTask(state, taskID, "cancelled")
		_ = taskstore.Cancel(m.businessDBPath, taskID, state.req.BatchID, "")
		return
	}
	// Stagger starts so authorize isn't a perfect simultaneous burst.
	// Keep short: high concurrency needs fast seat fill (was 1500ms → 400ms).
	// Multi-region + remint already diversify identity; long stagger only wastes ramp.
	var stagger int
	for _, ch := range taskID {
		stagger = (stagger*31 + int(ch)) & 0xffff
	}
	delay := time.Duration(stagger%400) * time.Millisecond
	if delay > 0 {
		select {
		case <-state.ctx.Done():
			m.completeBulkTask(state, taskID, "cancelled")
			_ = taskstore.Cancel(m.businessDBPath, taskID, state.req.BatchID, "")
			return
		case <-time.After(delay):
		}
	}
	m.setBulkRunning(state, taskID)

	emailTries := state.req.EmailTries
	if emailTries < 1 {
		emailTries = 1
	}
	regions := state.req.ProxyRegions
	if len(regions) == 0 {
		regions = proxypool.ParseSeedRegions(state.req.ProxyRegion)
	}
	regionIdx := 0
	// Spread initial region by task id.
	if len(regions) > 1 {
		picked := proxypool.PickSeedRegion(regions, taskID)
		for i, r := range regions {
			if r == picked {
				regionIdx = i
				break
			}
		}
	}

	var lastErr string
	for try := 1; try <= emailTries; try++ {
		if state.ctx.Err() != nil {
			m.completeBulkTask(state, taskID, "cancelled")
			_ = taskstore.Cancel(m.businessDBPath, taskID, state.req.BatchID, "")
			return
		}
		region := regions[regionIdx%len(regions)]
		ok, retryEmail, errMsg := m.runBulkTaskAttempt(state, taskID, region, try)
		if ok {
			return
		}
		lastErr = errMsg
		if !retryEmail || try >= emailTries {
			if lastErr == "" {
				lastErr = "registration failed"
			}
			// failBulkTask already called inside attempt when terminal fail without retry.
			if !strings.Contains(strings.ToLower(lastErr), "already failed") {
				// attempt may have already recorded failure; only fail if still open
				state.mu.Lock()
				already := state.done[taskID]
				state.mu.Unlock()
				if !already {
					m.failBulkTask(state, taskID, "", lastErr, true)
				}
			}
			return
		}
		// Rotate region for next email attempt (canary parity).
		regionIdx = (regionIdx + 1) % len(regions)
		select {
		case <-state.ctx.Done():
			m.completeBulkTask(state, taskID, "cancelled")
			_ = taskstore.Cancel(m.businessDBPath, taskID, state.req.BatchID, "")
			return
		case <-time.After(200 * time.Millisecond):
		}
	}
	if lastErr == "" {
		lastErr = "exhausted email tries"
	}
	state.mu.Lock()
	already := state.done[taskID]
	state.mu.Unlock()
	if !already {
		m.failBulkTask(state, taskID, "", lastErr, true)
	}
}

// runBulkTaskAttempt leases one mailbox + proxy, runs one Create through OTP/terminal.
// retryEmail=true means caller should re-lease a new mailbox (already-used / dead OTP).
func (m *Manager) runBulkTaskAttempt(state *bulkState, taskID, region string, try int) (ok bool, retryEmail bool, errMsg string) {
	mail, err := mailbox.LeaseFromDBProvider(m.businessDBPath, taskID, state.req.MailboxProvider)
	if err != nil {
		return false, false, "mailbox lease: " + err.Error()
	}
	mailDone := false
	defer func() {
		if !mailDone {
			_ = mailbox.MarkUsed(m.businessDBPath, mail.ID, "cooldown", "Go batch stopped before terminal result")
		}
	}()

	proxySession, err := proxypool.MintSeedSession(m.businessDBPath, fmt.Sprintf("%s_t%d", taskID, try), state.req.ProxyStyles, region, state.req.ProxyTTLSeconds)
	if err != nil {
		_ = mailbox.MarkUsed(m.businessDBPath, mail.ID, "available", "proxy seed unavailable")
		mailDone = true
		return false, false, err.Error()
	}
	password, err := bulkPassword()
	if err != nil {
		_ = mailbox.MarkUsed(m.businessDBPath, mail.ID, "available", "password generation failed")
		mailDone = true
		return false, false, err.Error()
	}
	deadline := time.Now().UTC().Add(time.Duration(state.req.TimeoutSeconds) * time.Second)
	fingerprint := bulkFingerprint(taskID, mail.Email, proxySession.ResourceKey, proxySession.URL, fmt.Sprintf("try%d", try))
	created, err := m.Create(state.ctx, CreateRequest{
		TaskID: taskID, AttemptID: try, IdempotencyKey: fmt.Sprintf("go-batch-%s-t%d", taskID, try),
		RequestFingerprint: fingerprint, Email: mail.Email, Password: password,
		MailboxClientID: mail.ClientID, MailboxRefreshToken: mail.RefreshToken,
		OTPTimeoutSeconds: state.req.OTPTimeoutSeconds, SkipPhone: state.req.SkipPhone, DeadlineAt: deadline,
		ResourceGrant: ResourceGrant{
			EmailKey: mail.ResourceKey, ProxyKey: bulkFingerprint(taskID, proxySession.ResourceKey, fmt.Sprintf("try%d", try)),
			Bridge: BridgeGrant{BridgeID: "direct-socks", URL: proxySession.URL, Capability: "direct", Generation: 1, Protocol: "socks5h"},
			ExpectedCountry: proxySession.Region,
		},
		Profile: json.RawMessage(`{"id":"go-batch-profile"}`),
	})
	if err != nil {
		msg := err.Error()
		mailStatus := mailboxStatusForFailure(msg)
		_ = mailbox.MarkUsed(m.businessDBPath, mail.ID, mailStatus, safeBulkMessage(msg))
		mailDone = true
		if isBulkEmailRetryable(msg) {
			return false, true, msg
		}
		return false, false, msg
	}
	job := bulkJob{jobID: created.JobID, capability: created.JobCapability}
	state.mu.Lock()
	state.jobs[taskID] = job
	state.mu.Unlock()
	_ = taskstore.StartJob(m.businessDBPath, taskID, state.req.BatchID, created.JobID)
	m.setBulkPhase(state, taskID, "protocol")

	// Continuous pipeline: wait only until OTP park or early terminal.
	view, err := m.waitBulkUntilOTPOrTerminal(state.ctx, state, taskID, job)
	if err != nil {
		if state.ctx.Err() != nil {
			_ = mailbox.MarkUsed(m.businessDBPath, mail.ID, "cooldown", "Go batch cancelled")
			mailDone = true
			_ = taskstore.Cancel(m.businessDBPath, taskID, state.req.BatchID, job.jobID)
			m.completeBulkTask(state, taskID, "cancelled")
			return true, false, "" // cancelled is terminal for this task
		}
		msg := err.Error()
		if isBulkEmailRetryable(msg) {
			_ = mailbox.MarkUsed(m.businessDBPath, mail.ID, mailboxStatusForFailure(msg), safeBulkMessage(msg))
			mailDone = true
			return false, true, msg
		}
		_ = mailbox.MarkUsed(m.businessDBPath, mail.ID, "cooldown", safeBulkMessage(msg))
		mailDone = true
		m.failBulkTask(state, taskID, job.jobID, msg, true)
		return true, false, msg // already failed
	}
	if view.Status == StatusWaitingForOTP {
		// Detach OTP+finish. On dead mailbox OTP, re-lease within email_tries
		// (canary parity) instead of terminal-failing the whole bulk task.
		mailDone = true
		state.otpWG.Add(1)
		m.goRun(func() {
			defer state.otpWG.Done()
			retryEmail, errMsg := m.finishBulkTaskAfterOTP(state, taskID, job, mail, password, proxySession)
			if !retryEmail {
				return
			}
			// Remaining tries after this attempt index.
			for next := try + 1; next <= emailTriesCap(state); next++ {
				if state.ctx.Err() != nil {
					m.completeBulkTask(state, taskID, "cancelled")
					_ = taskstore.Cancel(m.businessDBPath, taskID, state.req.BatchID, "")
					return
				}
				// Rotate region like runBulkTask email-tries loop.
				regions := bulkRegions(state)
				regionNext := regions[0]
				if len(regions) > 0 {
					regionNext = regions[(next-1)%len(regions)]
				}
				ok, again, msg := m.runBulkTaskAttempt(state, taskID, regionNext, next)
				if ok {
					return
				}
				errMsg = msg
				if !again {
					break
				}
				select {
				case <-state.ctx.Done():
					m.completeBulkTask(state, taskID, "cancelled")
					_ = taskstore.Cancel(m.businessDBPath, taskID, state.req.BatchID, "")
					return
				case <-time.After(200 * time.Millisecond):
				}
			}
			state.mu.Lock()
			already := state.done[taskID]
			state.mu.Unlock()
			if !already {
				if errMsg == "" {
					errMsg = "otp mailbox exhausted email tries"
				}
				m.failBulkTask(state, taskID, "", errMsg, true)
			}
		})
		return true, false, ""
	}
	// Early terminal before OTP.
	if view.Status != StatusSucceeded {
		msg := view.Message
		if msg == "" {
			msg = view.FailureCode
		}
		if isBulkEmailRetryable(msg) || isBulkEmailRetryable(view.FailureCode) {
			_ = mailbox.MarkUsed(m.businessDBPath, mail.ID, mailboxStatusForFailure(msg), safeBulkMessage(msg))
			mailDone = true
			return false, true, msg
		}
	}
	m.finalizeBulkTaskResult(state, taskID, job, mail, password, proxySession, view, &mailDone)
	return true, false, ""
}

func isBulkEmailRetryable(message string) bool {
	text := strings.ToLower(message)
	switch {
	case strings.Contains(text, "already exists"),
		strings.Contains(text, "already used"),
		strings.Contains(text, "email_already_used"),
		strings.Contains(text, "user_already_exists"),
		strings.Contains(text, "deleted or deactivated"),
		strings.Contains(text, "no openai"),
		strings.Contains(text, "otp_timeout"),
		strings.Contains(text, "otp timeout"),
		strings.Contains(text, "mailbox dead"),
		strings.Contains(text, "invalid_grant"):
		return true
	default:
		return false
	}
}

// finishBulkTaskAfterOTP continues a parked job until terminal and finalizes resources.
// Returns (retryEmail, errMsg): retryEmail means caller should re-lease a new mailbox.
func (m *Manager) finishBulkTaskAfterOTP(state *bulkState, taskID string, job bulkJob, mail *mailbox.Account, password string, proxySession *proxypool.SeedSession) (retryEmail bool, errMsg string) {
	mailDone := false
	defer func() {
		if !mailDone {
			_ = mailbox.MarkUsed(m.businessDBPath, mail.ID, "cooldown", "Go batch OTP waiter stopped early")
		}
	}()
	view, err := m.waitBulkTerminal(state.ctx, state, taskID, job)
	if err != nil {
		if state.ctx.Err() != nil {
			_ = mailbox.MarkUsed(m.businessDBPath, mail.ID, "cooldown", "Go batch cancelled")
			mailDone = true
			_ = taskstore.Cancel(m.businessDBPath, taskID, state.req.BatchID, job.jobID)
			m.completeBulkTask(state, taskID, "cancelled")
			return false, ""
		}
		msg := err.Error()
		if isBulkEmailRetryable(msg) {
			_ = mailbox.MarkUsed(m.businessDBPath, mail.ID, mailboxStatusForFailure(msg), safeBulkMessage(msg))
			mailDone = true
			return true, msg
		}
		_ = mailbox.MarkUsed(m.businessDBPath, mail.ID, "cooldown", safeBulkMessage(msg))
		mailDone = true
		m.failBulkTask(state, taskID, job.jobID, msg, true)
		return false, msg
	}
	if view.Status != StatusSucceeded {
		msg := view.Message
		if msg == "" {
			msg = view.FailureCode
		}
		if isBulkEmailRetryable(msg) || isBulkEmailRetryable(view.FailureCode) {
			_ = mailbox.MarkUsed(m.businessDBPath, mail.ID, mailboxStatusForFailure(msg+" "+view.FailureCode), safeBulkMessage(msg))
			mailDone = true
			return true, msg
		}
	}
	m.finalizeBulkTaskResult(state, taskID, job, mail, password, proxySession, view, &mailDone)
	return false, ""
}

func emailTriesCap(state *bulkState) int {
	if state == nil {
		return 1
	}
	n := state.req.EmailTries
	if n < 1 {
		return 1
	}
	if n > 20 {
		return 20
	}
	return n
}

func bulkRegions(state *bulkState) []string {
	if state == nil {
		return proxypool.ParseSeedRegions("")
	}
	regions := state.req.ProxyRegions
	if len(regions) == 0 {
		regions = proxypool.ParseSeedRegions(state.req.ProxyRegion)
	}
	if len(regions) == 0 {
		return []string{"JP"}
	}
	return regions
}

func (m *Manager) finalizeBulkTaskResult(state *bulkState, taskID string, job bulkJob, mail *mailbox.Account, password string, proxySession *proxypool.SeedSession, view *StatusView, mailDone *bool) {
	if view.Status != StatusSucceeded || view.Session == nil || strings.TrimSpace(view.Session.AccessToken) == "" {
		message := view.Message
		if message == "" {
			message = view.FailureCode
		}
		if message == "" {
			message = "registration did not return a session"
		}
		_ = mailbox.MarkUsed(m.businessDBPath, mail.ID, mailboxStatusForFailure(message), safeBulkMessage(message))
		*mailDone = true
		m.failBulkTask(state, taskID, job.jobID, message, view.Retryable)
		return
	}
	_, _ = accounts.ImportRegistered(m.businessDBPath, accounts.Record{
		Email: mail.Email, Password: password, AccessToken: view.Session.AccessToken,
		AccountID: view.Session.AccountID, TaskID: taskID, ProxyURL: redactBulkProxy(proxySession.URL),
		ProxyRegion: proxySession.Region, Engine: "go-daemon-batch", PlanType: view.Session.PlanType,
	})
	_ = mailbox.MarkUsed(m.businessDBPath, mail.ID, "used", "")
	*mailDone = true
	_ = taskstore.Succeed(m.businessDBPath, taskID, state.req.BatchID, job.jobID, mail.Email, view.Session.AccountID)
	m.completeBulkTask(state, taskID, "succeeded")
}

// waitBulkUntilOTPOrTerminal returns as soon as the job parks for OTP or ends.
// This is the continuous-pipeline handoff point.
func (m *Manager) waitBulkUntilOTPOrTerminal(ctx context.Context, state *bulkState, taskID string, job bulkJob) (*StatusView, error) {
	for {
		view, err := m.Get(ctx, job.jobID, job.capability, 30000)
		if err != nil {
			return nil, err
		}
		switch view.Status {
		case StatusSucceeded, StatusFailed, StatusCancelled, StatusReconcileRequired:
			return view, nil
		case StatusWaitingForOTP:
			m.setBulkPhase(state, taskID, "otp")
			return view, nil
		default:
			m.setBulkPhase(state, taskID, "protocol")
		}
		select {
		case <-ctx.Done():
			_, _ = m.Cancel(context.Background(), job.jobID, job.capability)
			return nil, ctx.Err()
		default:
		}
	}
}

func (m *Manager) waitBulkTerminal(ctx context.Context, state *bulkState, taskID string, job bulkJob) (*StatusView, error) {
	for {
		view, err := m.Get(ctx, job.jobID, job.capability, 30000)
		if err != nil {
			return nil, err
		}
		switch view.Status {
		case StatusSucceeded, StatusFailed, StatusCancelled, StatusReconcileRequired:
			return view, nil
		case StatusWaitingForOTP:
			m.setBulkPhase(state, taskID, "otp")
		default:
			m.setBulkPhase(state, taskID, "protocol")
		}
		select {
		case <-ctx.Done():
			_, _ = m.Cancel(context.Background(), job.jobID, job.capability)
			return nil, ctx.Err()
		default:
		}
	}
}

func (m *Manager) setBulkPhase(state *bulkState, taskID, phase string) {
	state.mu.Lock()
	defer state.mu.Unlock()
	if state.done[taskID] {
		return
	}
	prev := state.phases[taskID]
	if prev == phase {
		return
	}
	// Adjust counters when phase transitions.
	switch prev {
	case "otp":
		if state.waitingForOTP > 0 {
			state.waitingForOTP--
		}
	case "protocol":
		if state.protocolActive > 0 {
			state.protocolActive--
		}
	}
	switch phase {
	case "otp":
		state.waitingForOTP++
	case "protocol":
		state.protocolActive++
	}
	state.phases[taskID] = phase
}

func (m *Manager) setBulkRunning(state *bulkState, taskID string) {
	state.mu.Lock()
	if state.done[taskID] {
		state.mu.Unlock()
		return
	}
	if state.queued > 0 {
		state.queued--
	}
	state.running++
	state.mu.Unlock()
}

func (m *Manager) completeBulkTask(state *bulkState, taskID, outcome string) {
	state.mu.Lock()
	defer state.mu.Unlock()
	if state.done[taskID] {
		return
	}
	state.done[taskID] = true
	switch state.phases[taskID] {
	case "otp":
		if state.waitingForOTP > 0 {
			state.waitingForOTP--
		}
	case "protocol":
		if state.protocolActive > 0 {
			state.protocolActive--
		}
	}
	delete(state.phases, taskID)
	if state.running > 0 {
		state.running--
	} else if state.queued > 0 {
		state.queued--
	}
	switch outcome {
	case "succeeded":
		state.succeeded++
	case "cancelled":
		state.cancelled++
	default:
		state.failed++
	}
}

func (m *Manager) failBulkTask(state *bulkState, taskID, jobID, message string, retryable bool) {
	_ = taskstore.Fail(m.businessDBPath, taskID, state.req.BatchID, jobID, safeBulkMessage(message), retryable)
	m.completeBulkTask(state, taskID, "failed")
}

func (m *Manager) cancelUndoneBulkTasks(state *bulkState) {
	for _, taskID := range state.taskIDs {
		state.mu.Lock()
		done := state.done[taskID]
		job := state.jobs[taskID]
		state.mu.Unlock()
		if done {
			continue
		}
		if job.jobID != "" && job.capability != "" {
			_, _ = m.Cancel(context.Background(), job.jobID, job.capability)
		}
		_ = taskstore.Cancel(m.businessDBPath, taskID, state.req.BatchID, job.jobID)
		m.completeBulkTask(state, taskID, "cancelled")
	}
}

func (m *Manager) finishBulk(state *bulkState) {
	state.mu.Lock()
	state.finished = time.Now().UTC()
	state.mu.Unlock()
}

func bulkSnapshot(state *bulkState, includeIDs bool) *BulkStatus {
	state.mu.Lock()
	defer state.mu.Unlock()
	out := &BulkStatus{
		BatchID: state.req.BatchID, Count: len(state.taskIDs), MaxConcurrent: state.req.MaxConcurrent,
		Queued: state.queued, Running: state.running, WaitingForOTP: state.waitingForOTP,
		ProtocolActive: state.protocolActive, Succeeded: state.succeeded, Failed: state.failed,
		Cancelled: state.cancelled, Done: !state.finished.IsZero(), CreatedAt: state.created, FinishedAt: state.finished,
	}
	if includeIDs {
		out.TaskIDs = append([]string(nil), state.taskIDs...)
	}
	return out
}
func validateBulkRequest(req *BulkCreateRequest) error {
	req.BatchID = strings.TrimSpace(req.BatchID)
	if req.BatchID == "" {
		return &ValidationError{Code: "batch_id_required", Message: "batch_id required"}
	}
	if req.Count < 1 {
		return &ValidationError{Code: "invalid_count", Message: "count must be >= 1"}
	}
	if req.MaxConcurrent < 1 {
		req.MaxConcurrent = 1
	}
	// Continuous pipeline: workers refill during OTP, so max_concurrent may exceed
	// remaining count only as a protocol-seat target — keep as requested up to count.
	if req.MaxConcurrent > req.Count {
		req.MaxConcurrent = req.Count
	}
	if provider := strings.ToLower(strings.TrimSpace(req.MailboxProvider)); provider == "" || provider == "outlook" || provider == "hotmail" || provider == "graph" || provider == "outlook_token" {
		req.MailboxProvider = mailbox.ProviderOutlookToken
	} else {
		return &ValidationError{Code: "unsupported_mailbox", Message: "Go batch supports outlook_token only"}
	}
	if req.ProxyTTLSeconds < 1 {
		req.ProxyTTLSeconds = 15
	}
	// Success returns early (~30s). Dead path: early abort → resend → remain.
	if req.OTPTimeoutSeconds <= 0 {
		req.OTPTimeoutSeconds = 120
	}
	if req.OTPTimeoutSeconds < 60 {
		req.OTPTimeoutSeconds = 60
	}
	if req.OTPTimeoutSeconds > 240 {
		req.OTPTimeoutSeconds = 240
	}
	if req.TimeoutSeconds < req.OTPTimeoutSeconds+60 {
		req.TimeoutSeconds = req.OTPTimeoutSeconds + 90
	}
	if req.TimeoutSeconds > 1800 {
		req.TimeoutSeconds = 1800
	}
	if len(req.ProxyStyles) == 0 {
		req.ProxyStyles = []string{"bestgo", "1024"}
	}
	// Multi-region: prefer ProxyRegions; else parse ProxyRegion CSV; else canary default.
	if len(req.ProxyRegions) == 0 {
		req.ProxyRegions = proxypool.ParseSeedRegions(req.ProxyRegion)
	} else {
		// Normalize list.
		req.ProxyRegions = proxypool.ParseSeedRegions(strings.Join(req.ProxyRegions, ","))
	}
	if len(req.ProxyRegions) > 0 {
		req.ProxyRegion = req.ProxyRegions[0]
	}
	if req.EmailTries < 1 {
		req.EmailTries = 5 // canary_n10p forces 5; pure-go-register default 20
	}
	if req.EmailTries > 20 {
		req.EmailTries = 20
	}
	return nil
}

func bulkPassword() (string, error) {
	const alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
	buf := make([]byte, 14)
	mod := big.NewInt(int64(len(alphabet)))
	for index := range buf {
		n, err := rand.Int(rand.Reader, mod)
		if err != nil {
			return "", err
		}
		buf[index] = alphabet[n.Int64()]
	}
	return string(buf) + "Aa1!", nil
}

func bulkFingerprint(parts ...string) string {
	hash := sha256.Sum256([]byte(strings.Join(parts, "\x00")))
	return "sha256:" + hex.EncodeToString(hash[:])
}

func mailboxStatusForFailure(message string) string {
	text := strings.ToLower(message)
	switch {
	case strings.Contains(text, "already exists"),
		strings.Contains(text, "already used"),
		strings.Contains(text, "email_already_used"),
		strings.Contains(text, "user_already_exists"),
		strings.Contains(text, "deleted or deactivated"):
		return "used"
	case strings.Contains(text, "invalid_grant"), strings.Contains(text, "invalid refresh"), strings.Contains(text, "unauthorized_client"):
		return "disabled"
	default:
		return "cooldown"
	}
}

func safeBulkMessage(message string) string {
	message = strings.ReplaceAll(strings.TrimSpace(message), "\n", " ")
	if len(message) > 300 {
		return message[:300]
	}
	return message
}

func redactBulkProxy(raw string) string {
	if parsed, err := url.Parse(raw); err == nil && parsed.Host != "" {
		return parsed.Scheme + "://" + parsed.Host
	}
	return ""
}
