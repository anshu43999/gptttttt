package job

import "testing"

func TestIsBulkEmailRetryableOTPAndUsed(t *testing.T) {
	cases := []struct {
		msg  string
		want bool
	}{
		{"protocol: email_already_used", true},
		{"S11 user_already_exists", true},
		{"mailbox: outlook OTP timeout after 65s: last=graph_no_openai_code (no openai mail, early abort)", true},
		{"otp_timeout", true},
		{"invalid_grant", true},
		{"edge_challenge_required", false},
		{"proxy_or_network eof", false},
	}
	for _, tc := range cases {
		if got := isBulkEmailRetryable(tc.msg); got != tc.want {
			t.Fatalf("isBulkEmailRetryable(%q)=%v want %v", tc.msg, got, tc.want)
		}
	}
}

func TestMailboxStatusForFailureUsed(t *testing.T) {
	if got := mailboxStatusForFailure("user_already_exists"); got != "used" {
		t.Fatalf("status=%s want used", got)
	}
	if got := mailboxStatusForFailure("graph_no_openai_code"); got != "cooldown" {
		t.Fatalf("status=%s want cooldown", got)
	}
	if got := mailboxStatusForFailure("invalid_grant"); got != "disabled" {
		t.Fatalf("status=%s want disabled", got)
	}
}

func TestEmailTriesCap(t *testing.T) {
	if emailTriesCap(nil) != 1 {
		t.Fatal("nil state")
	}
	st := &bulkState{req: BulkCreateRequest{EmailTries: 0}}
	if emailTriesCap(st) != 1 {
		t.Fatal("zero → 1")
	}
	st.req.EmailTries = 5
	if emailTriesCap(st) != 5 {
		t.Fatal("5")
	}
	st.req.EmailTries = 99
	if emailTriesCap(st) != 20 {
		t.Fatal("clamp 20")
	}
}
