package main

import (
	"errors"
	"testing"
)

func TestIsEmailAlreadyUsedErr(t *testing.T) {
	cases := []struct {
		err  error
		want bool
	}{
		{nil, false},
		{errors.New("protocol: S11 status 400 body={\"error\":{\"code\":\"user_already_exists\"}}"), true},
		{errors.New("An account already exists for this email address."), true},
		{errors.New("protocol: S6 email-verification without passwordless marker (email used)"), true},
		{errors.New("protocol: S11 status 500 body=try again"), false},
		{errors.New("socks connect failed"), false},
	}
	for _, tc := range cases {
		if got := isEmailAlreadyUsedErr(tc.err); got != tc.want {
			t.Fatalf("isEmailAlreadyUsedErr(%v)=%v want %v", tc.err, got, tc.want)
		}
	}
}

func TestIsOTPMailboxRetryable(t *testing.T) {
	cases := []struct {
		err  error
		want bool
	}{
		{nil, false},
		{errors.New("mailbox: outlook OTP timeout for a@x after 6m0s: last=graph_no_openai_code"), true},
		{errors.New("mailbox: outlook OTP timeout for a@x after 50s: last=graph_empty_inbox (no openai mail, early abort)"), true},
		{errors.New("mailbox: outlook token refresh failed: HTTP 400 invalid_grant"), true},
		{errors.New("protocol: S11 status 400 body=user_already_exists"), false},
	}
	for _, tc := range cases {
		if got := isOTPMailboxRetryable(tc.err); got != tc.want {
			t.Fatalf("isOTPMailboxRetryable(%v)=%v want %v", tc.err, got, tc.want)
		}
	}
}

func TestMailboxStatusForErr(t *testing.T) {
	if got := mailboxStatusForErr(errors.New("user_already_exists")); got != "used" {
		t.Fatalf("already exists → used, got %s", got)
	}
	if got := mailboxStatusForErr(errors.New("invalid_grant AADSTS70000")); got != "disabled" {
		t.Fatalf("invalid_grant → disabled, got %s", got)
	}
	if got := mailboxStatusForErr(errors.New("graph_no_openai_code")); got != "cooldown" {
		t.Fatalf("otp miss → cooldown, got %s", got)
	}
}
