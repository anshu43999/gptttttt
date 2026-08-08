package rechallenge

import (
	"encoding/json"
	"fmt"
	"reflect"
	"sort"
	"strconv"
	"strings"
)

type DiffSeverity string

const (
	DiffBlocking      DiffSeverity = "blocking"
	DiffKnownVariant  DiffSeverity = "known_variant"
	DiffInformational DiffSeverity = "informational"
)

type FieldDiff struct {
	Severity  DiffSeverity `json:"severity"`
	Path      string       `json:"path"`
	Kind      string       `json:"kind"`
	Approved  any          `json:"approved,omitempty"`
	Candidate any          `json:"candidate,omitempty"`
	Reason    string       `json:"reason"`
}

type DiffReport struct {
	SchemaVersion int         `json:"schema_version"`
	ApprovedID    string      `json:"approved_contract_id"`
	CandidateID   string      `json:"candidate_contract_id"`
	PolicyID      string      `json:"policy_id"`
	Blocking      []FieldDiff `json:"blocking"`
	KnownVariants []FieldDiff `json:"known_variants"`
	Informational []FieldDiff `json:"informational"`
	Status        string      `json:"status"`
}

func (report DiffReport) HasBlocking() bool { return len(report.Blocking) != 0 }

func DiffContracts(approved, candidate *RegistrationContract) (DiffReport, error) {
	if approved == nil || candidate == nil {
		return DiffReport{}, fmt.Errorf("rechallenge: diff requires approved and candidate contracts")
	}
	approvedRaw, err := json.Marshal(approved)
	if err != nil { return DiffReport{}, err }
	candidateRaw, err := json.Marshal(candidate)
	if err != nil { return DiffReport{}, err }
	if err := ValidateRedactedJSON("approved_contract", approvedRaw); err != nil { return DiffReport{}, err }
	if err := ValidateRedactedJSON("candidate_contract", candidateRaw); err != nil { return DiffReport{}, err }

	report := DiffReport{
		SchemaVersion: CurrentSchemaVersion,
		ApprovedID: approved.ContractID,
		CandidateID: candidate.ContractID,
		PolicyID: firstNonEmpty(candidate.PolicyID, approved.PolicyID),
		Status: "pass",
	}
	compareValue(&report, "flow", approved.Flow, candidate.Flow, approved, candidate)
	compareValue(&report, "sentinel_release_id", approved.SentinelReleaseID, candidate.SentinelReleaseID, approved, candidate)
	compareValue(&report, "transport_profile_id", approved.TransportProfileID, candidate.TransportProfileID, approved, candidate)
	compareValue(&report, "policy_id", approved.PolicyID, candidate.PolicyID, approved, candidate)
	compareJSON(&report, "browser_identity", approved.BrowserIdentity, candidate.BrowserIdentity, approved, candidate)

	captureCount := maxInt(len(approved.Captures), len(candidate.Captures))
	for index := 0; index < captureCount; index++ {
		var left, right any
		captureID := strconv.Itoa(index)
		if index < len(approved.Captures) { left = approved.Captures[index]; captureID = approved.Captures[index].CaptureID }
		if index < len(candidate.Captures) { right = candidate.Captures[index]; if left == nil { captureID = candidate.Captures[index].CaptureID } }
		compareJSON(&report, "captures["+captureID+"]", left, right, approved, candidate)
	}

	exchangeCount := maxInt(len(approved.Exchanges), len(candidate.Exchanges))
	for index := 0; index < exchangeCount; index++ {
		var left, right any
		label := strconv.Itoa(index)
		if index < len(approved.Exchanges) {
			left = approved.Exchanges[index]
			label = exchangeLabel(approved.Exchanges[index])
		}
		if index < len(candidate.Exchanges) {
			right = candidate.Exchanges[index]
			if left == nil { label = exchangeLabel(candidate.Exchanges[index]) }
		}
		compareJSON(&report, "exchanges["+label+"]", left, right, approved, candidate)
	}
	sortDiffs := func(items []FieldDiff) { sort.Slice(items, func(i, j int) bool { return items[i].Path < items[j].Path }) }
	sortDiffs(report.Blocking)
	sortDiffs(report.KnownVariants)
	sortDiffs(report.Informational)
	if len(report.Blocking) != 0 { report.Status = "blocked" } else if len(report.KnownVariants) != 0 { report.Status = "known_variant" }
	return report, nil
}

func exchangeLabel(exchange StateExchangeContract) string {
	label := fmt.Sprintf("%s#%d@%d", exchange.State, exchange.ExchangeIndex, exchange.CaptureSequence)
	if exchange.SentinelOccurrence != nil { label += fmt.Sprintf(":occurrence=%d", *exchange.SentinelOccurrence) }
	return label
}

func compareJSON(report *DiffReport, path string, approvedValue, candidateValue any, approved, candidate *RegistrationContract) {
	leftRaw, _ := json.Marshal(approvedValue)
	rightRaw, _ := json.Marshal(candidateValue)
	var left, right any
	_ = json.Unmarshal(leftRaw, &left)
	_ = json.Unmarshal(rightRaw, &right)
	compareAny(report, path, left, right, approved, candidate)
}

func compareAny(report *DiffReport, path string, approvedValue, candidateValue any, approved, candidate *RegistrationContract) {
	if reflect.DeepEqual(approvedValue, candidateValue) { return }
	leftMap, leftIsMap := approvedValue.(map[string]any)
	rightMap, rightIsMap := candidateValue.(map[string]any)
	if leftIsMap || rightIsMap {
		keys := make(map[string]bool)
		for key := range leftMap { keys[key] = true }
		for key := range rightMap { keys[key] = true }
		ordered := make([]string, 0, len(keys))
		for key := range keys {
			if key == "contract_id" || key == "canonical_sha256" || key == "parent_contract_id" { continue }
			ordered = append(ordered, key)
		}
		sort.Strings(ordered)
		for _, key := range ordered {
			compareAny(report, path+"."+key, leftMap[key], rightMap[key], approved, candidate)
		}
		return
	}
	leftArray, leftIsArray := approvedValue.([]any)
	rightArray, rightIsArray := candidateValue.([]any)
	if leftIsArray || rightIsArray {
		if strings.HasSuffix(path, ".headers") {
			compareNamedArrays(report, path, leftArray, rightArray, func(value map[string]any) string { return fmt.Sprint(value["name"]) }, approved, candidate)
			return
		}
		if strings.HasSuffix(path, ".cookie_events") {
			compareNamedArrays(report, path, leftArray, rightArray, func(value map[string]any) string {
				return fmt.Sprintf("%s:%s:%s:%s", value["direction"], value["name"], value["domain"], value["path"])
			}, approved, candidate)
			return
		}
		count := maxInt(len(leftArray), len(rightArray))
		for index := 0; index < count; index++ {
			var left, right any
			if index < len(leftArray) { left = leftArray[index] }
			if index < len(rightArray) { right = rightArray[index] }
			compareAny(report, fmt.Sprintf("%s[%d]", path, index), left, right, approved, candidate)
		}
		return
	}
	compareValue(report, path, approvedValue, candidateValue, approved, candidate)
}

func compareNamedArrays(report *DiffReport, path string, approvedValues, candidateValues []any, identity func(map[string]any) string, approved, candidate *RegistrationContract) {
	left := make(map[string]any, len(approvedValues))
	right := make(map[string]any, len(candidateValues))
	for _, value := range approvedValues {
		if object, ok := value.(map[string]any); ok { left[identity(object)] = value }
	}
	for _, value := range candidateValues {
		if object, ok := value.(map[string]any); ok { right[identity(object)] = value }
	}
	keys := make(map[string]bool, len(left)+len(right))
	for key := range left { keys[key] = true }
	for key := range right { keys[key] = true }
	ordered := make([]string, 0, len(keys))
	for key := range keys { ordered = append(ordered, key) }
	sort.Strings(ordered)
	for _, key := range ordered {
		compareAny(report, path+"["+key+"]", left[key], right[key], approved, candidate)
	}
}
func compareValue(report *DiffReport, path string, approvedValue, candidateValue any, approved, candidate *RegistrationContract) {
	if reflect.DeepEqual(approvedValue, candidateValue) { return }
	kind := "changed"
	if approvedValue == nil { kind = "added" }
	if candidateValue == nil { kind = "removed" }
	severity, reason := classifySemanticDiff(path, approvedValue, candidateValue, approved, candidate)
	difference := FieldDiff{Severity: severity, Path: path, Kind: kind, Approved: approvedValue, Candidate: candidateValue, Reason: reason}
	switch severity {
	case DiffKnownVariant:
		report.KnownVariants = append(report.KnownVariants, difference)
	case DiffInformational:
		report.Informational = append(report.Informational, difference)
	default:
		report.Blocking = append(report.Blocking, difference)
	}
}

func classifySemanticDiff(path string, approvedValue, candidateValue any, approved, candidate *RegistrationContract) (DiffSeverity, string) {
	lower := strings.ToLower(path)
	if strings.Contains(lower, ".provenance.source_kind") || strings.Contains(lower, ".provenance.observed") {
		return DiffBlocking, "observed HAR provenance changed"
	}
	if strings.Contains(lower, ".provenance.") || strings.Contains(lower, ".role_evidence") || strings.Contains(lower, ".captured_at") || strings.Contains(lower, ".source_sha256") || strings.Contains(lower, ".capture_id") || strings.HasSuffix(lower, ".observed_status") {
		return DiffInformational, "capture metadata does not alter the normalized wire rule"
	}
	if strings.Contains(lower, ".locale") || strings.Contains(lower, ".timezone") {
		return DiffKnownVariant, "locale/timezone is an approved capture variant"
	}
	if strings.HasSuffix(lower, ".observed_script_source") {
		if knownScriptSourcePair(fmt.Sprint(approvedValue), fmt.Sprint(candidateValue)) && approved.SentinelReleaseID == candidate.SentinelReleaseID {
			return DiffKnownVariant, "loader and versioned SDK sources resolve to the same pinned release"
		}
		return DiffBlocking, "unknown Sentinel script source relationship"
	}
	if strings.HasSuffix(lower, ".requirements_fingerprint") {
		return DiffBlocking, "source-independent Sentinel requirements semantics changed"
	}
	if strings.Contains(lower, ".headers[") {
		approvedRule, candidateRule := headerRulesAtPath(path, approved, candidate)
		if approvedRule != nil && (approvedRule.Presence == PresenceRequired || approvedRule.Presence == PresenceForbidden) {
			return DiffBlocking, "required or forbidden header contract changed"
		}
		rule := candidateRule
		if rule == nil { rule = approvedRule }
		if rule != nil && (rule.Source == HeaderSourceTelemetry || rule.Source == HeaderSourceDynamicRuntime) {
			return DiffInformational, "telemetry/runtime identifier header is informational"
		}
		if rule != nil && (rule.Presence == PresenceOptional || rule.Presence == PresenceObserved) {
			return DiffKnownVariant, "optional or observed header variant"
		}
		return DiffBlocking, "app/transport header contract changed"
	}
	if strings.Contains(lower, ".cookie_events[") {
		approvedCookie, candidateCookie := cookieRulesAtPath(path, approved, candidate)
		if approvedCookie != nil && approvedCookie.Required {
			return DiffBlocking, "required cookie causality changed"
		}
		cookie := candidateCookie
		if cookie == nil { cookie = approvedCookie }
		if cookie != nil && !cookie.Required {
			return DiffKnownVariant, "optional approved cookie-name variant"
		}
		return DiffBlocking, "required cookie causality changed"
	}
	if strings.Contains(lower, ".allowed_status") {
		return DiffKnownVariant, "status change is explicit in the candidate response rule"
	}
	if strings.Contains(lower, ".response.content_type") || strings.Contains(lower, ".required_fields") || strings.Contains(lower, ".discriminators") || strings.Contains(lower, ".body_template") || strings.Contains(lower, ".outcome") || strings.Contains(lower, ".redirect") {
		return DiffBlocking, "response/redirect consumed contract changed"
	}
	if strings.Contains(lower, ".request.") || strings.HasSuffix(lower, ".state") || strings.Contains(lower, ".exchange_index") || strings.Contains(lower, ".capture_sequence") || strings.Contains(lower, ".sentinel_occurrence") || strings.Contains(lower, ".flow_name") || lower == "flow" {
		return DiffBlocking, "request, state, occurrence, or protected flow changed"
	}
	return DiffBlocking, "unclassified contract identity change fails closed"
}

func knownScriptSourcePair(left, right string) bool {
	known := map[string]bool{
		"https://sentinel.openai.com/backend-api/sentinel/sdk.js": true,
		"https://sentinel.openai.com/sentinel/20260219f9f6/sdk.js": true,
	}
	return left != right && known[left] && known[right]
}

func headerRulesAtPath(path string, approved, candidate *RegistrationContract) (*HeaderRule, *HeaderRule) {
	sequence, ok := captureSequenceFromPath(path)
	if !ok { return nil, nil }
	name, ok := namedSegment(path, ".headers[")
	if !ok { return nil, nil }
	find := func(contract *RegistrationContract) *HeaderRule {
		for exchangeIndex := range contract.Exchanges {
			exchange := &contract.Exchanges[exchangeIndex]
			if exchange.CaptureSequence != sequence { continue }
			for ruleIndex := range exchange.Request.Headers {
				if exchange.Request.Headers[ruleIndex].Name == name { return &exchange.Request.Headers[ruleIndex] }
			}
		}
		return nil
	}
	return find(approved), find(candidate)
}

func cookieRulesAtPath(path string, approved, candidate *RegistrationContract) (*CookieEvent, *CookieEvent) {
	sequence, ok := captureSequenceFromPath(path)
	if !ok { return nil, nil }
	identity, ok := namedSegment(path, ".cookie_events[")
	if !ok { return nil, nil }
	find := func(contract *RegistrationContract) *CookieEvent {
		for exchangeIndex := range contract.Exchanges {
			exchange := &contract.Exchanges[exchangeIndex]
			if exchange.CaptureSequence != sequence { continue }
			for ruleIndex := range exchange.CookieEvents {
				rule := &exchange.CookieEvents[ruleIndex]
				if fmt.Sprintf("%s:%s:%s:%s", rule.Direction, rule.Name, rule.Domain, rule.Path) == identity { return rule }
			}
		}
		return nil
	}
	return find(approved), find(candidate)
}

func namedSegment(path, marker string) (string, bool) {
	start := strings.Index(path, marker)
	if start < 0 { return "", false }
	start += len(marker)
	end := strings.Index(path[start:], "]")
	if end < 0 { return "", false }
	return path[start : start+end], true
}

func captureSequenceFromPath(path string) (int, bool) {
	at := strings.Index(path, "@")
	if at < 0 { return 0, false }
	end := strings.IndexAny(path[at+1:], ":]")
	if end < 0 { return 0, false }
	value, err := strconv.Atoi(path[at+1 : at+1+end])
	return value, err == nil
}


func maxInt(left, right int) int { if left > right { return left }; return right }
