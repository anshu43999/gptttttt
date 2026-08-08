// Package protocol defines typed protocol state IDs and request shape types
// used by fixtures. No live HTTP is performed in G0.
package protocol

// Kind classifies a protocol state for catalogue grouping.
type Kind string

const (
	KindMain         Kind = "main"         // S0–S15 primary email registration FSM
	KindSentinel     Kind = "sentinel"     // T1–T3 Sentinel / Turnstile
	KindContinuation Kind = "continuation" // C0–C6 continuation dispatcher branches
	KindLocal        Kind = "local"        // L1–L3 local/durable non-HTTP states
)

// StateID is a stable identifier for a protocol / continuation / local stage.
type StateID string

// Main FSM (plan §6.2 / §6.4).
const (
	S0  StateID = "S0"  // establish attempt context
	S1  StateID = "S1"  // GET chatgpt.com homepage, capture oai-did
	S2  StateID = "S2"  // CSRF cookie or GET /api/auth/csrf
	S3  StateID = "S3"  // POST /api/auth/signin/openai
	S4  StateID = "S4"  // GET authorize page from S3.url
	S5  StateID = "S5"  // Sentinel authorize_continue (see T*)
	S6  StateID = "S6"  // POST authorize/continue
	S7  StateID = "S7"  // POST user/register
	S8  StateID = "S8"  // GET email-otp/send
	S9  StateID = "S9"  // waiting_for_otp (durable)
	S10 StateID = "S10" // POST email-otp/validate
	S11 StateID = "S11" // POST create_account
	S12 StateID = "S12" // GET callback URL
	S13 StateID = "S13" // GET /api/auth/session
	S14 StateID = "S14" // durable result / handoff
	S15 StateID = "S15" // session-failure Codex OAuth reauth
)

// Sentinel stages (plan §9).
const (
	T1 StateID = "T1" // POST sentinel requirements
	T2 StateID = "T2" // assemble openai-sentinel-token enforcement header
	T3 StateID = "T3" // Turnstile/SDK/native dx path when turnstile.dx present
)

// Continuation dispatcher branches (plan §6.3).
// Mapped from documented continuation paths; not all have full wire capture yet.
const (
	C0 StateID = "C0" // /create-account/password → S7
	C1 StateID = "C1" // email-otp/send URL → S8
	C2 StateID = "C2" // /email-verification → S9/S10
	C3 StateID = "C3" // /about-you → S11
	C4 StateID = "C4" // chatgpt.com callback/openai → S12
	C5 StateID = "C5" // /add-phone → phone_verification_required
	C6 StateID = "C6" // /add-email | /mfa-challenge | /workspace | consent | unknown
)

// Local / durable non-HTTP states (plan §5 / §6 / §11).
const (
	L1 StateID = "L1" // durable waiting_for_otp challenge (mailbox handoff)
	L2 StateID = "L2" // durable succeeded session document / handoff
	L3 StateID = "L3" // reconcile_required / ambiguous_after_send checkpoint
)

// RequiredStateIDs is the complete G0 catalogue set.
func RequiredStateIDs() []StateID {
	return []StateID{
		S0, S1, S2, S3, S4, S5, S6, S7, S8, S9, S10, S11, S12, S13, S14, S15,
		T1, T2, T3,
		C0, C1, C2, C3, C4, C5, C6,
		L1, L2, L3,
	}
}

// KindOf returns the catalogue group for a state ID.
func KindOf(id StateID) Kind {
	switch id {
	case S0, S1, S2, S3, S4, S5, S6, S7, S8, S9, S10, S11, S12, S13, S14, S15:
		return KindMain
	case T1, T2, T3:
		return KindSentinel
	case C0, C1, C2, C3, C4, C5, C6:
		return KindContinuation
	case L1, L2, L3:
		return KindLocal
	default:
		return ""
	}
}

// IsKnown reports whether id is in the G0 required set.
func IsKnown(id StateID) bool {
	return KindOf(id) != ""
}
