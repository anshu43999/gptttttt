package protocol

import (
	"context"
	"testing"

	"github.com/gpt-register/go-email-protocol/internal/fingerprint"
	"github.com/gpt-register/go-email-protocol/internal/headerpreset"
	mathrand "math/rand/v2"
)

func TestNextLinear(t *testing.T) {
	id := S0
	for range 14 {
		n, err := Next(id)
		if err != nil {
			t.Fatal(err)
		}
		id = n
	}
	if id != S14 {
		t.Fatalf("got %s", id)
	}
	if _, err := Next(S14); err == nil {
		t.Fatal("expected terminal")
	}
}

func TestPresetForOTPSparse(t *testing.T) {
	if PresetFor(S8) != headerpreset.OTPSparse || PresetFor(S10) != headerpreset.OTPSparse {
		t.Fatal("otp presets")
	}
	if PresetFor(S1) != headerpreset.DocumentNavigation {
		t.Fatal("s1")
	}
}

func TestRunToOTPWithBundle(t *testing.T) {
	b, err := fingerprint.Generate(fingerprint.GenerateOptions{
		RNG:         mathrand.New(mathrand.NewPCG(1, 2)),
		ForceFamily: fingerprint.FamilyDesktop,
	})
	if err != nil {
		t.Fatal(err)
	}
	e := &Engine{Mode: ModeSynthetic, Bundle: b}
	cur, steps, err := e.RunToOTP(context.Background(), Cursor{})
	if err != nil {
		t.Fatal(err)
	}
	if cur.State != S9 {
		t.Fatalf("state %s", cur.State)
	}
	if len(steps) < 9 {
		t.Fatalf("steps %d", len(steps))
	}
}

func TestLiveRequiresClient(t *testing.T) {
	e := &Engine{Mode: ModeLive}
	_, _, err := e.Step(context.Background(), Cursor{State: S1})
	if err == nil {
		t.Fatal("expected error")
	}
}
