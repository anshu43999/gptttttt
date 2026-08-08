package job

import (
	"testing"

	"github.com/gpt-register/go-email-protocol/internal/ledger"
)

func TestRecordToViewIncludesSanitizedFailureMessage(t *testing.T) {
	m := &Manager{}
	view := m.recordToView(&ledger.Record{
		Status:      ledger.StatusFailed,
		FailureCode: "email_already_used",
		ResultJSON:  []byte(`{"error":"protocol: S6 email-verification"}`),
	}, "")

	if view.Message != "protocol: S6 email-verification" {
		t.Fatalf("message = %q", view.Message)
	}
}

func TestRecordToViewOmitsMalformedFailureMessage(t *testing.T) {
	m := &Manager{}
	view := m.recordToView(&ledger.Record{
		Status:     ledger.StatusFailed,
		ResultJSON: []byte(`not-json`),
	}, "")

	if view.Message != "" {
		t.Fatalf("message = %q", view.Message)
	}
}
