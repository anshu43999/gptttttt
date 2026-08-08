package releasegate

import (
	"encoding/hex"
	"fmt"
	"strconv"
	"strings"

	"github.com/gpt-register/go-email-protocol/internal/fingerprint"
	"github.com/gpt-register/go-email-protocol/internal/rechallenge"
	"github.com/gpt-register/go-email-protocol/internal/protocol"
	"github.com/gpt-register/go-email-protocol/internal/transport"
)

var requestStates = [...]protocol.StateID{
	protocol.S1, protocol.S2, protocol.S3, protocol.S4,
	protocol.S5, protocol.S6, protocol.S7, protocol.S8,
	protocol.S10, protocol.S11, protocol.S12, protocol.S13,
}

// Evaluate checks every startup component and returns all failures in stable
// component/check order. It never substitutes defaults for missing evidence.
func Evaluate(in Input) Vector {
	v := Vector{
		Purpose:            in.Purpose,
		ResumeMode:         in.ResumeMode,
		Contract:           GatePass,
		SentinelRelease:    GatePass,
		TransportProfile:   GatePass,
		HeaderContract:     GatePass,
		CheckpointCompat:   GatePass,
		MaxActiveAlignment: GatePass,
		Status:             StatusOpen,
	}

	checkContract(&v, in)
	checkSentinel(&v, in)
	checkTransport(&v, in)
	checkHeaders(&v, in)
	checkCheckpoint(&v, in)
	checkMaxActive(&v, in)

	if len(v.Failures) != 0 {
		v.Status = StatusClosed
	}
	return v
}

// Validate returns the vector and a typed aggregate error when admission is
// closed. Callers should publish the vector rather than parse the error text.
func Validate(in Input) (Vector, error) {
	v := Evaluate(in)
	if v.Status == StatusClosed {
		return v, &ClosedError{Vector: v}
	}
	return v, nil
}

func checkContract(v *Vector, in Input) {
	c := in.Contract
	if strings.TrimSpace(c.ReleaseID) == "" || !validSHA256(c.CanonicalSHA256) {
		fail(v, &v.Contract, "contract", CodeContractMissing, "release id and canonical SHA-256 are required")
	}
	if !c.Approved {
		fail(v, &v.Contract, "contract", CodeContractUnapproved, "contract release is not approved")
	}
	if !c.Complete || strings.TrimSpace(c.SentinelReleaseID) == "" || strings.TrimSpace(c.TransportProfileID) == "" || !validSHA256(c.TransportProfileSHA256) || strings.TrimSpace(c.HeaderContractID) == "" || !validSHA256(c.HeaderContractSHA256) {
		fail(v, &v.Contract, "contract", CodeContractIncomplete, "contract release bindings are incomplete")
	}
	if in.WireManifest == nil {
		fail(v, &v.Contract, "contract", CodeContractIncomplete, "validated immutable wire manifest is required")
	} else if err := validateWireManifestDocument(in.WireManifest.doc); err != nil {
		fail(v, &v.Contract, "contract", CodeContractBindingMismatch, "wire manifest is not the embedded approved release")
	} else if c.ReleaseID != in.WireManifest.ContractReleaseID() || c.CanonicalSHA256 != in.WireManifest.ContractCanonicalSHA256() ||
		c.SentinelReleaseID != in.WireManifest.SentinelReleaseID() || c.TransportProfileID != in.WireManifest.TransportProfileID() ||
		c.TransportProfileSHA256 != in.WireManifest.TransportProfileSHA256() || c.HeaderContractID != in.WireManifest.HeaderContractID() ||
		c.HeaderContractSHA256 != in.WireManifest.HeaderContractSHA256() {
		fail(v, &v.Contract, "contract", CodeContractBindingMismatch, "explicit contract evidence differs from approved wire manifest")
	}
	if in.ContractDocument == nil {
		fail(v, &v.Contract, "contract", CodeContractIncomplete, "validated normalized registration contract is required")
	} else if err := rechallenge.ValidateContract(in.ContractDocument); err != nil {
		fail(v, &v.Contract, "contract", CodeContractIncomplete, "normalized registration contract failed validation")
	} else if c.ReleaseID != in.ContractDocument.ContractID || c.CanonicalSHA256 != in.ContractDocument.CanonicalSHA256 ||
		c.SentinelReleaseID != in.ContractDocument.SentinelReleaseID || c.TransportProfileID != in.ContractDocument.TransportProfileID {
		fail(v, &v.Contract, "contract", CodeContractBindingMismatch, "explicit contract release identity differs from the validated normalized contract")
	}
	if c.SentinelReleaseID != in.SentinelRelease.ReleaseID {
		fail(v, &v.Contract, "contract", CodeContractBindingMismatch, "contract Sentinel release binding does not match effective release")
	}
	if c.TransportProfileID != in.TransportProfile.ID {
		fail(v, &v.Contract, "contract", CodeContractBindingMismatch, "contract transport profile binding does not match effective profile")
	}
	if c.HeaderContractID != in.HeaderContract.ID {
		fail(v, &v.Contract, "contract", CodeContractBindingMismatch, "contract header binding does not match effective header contract")
	}
}

func checkSentinel(v *Vector, in Input) {
	s := in.SentinelRelease
	if strings.TrimSpace(s.ReleaseID) == "" || !validSHA256(s.ManifestSHA256) {
		fail(v, &v.SentinelRelease, "sentinel_release", CodeSentinelReleaseMissing, "release id and manifest SHA-256 are required")
	}
	if !s.Approved {
		fail(v, &v.SentinelRelease, "sentinel_release", CodeSentinelReleaseUnapproved, "Sentinel release is not approved")
	}
	if !s.Complete || !validSHA256(s.LoaderSHA256) || !validSHA256(s.SDKSHA256) {
		fail(v, &v.SentinelRelease, "sentinel_release", CodeSentinelReleaseIncomplete, "verified loader and SDK relationship evidence is incomplete")
	}
	if in.SentinelManifest == nil {
		fail(v, &v.SentinelRelease, "sentinel_release", CodeSentinelReleaseIncomplete, "validated Sentinel release manifest is required")
		return
	}
	if err := in.SentinelManifest.ValidateEmbeddedPin(); err != nil {
		fail(v, &v.SentinelRelease, "sentinel_release", CodeSentinelReleaseIncomplete, "Sentinel release manifest no longer matches the embedded SDK pin")
		return
	}
	loader := in.SentinelManifest.Loader()
	sdk := in.SentinelManifest.SDK()
	if s.ReleaseID != in.SentinelManifest.ReleaseID() || s.ManifestSHA256 != in.SentinelManifest.ManifestSHA256() ||
		normalizeSHA256(s.LoaderSHA256) != normalizeSHA256(loader.SHA256) || normalizeSHA256(s.SDKSHA256) != normalizeSHA256(sdk.SHA256) {
		fail(v, &v.SentinelRelease, "sentinel_release", CodeSentinelReleaseMismatch, "explicit Sentinel release identity differs from the validated manifest")
	}
	if in.WireManifest == nil || s.ReleaseID != in.WireManifest.SentinelReleaseID() || s.ManifestSHA256 != in.WireManifest.SentinelManifestSHA256() {
		fail(v, &v.SentinelRelease, "sentinel_release", CodeSentinelReleaseMismatch, "Sentinel release differs from approved wire manifest")
	}
}

func checkTransport(v *Vector, in Input) {
	p := in.TransportProfile
	if strings.ToLower(strings.TrimSpace(p.Status)) != "active" {
		fail(v, &v.TransportProfile, "transport_profile", CodeTransportProfileInactive, "effective transport profile is not active")
	}
	if missing := incompleteTransportFields(p); len(missing) != 0 {
		fail(v, &v.TransportProfile, "transport_profile", CodeTransportProfileIncomplete, "missing profile evidence: "+strings.Join(missing, ","))
	}

	factory := in.TransportFactory
	switch in.Purpose {
	case PurposeDiagnostic:
		if factory != transport.FactoryDirect && factory != transport.FactoryFake {
			fail(v, &v.TransportProfile, "transport_profile", CodeTransportDiagnosticOnly, "diagnostic purpose requires an explicit direct or fake transport")
		}
	case PurposeOfflineReplay:
		if factory != transport.FactoryFake {
			fail(v, &v.TransportProfile, "transport_profile", CodeTransportDiagnosticOnly, "offline replay requires the explicit fake transport")
		}
	case PurposeLiveAdmission, PurposeWireCanary:
		if factory != transport.FactoryTLS {
			fail(v, &v.TransportProfile, "transport_profile", CodeTransportDiagnosticOnly, "live admission and wire canary require the approved TLS transport")
		}
	default:
		fail(v, &v.TransportProfile, "transport_profile", CodeTransportProfileIncomplete, "startup purpose is required")
	}
	if (in.Purpose == PurposeLiveAdmission || in.Purpose == PurposeWireCanary) && !p.BridgeRequired {
		fail(v, &v.TransportProfile, "transport_profile", CodeTransportProfileIncomplete, "live admission and wire canary require bridge_required=true")
	}

	browser, major, err := effectiveUA(in.UserAgent)
	if err != nil {
		fail(v, &v.TransportProfile, "transport_profile", CodeTransportProfileMismatch, err.Error())
		return
	}
	checkEffectiveTransport(v, in, browser, major)
	if doc := in.ContractDocument; doc != nil {
		if strings.ToLower(strings.TrimSpace(doc.BrowserIdentity.Browser)) != browser || doc.BrowserIdentity.UAMajor != major {
			fail(v, &v.TransportProfile, "transport_profile", CodeTransportProfileMismatch, "normalized contract browser identity differs from effective UA")
		}
		for _, capture := range doc.Captures {
			if strings.ToLower(strings.TrimSpace(capture.Browser)) != browser || capture.UAMajor != major {
				fail(v, &v.TransportProfile, "transport_profile", CodeTransportProfileMismatch, "capture browser provenance differs from effective UA")
				break
			}
		}
	}
	profileBrowser := strings.ToLower(strings.TrimSpace(p.Browser))
	idBrowser := transport.BrowserFromProfileID(p.ID)
	idMajor, idMajorOK := transport.MajorFromProfileID(p.ID)
	if profileBrowser != browser || idBrowser != browser || p.BrowserMajor != major || !idMajorOK || idMajor != major {
		fail(v, &v.TransportProfile, "transport_profile", CodeTransportProfileMismatch,
			fmt.Sprintf("effective UA %s/%d does not match profile browser/major", browser, major))
	}
	uaOS, uaOSOK := normalizedUAOS(in.UserAgent)
	profileOS, profileOSOK := normalizeOS(p.OS)
	idOS, idOSOK := profileIDOS(p.ID)
	if !uaOSOK || !profileOSOK || !idOSOK || profileOS != uaOS || idOS != uaOS {
		fail(v, &v.TransportProfile, "transport_profile", CodeTransportProfileMismatch,
			fmt.Sprintf("effective UA OS %q does not match profile OS identity", uaOS))
	}
	if rawVersion := strings.TrimSpace(p.BrowserVersion); rawVersion != "" {
		versionMajor, ok := browserVersionMajor(rawVersion)
		if !ok {
			fail(v, &v.TransportProfile, "transport_profile", CodeTransportProfileMismatch, "profile browser version is malformed")
		} else if versionMajor != major {
			fail(v, &v.TransportProfile, "transport_profile", CodeTransportProfileMismatch,
				fmt.Sprintf("profile browser version major %d does not match effective UA major %d", versionMajor, major))
		}
		uaVersion, uaVersionOK := browserVersionFromUA(in.UserAgent, browser)
		if !uaVersionOK {
			fail(v, &v.TransportProfile, "transport_profile", CodeTransportProfileMismatch, "effective UA browser version is malformed")
		} else if in.WireManifest != nil {
			if strings.ToLower(strings.TrimSpace(in.WireManifest.Browser())) != browser || in.WireManifest.BrowserVersion() != rawVersion {
				fail(v, &v.TransportProfile, "transport_profile", CodeTransportProfileMismatch, "profile browser version differs from approved wire manifest")
			} else if in.WireManifest.BrowserVersionPolicy() == VersionPolicyExact && rawVersion != uaVersion {
				fail(v, &v.TransportProfile, "transport_profile", CodeTransportProfileMismatch, "effective UA full version differs from exact profile policy")
			}
		}
	}
	approvedProfileHash := ""
	if in.WireManifest != nil {
		approvedProfileHash = in.WireManifest.TransportProfileSHA256()
	}
	profileHash, hashErr := TransportProfileCanonicalSHA256(p)
	if hashErr != nil || profileHash != approvedProfileHash || in.Contract.TransportProfileSHA256 != approvedProfileHash {
		fail(v, &v.TransportProfile, "transport_profile", CodeTransportProfileMismatch, "effective transport profile content hash differs from approved wire manifest")
	}
}

func checkHeaders(v *Vector, in Input) {
	h := in.HeaderContract
	if strings.TrimSpace(h.ID) == "" || !validSHA256(h.CanonicalSHA256) {
		fail(v, &v.HeaderContract, "header_contract", CodeHeaderContractMissing, "header contract id and canonical SHA-256 are required")
	}
	if !h.Approved {
		fail(v, &v.HeaderContract, "header_contract", CodeHeaderContractUnapproved, "header contract is not approved")
	}
	if !h.Complete {
		fail(v, &v.HeaderContract, "header_contract", CodeHeaderContractIncomplete, "header contract is marked incomplete")
	}
	approvedHeaderID, approvedHeaderHash := "", ""
	if in.WireManifest != nil {
		approvedHeaderID = in.WireManifest.HeaderContractID()
		approvedHeaderHash = in.WireManifest.HeaderContractSHA256()
	}
	if h.ContractReleaseID != in.Contract.ReleaseID || h.TransportProfileID != in.TransportProfile.ID || h.ID != approvedHeaderID || h.CanonicalSHA256 != approvedHeaderHash {
		fail(v, &v.HeaderContract, "header_contract", CodeHeaderContractMismatch, "header contract identity/release/profile binding does not match approved wire manifest")
	}
	expectedHeaderHash, headerHashErr := HeaderPolicyCanonicalSHA256(in.ContractDocument)
	if headerHashErr != nil || h.CanonicalSHA256 != expectedHeaderHash {
		fail(v, &v.HeaderContract, "header_contract", CodeHeaderContractMismatch, "header contract canonical hash differs from normalized contract policy")
	}
	for _, state := range requestStates {
		want := string(protocol.PresetFor(state))
		got := strings.TrimSpace(h.StatePresets[string(state)])
		if got == "" {
			fail(v, &v.HeaderContract, "header_contract", CodeHeaderContractIncomplete, fmt.Sprintf("missing header preset binding for %s", state))
			continue
		}
		if got != want {
			fail(v, &v.HeaderContract, "header_contract", CodeHeaderContractMismatch, fmt.Sprintf("header preset binding for %s does not match FSM", state))
		}
		if strings.TrimSpace(in.TransportProfile.HeaderPresets[got]) == "" {
			fail(v, &v.HeaderContract, "header_contract", CodeHeaderContractMismatch, fmt.Sprintf("transport profile has no fixture for preset %s", got))
		}
	}
}

func checkCheckpoint(v *Vector, in Input) {
	switch in.ResumeMode {
	case ResumeFresh:
		if in.Checkpoint != nil {
			fail(v, &v.CheckpointCompat, "checkpoint_compat", CodeCheckpointIncomplete, "fresh startup must not carry a recovery checkpoint")
		}
		return
	case ResumeRecovery:
		if in.Checkpoint == nil {
			fail(v, &v.CheckpointCompat, "checkpoint_compat", CodeCheckpointIncomplete, "recovery startup requires a checkpoint binding")
			return
		}
	default:
		fail(v, &v.CheckpointCompat, "checkpoint_compat", CodeCheckpointIncomplete, "explicit fresh or recovery mode is required")
		return
	}
	cp := *in.Checkpoint
	if strings.TrimSpace(cp.WireReleaseID) == "" || !validSHA256(cp.WireManifestSHA256) ||
		strings.TrimSpace(cp.ContractReleaseID) == "" || !validSHA256(cp.ContractCanonicalSHA256) ||
		strings.TrimSpace(cp.SentinelReleaseID) == "" || !validSHA256(cp.SentinelManifestSHA256) ||
		strings.TrimSpace(cp.TransportProfileID) == "" || !validSHA256(cp.TransportProfileSHA256) ||
		strings.TrimSpace(cp.HeaderContractID) == "" || !validSHA256(cp.HeaderContractSHA256) {
		fail(v, &v.CheckpointCompat, "checkpoint_compat", CodeCheckpointIncomplete, "checkpoint release binding is incomplete")
		return
	}
	manifestID, manifestHash := "", ""
	if in.WireManifest != nil {
		manifestID, manifestHash = in.WireManifest.ReleaseID(), in.WireManifest.ManifestSHA256()
	}
	if cp.WireReleaseID != manifestID || cp.WireManifestSHA256 != manifestHash ||
		cp.ContractReleaseID != in.Contract.ReleaseID || cp.ContractCanonicalSHA256 != in.Contract.CanonicalSHA256 ||
		cp.SentinelReleaseID != in.SentinelRelease.ReleaseID || cp.SentinelManifestSHA256 != in.SentinelRelease.ManifestSHA256 ||
		cp.TransportProfileID != in.TransportProfile.ID || cp.TransportProfileSHA256 != in.Contract.TransportProfileSHA256 ||
		cp.HeaderContractID != in.HeaderContract.ID || cp.HeaderContractSHA256 != in.HeaderContract.CanonicalSHA256 {
		fail(v, &v.CheckpointCompat, "checkpoint_compat", CodeCheckpointReleaseMismatch, "checkpoint wire/contract/Sentinel/transport/header content binding differs from recovery binding")
	}
}

func checkMaxActive(v *Vector, in Input) {
	m := in.MaxActive
	values := [...]struct {
		name  string
		value int
	}{
		{"authoritative", m.Authoritative},
		{"admission", m.Admission},
		{"worker", m.Worker},
		{"tasks_service", m.TasksService},
		{"config", m.Config},
	}
	for _, source := range values {
		if source.value <= 0 {
			fail(v, &v.MaxActiveAlignment, "max_active_alignment", CodeMaxActiveMissing, source.name+" max-active value is missing")
		}
	}
	if m.Authoritative <= 0 {
		return
	}
	for _, source := range values[1:] {
		if source.value > 0 && source.value != m.Authoritative {
			fail(v, &v.MaxActiveAlignment, "max_active_alignment", CodeMaxActiveMismatch,
				fmt.Sprintf("%s max-active %d differs from authoritative %d", source.name, source.value, m.Authoritative))
		}
	}
}

func checkEffectiveTransport(v *Vector, in Input, browser string, major int) {
	requiresExact := in.Purpose == PurposeLiveAdmission || in.Purpose == PurposeWireCanary
	if in.EffectiveTransport == nil {
		if requiresExact {
			fail(v, &v.TransportProfile, "transport_profile", CodeTransportProfileMismatch, "live admission and wire canary require factory-resolved effective profile evidence")
		}
		return
	}
	expected, err := transport.ResolveEffectiveTLSProfile(browser, major)
	if err != nil {
		fail(v, &v.TransportProfile, "transport_profile", CodeTransportProfileMismatch, "effective TLS profile resolver rejected declared browser/major")
		return
	}
	if *in.EffectiveTransport != expected {
		fail(v, &v.TransportProfile, "transport_profile", CodeTransportProfileMismatch, "supplied effective TLS profile evidence differs from factory resolver outcome")
		return
	}
	if requiresExact && (expected.Fallback || expected.EffectiveBrowser != browser || expected.EffectiveMajor != major || strings.TrimSpace(expected.ProfileName) == "") {
		fail(v, &v.TransportProfile, "transport_profile", CodeTransportProfileMismatch,
			fmt.Sprintf("factory resolved %s/%d (%s) for declared %s/%d", expected.EffectiveBrowser, expected.EffectiveMajor, expected.ProfileName, browser, major))
	}
}

func incompleteTransportFields(p transport.Profile) []string {
	missing := make([]string, 0, 12)
	require := func(name, value string) {
		if invalidFixtureReference(value) {
			missing = append(missing, name)
		}
	}

	require("id", p.ID)
	require("baseline_capture_id", p.BaselineCaptureID)
	require("browser", p.Browser)
	if p.BrowserMajor <= 0 {
		missing = append(missing, "browser_major")
	}
	require("browser_version", p.BrowserVersion)
	require("os", p.OS)
	require("go_version", p.GoVersion)
	require("module_graph_fixture", p.ModuleGraphFixture)
	require("tls_client_hello_fixture", p.TLSClientHelloFixture)
	require("tls_extension_order_fixture", p.TLSExtensionOrderFixture)
	if !sameStrings(p.ALPN, []string{"h2", "http/1.1"}) {
		missing = append(missing, "alpn")
	}
	require("http2_settings_fixture", p.HTTP2SettingsFixture)
	require("http2_connection_flow_fixture", p.HTTP2ConnectionFlowFixture)
	if !sameStrings(p.HTTP2PseudoHeaderOrder, []string{":method", ":authority", ":scheme", ":path"}) {
		missing = append(missing, "http2_pseudo_header_order")
	}
	for _, preset := range []string{"document_navigation", "same_origin_fetch", "cross_origin_oauth", "otp_sparse", "sentinel_req", "callback_navigation"} {
		if invalidFixtureReference(p.HeaderPresets[preset]) {
			missing = append(missing, "header_presets."+preset)
		}
	}
	require("response_content_encoding_fixture", p.ResponseContentEncodingFix)
	if p.RedirectMaxHops <= 0 {
		missing = append(missing, "redirect_max_hops")
	}
	if !p.CertificateValidation {
		missing = append(missing, "certificate_validation")
	}
	return missing
}

func invalidFixtureReference(value string) bool {
	value = strings.TrimSpace(value)
	return value == "" || strings.EqualFold(value, "capture_required")
}

func sameStrings(got, want []string) bool {
	if len(got) != len(want) {
		return false
	}
	for index := range want {
		if got[index] != want[index] || invalidFixtureReference(got[index]) {
			return false
		}
	}
	return true
}

func effectiveUA(ua string) (string, int, error) {
	ua = strings.TrimSpace(ua)
	if fingerprint.IsFirefoxUA(ua) {
		firefoxVersion, ok := versionAfterMarker(ua, "Firefox/")
		if !ok {
			return "", 0, fmt.Errorf("effective Firefox user agent has no version token")
		}
		firefoxMajor, _ := browserVersionMajor(firefoxVersion)
		rvVersion, hasRV := versionAfterMarker(ua, "rv:")
		if !hasRV || rvVersion != firefoxVersion {
			return "", 0, fmt.Errorf("effective Firefox user agent rv version %q differs from Firefox version %q", rvVersion, firefoxVersion)
		}
		return fingerprint.BrowserFirefox, firefoxMajor, nil
	}
	if strings.Contains(ua, "Edg/") {
		edgeMajor, ok := majorAfterMarker(ua, "Edg/")
		if !ok {
			return "", 0, fmt.Errorf("effective Edge user agent has no version token")
		}
		if chromeMajor, hasChrome := majorAfterMarker(ua, "Chrome/"); hasChrome && chromeMajor != edgeMajor {
			return "", 0, fmt.Errorf("effective Edge user agent Chrome major %d differs from Edge major %d", chromeMajor, edgeMajor)
		}
		return fingerprint.BrowserEdge, edgeMajor, nil
	}
	if strings.Contains(ua, "Chrome/") {
		chromeMajor, ok := majorAfterMarker(ua, "Chrome/")
		if !ok {
			return "", 0, fmt.Errorf("effective Chrome user agent has no version token")
		}
		return fingerprint.BrowserChrome, chromeMajor, nil
	}
	return "", 0, fmt.Errorf("effective user agent browser is unsupported")
}

func majorAfterMarker(value, marker string) (int, bool) {
	start := strings.Index(value, marker)
	if start < 0 {
		return 0, false
	}
	start += len(marker)
	end := start
	for end < len(value) && value[end] >= '0' && value[end] <= '9' {
		end++
	}
	if end == start {
		return 0, false
	}
	major, err := strconv.Atoi(value[start:end])
	return major, err == nil && major > 0
}

func versionAfterMarker(value, marker string) (string, bool) {
	start := strings.Index(value, marker)
	if start < 0 {
		return "", false
	}
	start += len(marker)
	end := start
	for end < len(value) && ((value[end] >= '0' && value[end] <= '9') || value[end] == '.') {
		end++
	}
	if end < len(value) && !strings.ContainsRune(" \t);", rune(value[end])) {
		return "", false
	}
	version := value[start:end]
	if _, ok := browserVersionMajor(version); !ok {
		return "", false
	}
	return version, true
}

func browserVersionFromUA(ua, browser string) (string, bool) {
	marker := ""
	switch browser {
	case fingerprint.BrowserFirefox:
		marker = "Firefox/"
	case fingerprint.BrowserEdge:
		marker = "Edg/"
	case fingerprint.BrowserChrome:
		marker = "Chrome/"
	default:
		return "", false
	}
	return versionAfterMarker(ua, marker)
}

func normalizedUAOS(ua string) (string, bool) {
	switch {
	case strings.Contains(ua, "Windows NT"):
		return "windows", true
	case strings.Contains(ua, "Android"):
		return "android", true
	case strings.Contains(ua, "Linux"):
		return "linux", true
	default:
		return "", false
	}
}

func normalizeOS(value string) (string, bool) {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "windows", "win", "win32":
		return "windows", true
	case "android":
		return "android", true
	case "linux":
		return "linux", true
	default:
		return "", false
	}
}

func profileIDOS(id string) (string, bool) {
	parts := strings.Split(strings.ToLower(strings.TrimSpace(id)), "-")
	if len(parts) < 3 {
		return "", false
	}
	return normalizeOS(parts[2])
}

func browserVersionMajor(version string) (int, bool) {
	parts := strings.Split(strings.TrimSpace(version), ".")
	if len(parts) == 0 {
		return 0, false
	}
	major := 0
	for i, part := range parts {
		if part == "" {
			return 0, false
		}
		for _, digit := range part {
			if digit < '0' || digit > '9' {
				return 0, false
			}
		}
		value, err := strconv.Atoi(part)
		if err != nil {
			return 0, false
		}
		if i == 0 {
			major = value
		}
	}
	return major, major > 0
}

func normalizeSHA256(value string) string {
	return strings.TrimPrefix(strings.ToLower(strings.TrimSpace(value)), "sha256:")
}

func validSHA256(value string) bool {
	value = normalizeSHA256(value)
	if len(value) != 64 {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

func fail(v *Vector, state *GateState, component, code, message string) {
	*state = GateFail
	v.Failures = append(v.Failures, Failure{Component: component, Code: code, Message: message})
}
