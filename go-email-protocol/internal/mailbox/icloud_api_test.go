package mailbox

import "testing"

func TestExtractCodeWithProbe_StaleAndEmpty(t *testing.T) {
	cases := []struct {
		name      string
		body      string
		wantCode  string
		wantProbe string
	}{
		{
			name:      "stale_code ignored",
			body:      `{"success":true,"data":{"code":"123456","found":true,"stale_code":true}}`,
			wantCode:  "",
			wantProbe: "stale_code",
		},
		{
			name:      "found false waiting",
			body:      `{"success":true,"data":{"code":"","found":false,"stale_code":false}}`,
			wantCode:  "",
			wantProbe: "waiting_found_false",
		},
		{
			name:      "fresh code",
			body:      `{"success":true,"data":{"code":"654321","found":true,"stale_code":false}}`,
			wantCode:  "654321",
			wantProbe: "found_code",
		},
		{
			name:      "empty body",
			body:      "",
			wantCode:  "",
			wantProbe: "empty_body",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			code, probe := extractCodeWithProbe(tc.body)
			if code != tc.wantCode {
				t.Fatalf("code got %q want %q", code, tc.wantCode)
			}
			if probe != tc.wantProbe {
				t.Fatalf("probe got %q want %q", probe, tc.wantProbe)
			}
		})
	}
}

func TestWaitForOTPDefaultTimeout(t *testing.T) {
	// nil account fails fast; documents default path is wired
	_, err := WaitForOTP(t.Context(), nil, 0)
	if err == nil || err.Error() != "mailbox: nil account" {
		t.Fatalf("want nil account error, got %v", err)
	}
}
