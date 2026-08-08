package fixture_test

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/gpt-register/go-email-protocol/internal/fixture"
	"github.com/gpt-register/go-email-protocol/internal/protocol"
)

func testdataRoot(t *testing.T) string {
	t.Helper()
	_, file, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("runtime.Caller failed")
	}
	root := filepath.Clean(filepath.Join(filepath.Dir(file), "..", "..", "testdata"))
	if st, err := os.Stat(root); err != nil || !st.IsDir() {
		t.Fatalf("testdata root missing: %s (%v)", root, err)
	}
	return root
}

func TestCatalogueCompleteness(t *testing.T) {
	root := testdataRoot(t)
	cat, err := fixture.LoadCatalogue(root)
	if err != nil {
		t.Fatalf("LoadCatalogue: %v", err)
	}
	if err := cat.ValidateCompleteness(); err != nil {
		t.Fatalf("completeness: %v", err)
	}
	required := protocol.RequiredStateIDs()
	if len(cat.Fixtures) < len(required) {
		t.Fatalf("fixture count %d < required %d", len(cat.Fixtures), len(required))
	}
	for _, id := range required {
		f, ok := cat.Fixtures[id]
		if !ok {
			t.Errorf("missing fixture %s", id)
			continue
		}
		if f.Kind != protocol.KindOf(id) {
			t.Errorf("%s kind=%s want %s", id, f.Kind, protocol.KindOf(id))
		}
		if f.Status != fixture.StatusSpecified && f.Status != fixture.StatusCaptureRequired {
			t.Errorf("%s bad status %q", id, f.Status)
		}
	}
	t.Logf("covered=%d specified=%v capture_required=%v",
		len(cat.Fixtures), cat.Specified(), cat.CaptureRequired())
}

func TestJSONSchemaLoad(t *testing.T) {
	root := testdataRoot(t)
	var n int
	err := filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			if path != root && strings.EqualFold(d.Name(), "rechallenge") {
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(strings.ToLower(d.Name()), ".json") {
			return nil
		}
		if strings.HasPrefix(d.Name(), "_") {
			return nil
		}
		raw, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		var f fixture.Fixture
		if err := json.Unmarshal(raw, &f); err != nil {
			t.Errorf("json %s: %v", path, err)
			return nil
		}
		if err := fixture.ValidateFixture(&f, path); err != nil {
			t.Errorf("validate %s: %v", path, err)
		}
		if err := fixture.ValidateRedactedJSON(path, raw); err != nil {
			t.Errorf("redact %s: %v", path, err)
		}
		n++
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if n < len(protocol.RequiredStateIDs()) {
		t.Fatalf("loaded %d state fixtures, want >= %d", n, len(protocol.RequiredStateIDs()))
	}
}

func mustRedactionError(t *testing.T, err error) *fixture.RedactionError {
	t.Helper()
	if err == nil {
		t.Fatal("expected redaction error, got nil")
	}
	var re *fixture.RedactionError
	if !errors.As(err, &re) {
		t.Fatalf("expected *fixture.RedactionError, got %T: %v", err, err)
	}
	return re
}

func TestRedactionRejection(t *testing.T) {
	cases := []struct {
		name string
		raw  string
	}{
		{"password", `{"password":"SuperSecret1!"}`},
		{"otp", `{"otp":"123456","code":"123456"}`},
		{"authorization", `{"authorization":"Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.aaaa.bbbb"}`},
		{"access_token", `{"access_token":"ya29.a0AfH6SMBx_fake_token_value_here_long"}`},
		{"cookie_value", `{"cookie":"session=abc123def456; Path=/"}`},
		{"proxy_userinfo", `{"proxy_url":"http://user:p4ssw0rd@proxy.example:8080"}`},
		{"capability", `{"bridge_capability":"cap_live_secret_value_001"}`},
		{"token", `{"token":"gAAAAACxxxxxxxxxxxxxxxxxxxxxxxxx"}`},
		{"csrfToken", `{"csrfToken":"live-csrf-value-here"}`},
		{"openai_sentinel_token", `{"openai-sentinel-token":"sentinel_live_header_value_001"}`},
		{"marker_prefix_leak", `{"password":"[REDACTED] SuperSecret1!"}`},
		{"hash_substring_leak", `{"password":"password_hash_live_value_001"}`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := fixture.ValidateRedactedJSON("inject/"+tc.name+".json", []byte(tc.raw))
			mustRedactionError(t, err)
		})
	}
}

func TestRedactionAllowsFieldNamesOnly(t *testing.T) {
	raw := []byte(`{
		"schema_version": 1,
		"id": "S7",
		"kind": "main",
		"status": "specified",
		"title": "register",
		"request": {
			"method": "POST",
			"url_template": "https://auth.openai.com/api/accounts/user/register",
			"body_kind": "json",
			"body_fields": [
				{"name": "password", "type": "secret", "required": true},
				{"name": "username", "type": "template", "required": true}
			],
			"header_preset": "J_register_password",
			"header_keys": ["openai-sentinel-token"]
		},
		"notes": "password and access_token are field names only"
	}`)
	if err := fixture.ValidateRedactedJSON("ok.json", raw); err != nil {
		t.Fatalf("structural fixture should pass: %v", err)
	}
}

func TestSpecifiedShapeRequiresCatalogueFields(t *testing.T) {
	base := fixture.Fixture{
		SchemaVersion: fixture.CurrentSchemaVersion,
		ID:            protocol.S3,
		Kind:          protocol.KindMain,
		Status:        fixture.StatusSpecified,
		Title:         "signin",
		Request: &protocol.RequestShape{
			Method:       "POST",
			URLTemplate:  "https://chatgpt.com/api/auth/signin/openai",
			QueryKeys:    []string{"prompt"},
			BodyKind:     "form",
			BodyFields:   []protocol.FieldSpec{{Name: "csrfToken", Type: protocol.BodyTypeSecret, Required: true}},
			HeaderPreset: "S3_nextauth_signin",
			HeaderKeys:   []string{"content-type"},
		},
	}
	if err := fixture.ValidateFixture(&base, "ok"); err != nil {
		t.Fatalf("complete shape should pass: %v", err)
	}

	missingPreset := base
	req := *base.Request
	req.HeaderPreset = ""
	missingPreset.Request = &req
	if err := fixture.ValidateFixture(&missingPreset, "no-preset"); err == nil {
		t.Fatal("expected error when header_preset missing")
	}

	missingKeys := base
	req2 := *base.Request
	req2.HeaderKeys = nil
	missingKeys.Request = &req2
	if err := fixture.ValidateFixture(&missingKeys, "no-header-keys"); err == nil {
		t.Fatal("expected error when header_keys omitted")
	}

	missingBody := base
	req3 := *base.Request
	req3.BodyFields = nil
	missingBody.Request = &req3
	if err := fixture.ValidateFixture(&missingBody, "no-body"); err == nil {
		t.Fatal("expected error when form body_fields empty")
	}
}

func TestGoldenMainPathFixtures(t *testing.T) {
	root := testdataRoot(t)
	cat, err := fixture.LoadCatalogue(root)
	if err != nil {
		t.Fatal(err)
	}

	type expect struct {
		method       string
		url          string
		headerPreset string
		query        []string
		bodyNames    []string
		headerHas    []string
		headerNot    []string
	}
	cases := map[protocol.StateID]expect{
		protocol.S1: {
			method: "GET", url: "https://chatgpt.com/", headerPreset: "S1_homepage",
			headerHas: []string{"user-agent", "sec-fetch-dest"},
		},
		protocol.S3: {
			method: "POST", url: "https://chatgpt.com/api/auth/signin/openai", headerPreset: "S3_nextauth_signin",
			query: []string{"prompt", "ext-oai-did", "auth_session_logging_id", "ext-passkey-client-capabilities", "screen_hint", "login_hint"},
			bodyNames: []string{"callbackUrl", "csrfToken", "json"},
		},
		protocol.S7: {
			method: "POST", url: "https://auth.openai.com/api/accounts/user/register", headerPreset: "J_register_password",
			bodyNames: []string{"password", "username"},
			headerHas: []string{"openai-sentinel-token"},
		},
		protocol.S10: {
			method: "POST", url: "https://auth.openai.com/api/accounts/email-otp/validate", headerPreset: "S10_validate_otp",
			bodyNames: []string{"code"},
			headerHas: []string{"accept", "content-type", "origin", "referer", "user-agent"},
			headerNot: []string{"sec-ch-ua", "sec-fetch-dest", "accept-language"},
		},
		protocol.T1: {
			method: "POST", url: "https://sentinel.openai.com/backend-api/sentinel/req", headerPreset: "T1_requirements",
			bodyNames: []string{"p", "id", "flow"},
			headerHas: []string{"content-type", "user-agent"},
		},
	}
	for id, want := range cases {
		f, ok := cat.Fixtures[id]
		if !ok {
			t.Fatalf("missing %s", id)
			continue
		}
		if f.Status != fixture.StatusSpecified {
			t.Fatalf("%s status=%s want specified", id, f.Status)
		}
		if f.Request == nil {
			t.Fatalf("%s missing request", id)
		}
		if f.Request.Method != want.method {
			t.Errorf("%s method=%q want %q", id, f.Request.Method, want.method)
		}
		if f.Request.URLTemplate != want.url {
			t.Errorf("%s url=%q want %q", id, f.Request.URLTemplate, want.url)
		}
		if f.Request.HeaderPreset != want.headerPreset {
			t.Errorf("%s header_preset=%q want %q", id, f.Request.HeaderPreset, want.headerPreset)
		}
		if f.Request.HeaderKeys == nil {
			t.Errorf("%s header_keys nil", id)
		}
		if len(want.query) > 0 {
			if strings.Join(f.Request.QueryKeys, ",") != strings.Join(want.query, ",") {
				t.Errorf("%s query_keys=%v want %v", id, f.Request.QueryKeys, want.query)
			}
		}
		if len(want.bodyNames) > 0 {
			gotNames := make([]string, 0, len(f.Request.BodyFields))
			for _, bf := range f.Request.BodyFields {
				gotNames = append(gotNames, bf.Name)
			}
			if strings.Join(gotNames, ",") != strings.Join(want.bodyNames, ",") {
				t.Errorf("%s body_fields=%v want %v", id, gotNames, want.bodyNames)
			}
		}
		keySet := map[string]bool{}
		for _, k := range f.Request.HeaderKeys {
			keySet[k] = true
		}
		for _, k := range want.headerHas {
			if !keySet[k] {
				t.Errorf("%s missing header key %q", id, k)
			}
		}
		for _, k := range want.headerNot {
			if keySet[k] {
				t.Errorf("%s unexpected header key %q", id, k)
			}
		}
	}
}

func TestRecordFromObservationRedactsSecrets(t *testing.T) {
	obs := fixture.Observation{
		Fixture: fixture.Fixture{
			SchemaVersion: fixture.CurrentSchemaVersion,
			ID:            protocol.S7,
			Kind:          protocol.KindMain,
			Status:        fixture.StatusSpecified,
			Title:         "register",
			Request: &protocol.RequestShape{
				Method:       "POST",
				URLTemplate:  "https://auth.openai.com/api/accounts/user/register",
				BodyKind:     "json",
				BodyFields:   []protocol.FieldSpec{{Name: "password", Type: protocol.BodyTypeSecret, Required: true}},
				HeaderPreset: "J_register_password",
				HeaderKeys:   []string{"openai-sentinel-token"},
			},
		},
		Raw: []byte(`{
			"schema_version":1,
			"id":"S7",
			"kind":"main",
			"status":"specified",
			"title":"register",
			"request":{
				"method":"POST",
				"url_template":"https://auth.openai.com/api/accounts/user/register",
				"body_kind":"json",
				"body_fields":[{"name":"password","type":"secret","required":true}],
				"header_preset":"J_register_password",
				"header_keys":["openai-sentinel-token"]
			},
			"password":"SuperSecret1!"
		}`),
	}
	got, err := fixture.RecordFromObservation(obs)
	if err != nil {
		t.Fatalf("RecordFromObservation: %v", err)
	}
	if got.ID != protocol.S7 {
		t.Fatalf("id=%s", got.ID)
	}
	// Re-marshal and ensure raw secret is gone.
	raw, _ := json.Marshal(got)
	if strings.Contains(string(raw), "SuperSecret1!") {
		t.Fatal("secret leaked after RecordFromObservation")
	}
}

func TestNoNetworkInG0Tests(t *testing.T) {
	root := testdataRoot(t)
	if _, err := fixture.LoadCatalogue(root); err != nil {
		t.Fatal(err)
	}
	t.Log("G0 tests are filesystem-only; no live OpenAI network")
}

func TestRequiredStateIDsCoverageList(t *testing.T) {
	ids := protocol.RequiredStateIDs()
	want := 16 + 3 + 7 + 3 // S0-15, T1-3, C0-6, L1-3
	if len(ids) != want {
		t.Fatalf("RequiredStateIDs len=%d want %d", len(ids), want)
	}
	seen := map[protocol.StateID]bool{}
	for _, id := range ids {
		if seen[id] {
			t.Errorf("duplicate %s", id)
		}
		seen[id] = true
		if !protocol.IsKnown(id) {
			t.Errorf("unknown %s", id)
		}
	}
}
