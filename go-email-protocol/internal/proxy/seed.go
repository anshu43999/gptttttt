package proxy

import (
	"crypto/rand"
	"encoding/json"
	"fmt"
	"math/big"
	"net/url"
	"regexp"
	"sort"
	"strings"

	"github.com/gpt-register/go-email-protocol/internal/store"
)

// SeedSession is a new sticky session minted from a reusable proxy seed. The
// seed is deliberately not exclusively leased: one base account can safely
// issue independent SID sessions, while each registration keeps its own URL.
type SeedSession struct {
	ResourceID  int64
	ResourceKey string
	URL         string
	Region      string
	Style       string
}

type seedRecord struct {
	id          int64
	key         string
	account     string
	password    string
	host        string
	port        string
	style       string
	vendor      string
	protocol    string
	styleTags   string
}

var (
	bestgoUserRe = regexp.MustCompile(`^(?P<base>.+?)-zone-custom-region-[A-Za-z]{2}(?:-session-[A-Za-z0-9]+)?(?:-sessTime-\d+)?$`)
	lajiaoUserRe = regexp.MustCompile(`^(?P<base>.+?)-region-[A-Za-z]{2}-sid-[^-]+-t-\d+$`)
	kookeeyUserRe = regexp.MustCompile(`^(?P<base>.+?)_custom_zone_[A-Za-z]{2}_sid_[^_]+_time_\d+$`)
)


// DefaultSeedRegions matches pure-go-register / canary_n10p multi-region rotation.
func DefaultSeedRegions() []string {
	return []string{"JP", "US", "DE", "GB", "BR"}
}

// ParseSeedRegions splits a comma list; empty → DefaultSeedRegions.
func ParseSeedRegions(raw string) []string {
	parts := strings.Split(raw, ",")
	out := make([]string, 0, len(parts))
	seen := map[string]struct{}{}
	for _, p := range parts {
		r := strings.ToUpper(strings.TrimSpace(p))
		if r == "" || r == "AUTO" || r == "ANY" || r == "*" {
			continue
		}
		if len(r) != 2 {
			continue
		}
		if _, ok := seen[r]; ok {
			continue
		}
		seen[r] = struct{}{}
		out = append(out, r)
	}
	if len(out) == 0 {
		return DefaultSeedRegions()
	}
	return out
}

// NextSeedRegion picks the next region after current (round-robin). Empty current → first.
func NextSeedRegion(regions []string, current string) string {
	if len(regions) == 0 {
		regions = DefaultSeedRegions()
	}
	cur := strings.ToUpper(strings.TrimSpace(current))
	if cur == "" {
		return regions[0]
	}
	for i, r := range regions {
		if r == cur {
			return regions[(i+1)%len(regions)]
		}
	}
	// Unknown current: pick a different region when possible.
	for _, r := range regions {
		if r != cur {
			return r
		}
	}
	return regions[0]
}

// PickSeedRegion chooses a region for a new task (hash of taskID for spread).
func PickSeedRegion(regions []string, taskID string) string {
	if len(regions) == 0 {
		regions = DefaultSeedRegions()
	}
	if len(regions) == 1 {
		return regions[0]
	}
	h := 0
	for _, ch := range taskID {
		h = (h*31 + int(ch)) & 0x7fffffff
	}
	return regions[h%len(regions)]
}

// NextSeedStyle round-robins approved styles (canary remint diversity).
// Empty current → first style; unknown current → first different style when possible.
func NextSeedStyle(styles []string, current string) string {
	list := NormalizeStyleList(styles)
	if len(list) == 0 {
		list = []string{"bestgo", "1024"}
	}
	cur := strings.ToLower(strings.TrimSpace(current))
	if cur == "" {
		return list[0]
	}
	// Treat lajiao as 1024 alias for rotation bookkeeping.
	if cur == "lajiao" {
		cur = "1024"
	}
	for i, s := range list {
		if s == cur || (s == "1024" && cur == "lajiao") || (s == "lajiao" && cur == "1024") {
			return list[(i+1)%len(list)]
		}
	}
	for _, s := range list {
		if s != cur {
			return s
		}
	}
	return list[0]
}

// NormalizeStyleList lowercases, splits commas, de-dupes, preserves order.
func NormalizeStyleList(values []string) []string {
	out := make([]string, 0, len(values))
	seen := map[string]struct{}{}
	for _, value := range values {
		for _, piece := range strings.Split(value, ",") {
			piece = strings.ToLower(strings.TrimSpace(piece))
			if piece == "" {
				continue
			}
			if _, ok := seen[piece]; ok {
				continue
			}
			seen[piece] = struct{}{}
			out = append(out, piece)
		}
	}
	return out
}

// StyleFromProxyURL best-effort style detection from a minted socks URL.
func StyleFromProxyURL(raw string) string {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return ""
	}
	u, err := url.Parse(raw)
	if err != nil {
		return ""
	}
	user := ""
	if u.User != nil {
		user = u.User.Username()
	}
	return detectStyle(user, u.Hostname())
}

// MintSeedSession loads only proxy_seed rows, filters to approved styles, and
// mints a fresh SID. It never falls back to legacy lajiao_credentials rows.
func MintSeedSession(dbPath, taskID string, allowedStyles []string, region string, ttl int) (*SeedSession, error) {
	return MintSeedSessionPrefer(dbPath, taskID, allowedStyles, "", region, ttl)
}

// MintSeedSessionPrefer is MintSeedSession with an optional preferred style.
// When prefer is set and at least one matching seed exists, only that style is used.
// Otherwise falls back to the full allowed style set (canary remint: rotate style, don't fail closed).
func MintSeedSessionPrefer(dbPath, taskID string, allowedStyles []string, prefer, region string, ttl int) (*SeedSession, error) {
	if ttl < 1 {
		ttl = 10
	}
	region = strings.ToUpper(strings.TrimSpace(strings.Split(region, ",")[0]))
	if region == "" || region == "AUTO" || region == "ANY" || region == "*" {
		region = "JP"
	}
	allowedList := NormalizeStyleList(allowedStyles)
	if len(allowedList) == 0 {
		allowedList = []string{"bestgo", "1024"}
	}
	prefer = strings.ToLower(strings.TrimSpace(prefer))
	if prefer == "lajiao" {
		// 1024 seeds are often tagged style=lajiao vendor=1024
		prefer = "1024"
	}
	tryStyles := allowedList
	if prefer != "" {
		// Prefer exact style first; include lajiao alias when preferring 1024.
		prefList := []string{prefer}
		if prefer == "1024" {
			prefList = []string{"1024", "lajiao"}
		}
		if sess, err := mintSeedSessionWithStyles(dbPath, taskID, prefList, region, ttl); err == nil {
			return sess, nil
		}
		// fall through to full allowed set
		tryStyles = allowedList
	}
	return mintSeedSessionWithStyles(dbPath, taskID, tryStyles, region, ttl)
}

func mintSeedSessionWithStyles(dbPath, taskID string, allowedStyles []string, region string, ttl int) (*SeedSession, error) {
	allowed := normalizeStyles(allowedStyles)
	if len(allowed) == 0 {
		allowed = map[string]struct{}{"bestgo": {}, "1024": {}}
	}

	db, backend, err := store.OpenPath(dbPath)
	if err != nil {
		return nil, err
	}
	defer db.Close()
	// Seeds are reusable templates (mint sticky SID per task). Never exclusive-lease.
	// Task report used to wrongly mark seeds disabled/cooldown; still mint from them
	// when style matches (bestgo/1024). Only skip intentionally disabled junk seeds
	// that do not match approved styles.
	q := store.Rebind(backend, `
		SELECT id, resource_key, payload_json, COALESCE(status,'') AS status
		FROM resource_pool
		WHERE resource_type='proxy' AND provider='proxy_seed'
	`)
	rows, err := db.Query(q)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	candidates := make([]seedRecord, 0)
	for rows.Next() {
		var id int64
		var key, raw, status string
		if err := rows.Scan(&id, &key, &raw, &status); err != nil {
			return nil, err
		}
		seed, err := parseSeedRecord(id, key, raw)
		if err != nil || !seed.matches(allowed) {
			continue
		}
		// Prefer non-disabled, but approved-style disabled seeds remain mintable
		// (status only affects ranking via score later if needed).
		_ = status
		candidates = append(candidates, seed)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	if len(candidates) == 0 {
		return nil, fmt.Errorf("proxy: no available proxy_seed matching approved styles")
	}

	sort.SliceStable(candidates, func(i, j int) bool { return candidates[i].score() > candidates[j].score() })
	top := candidates[0].score()
	tier := make([]seedRecord, 0, len(candidates))
	for _, candidate := range candidates {
		if candidate.score() >= max(0, top-5) {
			tier = append(tier, candidate)
		}
	}
	if len(tier) == 0 {
		tier = candidates
	}
	index := 0
	for _, char := range taskID {
		index += int(char)
	}
	chosen := tier[index%len(tier)]
	sid, err := newSID(10)
	if err != nil {
		return nil, err
	}
	username := sessionUsername(chosen.account, chosen.style, region, sid, ttl)
	// socks5h = remote DNS through the proxy (Python normalize_proxy_url / Graph path).
	// Plain socks5 can force local DNS and break under Clash fake-ip / system resolvers.
	u := &url.URL{Scheme: "socks5h", Host: chosen.host + ":" + chosen.port}
	if chosen.password == "" {
		u.User = url.User(username)
	} else {
		u.User = url.UserPassword(username, chosen.password)
	}
	return &SeedSession{ResourceID: chosen.id, ResourceKey: chosen.key, URL: u.String(), Region: region, Style: chosen.style}, nil
}

func parseSeedRecord(id int64, key, raw string) (seedRecord, error) {
	var payload map[string]any
	if err := json.Unmarshal([]byte(raw), &payload); err != nil {
		return seedRecord{}, err
	}
	get := func(name string) string {
		value, _ := payload[name].(string)
		return strings.TrimSpace(value)
	}
	record := seedRecord{
		id: id, key: key, account: get("account"), password: get("password"), host: get("host"),
		port: get("port"), style: strings.ToLower(get("style")), vendor: strings.ToLower(get("vendor")),
		protocol: strings.ToLower(get("protocol")),
	}
	if tags, ok := payload["style_tags"].([]any); ok {
		parts := make([]string, 0, len(tags))
		for _, tag := range tags {
			parts = append(parts, strings.ToLower(fmt.Sprint(tag)))
		}
		record.styleTags = strings.Join(parts, " ")
	}
	if record.account == "" || record.host == "" || record.port == "" {
		fromURL, err := parseRawSeed(get("url"))
		if err != nil {
			return seedRecord{}, err
		}
		if record.account == "" {
			record.account = fromURL.account
		}
		if record.password == "" {
			record.password = fromURL.password
		}
		if record.host == "" {
			record.host = fromURL.host
		}
		if record.port == "" {
			record.port = fromURL.port
		}
	}
	record.account = baseAccount(record.account)
	if record.style == "" {
		record.style = detectStyle(record.account, record.host)
	}
	if record.style == "plain" && record.vendor == "1024" {
		record.style = "lajiao"
	}
	if record.account == "" || record.host == "" || record.port == "" {
		return seedRecord{}, fmt.Errorf("proxy: malformed proxy_seed %d", id)
	}
	return record, nil
}

func parseRawSeed(raw string) (seedRecord, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return seedRecord{}, fmt.Errorf("proxy: seed url missing")
	}
	if !strings.Contains(raw, "://") {
		raw = "socks5://" + raw
	}
	u, err := url.Parse(raw)
	if err != nil || u.Hostname() == "" || u.Port() == "" || u.User == nil {
		return seedRecord{}, fmt.Errorf("proxy: invalid seed url")
	}
	password, _ := u.User.Password()
	return seedRecord{account: u.User.Username(), password: password, host: u.Hostname(), port: u.Port(), protocol: u.Scheme}, nil
}

func (s seedRecord) matches(allowed map[string]struct{}) bool {
	haystack := strings.ToLower(strings.Join([]string{s.style, s.vendor, s.host, s.key, s.styleTags}, " "))
	for allowedStyle := range allowed {
		if allowedStyle == s.style || allowedStyle == s.vendor || strings.Contains(haystack, allowedStyle) {
			return true
		}
	}
	return false
}

func (s seedRecord) score() int {
	score := 0
	host := strings.ToLower(s.host)
	if s.style == "bestgo" || s.vendor == "bestgo" || strings.Contains(host, "bestgo") {
		score += 20
	}
	if s.vendor == "1024" || strings.Contains(host, "1024") || strings.Contains(strings.ToLower(s.key), "1024") {
		score += 20
	}
	if s.style == "bestgo" || s.style == "lajiao" || s.style == "kookeey" {
		score += 10
	}
	return score
}

func normalizeStyles(values []string) map[string]struct{} {
	out := make(map[string]struct{}, len(values))
	for _, value := range values {
		for _, piece := range strings.Split(value, ",") {
			piece = strings.ToLower(strings.TrimSpace(piece))
			if piece != "" {
				out[piece] = struct{}{}
			}
		}
	}
	return out
}

func detectStyle(account, host string) string {
	lowerHost := strings.ToLower(host)
	if strings.Contains(lowerHost, "bestgo") || strings.Contains(lowerHost, "rrp.best") || strings.Contains(account, "-zone-custom-region-") {
		return "bestgo"
	}
	if strings.Contains(lowerHost, "1024") || strings.Contains(lowerHost, "lajiao") || strings.Contains(account, "-region-") {
		return "lajiao"
	}
	if strings.Contains(lowerHost, "proxy001") || strings.Contains(account, "_custom_zone_") {
		return "kookeey"
	}
	return "plain"
}

func baseAccount(account string) string {
	for _, re := range []*regexp.Regexp{bestgoUserRe, lajiaoUserRe, kookeeyUserRe} {
		if match := re.FindStringSubmatch(account); len(match) > 1 {
			return match[1]
		}
	}
	return strings.TrimSpace(account)
}

func sessionUsername(account, style, region, sid string, ttl int) string {
	switch strings.ToLower(style) {
	case "bestgo":
		return fmt.Sprintf("%s-zone-custom-region-%s-session-%s-sessTime-%d", account, region, sid, ttl)
	case "lajiao":
		return fmt.Sprintf("%s-region-%s-sid-%s-t-%d", account, region, sid, ttl)
	case "kookeey":
		return fmt.Sprintf("%s_custom_zone_%s_sid_%s_time_%d", account, region, sid, ttl)
	default:
		return account
	}
}

func newSID(length int) (string, error) {
	const alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
	buf := make([]byte, length)
	for index := range buf {
		n, err := rand.Int(rand.Reader, big.NewInt(int64(len(alphabet))))
		if err != nil {
			return "", err
		}
		buf[index] = alphabet[n.Int64()]
	}
	return string(buf), nil
}

func max(a, b int) int {
	if a > b {
		return a
	}
	return b
}
