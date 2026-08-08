# HAR Rechallenge Port Status (lab → original tree)

Date: 2026-07-24  
Source: `rechallenge-lab/go-email-protocol`  
Target: `go-email-protocol/`

## Verdict

**Offline wire corrections are ported into the original tree.**  
Focused packages are green. **No default config / concurrency cutover. No live canary yet.**

```bash
cd go-email-protocol
go test -count=1 \
  ./internal/rechallenge \
  ./internal/replay \
  ./internal/releasegate \
  ./internal/sentinel \
  ./internal/protocol \
  ./internal/fixture \
  ./internal/transport
# all packages ok (2026-07-24)
```

## Ported (this pass)

### New packages / assets

| Path | Notes |
|---|---|
| `internal/rechallenge/**` | HAR ingest, normalize, redaction, contract |
| `internal/replay/**` | zero-network `transport.Client` + matchers |
| `internal/releasegate/**` | startup wire gate + embedded approved manifest |
| `internal/sentinel/release.go` (+ tests) | immutable Sentinel release pin |
| `testdata/rechallenge/**` | sanitized d17/d24 contracts + sentinel manifest |
| `internal/protocol/edge_challenge.go` (+ tests) | passive CF/edge detector |
| `internal/protocol/replay_integration_test.go` | ModeLive ↔ d17/d24 gates |
| `cmd/protocol-rechallenge/**` | offline CLI (if present after copy) |
| `internal/transport/profile_map.go` | `EffectiveProfile` / `ResolveEffectiveTLSProfile` for releasegate |

### Merged into existing production file

`internal/protocol/live.go` — **surgical merge**, not whole-file overwrite:

| Change | Why |
|---|---|
| S1 → `GET /api/auth/providers` | d17/d24 observed |
| S1 DeviceID cookie/mint fallback | S3/S4 need `ext-oai-did` |
| S2 content-type alignment | HAR-ish fetch headers |
| S3 query (`login_hint`, `ext-oai-did`, …) | observed signin contract |
| S4 authorize query rebuild + final URL | observed authorize params |
| S4 → **S9** on `email-verification` / `email-otp` | skip S5–S8 on passwordless lane |
| mintSentinel: auth origin, `text/plain;charset=UTF-8`, state **T1** | observed sentinel wire |
| mintSentinel: `SoftSOStrict` + required SO | S11 must not soft-omit SO |
| `extractJSONString` JSON-escape safe | preserves callback `scope` (`\u0026`) |
| `doHTTP` → `DetectEdgeChallenge` | typed non-retryable challenge |

**Preserved from original tree (lab intentionally not overwriting):**

- SOCKS / transient transport retry loop (`GetBody`, maxAttempts)
- dump-wire path (`GPT_REGISTER_WIRE_DIR`)
- password-path S5–S8 handlers for non-HAR lanes

### Fixture catalogue

| Path | Change |
|---|---|
| `internal/fixture/catalogue.go` | skip `testdata/rechallenge` subtree |
| `internal/fixture/catalogue_test.go` | same skip in walk |

### Fixture unit test

| Path | Change |
|---|---|
| `internal/protocol/live_test.go` | `TestLiveS0ToS4WithFixtureDo` sets `Email` |

## Not ported / still blocked

| Item | Reason |
|---|---|
| Default backend / transport / max_active config | production cutover needs canary |
| Live OpenAI traffic | separate n=1→5→10→25 ticket |
| Cloudflare auto-solver | plan forbids; detector only |
| Claim S13 covered by d17/d24 HAR | captures end at S12 homepage |
| Blind overwrite of `tls_client.go` | only profile resolution API ported |
| Soft-omit SO in production mint | now **strict** on mint path (intentional) |

## Risk notes for live canary

1. S4 landing on email-verification **skips password path** (S5–S8). Matches d17/d24. Password continue URLs still use old route.
2. mintSentinel now tags **T1** and uses auth.openai.com origin — differs from older chatgpt.com + S5 tagging.
3. Required SO may increase create_account success surface; also fails closed if SO cannot be built.
4. Edge challenge is **non-retryable** — expect typed `edge_challenge_required` under CF hits.

## Next step (when you say go)

1. `n=1` pure-go-register canary: bestgo+1024 + outlook_token  
2. Success = non-empty `access_token` + usable session  
3. Then 5 → 10 → 25 only if failure codes are understood  

Do **not** jump to 100 concurrency until single-session live is green.
