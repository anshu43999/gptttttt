package protocol

// BodyFieldType describes a non-secret body field type for fixtures.
type BodyFieldType string

const (
	BodyTypeString   BodyFieldType = "string"
	BodyTypeNumber   BodyFieldType = "number"
	BodyTypeBoolean  BodyFieldType = "boolean"
	BodyTypeObject   BodyFieldType = "object"
	BodyTypeArray    BodyFieldType = "array"
	BodyTypeNull     BodyFieldType = "null"
	BodyTypeSecret   BodyFieldType = "secret" // value never stored; key may be listed
	BodyTypeRedacted BodyFieldType = "redacted"
	BodyTypeTemplate BodyFieldType = "template" // e.g. email placeholder
)

// FieldSpec describes a query/body field without secret values.
type FieldSpec struct {
	Name     string        `json:"name"`
	Type     BodyFieldType `json:"type"`
	Required bool          `json:"required"`
	// Nested object keys when Type is object (e.g. username.kind / username.value).
	ObjectKeys []string `json:"object_keys,omitempty"`
	// Notes for implementers; never holds secret material.
	Notes string `json:"notes,omitempty"`
}

// RequestShape is the redacted wire shape for an HTTP-bearing state.
type RequestShape struct {
	Method      string      `json:"method,omitempty"`
	URLTemplate string      `json:"url_template,omitempty"`
	QueryKeys   []string    `json:"query_keys,omitempty"` // ordered
	BodyKind    string      `json:"body_kind,omitempty"`  // none|json|form|empty
	BodyFields  []FieldSpec `json:"body_fields,omitempty"`
	// HeaderPreset names a documented preset (B, J, or state-specific).
	HeaderPreset string   `json:"header_preset,omitempty"`
	HeaderKeys   []string `json:"header_keys,omitempty"` // ordered keys only
}

// CookieMeta is cookie metadata without values.
type CookieMeta struct {
	Name     string `json:"name"`
	Domain   string `json:"domain,omitempty"`
	Path     string `json:"path,omitempty"`
	HTTPOnly bool   `json:"http_only"`
	Secure   bool   `json:"secure"`
	SameSite string `json:"same_site,omitempty"`
	// ValuePolicy: omit | hash | redacted_marker
	ValuePolicy string `json:"value_policy"`
}

// ResponseUsed documents response fields consumed by the FSM (names only).
type ResponseUsed struct {
	Fields         []string `json:"fields,omitempty"`
	Discriminators []string `json:"discriminators,omitempty"`
	Notes          string   `json:"notes,omitempty"`
}

// SentinelObservation records Sentinel flow/input field names for a state.
type SentinelObservation struct {
	FlowName   string   `json:"flow_name,omitempty"`
	InputKeys  []string `json:"input_keys,omitempty"`
	HeaderName string   `json:"header_name,omitempty"` // openai-sentinel-token
	Notes      string   `json:"notes,omitempty"`
}

// TransportObservation slots for profile/ALPN/bridge (no secrets).
type TransportObservation struct {
	ProfileID      string   `json:"profile_id,omitempty"`
	ALPN           []string `json:"alpn,omitempty"`
	BridgeRequired bool     `json:"bridge_required"`
	Notes          string   `json:"notes,omitempty"`
}
