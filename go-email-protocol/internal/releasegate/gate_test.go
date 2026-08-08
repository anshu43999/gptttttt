package releasegate

import (
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"github.com/gpt-register/go-email-protocol/internal/protocol"
	"github.com/gpt-register/go-email-protocol/internal/rechallenge"
	"github.com/gpt-register/go-email-protocol/internal/sentinel"
	"github.com/gpt-register/go-email-protocol/internal/transport"
)

const firefox150UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:150.0) Gecko/20100101 Firefox/150.0"

func testSHA(ch byte) string { return "sha256:" + strings.Repeat(string(ch), 64) }

var testSentinelManifest = func() *sentinel.ReleaseManifest {
	manifest, err := sentinel.LoadRelease("../../testdata/rechallenge/sentinel-releases/20260219f9f6-r1/manifest.json")
	if err != nil {
		panic(err)
	}
	return manifest
}()

func testRegistrationContract(transportProfileID, sentinelReleaseID string) *rechallenge.RegistrationContract {
	const captureID = "registration-firefox150-test"
	contract := &rechallenge.RegistrationContract{
		SchemaVersion: rechallenge.CurrentSchemaVersion,
		Captures: []rechallenge.CaptureManifest{{
			SchemaVersion: rechallenge.CurrentSchemaVersion,
			CaptureID: captureID,
			Role: rechallenge.RoleRegistration,
			SourceSHA256: testSHA('f'),
			Browser: "firefox",
			UAMajor: 150,
			SourceKind: "har",
			RedactionPolicyID: "registration-v1",
		}},
		Flow: "oauth_create_account",
		BrowserIdentity: rechallenge.BrowserIdentity{Browser: "firefox", UAMajor: 150},
		SentinelReleaseID: sentinelReleaseID,
		TransportProfileID: transportProfileID,
		PolicyID: "registration-contract-v1",
	}
	type exchangeSpec struct {
		state protocol.StateID
		kind, method, host, path string
		occurrence int
	}
	specs := []exchangeSpec{
		{protocol.S1, "auth_providers", "GET", "chatgpt.com", "/api/auth/providers", -1},
		{protocol.S2, "csrf", "GET", "chatgpt.com", "/api/auth/csrf", -1},
		{protocol.S3, "signin", "POST", "chatgpt.com", "/api/auth/signin/openai", -1},
		{protocol.S4, "authorize", "GET", "auth.openai.com", "/api/accounts/authorize", -1},
		{protocol.S4, "redirect_hop", "GET", "auth.openai.com", "/email-verification", -1},
		{protocol.S10, "otp_validate", "POST", "auth.openai.com", "/api/accounts/email-otp/validate", -1},
		{protocol.T1, "sentinel_req", "POST", "sentinel.openai.com", "/backend-api/sentinel/req", 0},
		{protocol.S11, "create_account", "POST", "auth.openai.com", "/api/accounts/create_account", -1},
		{protocol.T1, "sentinel_req", "POST", "sentinel.openai.com", "/backend-api/sentinel/req", 1},
		{protocol.S12, "callback", "GET", "chatgpt.com", "/api/auth/callback/openai", -1},
		{protocol.T1, "sentinel_req", "POST", "sentinel.openai.com", "/backend-api/sentinel/req", 2},
		{protocol.S12, "redirect_hop", "GET", "chatgpt.com", "/", -1},
	}
	for index, spec := range specs {
		exchange := rechallenge.StateExchangeContract{
			State: spec.state,
			ExchangeIndex: index,
			CaptureSequence: index,
			Provenance: rechallenge.ExchangeProvenance{CaptureID: captureID, SourceKind: "har", HARIndex: index, Observed: true},
			Request: rechallenge.RequestContract{Kind: spec.kind, Method: spec.method, Host: spec.host, Path: spec.path, Body: rechallenge.BodyRule{Kind: "json"}},
			Response: rechallenge.ResponseContract{AllowedStatus: []int{200}, ObservedStatus: 200, Outcome: "observed"},
		}
		if spec.occurrence >= 0 {
			occurrence := spec.occurrence
			exchange.SentinelOccurrence = &occurrence
			exchange.FlowName = contract.Flow
			exchange.RequirementsFingerprint = testSHA('a')
			exchange.ObservedScriptSource = "https://sentinel.openai.com/backend-api/sentinel/sdk.js"
		}
		contract.Exchanges = append(contract.Exchanges, exchange)
	}
	if err := rechallenge.FinalizeContract(contract); err != nil {
		panic(err)
	}
	return contract
}

func testWireManifest(contract *rechallenge.RegistrationContract, profile transport.Profile, sentinelManifest *sentinel.ReleaseManifest, headerContractID string) *WireManifest {
	profileHash, err := TransportProfileCanonicalSHA256(profile)
	if err != nil {
		panic(err)
	}
	headerHash, err := HeaderPolicyCanonicalSHA256(contract)
	if err != nil {
		panic(err)
	}
	doc := wireManifestDocument{
		SchemaVersion: WireManifestSchemaVersion,
		ReleaseID: "wire-registration-r1",
		ContractReleaseID: contract.ContractID,
		ContractCanonicalSHA256: contract.CanonicalSHA256,
		SentinelReleaseID: sentinelManifest.ReleaseID(),
		SentinelManifestSHA256: sentinelManifest.ManifestSHA256(),
		TransportProfileID: profile.ID,
		TransportProfileSHA256: profileHash,
		HeaderContractID: headerContractID,
		HeaderContractSHA256: headerHash,
		Browser: profile.Browser,
		BrowserVersion: profile.BrowserVersion,
		BrowserVersionPolicy: VersionPolicyExact,
	}
	doc.ManifestSHA256, err = wireManifestIdentity(doc)
	if err != nil {
		panic(err)
	}
	return &WireManifest{doc: doc}
}

func untrustedContractEvidence(contract *rechallenge.RegistrationContract, manifest *WireManifest) ContractEvidence {
	return ContractEvidence{
		ReleaseID: contract.ContractID,
		CanonicalSHA256: contract.CanonicalSHA256,
		SentinelReleaseID: manifest.SentinelReleaseID(),
		TransportProfileID: manifest.TransportProfileID(),
		TransportProfileSHA256: manifest.TransportProfileSHA256(),
		HeaderContractID: manifest.HeaderContractID(),
		HeaderContractSHA256: manifest.HeaderContractSHA256(),
		Approved: true,
		Complete: true,
	}
}

func approvedInput() Input {
	profile := transport.Profile{
		ID:                         "firefox-150-win-h2-r1",
		BaselineCaptureID:          "registration-firefox150-20260724-jp",
		Browser:                    "firefox",
		BrowserVersion:             "150.0",
		BrowserMajor:               150,
		OS:                         "windows",
		GoVersion:                  "go1.22.12",
		ModuleGraphFixture:         "sha256:module-graph-fixture",
		TLSClientHelloFixture:      "sha256:clienthello-fixture",
		TLSExtensionOrderFixture:   "sha256:extension-order-fixture",
		ALPN:                       []string{"h2", "http/1.1"},
		HTTP2SettingsFixture:       "sha256:http2-settings-fixture",
		HTTP2ConnectionFlowFixture: "sha256:http2-flow-fixture",
		HTTP2PseudoHeaderOrder:     []string{":method", ":authority", ":scheme", ":path"},
		HeaderPresets: map[string]string{
			"document_navigation": "sha256:document-headers",
			"same_origin_fetch":   "sha256:fetch-headers",
			"cross_origin_oauth":  "sha256:oauth-headers",
			"otp_sparse":          "sha256:otp-headers",
			"sentinel_req":        "sha256:sentinel-headers",
			"callback_navigation": "sha256:callback-headers",
		},
		ResponseContentEncodingFix: "sha256:content-encoding-fixture",
		RedirectMaxHops:            10,
		CertificateValidation:      true,
		BridgeRequired:             true,
		Status:                     "active",
	}
	states := make(map[string]string, len(requestStates))
	for _, state := range requestStates {
		states[string(state)] = string(protocol.PresetFor(state))
	}
	contractDocument := testRegistrationContract(profile.ID, testSentinelManifest.ReleaseID())
	wireManifest, err := LoadWireManifest("testdata/approved-wire-manifest.json")
	if err != nil {
		panic(err)
	}
	contractEvidence, err := ContractEvidenceFromDocument(contractDocument, wireManifest, true)
	if err != nil {
		panic(err)
	}
	return Input{
		Purpose:          PurposeOfflineReplay,
		ResumeMode:       ResumeFresh,
		UserAgent:        firefox150UA,
		TransportFactory: transport.FactoryFake,
		TransportProfile: profile,
		Contract:          contractEvidence,
		ContractDocument:  contractDocument,
		WireManifest:      wireManifest,
		SentinelRelease: func() SentinelEvidence {
			evidence, err := SentinelEvidenceFromManifest(testSentinelManifest, true)
			if err != nil {
				panic(err)
			}
			return evidence
		}(),
		SentinelManifest: testSentinelManifest,
		HeaderContract: HeaderEvidence{
			ID:                 "registration-headers-r1",
			CanonicalSHA256:    contractEvidence.HeaderContractSHA256,
			ContractReleaseID:  contractDocument.ContractID,
			TransportProfileID: profile.ID,
			StatePresets:       states,
			Approved:           true,
			Complete:           true,
		},
		MaxActive: MaxActiveEvidence{
			Authoritative: 100,
			Admission:     100,
			Worker:        100,
			TasksService:  100,
			Config:        100,
		},
	}
}

func TestFullyApprovedFixtureOpensGate(t *testing.T) {
	in := approvedInput()
	v, err := Validate(in)
	if err != nil {
		t.Fatalf("Validate: %v; vector=%+v", err, v)
	}
	if v.Status != StatusOpen || len(v.Failures) != 0 {
		t.Fatalf("vector=%+v", v)
	}
	for name, got := range map[string]GateState{
		"contract": v.Contract, "sentinel": v.SentinelRelease,
		"transport": v.TransportProfile, "headers": v.HeaderContract,
		"checkpoint": v.CheckpointCompat, "max_active": v.MaxActiveAlignment,
	} {
		if got != GatePass {
			t.Fatalf("%s=%s", name, got)
		}
	}
}

func TestEveryMissingComponentClosesAdmission(t *testing.T) {
	tests := []struct {
		name      string
		mutate    func(*Input)
		component string
		code      string
	}{
		{"contract", func(in *Input) { in.Contract.ReleaseID, in.Contract.CanonicalSHA256 = "", "" }, "contract", CodeContractMissing},
		{"sentinel", func(in *Input) { in.SentinelRelease.ReleaseID, in.SentinelRelease.ManifestSHA256 = "", "" }, "sentinel_release", CodeSentinelReleaseMissing},
		{"contract-document", func(in *Input) { in.ContractDocument = nil }, "contract", CodeContractIncomplete},
		{"wire-manifest", func(in *Input) { in.WireManifest = nil }, "contract", CodeContractIncomplete},
		{"zero-wire-manifest", func(in *Input) { in.WireManifest = &WireManifest{} }, "contract", CodeContractBindingMismatch},
		{"sentinel-manifest", func(in *Input) { in.SentinelManifest = nil }, "sentinel_release", CodeSentinelReleaseIncomplete},
		{"transport", func(in *Input) { in.TransportProfile.TLSClientHelloFixture = "" }, "transport_profile", CodeTransportProfileIncomplete},
		{"headers", func(in *Input) { in.HeaderContract.ID, in.HeaderContract.CanonicalSHA256 = "", "" }, "header_contract", CodeHeaderContractMissing},
		{"resume-mode", func(in *Input) { in.ResumeMode = "" }, "checkpoint_compat", CodeCheckpointIncomplete},
		{"checkpoint", func(in *Input) { in.Checkpoint = &ReleaseBinding{} }, "checkpoint_compat", CodeCheckpointIncomplete},
		{"max-active", func(in *Input) { in.MaxActive.Worker = 0 }, "max_active_alignment", CodeMaxActiveMissing},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			in := approvedInput()
			tc.mutate(&in)
			v, err := Validate(in)
			if err == nil || v.Status != StatusClosed {
				t.Fatalf("err=%v vector=%+v", err, v)
			}
			var closed *ClosedError
			if !errors.As(err, &closed) || closed.Code() != CodeRuntimeGateClosed {
				t.Fatalf("untyped closed error: %T %v", err, err)
			}
			if !hasFailure(v, tc.component, tc.code) {
				t.Fatalf("missing %s/%s in %+v", tc.component, tc.code, v.Failures)
			}
		})
	}
}

func TestMismatchedReleaseAndHeaderEvidenceClosesAdmission(t *testing.T) {
	tests := []struct {
		name      string
		mutate    func(*Input)
		component string
		code      string
	}{
		{"contract", func(in *Input) { in.Contract.ReleaseID += "-other" }, "contract", CodeContractBindingMismatch},
		{"sentinel", func(in *Input) { in.SentinelRelease.ManifestSHA256 = testSHA('a') }, "sentinel_release", CodeSentinelReleaseMismatch},
		{"headers", func(in *Input) { in.HeaderContract.StatePresets[string(protocol.S11)] = "otp_sparse" }, "header_contract", CodeHeaderContractMismatch},
		{"header-hash", func(in *Input) { in.HeaderContract.CanonicalSHA256 = testSHA('b') }, "header_contract", CodeHeaderContractMismatch},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			in := approvedInput()
			tc.mutate(&in)
			v := Evaluate(in)
			if v.Status != StatusClosed || !hasFailure(v, tc.component, tc.code) {
				t.Fatalf("vector=%+v", v)
			}
		})
	}
}

func TestHeaderPolicyCanonicalHashIgnoresNormalizedPermutations(t *testing.T) {
	contract := testRegistrationContract("firefox-150-win-h2-r1", testSentinelManifest.ReleaseID())
	contract.Exchanges[0].Request.Headers = []rechallenge.HeaderRule{{
		Name: "accept", Source: "capture", Presence: "required", ValuePolicy: "exact",
		Expected: "application/json", OrderPolicy: "relative", Order: 1,
		Multiplicity: "single", Provenance: []string{"har:9", "har:1"},
	}}
	if err := rechallenge.FinalizeContract(contract); err != nil {
		t.Fatal(err)
	}
	want, err := HeaderPolicyCanonicalSHA256(contract)
	if err != nil {
		t.Fatal(err)
	}
	raw, err := json.Marshal(contract)
	if err != nil {
		t.Fatal(err)
	}
	var permuted rechallenge.RegistrationContract
	if err := json.Unmarshal(raw, &permuted); err != nil {
		t.Fatal(err)
	}
	for left, right := 0, len(permuted.Exchanges)-1; left < right; left, right = left+1, right-1 {
		permuted.Exchanges[left], permuted.Exchanges[right] = permuted.Exchanges[right], permuted.Exchanges[left]
	}
	for index := range permuted.Exchanges {
		if permuted.Exchanges[index].CaptureSequence == 0 {
			provenance := permuted.Exchanges[index].Request.Headers[0].Provenance
			provenance[0], provenance[1] = provenance[1], provenance[0]
			break
		}
	}
	got, err := HeaderPolicyCanonicalSHA256(&permuted)
	if err != nil {
		t.Fatal(err)
	}
	if got != want {
		t.Fatalf("canonical header hash changed across normalized permutations: got %s want %s", got, want)
	}
}

func TestSameIDContentSubstitutionClosesGate(t *testing.T) {
	in := approvedInput()
	in.TransportProfile.TLSClientHelloFixture = "sha256:substituted-clienthello-fixture"
	v := Evaluate(in)
	if !hasFailure(v, "transport_profile", CodeTransportProfileMismatch) {
		t.Fatalf("same-ID profile substitution vector=%+v", v)
	}

	in = approvedInput()
	in.TransportProfile.TLSClientHelloFixture = "sha256:coordinated-substituted-clienthello"
	substitutedManifest := testWireManifest(in.ContractDocument, in.TransportProfile, testSentinelManifest, in.HeaderContract.ID)
	evidence := untrustedContractEvidence(in.ContractDocument, substitutedManifest)
	in.WireManifest = substitutedManifest
	in.Contract = evidence
	// The package-embedded approved manifest authority intentionally remains unchanged.
	v = Evaluate(in)
	if !hasFailure(v, "contract", CodeContractBindingMismatch) {
		t.Fatalf("coordinated same-ID recomputation vector=%+v", v)
	}

	in = approvedInput()
	forgedHeaderDoc := in.WireManifest.doc
	forgedHeaderDoc.HeaderContractSHA256 = testSHA('b')
	forgedHeaderDoc.ManifestSHA256, _ = wireManifestIdentity(forgedHeaderDoc)
	forgedHeaderManifest := &WireManifest{doc: forgedHeaderDoc}
	in.WireManifest = forgedHeaderManifest
	in.Contract.HeaderContractSHA256 = forgedHeaderDoc.HeaderContractSHA256
	in.HeaderContract.CanonicalSHA256 = forgedHeaderDoc.HeaderContractSHA256
	forgedRaw, err := json.Marshal(forgedHeaderDoc)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := ParseWireManifest(forgedRaw); err == nil {
		t.Fatal("recomputed header manifest unexpectedly passed embedded authority")
	}
	v = Evaluate(in)
	if !hasFailure(v, "contract", CodeContractBindingMismatch) {
		t.Fatalf("coordinated header manifest recomputation vector=%+v", v)
	}

	in = approvedInput()
	in.HeaderContract.CanonicalSHA256 = testSHA('b')
	v = Evaluate(in)
	if !hasFailure(v, "header_contract", CodeHeaderContractMismatch) {
		t.Fatalf("same-ID header substitution vector=%+v", v)
	}
}

func TestFirefox150RejectsFirefox135FallbackProfile(t *testing.T) {
	in := approvedInput()
	in.TransportProfile.ID = "firefox-135-win-h2-fallback"
	in.TransportProfile.BrowserMajor = 135
	in.TransportProfile.BrowserVersion = "135.0"
	in.Contract.TransportProfileID = in.TransportProfile.ID
	in.HeaderContract.TransportProfileID = in.TransportProfile.ID
	v := Evaluate(in)
	if v.Status != StatusClosed || !hasFailure(v, "transport_profile", CodeTransportProfileMismatch) {
		t.Fatalf("vector=%+v", v)
	}
}
func TestCoordinatedFirefox135RebindCannotRewriteCaptureIdentity(t *testing.T) {
	in := approvedInput()
	in.UserAgent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0"
	in.TransportProfile.ID = "firefox-135-win-h2-r1"
	in.TransportProfile.BrowserMajor = 135
	in.TransportProfile.BrowserVersion = "135.0"
	in.ContractDocument.TransportProfileID = in.TransportProfile.ID
	if err := rechallenge.FinalizeContract(in.ContractDocument); err != nil {
		t.Fatal(err)
	}
	newWireManifest := testWireManifest(in.ContractDocument, in.TransportProfile, testSentinelManifest, in.HeaderContract.ID)
	evidence := untrustedContractEvidence(in.ContractDocument, newWireManifest)
	in.WireManifest = newWireManifest
	in.Contract = evidence
	in.HeaderContract.ContractReleaseID = evidence.ReleaseID
	in.HeaderContract.TransportProfileID = in.TransportProfile.ID
	in.HeaderContract.CanonicalSHA256 = evidence.HeaderContractSHA256
	v := Evaluate(in)
	if !hasFailure(v, "transport_profile", CodeTransportProfileMismatch) {
		t.Fatalf("coordinated rebind vector=%+v", v)
	}
}


func TestProfileIDCannotHideFallbackBrowserVersion(t *testing.T) {
	in := approvedInput()
	in.TransportProfile.BrowserVersion = "135.0"
	v := Evaluate(in)
	if !hasFailure(v, "transport_profile", CodeTransportProfileMismatch) {
		t.Fatalf("vector=%+v", v)
	}
}

func TestMalformedBrowserVersionClosesGate(t *testing.T) {
	for _, version := range []string{"not-a-version", "150.invalid", "150.", "150..0"} {
		t.Run(version, func(t *testing.T) {
			in := approvedInput()
			in.TransportProfile.BrowserVersion = version
			v := Evaluate(in)
			if !hasFailure(v, "transport_profile", CodeTransportProfileMismatch) {
				t.Fatalf("version=%q vector=%+v", version, v)
			}
		})
	}
}

func TestEdgeMajorComesFromEdgeToken(t *testing.T) {
	in := approvedInput()
	in.UserAgent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/150.0.0.0 Safari/537.36 Edg/135.0.0.0"
	in.TransportProfile.ID = "edge-150-win-h2-r1"
	in.TransportProfile.Browser = "edge"
	in.TransportProfile.BrowserMajor = 150
	in.TransportProfile.BrowserVersion = "150.0.0.0"
	in.Contract.TransportProfileID = in.TransportProfile.ID
	in.HeaderContract.TransportProfileID = in.TransportProfile.ID
	v := Evaluate(in)
	if !hasFailure(v, "transport_profile", CodeTransportProfileMismatch) {
		t.Fatalf("mismatched Edge token vector=%+v", v)
	}

	browser, major, err := effectiveUA("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0")
	if err != nil || browser != "edge" || major != 150 {
		t.Fatalf("matching Edge parse browser=%q major=%d err=%v", browser, major, err)
	}
}

func TestFirefoxRVAndVersionTokenMustAgree(t *testing.T) {
	for _, ua := range []string{
		"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:150.0) Gecko/20100101 Firefox/135.0",
		"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:150.99) Gecko/20100101 Firefox/150.0",
		"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:150.0) Gecko/20100101 Firefox/150.0evil",
	} {
		in := approvedInput()
		in.UserAgent = ua
		v := Evaluate(in)
		if !hasFailure(v, "transport_profile", CodeTransportProfileMismatch) {
			t.Fatalf("ua=%q vector=%+v", ua, v)
		}
	}
}

func TestCaptureRequiredAndIncompleteProfilesCloseGate(t *testing.T) {
	t.Run("capture-required", func(t *testing.T) {
		in := approvedInput()
		in.TransportProfile.Status = "capture_required"
		v := Evaluate(in)
		if !hasFailure(v, "transport_profile", CodeTransportProfileInactive) {
			t.Fatalf("vector=%+v", v)
		}
	})
	t.Run("incomplete", func(t *testing.T) {
		in := approvedInput()
		in.TransportProfile.HTTP2SettingsFixture = ""
		in.TransportProfile.CertificateValidation = false
		v := Evaluate(in)
		if !hasFailure(v, "transport_profile", CodeTransportProfileIncomplete) {
			t.Fatalf("vector=%+v", v)
		}
	})
	t.Run("missing-os", func(t *testing.T) {
		in := approvedInput()
		in.TransportProfile.OS = ""
		v := Evaluate(in)
		if !hasFailure(v, "transport_profile", CodeTransportProfileIncomplete) {
			t.Fatalf("vector=%+v", v)
		}
	})
	t.Run("os-mismatch", func(t *testing.T) {
		in := approvedInput()
		in.TransportProfile.OS = "android"
		v := Evaluate(in)
		if !hasFailure(v, "transport_profile", CodeTransportProfileMismatch) {
			t.Fatalf("vector=%+v", v)
		}
	})
	for _, tc := range []struct {
		name   string
		mutate func(*transport.Profile)
	}{
		{"tls-placeholder", func(p *transport.Profile) { p.TLSClientHelloFixture = "capture_required" }},
		{"http2-placeholder", func(p *transport.Profile) { p.HTTP2SettingsFixture = "capture_required" }},
		{"header-placeholder", func(p *transport.Profile) { p.HeaderPresets["sentinel_req"] = "capture_required" }},
		{"alpn-reordered", func(p *transport.Profile) { p.ALPN = []string{"http/1.1", "h2"} }},
		{"alpn-placeholder", func(p *transport.Profile) { p.ALPN = []string{"h2", "capture_required"} }},
		{"pseudo-header-reordered", func(p *transport.Profile) { p.HTTP2PseudoHeaderOrder = []string{":method", ":scheme", ":authority", ":path"} }},
		{"pseudo-header-empty", func(p *transport.Profile) { p.HTTP2PseudoHeaderOrder = []string{":method", "", ":scheme", ":path"} }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			in := approvedInput()
			tc.mutate(&in.TransportProfile)
			v := Evaluate(in)
			if !hasFailure(v, "transport_profile", CodeTransportProfileIncomplete) {
				t.Fatalf("vector=%+v", v)
			}
		})
	}

	t.Run("wire-bridge-required", func(t *testing.T) {
		in := approvedInput()
		in.Purpose = PurposeWireCanary
		in.TransportFactory = transport.FactoryTLS
		in.TransportProfile.BridgeRequired = false
		resolved, err := transport.ResolveEffectiveTLSProfile("firefox", 150)
		if err != nil {
			t.Fatal(err)
		}
		in.EffectiveTransport = &resolved
		v := Evaluate(in)
		if !hasFailure(v, "transport_profile", CodeTransportProfileIncomplete) {
			t.Fatalf("vector=%+v", v)
		}
	})
}

func TestFactoryResolvedFirefoxFallbackClosesWireCanary(t *testing.T) {
	in := approvedInput()
	in.Purpose = PurposeWireCanary
	in.TransportFactory = transport.FactoryTLS
	resolved, err := transport.ResolveEffectiveTLSProfile("firefox", 150)
	if err != nil {
		t.Fatal(err)
	}
	if !resolved.Fallback || resolved.EffectiveMajor != 135 || resolved.ProfileName != "Firefox_135" {
		t.Fatalf("unexpected resolver outcome %+v", resolved)
	}
	in.EffectiveTransport = &resolved
	v := Evaluate(in)
	if !hasFailure(v, "transport_profile", CodeTransportProfileMismatch) {
		t.Fatalf("fallback wire canary vector=%+v", v)
	}

	in.EffectiveTransport = nil
	v = Evaluate(in)
	if !hasFailure(v, "transport_profile", CodeTransportProfileMismatch) {
		t.Fatalf("missing resolver evidence vector=%+v", v)
	}
}

func TestDirectTransportIsDiagnosticOnly(t *testing.T) {
	in := approvedInput()
	in.TransportFactory = transport.FactoryDirect
	v := Evaluate(in)
	if !hasFailure(v, "transport_profile", CodeTransportDiagnosticOnly) {
		t.Fatalf("wire canary vector=%+v", v)
	}

	in.Purpose = PurposeLiveAdmission
	v = Evaluate(in)
	if !hasFailure(v, "transport_profile", CodeTransportDiagnosticOnly) {
		t.Fatalf("live admission vector=%+v", v)
	}

	in.Purpose = PurposeDiagnostic
	v = Evaluate(in)
	if v.Status != StatusOpen {
		t.Fatalf("diagnostic vector=%+v", v)
	}

	for _, factory := range []transport.FactoryName{transport.FactoryFake, transport.FactoryDirect} {
		in.TransportFactory = factory
		v = Evaluate(in)
		if v.Status != StatusOpen {
			t.Fatalf("explicit diagnostic factory %q vector=%+v", factory, v)
		}
	}
	for _, factory := range []transport.FactoryName{"", "unknown", transport.FactoryTLS} {
		in.TransportFactory = factory
		v = Evaluate(in)
		if !hasFailure(v, "transport_profile", CodeTransportDiagnosticOnly) {
			t.Fatalf("unapproved diagnostic factory %q vector=%+v", factory, v)
		}
	}
}

func TestCheckpointRequiresExactReleaseAndProfile(t *testing.T) {
	base := approvedInput()
	base.ResumeMode = ResumeRecovery
	base.Checkpoint = &ReleaseBinding{
		WireReleaseID: base.WireManifest.ReleaseID(),
		WireManifestSHA256: base.WireManifest.ManifestSHA256(),
		ContractReleaseID:  base.Contract.ReleaseID,
		ContractCanonicalSHA256: base.Contract.CanonicalSHA256,
		TransportProfileSHA256: base.Contract.TransportProfileSHA256,
		SentinelReleaseID:  base.SentinelRelease.ReleaseID,
		SentinelManifestSHA256: base.SentinelRelease.ManifestSHA256,
		HeaderContractID: base.HeaderContract.ID,
		HeaderContractSHA256: base.HeaderContract.CanonicalSHA256,
		TransportProfileID: base.TransportProfile.ID,
	}
	if v := Evaluate(base); v.Status != StatusOpen {
		t.Fatalf("matching checkpoint=%+v", v)
	}

	for _, tc := range []struct {
		name   string
		mutate func(*ReleaseBinding)
	}{
		{"wire-release", func(cp *ReleaseBinding) { cp.WireReleaseID += "-old" }},
		{"wire-manifest-hash", func(cp *ReleaseBinding) { cp.WireManifestSHA256 = testSHA('a') }},
		{"contract", func(cp *ReleaseBinding) { cp.ContractReleaseID += "-old" }},
		{"contract-hash", func(cp *ReleaseBinding) { cp.ContractCanonicalSHA256 = testSHA('a') }},
		{"sentinel-release", func(cp *ReleaseBinding) { cp.SentinelReleaseID += "-old" }},
		{"sentinel-manifest-hash", func(cp *ReleaseBinding) { cp.SentinelManifestSHA256 = testSHA('a') }},
		{"transport-profile", func(cp *ReleaseBinding) { cp.TransportProfileID = "firefox-135-win-h2-fallback" }},
		{"transport-profile-hash", func(cp *ReleaseBinding) { cp.TransportProfileSHA256 = testSHA('a') }},
		{"header-contract", func(cp *ReleaseBinding) { cp.HeaderContractID += "-old" }},
		{"header-contract-hash", func(cp *ReleaseBinding) { cp.HeaderContractSHA256 = testSHA('a') }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			in := approvedInput()
			cp := *base.Checkpoint
			tc.mutate(&cp)
			in.Checkpoint = &cp
			in.ResumeMode = ResumeRecovery
			v := Evaluate(in)
			if !hasFailure(v, "checkpoint_compat", CodeCheckpointReleaseMismatch) {
				t.Fatalf("vector=%+v", v)
			}
		})
	}
}

func TestRecoveryRequiresCheckpoint(t *testing.T) {
	in := approvedInput()
	in.ResumeMode = ResumeRecovery
	in.Checkpoint = nil
	v := Evaluate(in)
	if !hasFailure(v, "checkpoint_compat", CodeCheckpointIncomplete) {
		t.Fatalf("vector=%+v", v)
	}
}

func TestMaxActiveMustHaveOneEffectiveValue(t *testing.T) {
	for _, tc := range []struct {
		name   string
		mutate func(*MaxActiveEvidence)
	}{
		{"admission", func(m *MaxActiveEvidence) { m.Admission = 99 }},
		{"worker", func(m *MaxActiveEvidence) { m.Worker = 101 }},
		{"tasks-service", func(m *MaxActiveEvidence) { m.TasksService = 200 }},
		{"config", func(m *MaxActiveEvidence) { m.Config = 16 }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			in := approvedInput()
			tc.mutate(&in.MaxActive)
			v := Evaluate(in)
			if !hasFailure(v, "max_active_alignment", CodeMaxActiveMismatch) {
				t.Fatalf("vector=%+v", v)
			}
		})
	}
}

func TestVectorJSONIsMachineReadable(t *testing.T) {
	v := Evaluate(approvedInput())
	data, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	var decoded map[string]any
	if err := json.Unmarshal(data, &decoded); err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{"contract", "sentinel_release", "transport_profile", "header_contract", "checkpoint_compat", "max_active_alignment", "status"} {
		if _, ok := decoded[key]; !ok {
			t.Fatalf("missing JSON key %q: %s", key, data)
		}
	}
	if decoded["status"] != string(StatusOpen) {
		t.Fatalf("status=%v JSON=%s", decoded["status"], data)
	}
}

func hasFailure(v Vector, component, code string) bool {
	for _, failure := range v.Failures {
		if failure.Component == component && failure.Code == code {
			return true
		}
	}
	return false
}
