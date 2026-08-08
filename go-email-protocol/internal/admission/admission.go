// Package admission implements 100-slot active job admission with seat guards.
package admission

import (
	"errors"
	"fmt"
	"sync"
)

// DefaultMaxActive is the G1 global concurrent active job cap.
const DefaultMaxActive = 200

// ErrRejected is returned when admission cannot grant a seat.
var ErrRejected = errors.New("admission rejected")

// Reason classifies rejection / seat type.
type Reason string

const (
	ReasonGlobal  Reason = "global"
	ReasonProxy   Reason = "proxy"
	ReasonMailbox Reason = "mailbox"
	ReasonDomain  Reason = "domain"
	ReasonQueue   Reason = "queue"
)

// RejectError carries a reason.
type RejectError struct {
	Reason Reason
	Detail string
}

func (e *RejectError) Error() string {
	if e.Detail != "" {
		return fmt.Sprintf("admission rejected: %s: %s", e.Reason, e.Detail)
	}
	return fmt.Sprintf("admission rejected: %s", e.Reason)
}

func (e *RejectError) Is(target error) bool {
	return target == ErrRejected
}

// Seat is a held admission grant for one job.
type Seat struct {
	JobID    string
	EmailKey string
	ProxyKey string
	Domain   string
}

// Config tunes admission limits.
type Config struct {
	MaxActive int
	// MaxPerProxy / MaxPerMailbox default to 1 (resource mutual exclusion).
	MaxPerProxy   int
	MaxPerMailbox int
	MaxPerDomain  int
	// MaxQueued bounds non-active queued jobs (0 = unlimited for tests that only care about active).
	MaxQueued int
}

// Snapshot is an aggregate, non-sensitive view of admission state.
type Snapshot struct {
	MaxActive   int
	ActiveCount int
	QueuedCount int
}

// Controller tracks active seats.
type Controller struct {
	mu     sync.Mutex
	cfg    Config
	active map[string]Seat // jobID -> seat
	proxy  map[string]int
	mail   map[string]int
	domain map[string]int
	queued int
}

// New creates a controller.
func New(cfg Config) *Controller {
	if cfg.MaxActive <= 0 {
		cfg.MaxActive = DefaultMaxActive
	}
	if cfg.MaxPerProxy <= 0 {
		cfg.MaxPerProxy = 1
	}
	if cfg.MaxPerMailbox <= 0 {
		cfg.MaxPerMailbox = 1
	}
	// MaxPerDomain 0 means disabled.
	return &Controller{
		cfg:    cfg,
		active: make(map[string]Seat),
		proxy:  make(map[string]int),
		mail:   make(map[string]int),
		domain: make(map[string]int),
	}
}

// ActiveCount returns currently held seats.
func (c *Controller) ActiveCount() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return len(c.active)
}

// Snapshot returns a consistent aggregate of the controller's admission state.
func (c *Controller) Snapshot() Snapshot {
	c.mu.Lock()
	defer c.mu.Unlock()
	return Snapshot{
		MaxActive:   c.cfg.MaxActive,
		ActiveCount: len(c.active),
		QueuedCount: c.queued,
	}
}

// Has reports whether job holds a seat.
func (c *Controller) Has(jobID string) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	_, ok := c.active[jobID]
	return ok
}

// TryAdmit attempts to grant a seat. Idempotent if job already holds one.
func (c *Controller) TryAdmit(seat Seat) error {
	if seat.JobID == "" {
		return &RejectError{Reason: ReasonGlobal, Detail: "empty job_id"}
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if _, ok := c.active[seat.JobID]; ok {
		return nil
	}
	if len(c.active) >= c.cfg.MaxActive {
		return &RejectError{Reason: ReasonGlobal, Detail: fmt.Sprintf("max_active=%d", c.cfg.MaxActive)}
	}
	if seat.ProxyKey != "" && c.proxy[seat.ProxyKey] >= c.cfg.MaxPerProxy {
		return &RejectError{Reason: ReasonProxy, Detail: seat.ProxyKey}
	}
	if seat.EmailKey != "" && c.mail[seat.EmailKey] >= c.cfg.MaxPerMailbox {
		return &RejectError{Reason: ReasonMailbox, Detail: seat.EmailKey}
	}
	if seat.Domain != "" && c.cfg.MaxPerDomain > 0 && c.domain[seat.Domain] >= c.cfg.MaxPerDomain {
		return &RejectError{Reason: ReasonDomain, Detail: seat.Domain}
	}
	c.active[seat.JobID] = seat
	if seat.ProxyKey != "" {
		c.proxy[seat.ProxyKey]++
	}
	if seat.EmailKey != "" {
		c.mail[seat.EmailKey]++
	}
	if seat.Domain != "" {
		c.domain[seat.Domain]++
	}
	return nil
}

// Release frees a seat (idempotent).
func (c *Controller) Release(jobID string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	seat, ok := c.active[jobID]
	if !ok {
		return
	}
	delete(c.active, jobID)
	if seat.ProxyKey != "" {
		c.proxy[seat.ProxyKey]--
		if c.proxy[seat.ProxyKey] <= 0 {
			delete(c.proxy, seat.ProxyKey)
		}
	}
	if seat.EmailKey != "" {
		c.mail[seat.EmailKey]--
		if c.mail[seat.EmailKey] <= 0 {
			delete(c.mail, seat.EmailKey)
		}
	}
	if seat.Domain != "" {
		c.domain[seat.Domain]--
		if c.domain[seat.Domain] <= 0 {
			delete(c.domain, seat.Domain)
		}
	}
}

// TryQueue increments queued counter if under MaxQueued (MaxQueued<=0 means no queue limit).
func (c *Controller) TryQueue() error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.cfg.MaxQueued > 0 && c.queued >= c.cfg.MaxQueued {
		return &RejectError{Reason: ReasonQueue, Detail: fmt.Sprintf("max_queued=%d", c.cfg.MaxQueued)}
	}
	c.queued++
	return nil
}

// Dequeue decrements queued counter.
func (c *Controller) Dequeue() {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.queued > 0 {
		c.queued--
	}
}

// SnapshotActiveJobIDs returns a copy of active job IDs.
func (c *Controller) SnapshotActiveJobIDs() []string {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make([]string, 0, len(c.active))
	for id := range c.active {
		out = append(out, id)
	}
	return out
}

// SeatOf returns seat for job if present.
func (c *Controller) SeatOf(jobID string) (Seat, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	s, ok := c.active[jobID]
	return s, ok
}
