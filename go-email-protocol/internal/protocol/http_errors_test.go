package protocol

import "testing"

func TestClassifyHTTPFailureCF(t *testing.T) {
	code, retry, _ := classifyHTTPFailure("S6", 403, []byte(`<!DOCTYPE html><title>Just a moment...</title>`))
	if code != "cf_challenge" || !retry {
		t.Fatalf("got %s retry=%v", code, retry)
	}
}

func TestClassifyHTTPFailureSessionInvalid(t *testing.T) {
	body := []byte(`{"error":{"message":"Your sign-in session is no longer valid. Please start over","code":"invalid_state"}}`)
	code, retry, _ := classifyHTTPFailure("S6", 409, body)
	if code != "session_invalid" || !retry {
		t.Fatalf("got %s retry=%v", code, retry)
	}
}

func TestClassifyHTTPFailureAlreadyExists(t *testing.T) {
	body := []byte(`{"error":{"code":"user_already_exists","message":"An account already exists"}}`)
	code, retry, _ := classifyHTTPFailure("S11", 400, body)
	if code != "email_already_used" || retry {
		t.Fatalf("got %s retry=%v", code, retry)
	}
}

func TestClassifyHTTPFailureServer(t *testing.T) {
	code, retry, _ := classifyHTTPFailure("S11", 500, []byte(`{"error":{"message":"Please try again later."}}`))
	if code != "server_error" || !retry {
		t.Fatalf("got %s retry=%v", code, retry)
	}
}
