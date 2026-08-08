package protocol

import (
	"strings"
	"testing"
)

func TestRouteS6PasswordlessToOTP(t *testing.T) {
	body := []byte(`{"continue_url":"https://auth.openai.com/email-verification","page":{"payload":{"email_verification_mode":"passwordless_signup"}}}`)
	cur := Cursor{State: S6}
	res := StepResult{From: S6}
	out, r, err := routeAfterAuthContinue(cur, res, body)
	if err != nil {
		t.Fatal(err)
	}
	if out.State != S9 {
		t.Fatalf("state %s want S9 stage=%s", out.State, r.Stage)
	}
}

func TestRouteS6EmailVerificationWithoutMarkerFails(t *testing.T) {
	body := []byte(`{"continue_url":"https://auth.openai.com/email-verification"}`)
	cur := Cursor{State: S6}
	res := StepResult{From: S6}
	_, _, err := routeAfterAuthContinue(cur, res, body)
	if err == nil {
		t.Fatal("expected error")
	}
}

func TestRouteS6PasswordPath(t *testing.T) {
	body := []byte(`{"continue_url":"https://auth.openai.com/create-account/password"}`)
	cur := Cursor{State: S6}
	res := StepResult{From: S6}
	out, _, err := routeAfterAuthContinue(cur, res, body)
	if err != nil {
		t.Fatal(err)
	}
	if out.State != S7 {
		t.Fatalf("state %s", out.State)
	}
}

func TestRouteS6AlreadyRegistered(t *testing.T) {
	body := []byte(`{"continue_url":"https://auth.openai.com/email-verification","error":{"code":"user_already_exists"}}`)
	cur := Cursor{State: S6}
	res := StepResult{From: S6}
	_, _, err := routeAfterAuthContinue(cur, res, body)
	if err == nil || !strings.Contains(err.Error(), "already registered") {
		t.Fatalf("err=%v", err)
	}
}
