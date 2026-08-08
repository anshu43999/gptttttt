package rechallenge

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/gpt-register/go-email-protocol/internal/protocol"
)

type privateHARInputs struct {
	Captures []struct {
		ID string `json:"id"`
		Role CaptureRole `json:"role"`
		Path string `json:"path"`
		SHA256 string `json:"sha256"`
	} `json:"captures"`
}

func moduleRoot(t *testing.T) string {
	t.Helper()
	_, file, _, ok := runtime.Caller(0)
	if !ok { t.Fatal("runtime.Caller failed") }
	return filepath.Clean(filepath.Join(filepath.Dir(file), "..", ".."))
}

func loadPrivateHARInputs(t *testing.T) privateHARInputs {
	t.Helper()
	path := filepath.Join(moduleRoot(t), "..", "private", "har-inputs.json")
	raw, err := os.ReadFile(path)
	if err != nil { t.Skipf("external HAR manifest unavailable: %v", err) }
	var inputs privateHARInputs
	if err := json.Unmarshal(raw, &inputs); err != nil { t.Fatalf("decode private HAR manifest: %v", err) }
	return inputs
}

func fixtureContractPath(t *testing.T, fixture string) string {
	t.Helper()
	return filepath.Join(moduleRoot(t), "testdata", "rechallenge", "registration", fixture, "contract.json")
}

func cloneContract(t *testing.T, source *RegistrationContract) *RegistrationContract {
	t.Helper()
	raw, err := json.Marshal(source)
	if err != nil { t.Fatal(err) }
	var clone RegistrationContract
	if err := json.Unmarshal(raw, &clone); err != nil { t.Fatal(err) }
	return &clone
}

func TestExternalHARNormalizationDeterministicAndRoleGated(t *testing.T) {
	inputs := loadPrivateHARInputs(t)
	fixtureByID := map[string]string{"d17": "d17-firefox150-ptbr", "d24": "d24-firefox150-jajp"}
	sourceByID := map[string]string{
		"d17": "https://sentinel.openai.com/sentinel/20260219f9f6/sdk.js",
		"d24": "https://sentinel.openai.com/backend-api/sentinel/sdk.js",
	}
	seenRegistration := 0
	seenCheckout := false
	for _, input := range inputs.Captures {
		input := input
		t.Run(input.ID, func(t *testing.T) {
			if input.Role == RoleCheckout {
				seenCheckout = true
				capture, err := IngestHAR(context.Background(), input.Path, IngestOptions{CaptureID: "d18-checkout", ExpectedRole: RoleCheckout, ExpectedSHA256: input.SHA256})
				if err != nil { t.Fatalf("checkout ingest: %v", err) }
				if capture.Manifest.Role != RoleCheckout { t.Fatalf("role=%s want checkout", capture.Manifest.Role) }
				_, err = NormalizeCapture(capture, NormalizeOptions{ExpectedRole: RoleRegistration})
				var contractErr *ContractError
				if !errors.As(err, &contractErr) || contractErr.Code != CodeCaptureRoleMismatch {
					t.Fatalf("expected typed capture_role_mismatch, got %T %v", err, err)
				}
				_, err = NormalizeHAR(context.Background(), input.Path, NormalizeOptions{CaptureID: "d18-negative", ExpectedRole: RoleRegistration, ExpectedSHA256: input.SHA256})
				if !errors.As(err, &contractErr) || contractErr.Code != CodeCaptureRoleMismatch {
					t.Fatalf("registration pipeline accepted checkout: %T %v", err, err)
				}
				return
			}

			fixture := fixtureByID[input.ID]
			options := NormalizeOptions{CaptureID: fixture, ExpectedRole: RoleRegistration, ExpectedSHA256: input.SHA256}
			first, err := NormalizeHAR(context.Background(), input.Path, options)
			if err != nil { t.Fatalf("first normalize: %v", err) }
			second, err := NormalizeHAR(context.Background(), input.Path, options)
			if err != nil { t.Fatalf("second normalize: %v", err) }
			if first.Contract.CanonicalSHA256 != second.Contract.CanonicalSHA256 || first.Contract.ContractID != second.Contract.ContractID {
				t.Fatalf("non-deterministic identities: %s/%s vs %s/%s", first.Contract.ContractID, first.Contract.CanonicalSHA256, second.Contract.ContractID, second.Contract.CanonicalSHA256)
			}
			committed, err := LoadContract(fixtureContractPath(t, fixture))
			if err != nil { t.Fatalf("load committed fixture: %v", err) }
			if first.Contract.CanonicalSHA256 != committed.CanonicalSHA256 {
				t.Fatalf("generated hash %s != committed %s", first.Contract.CanonicalSHA256, committed.CanonicalSHA256)
			}
			assertSentinelOccurrences(t, first.Contract, sourceByID[input.ID])
			seenRegistration++
		})
	}
	if seenRegistration != 2 || !seenCheckout { t.Fatalf("inputs registration=%d checkout=%v", seenRegistration, seenCheckout) }
}

func assertSentinelOccurrences(t *testing.T, contract *RegistrationContract, source string) {
	t.Helper()
	var sentinel []StateExchangeContract
	for _, exchange := range contract.Exchanges {
		if exchange.State == protocol.T1 { sentinel = append(sentinel, exchange) }
	}
	if len(sentinel) != 3 { t.Fatalf("sentinel occurrences=%d want 3", len(sentinel)) }
	for occurrence, exchange := range sentinel {
		if exchange.SentinelOccurrence == nil || *exchange.SentinelOccurrence != occurrence {
			t.Fatalf("occurrence[%d]=%v", occurrence, exchange.SentinelOccurrence)
		}
		if exchange.FlowName != "oauth_create_account" || exchange.FSMAssociation != "" {
			t.Fatalf("occurrence[%d] flow=%q fsm_association=%q", occurrence, exchange.FlowName, exchange.FSMAssociation)
		}
		if exchange.ObservedScriptSource != source {
			t.Fatalf("occurrence[%d] source=%q want %q", occurrence, exchange.ObservedScriptSource, source)
		}
		if exchange.RequirementsFingerprint == "" { t.Fatalf("occurrence[%d] missing fingerprint", occurrence) }
	}
}

func TestHeaderSourceProvenance(t *testing.T) {
	contract, err := LoadContract(fixtureContractPath(t, "d24-firefox150-jajp"))
	if err != nil { t.Fatal(err) }
	var create *StateExchangeContract
	for index := range contract.Exchanges {
		if contract.Exchanges[index].Request.Kind == "create_account" { create = &contract.Exchanges[index]; break }
	}
	if create == nil { t.Fatal("create_account exchange missing") }
	expected := map[string]string{
		"openai-sentinel-token": HeaderSourceApp,
		"openai-sentinel-so-token": HeaderSourceApp,
		"cookie": HeaderSourceCookieJar,
		"host": HeaderSourceTransport,
		"traceparent": HeaderSourceTelemetry,
		"x-access-flow-invocation-id": HeaderSourceDynamicRuntime,
	}
	for name, source := range expected {
		found := false
		for _, rule := range create.Request.Headers {
			if rule.Name != name { continue }
			found = true
			if rule.Source != source || len(rule.Provenance) < 2 { t.Errorf("%s source=%s provenance=%v want %s", name, rule.Source, rule.Provenance, source) }
			if (name == "openai-sentinel-token" || name == "openai-sentinel-so-token") && (rule.Presence != PresenceRequired || rule.ValuePolicy != "secret_json_shape") {
				t.Errorf("protected %s presence=%s value_policy=%s", name, rule.Presence, rule.ValuePolicy)
			}
		}
		if !found { t.Errorf("header %s missing", name) }
	}
	for _, forbidden := range forbiddenFirefoxHeaders {
		found := false
		for _, rule := range create.Request.Headers { if rule.Name == forbidden && rule.Presence == PresenceForbidden { found = true } }
		if !found { t.Errorf("Firefox forbidden header rule missing for %s", forbidden) }
	}
}

func TestSecretMutationRejected(t *testing.T) {
	contract, err := LoadContract(fixtureContractPath(t, "d24-firefox150-jajp"))
	if err != nil { t.Fatal(err) }
	mutated := cloneContract(t, contract)
	for index := range mutated.Exchanges {
		if mutated.Exchanges[index].State == protocol.T1 && mutated.Exchanges[index].Response.BodyTemplate != nil {
			mutated.Exchanges[index].Response.BodyTemplate.Fields["token"] = TemplateValue{Kind: "literal", Literal: "sentinel_live_secret_value_001"}
			break
		}
	}
	err = FinalizeContract(mutated)
	var contractErr *ContractError
	if !errors.As(err, &contractErr) || contractErr.Code != CodeContractRedactionViolation {
		t.Fatalf("expected contract_redaction_violation for response token, got %T %v", err, err)
	}

	mutated = cloneContract(t, contract)
	for exchangeIndex := range mutated.Exchanges {
		for headerIndex := range mutated.Exchanges[exchangeIndex].Request.Headers {
			rule := &mutated.Exchanges[exchangeIndex].Request.Headers[headerIndex]
			if rule.Name == "openai-sentinel-so-token" {
				rule.Expected = "raw_so_token_material_should_never_persist"
				break
			}
		}
	}
	err = FinalizeContract(mutated)
	if !errors.As(err, &contractErr) || contractErr.Code != CodeContractRedactionViolation {
		t.Fatalf("expected SO-token redaction violation, got %T %v", err, err)
	}
}

func TestSemanticDiffPreciseBlockingKnownAndInformational(t *testing.T) {
	approved, err := LoadContract(fixtureContractPath(t, "d24-firefox150-jajp"))
	if err != nil { t.Fatal(err) }

	blockingCandidate := cloneContract(t, approved)
	for index := range blockingCandidate.Exchanges {
		if blockingCandidate.Exchanges[index].State == protocol.S10 { blockingCandidate.Exchanges[index].Request.Method = "PATCH"; break }
	}
	blocking, err := DiffContracts(approved, blockingCandidate)
	if err != nil { t.Fatal(err) }
	if len(blocking.Blocking) != 1 || !strings.Contains(blocking.Blocking[0].Path, "exchanges[S10#0@5].request.method") {
		t.Fatalf("precise method diff missing: %+v", blocking.Blocking)
	}

	knownCandidate := cloneContract(t, approved)
	for index := range knownCandidate.Exchanges {
		if knownCandidate.Exchanges[index].State == protocol.T1 {
			knownCandidate.Exchanges[index].ObservedScriptSource = "https://sentinel.openai.com/sentinel/20260219f9f6/sdk.js"
		}
	}
	known, err := DiffContracts(approved, knownCandidate)
	if err != nil { t.Fatal(err) }
	if known.HasBlocking() || len(known.KnownVariants) != 3 { t.Fatalf("known source variant misclassified: %+v", known) }

	infoCandidate := cloneContract(t, approved)
	infoCandidate.Captures[0].CapturedAt = "2026-07-24T05:13:23+09:00"
	info, err := DiffContracts(approved, infoCandidate)
	if err != nil { t.Fatal(err) }
	if info.HasBlocking() || len(info.Informational) != 1 || !strings.Contains(info.Informational[0].Path, ".captured_at") {
		t.Fatalf("capture provenance diff misclassified: %+v", info)
	}
}

func TestCommittedFixturesAreRedactedAndLoadable(t *testing.T) {
	root := filepath.Join(moduleRoot(t), "testdata", "rechallenge")
	err := filepath.WalkDir(root, func(path string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil { return walkErr }
		if entry.IsDir() || !strings.HasSuffix(strings.ToLower(entry.Name()), ".json") { return nil }
		raw, err := os.ReadFile(path)
		if err != nil { return err }
		if strings.Contains(string(raw), "cs_live_") { t.Errorf("raw checkout token marker in %s", path) }
		if err := ValidateRedactedJSON(path, raw); err != nil { t.Errorf("redaction %s: %v", path, err) }
		return nil
	})
	if err != nil { t.Fatal(err) }
	for _, fixture := range []string{"d17-firefox150-ptbr", "d24-firefox150-jajp"} {
		if _, err := LoadContract(fixtureContractPath(t, fixture)); err != nil { t.Errorf("load %s: %v", fixture, err) }
	}
	manifest, err := LoadCaptureManifest(filepath.Join(root, "role-fixtures", "d18-checkout-manifest.json"))
	if err != nil { t.Fatal(err) }
	if manifest.Role != RoleCheckout { t.Fatalf("d18 role=%s", manifest.Role) }
}

func TestRoleClassifierRejectsRepeatedAndWrongMethodEvidence(t *testing.T) {
	newCollector := func() *roleCollector {
		return &roleCollector{registrationSignals: make(map[string]int), checkoutSignals: make(map[string]int), evidence: make(map[string]RoleEvidence)}
	}
	bad := newCollector()
	for index := range 3 { bad.observe(index, "POST", "attacker.example", "/backend-api/sentinel/req", "oauth_create_account") }
	bad.observe(3, "GET", "chatgpt.com", "/api/auth/signin/openai", "")
	bad.observe(4, "POST", "auth.openai.com", "/api/accounts/authorize", "")
	bad.observe(5, "GET", "auth.openai.com", "/api/accounts/create_account", "")
	role, _ := bad.classify()
	if role != RoleUnknown { t.Fatalf("wrong-method/repeated evidence classified as %s", role) }

	good := newCollector()
	good.observe(0, "GET", "chatgpt.com", "/api/auth/providers", "")
	good.observe(1, "GET", "chatgpt.com", "/api/auth/csrf", "")
	good.observe(2, "POST", "chatgpt.com", "/api/auth/signin/openai", "")
	good.observe(3, "GET", "auth.openai.com", "/api/accounts/authorize", "")
	good.observe(4, "POST", "auth.openai.com", "/api/accounts/email-otp/validate", "")
	for index := range 3 { good.observe(5+index, "POST", "sentinel.openai.com", "/backend-api/sentinel/req", "oauth_create_account") }
	good.observe(8, "POST", "auth.openai.com", "/api/accounts/create_account", "")
	good.observe(9, "GET", "chatgpt.com", "/api/auth/callback/openai", "")
	role, _ = good.classify()
	if role != RoleRegistration { t.Fatalf("exact registration evidence classified as %s", role) }
}

func TestObservedResponseRulesNeverInventMissingFields(t *testing.T) {
	response, err := normalizeHARResponse("sentinel_req", &harResponse{Status: 200, Content: harContent{MimeType: "application/json", Text: `{"token":"raw-secret","turnstile":{"required":true}}`}})
	if err != nil { t.Fatal(err) }
	if response.Outcome != "success" || response.BodyTemplate == nil { t.Fatalf("observed response not normalized: %+v", response) }
	if len(response.RequiredFields) != 2 || response.RequiredFields[0] != "token" || response.RequiredFields[1] != "turnstile.required" {
		t.Fatalf("invented or missing response fields: %v", response.RequiredFields)
	}
	if _, exists := response.BodyTemplate.Fields["proofofwork"]; exists { t.Fatal("unobserved proofofwork was invented") }
	encoded, _ := json.Marshal(response)
	if strings.Contains(string(encoded), "raw-secret") { t.Fatal("raw response token persisted") }

	unknown, err := normalizeHARResponse("sentinel_req", &harResponse{Status: 200, Content: harContent{MimeType: "application/json", Text: `not-json`}})
	if err != nil { t.Fatal(err) }
	if unknown.Outcome != "unknown" || unknown.BodyTemplate != nil || len(unknown.RequiredFields) != 0 { t.Fatalf("invalid response was not marked unknown: %+v", unknown) }
	incomplete, err := normalizeHARResponse("sentinel_req", &harResponse{Status: 0})
	if err != nil { t.Fatal(err) }
	if incomplete.Outcome != "capture_transport_incomplete" || len(incomplete.AllowedStatus) != 0 { t.Fatalf("status 0 fabricated success: %+v", incomplete) }
}

func TestLoadersAndHARRejectTrailingContentWithoutEchoingSecrets(t *testing.T) {
	contractRaw, err := os.ReadFile(fixtureContractPath(t, "d24-firefox150-jajp"))
	if err != nil { t.Fatal(err) }
	contractPath := filepath.Join(t.TempDir(), "contract.json")
	if err := os.WriteFile(contractPath, append(contractRaw, []byte(`{}`)...), 0o600); err != nil { t.Fatal(err) }
	if _, err := LoadContract(contractPath); err == nil || !strings.Contains(err.Error(), "trailing") { t.Fatalf("trailing contract content accepted: %v", err) }

	harPath := filepath.Join(t.TempDir(), "capture.har")
	if err := os.WriteFile(harPath, []byte(`{"log":{"entries":[]}} {}`), 0o600); err != nil { t.Fatal(err) }
	if _, err := IngestHAR(context.Background(), harPath, IngestOptions{}); err == nil || !strings.Contains(err.Error(), "trailing") { t.Fatalf("trailing HAR content accepted: %v", err) }

	secret := "DO_NOT_ECHO_THIS_SECRET_123456"
	err = ValidateRedactedJSON("adversarial", []byte(`{"openai-sentinel-so-token":"`+secret+`"}`))
	if err == nil { t.Fatal("raw SO token accepted") }
	if strings.Contains(err.Error(), secret) { t.Fatalf("redaction error echoed secret: %v", err) }
}

func TestDiffCannotDowngradeRequiredRulesOrObservedProvenance(t *testing.T) {
	approved, err := LoadContract(fixtureContractPath(t, "d24-firefox150-jajp"))
	if err != nil { t.Fatal(err) }
	headerCandidate := cloneContract(t, approved)
	for exchangeIndex := range headerCandidate.Exchanges {
		for headerIndex := range headerCandidate.Exchanges[exchangeIndex].Request.Headers {
			rule := &headerCandidate.Exchanges[exchangeIndex].Request.Headers[headerIndex]
			if rule.Name == "user-agent" { rule.Name = "x-optional-replacement"; rule.Presence = PresenceObserved; break }
		}
	}
	report, err := DiffContracts(approved, headerCandidate)
	if err != nil { t.Fatal(err) }
	if !report.HasBlocking() { t.Fatal("required user-agent replacement was downgraded") }

	provenanceCandidate := cloneContract(t, approved)
	provenanceCandidate.Exchanges[0].Provenance.Observed = false
	report, err = DiffContracts(approved, provenanceCandidate)
	if err != nil { t.Fatal(err) }
	if !report.HasBlocking() || !strings.Contains(report.Blocking[0].Path, "provenance.observed") { t.Fatalf("observed provenance drift not blocked: %+v", report.Blocking) }

	fingerprintCandidate := cloneContract(t, approved)
	fingerprintCandidate.Exchanges[6].ObservedScriptSource = "https://sentinel.openai.com/sentinel/20260219f9f6/sdk.js"
	fingerprintCandidate.Exchanges[6].RequirementsFingerprint = "sha256:"+strings.Repeat("0", 64)
	report, err = DiffContracts(approved, fingerprintCandidate)
	if err != nil { t.Fatal(err) }
	if !report.HasBlocking() { t.Fatal("requirements drift hidden behind known source switch") }
}
