package job

import (
	"errors"
	"testing"

	"github.com/gpt-register/go-email-protocol/internal/protocol"
)

func TestClassifyHTTPToHTTPSAsProxyNetwork(t *testing.T) {
	err := errors.New(`Get "https://chatgpt.com/api/auth/providers": http: server gave HTTP response to HTTPS client`)
	code, retryable := classifyProtocolErr(err)
	if code != "proxy_or_network" || !retryable {
		t.Fatalf("got %s retryable=%v", code, retryable)
	}
}

func TestClassifyWsarecvAsProxyNetwork(t *testing.T) {
	err := errors.New(`Get "https://chatgpt.com/": read tcp 198.18.0.1:49485->198.18.0.70:10000: wsarecv: A connection attempt failed`)
	code, retryable := classifyProtocolErr(err)
	if code != "proxy_or_network" || !retryable {
		t.Fatalf("got %s retryable=%v", code, retryable)
	}
}

func TestStepFailureCodeOverridesAmbiguousTransport(t *testing.T) {
	err := errors.New(`Post "https://auth.openai.com/api/accounts/create_account": http: server gave HTTP response to HTTPS client`)
	res := protocol.StepResult{FailureCode: "ambiguous_after_send", Retryable: false}
	code, retryable := stepFailureCode(res, err)
	if code != "proxy_or_network" || !retryable {
		t.Fatalf("got %s retryable=%v", code, retryable)
	}
}

func TestEmailUsedStillPermanent(t *testing.T) {
	err := errors.New(`protocol: S11 status 400 body=user_already_exists`)
	code, retryable := classifyProtocolErr(err)
	if code != "email_already_used" || retryable {
		t.Fatalf("got %s retryable=%v", code, retryable)
	}
}

func TestClassifyBareEOFAsProxyNetwork(t *testing.T) {
	err := errors.New(`Post "https://sentinel.openai.com/backend-api/sentinel/req": EOF`)
	code, retryable := classifyProtocolErr(err)
	if code != "proxy_or_network" || !retryable {
		t.Fatalf("got %s retryable=%v", code, retryable)
	}
}
