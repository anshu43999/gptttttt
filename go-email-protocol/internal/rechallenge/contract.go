package rechallenge

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"io"
	"sort"
	"strings"

	"github.com/gpt-register/go-email-protocol/internal/protocol"
)

const CurrentSchemaVersion = 1

type CaptureRole string

const (
	RoleRegistration CaptureRole = "registration"
	RoleCheckout     CaptureRole = "checkout"
	RoleUnknown      CaptureRole = "unknown"
)

type CaptureManifest struct {
	SchemaVersion     int            `json:"schema_version"`
	CaptureID         string         `json:"capture_id"`
	Role              CaptureRole    `json:"role"`
	RoleEvidence      []RoleEvidence `json:"role_evidence,omitempty"`
	CapturedAt        string         `json:"captured_at"`
	SourceSHA256      string         `json:"source_sha256"`
	Browser           string         `json:"browser"`
	UAMajor           int            `json:"ua_major"`
	Locale            string         `json:"locale,omitempty"`
	Timezone          string         `json:"timezone,omitempty"`
	SourceKind        string         `json:"source_kind"`
	RedactionPolicyID string         `json:"redaction_policy_id"`
}

type RoleEvidence struct {
	Kind string `json:"kind"`
	Host string `json:"host,omitempty"`
	Path string `json:"path,omitempty"`
	Flow string `json:"flow,omitempty"`
}

type BrowserIdentity struct {
	Browser string `json:"browser"`
	UAMajor int    `json:"ua_major"`
	Locale  string `json:"locale,omitempty"`
}

type RegistrationContract struct {
	SchemaVersion     int                     `json:"schema_version"`
	ContractID        string                  `json:"contract_id"`
	ParentContractID  string                  `json:"parent_contract_id,omitempty"`
	Captures          []CaptureManifest       `json:"captures"`
	Flow              string                  `json:"flow"`
	BrowserIdentity   BrowserIdentity         `json:"browser_identity"`
	Exchanges         []StateExchangeContract `json:"exchanges"`
	SentinelReleaseID string                  `json:"sentinel_release_id"`
	TransportProfileID string                 `json:"transport_profile_id"`
	PolicyID          string                  `json:"policy_id"`
	CanonicalSHA256   string                  `json:"canonical_sha256"`
}

type ExchangeProvenance struct {
	CaptureID string `json:"capture_id"`
	SourceKind string `json:"source_kind"`
	HARIndex int `json:"har_index"`
	StartedAt string `json:"started_at,omitempty"`
	Observed bool `json:"observed"`
}

type StateExchangeContract struct {
	State                   protocol.StateID `json:"state"`
	FSMAssociation          protocol.StateID `json:"fsm_association,omitempty"`
	ExchangeIndex           int              `json:"exchange_index"`
	CaptureSequence         int              `json:"capture_sequence"`
	SentinelOccurrence      *int             `json:"sentinel_occurrence,omitempty"`
	FlowName                string           `json:"flow_name,omitempty"`
	RequirementsFingerprint string           `json:"requirements_fingerprint,omitempty"`
	ObservedScriptSource    string           `json:"observed_script_source,omitempty"`
	Provenance              ExchangeProvenance `json:"provenance"`
	Request                 RequestContract  `json:"request"`
	Response                ResponseContract `json:"response"`
	Redirect                *RedirectRule    `json:"redirect,omitempty"`
	CookieEvents            []CookieEvent    `json:"cookie_events,omitempty"`
}

type RequestContract struct {
	Kind        string       `json:"kind,omitempty"`
	Method      string       `json:"method"`
	Host        string       `json:"host"`
	Path        string       `json:"path"`
	Query       []FieldRule  `json:"query,omitempty"`
	ContentType string       `json:"content_type,omitempty"`
	Body        BodyRule     `json:"body"`
	Headers     []HeaderRule `json:"headers"`
}

type FieldRule struct {
	Name        string   `json:"name"`
	Presence    string   `json:"presence"`
	ValuePolicy string   `json:"value_policy"`
	ValueType   string   `json:"value_type,omitempty"`
	ObjectKeys  []string `json:"object_keys,omitempty"`
	Order       int      `json:"order,omitempty"`
	Provenance  []string `json:"provenance"`
}

type BodyRule struct {
	Kind   string      `json:"kind"`
	Fields []FieldRule `json:"fields,omitempty"`
}

type HeaderRule struct {
	Name          string   `json:"name"`
	Source        string   `json:"source"`
	Presence      string   `json:"presence"`
	ValuePolicy   string   `json:"value_policy"`
	Expected      string   `json:"expected,omitempty"`
	OrderPolicy   string   `json:"order_policy"`
	Order         int      `json:"order,omitempty"`
	Multiplicity  string   `json:"multiplicity"`
	Provenance    []string `json:"provenance"`
}

type ResponseContract struct {
	AllowedStatus  []int          `json:"allowed_status"`
	ObservedStatus int            `json:"observed_status"`
	ContentType    string         `json:"content_type,omitempty"`
	RequiredFields []string       `json:"required_fields,omitempty"`
	Discriminators []string       `json:"discriminators,omitempty"`
	Outcome        string         `json:"outcome"`
	BodyTemplate   *BodyTemplate  `json:"body_template,omitempty"`
}

type BodyTemplate struct {
	Kind   string                   `json:"kind"`
	Fields map[string]TemplateValue `json:"fields,omitempty"`
}

type TemplateValue struct {
	Kind    string                   `json:"kind"`
	Slot    string                   `json:"slot,omitempty"`
	Literal any                      `json:"literal,omitempty"`
	Fields  map[string]TemplateValue `json:"fields,omitempty"`
	Items   []TemplateValue          `json:"items,omitempty"`
}

type RedirectRule struct {
	Status         int    `json:"status"`
	LocationPolicy string `json:"location_policy"`
	LocationPath   string `json:"location_path,omitempty"`
	MaxHops        int    `json:"max_hops"`
	FinalHost      string `json:"final_host,omitempty"`
}

type CookieEvent struct {
	Direction string `json:"direction"`
	Hop       int    `json:"hop"`
	Name      string `json:"name"`
	Domain    string `json:"domain,omitempty"`
	Path      string `json:"path,omitempty"`
	HTTPOnly  bool   `json:"http_only"`
	Secure    bool   `json:"secure"`
	SameSite  string `json:"same_site,omitempty"`
	ValueSlot string `json:"value_slot"`
	Required  bool   `json:"required"`
	Provenance []string `json:"provenance"`
}

type NormalizedCapture struct {
	Manifest *CaptureManifest      `json:"manifest"`
	Contract *RegistrationContract `json:"contract"`
}

func CanonicalJSON(c *RegistrationContract) ([]byte, error) {
	if c == nil {
		return nil, errors.New("rechallenge: nil contract")
	}
	clone := *c
	clone.ContractID = ""
	clone.CanonicalSHA256 = ""
	normalizeContract(&clone)
	return json.Marshal(&clone)
}

func FinalizeContract(c *RegistrationContract) error {
	if c == nil {
		return errors.New("rechallenge: nil contract")
	}
	normalizeContract(c)
	raw, err := CanonicalJSON(c)
	if err != nil {
		return err
	}
	sum := sha256.Sum256(raw)
	digest := hex.EncodeToString(sum[:])
	c.CanonicalSHA256 = "sha256:" + digest
	c.ContractID = "rc_" + digest[:16]
	return ValidateContract(c)
}

func ValidateContract(c *RegistrationContract) error {
	if c == nil {
		return errors.New("rechallenge: nil contract")
	}
	if c.SchemaVersion != CurrentSchemaVersion {
		return fmt.Errorf("rechallenge: schema_version=%d want %d", c.SchemaVersion, CurrentSchemaVersion)
	}
	if len(c.Captures) != 1 || len(c.Exchanges) != 12 {
		return errors.New("rechallenge: v1 registration contract requires one capture and 12 observed exchanges")
	}
	for _, capture := range c.Captures {
		if err := ValidateCaptureManifest(&capture); err != nil {
			return err
		}
		if capture.Role != RoleRegistration {
			return contractError(CodeCaptureRoleMismatch, "validate", "capture", "captures.role", capture.CaptureID, nil)
		}
	}
	if c.Flow != "oauth_create_account" || strings.TrimSpace(c.SentinelReleaseID) == "" || strings.TrimSpace(c.TransportProfileID) == "" || strings.TrimSpace(c.PolicyID) == "" {
		return errors.New("rechallenge: protected flow/release/profile/policy identity is invalid")
	}
	type expectedExchange struct { state protocol.StateID; kind, method, host, path string; occurrence int }
	expected := []expectedExchange{
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
	captureID := c.Captures[0].CaptureID
	for index := range c.Exchanges {
		exchange := &c.Exchanges[index]
		want := expected[index]
		if exchange.CaptureSequence != index || exchange.State != want.state || exchange.ExchangeIndex < 0 || exchange.Request.Kind != want.kind || strings.ToUpper(exchange.Request.Method) != want.method || strings.ToLower(exchange.Request.Host) != want.host || exchange.Request.Path != want.path {
			return contractError(CodeWireContractDrift, "validate", "sequence", fmt.Sprintf("exchanges[%d]", index), captureID, errors.New("observed endpoint sequence mismatch"))
		}
		if !exchange.Provenance.Observed || exchange.Provenance.SourceKind != "har" || exchange.Provenance.CaptureID != captureID {
			return contractError(CodeWireContractDrift, "validate", "provenance", fmt.Sprintf("exchanges[%d].provenance", index), captureID, errors.New("exchange is not observed HAR evidence"))
		}
		if want.occurrence >= 0 {
			if exchange.SentinelOccurrence == nil || *exchange.SentinelOccurrence != want.occurrence || exchange.FlowName != c.Flow || exchange.FSMAssociation != "" {
				return contractError(CodeWireContractDrift, "validate", "sentinel", fmt.Sprintf("exchanges[%d].sentinel_occurrence", index), captureID, errors.New("Sentinel occurrence or flow mismatch"))
			}
			if exchange.ObservedScriptSource != "https://sentinel.openai.com/backend-api/sentinel/sdk.js" && exchange.ObservedScriptSource != "https://sentinel.openai.com/sentinel/20260219f9f6/sdk.js" {
				return contractError(CodeWireContractDrift, "validate", "sentinel", fmt.Sprintf("exchanges[%d].observed_script_source", index), captureID, errors.New("untrusted Sentinel script source"))
			}
			if !strings.HasPrefix(exchange.RequirementsFingerprint, "sha256:") || len(exchange.RequirementsFingerprint) != 71 {
				return contractError(CodeWireContractDrift, "validate", "sentinel", fmt.Sprintf("exchanges[%d].requirements_fingerprint", index), captureID, errors.New("invalid requirements fingerprint"))
			}
		} else if exchange.SentinelOccurrence != nil {
			return contractError(CodeWireContractDrift, "validate", "sentinel", fmt.Sprintf("exchanges[%d].sentinel_occurrence", index), captureID, errors.New("unexpected Sentinel occurrence"))
		}
	}
	if c.CanonicalSHA256 != "" {
		raw, err := CanonicalJSON(c)
		if err != nil {
			return err
		}
		sum := sha256.Sum256(raw)
		want := "sha256:" + hex.EncodeToString(sum[:])
		if c.CanonicalSHA256 != want {
			return contractError(CodeWireContractDrift, "validate", "identity", "canonical_sha256", c.ContractID, fmt.Errorf("got %s want %s", c.CanonicalSHA256, want))
		}
		if c.ContractID != "rc_"+hex.EncodeToString(sum[:])[:16] {
			return contractError(CodeWireContractDrift, "validate", "identity", "contract_id", c.ContractID, errors.New("contract id does not match canonical hash"))
		}
	}
	raw, err := json.Marshal(c)
	if err != nil {
		return err
	}
	return ValidateRedactedJSON("contract", raw)
}

func SaveContract(path string, c *RegistrationContract) error {
	if err := FinalizeContract(c); err != nil {
		return err
	}
	raw, err := json.MarshalIndent(c, "", "  ")
	if err != nil {
		return err
	}
	if err := ValidateRedactedJSON(path, raw); err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	return os.WriteFile(path, append(raw, '\n'), 0o600)
}

func LoadContract(path string) (*RegistrationContract, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	if err := ValidateRedactedJSON(path, raw); err != nil {
		return nil, err
	}
	var c RegistrationContract
	if err := decodeSingleJSON(raw, &c); err != nil {
		return nil, fmt.Errorf("rechallenge: decode contract %s: %w", path, err)
	}
	if err := ValidateContract(&c); err != nil {
		return nil, err
	}
	return &c, nil
}

func SaveCaptureManifest(path string, manifest *CaptureManifest) error {
	if err := ValidateCaptureManifest(manifest); err != nil {
		return err
	}
	raw, err := json.MarshalIndent(manifest, "", "  ")
	if err != nil {
		return err
	}
	if err := ValidateRedactedJSON(path, raw); err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	return os.WriteFile(path, append(raw, '\n'), 0o600)
}

func LoadCaptureManifest(path string) (*CaptureManifest, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	if err := ValidateRedactedJSON(path, raw); err != nil {
		return nil, err
	}
	var manifest CaptureManifest
	if err := decodeSingleJSON(raw, &manifest); err != nil {
		return nil, fmt.Errorf("rechallenge: decode manifest %s: %w", path, err)
	}
	if err := ValidateCaptureManifest(&manifest); err != nil {
		return nil, err
	}
	return &manifest, nil
}
func decodeSingleJSON(raw []byte, destination any) error {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(destination); err != nil {
		return err
	}
	var trailing any
	if err := decoder.Decode(&trailing); err != io.EOF {
		if err == nil {
			return errors.New("trailing JSON value")
		}
		return fmt.Errorf("trailing content: %w", err)
	}
	return nil
}

func ValidateCaptureManifest(manifest *CaptureManifest) error {
	if manifest == nil {
		return errors.New("rechallenge: nil capture manifest")
	}
	if manifest.SchemaVersion != CurrentSchemaVersion || manifest.CaptureID == "" || manifest.SourceKind != "har" || !strings.HasPrefix(manifest.SourceSHA256, "sha256:") {
		return errors.New("rechallenge: invalid capture manifest identity")
	}
	if manifest.Role != RoleRegistration && manifest.Role != RoleCheckout && manifest.Role != RoleUnknown {
		return fmt.Errorf("rechallenge: invalid capture role %q", manifest.Role)
	}
	raw, err := json.Marshal(manifest)
	if err != nil {
		return err
	}
	return ValidateRedactedJSON("capture_manifest", raw)
}

func normalizeContract(c *RegistrationContract) {
	sort.SliceStable(c.Captures, func(i, j int) bool { return c.Captures[i].CaptureID < c.Captures[j].CaptureID })
	for i := range c.Captures {
		sort.SliceStable(c.Captures[i].RoleEvidence, func(a, b int) bool {
			x, y := c.Captures[i].RoleEvidence[a], c.Captures[i].RoleEvidence[b]
			return x.Kind+x.Host+x.Path+x.Flow < y.Kind+y.Host+y.Path+y.Flow
		})
	}
	sort.SliceStable(c.Exchanges, func(i, j int) bool {
		if c.Exchanges[i].Provenance.CaptureID != c.Exchanges[j].Provenance.CaptureID {
			return c.Exchanges[i].Provenance.CaptureID < c.Exchanges[j].Provenance.CaptureID
		}
		return c.Exchanges[i].CaptureSequence < c.Exchanges[j].CaptureSequence
	})
	for i := range c.Exchanges {
		e := &c.Exchanges[i]
		sort.SliceStable(e.Request.Body.Fields, func(a, b int) bool { return e.Request.Body.Fields[a].Name < e.Request.Body.Fields[b].Name })
		for j := range e.Request.Body.Fields { sort.Strings(e.Request.Body.Fields[j].ObjectKeys); sort.Strings(e.Request.Body.Fields[j].Provenance) }
		for j := range e.Request.Query { sort.Strings(e.Request.Query[j].ObjectKeys); sort.Strings(e.Request.Query[j].Provenance) }
		for j := range e.Request.Headers { sort.Strings(e.Request.Headers[j].Provenance) }
		sort.Ints(e.Response.AllowedStatus)
		sort.Strings(e.Response.RequiredFields)
		sort.Strings(e.Response.Discriminators)
		sort.SliceStable(e.CookieEvents, func(a, b int) bool {
			x, y := e.CookieEvents[a], e.CookieEvents[b]
			return x.Direction+x.Name+x.Domain+x.Path < y.Direction+y.Name+y.Domain+y.Path
		})
		for j := range e.CookieEvents { sort.Strings(e.CookieEvents[j].Provenance) }
	}
}
