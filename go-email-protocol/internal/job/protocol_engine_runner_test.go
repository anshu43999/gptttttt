package job

import (
	"context"
	"testing"

	"github.com/gpt-register/go-email-protocol/internal/admission"
	"github.com/gpt-register/go-email-protocol/internal/cryptostore"
	"github.com/gpt-register/go-email-protocol/internal/ledger"
	"github.com/gpt-register/go-email-protocol/internal/protocol"
)

func TestRunLiveFromOTPSuccessReleasesAdmissionSeat(t *testing.T) {
	led, err := ledger.Open(":memory:")
	if err != nil {
		t.Fatal(err)
	}
	defer led.Close()

	crypto, _, err := cryptostore.NewRandomKey()
	if err != nil {
		t.Fatal(err)
	}
	adm := admission.New(admission.Config{MaxActive: 1})
	m := NewManager(led, adm, crypto, nil, RunnerConfig{})

	const jobID = "live-success-release"
	if _, err := led.Create(context.Background(), ledger.CreateInput{
		JobID:              jobID,
		TaskID:             "task-live-success-release",
		AttemptID:          1,
		IdempotencyKey:     "idem-live-success-release",
		RequestFingerprint: "fingerprint-live-success-release",
		Capability:         "capability-live-success-release",
		Email:              "test@example.com",
		Status:             ledger.StatusRunning,
		Stage:              "protocol_S13",
	}); err != nil {
		t.Fatal(err)
	}
	if err := adm.TryAdmit(admission.Seat{JobID: jobID}); err != nil {
		t.Fatal(err)
	}

	rt := &Runtime{
		JobID:      jobID,
		ctx:        context.Background(),
		password:   "password",
		capability: "capability-live-success-release",
		otpCode:    "123456",
		liveEng:    &protocol.Engine{Mode: protocol.ModeFixture},
		liveCur: protocol.Cursor{
			State:       protocol.S13,
			AccessToken: "access-token",
			AccountID:   "account-id",
		},
		otpSignal: make(chan string, 1),
	}
	m.runtimes[jobID] = rt
	rt.otpSignal <- "123456"

	m.runLiveFromOTP(rt)

	rec, err := led.GetByID(context.Background(), jobID)
	if err != nil {
		t.Fatal(err)
	}
	if rec.Status != ledger.StatusSucceeded {
		t.Fatalf("status = %s, want %s", rec.Status, ledger.StatusSucceeded)
	}
	if got := adm.ActiveCount(); got != 0 {
		t.Fatalf("active admission seats = %d, want 0", got)
	}
	if m.Runtime(jobID) != nil {
		t.Fatal("completed runtime remains registered")
	}
}
