// Package api implements the G1 V2 HTTP surface for the email-protocol worker.
//
// Auth: subsequent GET/OTP/DELETE require header X-Job-Capability: <job_capability>.
// Default bind is loopback 127.0.0.1.
package api

import (
	"context"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/gpt-register/go-email-protocol/internal/admission"
	"github.com/gpt-register/go-email-protocol/internal/job"
	"github.com/gpt-register/go-email-protocol/internal/ledger"
	"github.com/gpt-register/go-email-protocol/internal/plusverify"
)

// Server is the V2 HTTP API.
type Server struct {
	mgr         *job.Manager
	mux         *http.ServeMux
	version     string
	runtime     RuntimeInfo
	plusService *plusverify.Service
}

// RuntimeInfo is non-sensitive process mode (exposed on /health and /diagnostics).
// Lets Python refuse silent mailat fallback when config expects pure-Go.
type RuntimeInfo struct {
	Runner             string `json:"runner"`        // mailat | protocol
	ProtocolMode       string `json:"protocol_mode"` // live | engine | synthetic | empty for mailat
	Transport          string `json:"transport"`     // fake | tls | direct
	GraphMaxConcurrent int    `json:"graph_max_concurrent"`
}

type diagnosticsResponse struct {
	Phase              string `json:"phase"`
	Version            string `json:"version"`
	Runner             string `json:"runner"`
	ProtocolMode       string `json:"protocol_mode,omitempty"`
	Transport          string `json:"transport"`
	MaxActive          int    `json:"max_active"`
	ActiveCount        int    `json:"active_count"`
	QueuedCount        int    `json:"queued_count"`
	GraphMaxConcurrent int    `json:"graph_max_concurrent"`
}

// New constructs the API server.
func New(mgr *job.Manager, version string, info RuntimeInfo) *Server {
	if version == "" {
		version = "0.1.0-g1"
	}
	if strings.TrimSpace(info.Runner) == "" {
		info.Runner = "unknown"
	}
	if strings.TrimSpace(info.Transport) == "" {
		info.Transport = "unknown"
	}
	s := &Server{mgr: mgr, mux: http.NewServeMux(), version: version, runtime: info, plusService: plusverify.New()}
	s.routes()
	return s
}

// Handler returns the root handler.
func (s *Server) Handler() http.Handler { return s.mux }

// ListenAndServe binds addr (prefer 127.0.0.1:port) and serves until ctx done.
func (s *Server) ListenAndServe(ctx context.Context, addr string) error {
	if addr == "" {
		addr = "127.0.0.1:0"
	}
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		return err
	}
	srv := &http.Server{Handler: s.mux}
	go func() {
		<-ctx.Done()
		shctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_ = srv.Shutdown(shctx)
	}()
	err = srv.Serve(ln)
	if errors.Is(err, http.ErrServerClosed) {
		return nil
	}
	return err
}

func (s *Server) routes() {
	s.mux.HandleFunc("GET /health", s.handleHealth)
	s.mux.HandleFunc("GET /diagnostics", s.handleDiagnostics)
	s.mux.HandleFunc("POST /v2/email-register", s.handleCreate)
	s.mux.HandleFunc("GET /v2/email-register/{job_id}", s.handleGet)
	s.mux.HandleFunc("POST /v2/email-register/{job_id}/otp", s.handleOTP)
	s.mux.HandleFunc("DELETE /v2/email-register/{job_id}", s.handleDelete)
	s.mux.HandleFunc("POST /v2/email-register-batches", s.handleBulkCreate)
	s.mux.HandleFunc("GET /v2/email-register-batches/{batch_id}", s.handleBulkGet)
	s.mux.HandleFunc("DELETE /v2/email-register-batches/{batch_id}", s.handleBulkDelete)
	// Multi-worker Plus / subscription checks (replaces Python ThreadPool verify_plus_batch).
	s.mux.HandleFunc("POST /v2/plus-verify", s.handlePlusVerifyBatch)
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	snapshot := s.mgr.Admission().Snapshot()
	writeJSON(w, http.StatusOK, map[string]any{
		"status":               "ok",
		"phase":                "g1",
		"version":              s.version,
		"runner":               s.runtime.Runner,
		"protocol_mode":        s.runtime.ProtocolMode,
		"transport":            s.runtime.Transport,
		"max_active":           snapshot.MaxActive,
		"active_count":         snapshot.ActiveCount,
		"graph_max_concurrent": s.runtime.GraphMaxConcurrent,
		"features":             []string{"email-register", "email-register-batches", "plus-verify"},
	})
}

func (s *Server) handleDiagnostics(w http.ResponseWriter, r *http.Request) {
	if !isLoopbackRequest(r) {
		writeErr(w, http.StatusForbidden, "loopback_required", "diagnostics are available only from loopback")
		return
	}
	snapshot := s.mgr.Admission().Snapshot()
	writeJSON(w, http.StatusOK, diagnosticsResponse{
		Phase:              "g1",
		Version:            s.version,
		Runner:             s.runtime.Runner,
		ProtocolMode:       s.runtime.ProtocolMode,
		Transport:          s.runtime.Transport,
		MaxActive:          snapshot.MaxActive,
		ActiveCount:        snapshot.ActiveCount,
		QueuedCount:        snapshot.QueuedCount,
		GraphMaxConcurrent: s.runtime.GraphMaxConcurrent,
	})
}

func isLoopbackRequest(r *http.Request) bool {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		host = r.RemoteAddr
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

func (s *Server) handlePlusVerifyBatch(w http.ResponseWriter, r *http.Request) {
	var req plusverify.BatchRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid_json", err.Error())
		return
	}
	if len(req.Items) == 0 {
		writeErr(w, http.StatusBadRequest, "empty_items", "items required")
		return
	}
	if len(req.Items) > 2000 {
		writeErr(w, http.StatusBadRequest, "too_many_items", "max 2000 items per batch")
		return
	}
	svc := s.plusService
	if svc == nil {
		svc = plusverify.New()
	}
	out := svc.VerifyBatch(r.Context(), req)
	writeJSON(w, http.StatusOK, out)
}

func (s *Server) handleCreate(w http.ResponseWriter, r *http.Request) {
	var req job.CreateRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid_json", err.Error())
		return
	}
	view, err := s.mgr.Create(r.Context(), req)
	if err != nil {
		s.mapErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, view)
}

func (s *Server) handleBulkCreate(w http.ResponseWriter, r *http.Request) {
	if !isLoopbackRequest(r) {
		writeErr(w, http.StatusForbidden, "loopback_required", "batch registration is available only from loopback")
		return
	}
	var req job.BulkCreateRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid_json", err.Error())
		return
	}
	view, err := s.mgr.StartBulk(req)
	if err != nil {
		s.mapErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, view)
}

func (s *Server) handleBulkGet(w http.ResponseWriter, r *http.Request) {
	if !isLoopbackRequest(r) {
		writeErr(w, http.StatusForbidden, "loopback_required", "batch registration is available only from loopback")
		return
	}
	view, err := s.mgr.GetBulk(r.PathValue("batch_id"))
	if err != nil {
		writeErr(w, http.StatusNotFound, "not_found", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, view)
}

func (s *Server) handleBulkDelete(w http.ResponseWriter, r *http.Request) {
	if !isLoopbackRequest(r) {
		writeErr(w, http.StatusForbidden, "loopback_required", "batch registration is available only from loopback")
		return
	}
	view, err := s.mgr.CancelBulk(r.PathValue("batch_id"))
	if err != nil {
		writeErr(w, http.StatusNotFound, "not_found", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, view)
}

func (s *Server) handleGet(w http.ResponseWriter, r *http.Request) {
	jobID := r.PathValue("job_id")
	cap := capabilityFrom(r)
	if cap == "" {
		writeErr(w, http.StatusUnauthorized, "missing_capability", "X-Job-Capability required")
		return
	}
	waitMS := 0
	if q := r.URL.Query().Get("wait_ms"); q != "" {
		n, err := strconv.Atoi(q)
		if err != nil || n < 0 || n > 30000 {
			writeErr(w, http.StatusBadRequest, "invalid_wait_ms", "wait_ms must be 0..30000")
			return
		}
		waitMS = n
	}
	view, err := s.mgr.Get(r.Context(), jobID, cap, waitMS)
	if err != nil {
		s.mapErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, view)
}

func (s *Server) handleOTP(w http.ResponseWriter, r *http.Request) {
	jobID := r.PathValue("job_id")
	cap := capabilityFrom(r)
	if cap == "" {
		writeErr(w, http.StatusUnauthorized, "missing_capability", "X-Job-Capability required")
		return
	}
	var body job.OTPSubmit
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid_json", err.Error())
		return
	}
	view, err := s.mgr.SubmitOTP(r.Context(), jobID, cap, body)
	if err != nil {
		s.mapErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, view)
}

func (s *Server) handleDelete(w http.ResponseWriter, r *http.Request) {
	jobID := r.PathValue("job_id")
	cap := capabilityFrom(r)
	if cap == "" {
		writeErr(w, http.StatusUnauthorized, "missing_capability", "X-Job-Capability required")
		return
	}
	view, err := s.mgr.Cancel(r.Context(), jobID, cap)
	if err != nil {
		s.mapErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, view)
}

func (s *Server) mapErr(w http.ResponseWriter, err error) {
	var conf *job.ConflictError
	var val *job.ValidationError
	var rej *admission.RejectError
	switch {
	case errors.Is(err, job.ErrUnauthorized):
		writeErr(w, http.StatusUnauthorized, "unauthorized", "invalid job capability")
	case errors.Is(err, ledger.ErrNotFound):
		writeErr(w, http.StatusNotFound, "not_found", "job not found")
	case errors.As(err, &conf):
		writeErr(w, http.StatusConflict, conf.Code, conf.Message)
	case errors.As(err, &val):
		code := "validation_error"
		if val.Code != "" {
			code = val.Code
		}
		writeErr(w, http.StatusBadRequest, code, val.Message)
	case errors.As(err, &rej):
		message := "admission rejected: " + string(rej.Reason)
		if detail := sanitizeAdmissionDetail(rej.Detail); detail != "" {
			message += ": " + detail
		}
		writeJSON(w, http.StatusTooManyRequests, map[string]any{
			"error":   "admission_rejected",
			"message": message,
			"reason":  string(rej.Reason),
		})
	case errors.Is(err, admission.ErrRejected):
		writeJSON(w, http.StatusTooManyRequests, map[string]any{
			"error":   "admission_rejected",
			"message": err.Error(),
			"reason":  "unknown",
		})
	default:
		writeErr(w, http.StatusInternalServerError, "internal", err.Error())
	}
}

func sanitizeAdmissionDetail(detail string) string {
	detail = strings.TrimSpace(detail)
	if detail == "" {
		return ""
	}
	at := strings.LastIndex(detail, "@")
	if at < 0 {
		return detail
	}
	left := detail[:at]
	right := detail[at+1:]
	prefix := ""
	if scheme := strings.LastIndex(left, "://"); scheme >= 0 {
		prefix = left[:scheme+3]
		left = left[scheme+3:]
	}
	if colon := strings.Index(left, ":"); colon >= 0 {
		return prefix + left[:colon] + ":***@" + right
	}
	return prefix + "***@" + right
}

// capabilityFrom reads X-Job-Capability or Authorization: Bearer.
func capabilityFrom(r *http.Request) string {
	if v := strings.TrimSpace(r.Header.Get("X-Job-Capability")); v != "" {
		return v
	}
	auth := r.Header.Get("Authorization")
	if strings.HasPrefix(strings.ToLower(auth), "bearer ") {
		return strings.TrimSpace(auth[7:])
	}
	return ""
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, code int, errCode, msg string) {
	writeJSON(w, code, map[string]any{
		"error":   errCode,
		"message": msg,
	})
}
