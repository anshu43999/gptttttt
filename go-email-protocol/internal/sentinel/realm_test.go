package sentinel

import (
	"testing"

	"github.com/gpt-register/go-email-protocol/internal/fingerprint"
	mathrand "math/rand/v2"
)

func TestRealmProjectsUA(t *testing.T) {
	b, err := fingerprint.Generate(fingerprint.GenerateOptions{
		RNG:         mathrand.New(mathrand.NewPCG(1, 1)),
		ForceFamily: fingerprint.FamilyDesktop,
	})
	if err != nil {
		t.Fatal(err)
	}
	r, err := NewRealm("jr_a", b)
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	ua, err := r.NavigatorUserAgent()
	if err != nil {
		t.Fatal(err)
	}
	if ua != b.Device.UserAgent {
		t.Fatalf("ua mismatch")
	}
	v, err := r.Eval(`screen.width + "x" + screen.height`)
	if err != nil {
		t.Fatal(err)
	}
	_ = v
}

func TestRealmIsolationTwoJobs(t *testing.T) {
	b1, err := fingerprint.Generate(fingerprint.GenerateOptions{
		RNG:         mathrand.New(mathrand.NewPCG(2, 2)),
		ForceFamily: fingerprint.FamilyDesktop,
	})
	if err != nil {
		t.Fatal(err)
	}
	b2, err := fingerprint.Generate(fingerprint.GenerateOptions{
		RNG:         mathrand.New(mathrand.NewPCG(3, 3)),
		ForceFamily: fingerprint.FamilyMobile,
	})
	if err != nil {
		t.Fatal(err)
	}
	r1, err := NewRealm("jr_1", b1)
	if err != nil {
		t.Fatal(err)
	}
	r2, err := NewRealm("jr_2", b2)
	if err != nil {
		t.Fatal(err)
	}
	defer r1.Close()
	defer r2.Close()
	// mutate r1 only
	if _, err := r1.Eval(`navigator.userAgent = "mutated"`); err != nil {
		t.Fatal(err)
	}
	ua1, _ := r1.NavigatorUserAgent()
	ua2, _ := r2.NavigatorUserAgent()
	if ua1 != "mutated" {
		t.Fatalf("r1 %s", ua1)
	}
	if ua2 == "mutated" || ua2 != b2.Device.UserAgent {
		t.Fatalf("r2 leaked: %s want %s", ua2, b2.Device.UserAgent)
	}
	if r1.JobKey() == r2.JobKey() {
		t.Fatal("job keys")
	}
}
