package protocol

import "github.com/gpt-register/go-email-protocol/internal/headerpreset"

// MainStepSpec is a table-driven main-path step (fixture-first).
type MainStepSpec struct {
	State        StateID
	Preset       headerpreset.Name
	Request      RequestShape
	ResponseUsed ResponseUsed
	Notes        string
}

// S0S3Specs documents the first navigation/auth steps (EMAIL_PROTOCOL_GO_PLAN §6).
// Values are shapes only — no live secrets.
func S0S3Specs() []MainStepSpec {
	return []MainStepSpec{
		{
			State:  S0,
			Preset: "",
			Request: RequestShape{
				Method:   "",
				BodyKind: "none",
			},
			Notes: "local: admit job, lock Bundle+Transport+Bridge grant",
		},
		{
			State:  S1,
			Preset: headerpreset.DocumentNavigation,
			Request: RequestShape{
				Method:       "GET",
				URLTemplate:  "https://chatgpt.com/",
				BodyKind:     "none",
				HeaderPreset: string(headerpreset.DocumentNavigation),
			},
			ResponseUsed: ResponseUsed{
				Fields: []string{"set-cookie:oai-did", "status"},
				Notes:  "deviceID only from wire Set-Cookie oai-did",
			},
		},
		{
			State:  S2,
			Preset: headerpreset.SameOriginFetch,
			Request: RequestShape{
				Method:       "GET",
				URLTemplate:  "https://chatgpt.com/api/auth/csrf",
				BodyKind:     "none",
				HeaderPreset: string(headerpreset.SameOriginFetch),
			},
			ResponseUsed: ResponseUsed{
				Fields: []string{"csrfToken", "set-cookie:__Host-next-auth.csrf-token"},
			},
		},
		{
			State:  S3,
			Preset: headerpreset.SameOriginFetch,
			Request: RequestShape{
				Method:       "POST",
				URLTemplate:  "https://chatgpt.com/api/auth/signin/openai",
				BodyKind:     "form",
				HeaderPreset: string(headerpreset.SameOriginFetch),
				BodyFields: []FieldSpec{
					{Name: "callbackUrl", Type: BodyTypeString, Required: true},
					{Name: "csrfToken", Type: BodyTypeString, Required: true},
					{Name: "json", Type: BodyTypeBoolean, Required: false},
				},
			},
			ResponseUsed: ResponseUsed{
				Fields:         []string{"url", "status"},
				Discriminators: []string{"url"},
			},
		},
	}
}

// SpecFor returns the first matching MainStepSpec or nil.
func SpecFor(id StateID) *MainStepSpec {
	for _, s := range append(S0S3Specs(), S4S14Specs()...) {
		if s.State == id {
			cp := s
			return &cp
		}
	}
	return nil
}

// S4S14Specs is a thin placeholder table (full wire fields land with capture fixtures).
func S4S14Specs() []MainStepSpec {
	return []MainStepSpec{
		{State: S4, Preset: headerpreset.DocumentNavigation, Request: RequestShape{Method: "GET", URLTemplate: "{authorize_url}", BodyKind: "none", HeaderPreset: string(headerpreset.DocumentNavigation)}},
		{State: S5, Preset: headerpreset.SentinelReq, Request: RequestShape{Method: "POST", URLTemplate: "https://sentinel.openai.com/backend-api/sentinel/req", BodyKind: "json", HeaderPreset: string(headerpreset.SentinelReq)}},
		{State: S6, Preset: headerpreset.SameOriginFetch, Request: RequestShape{Method: "POST", URLTemplate: "{auth}/api/accounts/authorize/continue", BodyKind: "json", HeaderPreset: string(headerpreset.SameOriginFetch)}},
		{State: S7, Preset: headerpreset.SameOriginFetch, Request: RequestShape{Method: "POST", URLTemplate: "{auth}/api/accounts/user/register", BodyKind: "json", HeaderPreset: string(headerpreset.SameOriginFetch)}, Notes: "ambiguous_after_send if response lost"},
		{State: S8, Preset: headerpreset.OTPSparse, Request: RequestShape{Method: "GET", URLTemplate: "{auth}/api/accounts/email-otp/send", BodyKind: "none", HeaderPreset: string(headerpreset.OTPSparse)}},
		{State: S9, Preset: "", Request: RequestShape{BodyKind: "none"}, Notes: "durable waiting_for_otp"},
		{State: S10, Preset: headerpreset.OTPSparse, Request: RequestShape{Method: "POST", URLTemplate: "{auth}/api/accounts/email-otp/validate", BodyKind: "json", HeaderPreset: string(headerpreset.OTPSparse)}},
		{State: S11, Preset: headerpreset.SameOriginFetch, Request: RequestShape{Method: "POST", URLTemplate: "{auth}/api/accounts/create_account", BodyKind: "json", HeaderPreset: string(headerpreset.SameOriginFetch)}},
		{State: S12, Preset: headerpreset.CallbackNavigation, Request: RequestShape{Method: "GET", URLTemplate: "{callback_url}", BodyKind: "none", HeaderPreset: string(headerpreset.CallbackNavigation)}},
		{State: S13, Preset: headerpreset.SameOriginFetch, Request: RequestShape{Method: "GET", URLTemplate: "https://chatgpt.com/api/auth/session", BodyKind: "none", HeaderPreset: string(headerpreset.SameOriginFetch)}},
		{State: S14, Preset: "", Request: RequestShape{BodyKind: "none"}, Notes: "session document handoff"},
	}
}
