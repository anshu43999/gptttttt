# True parallel concurrency — plan + n=10 result (2026-07-24)

## Goal

真并发（不是串行伪装）：N 个 worker 同时跑协议，可恢复 CF，可扩到更高并发。

## What was wrong before

| 问题 | 证据 |
|---|---|
| CLI 固定 `-proxy` | remint 被挡住；撞 CF 直接死 |
| 并行 n=10 仅换 SID | 30% 成功；4× S4 `403 cf-mitigated` |
| 无 session 层 edge 恢复 | job 层有 `rotateRuntimeProxy`，CLI 没有 |
| 区域/指纹不对齐 | SG 无 locale → `proxy_affinity_mismatch` |

## Architecture (what to do)

```text
                    ┌─────────────────────────────┐
  N workers ──────► │ admission (global seats)    │  already in internal/admission
  (processes/jobs)  │  MaxActive + per mailbox    │
                    └─────────────┬───────────────┘
                                  │
                    ┌─────────────▼───────────────┐
                    │ per worker:                 │
                    │  mint SID (proxy_seed)      │  non-exclusive seed
                    │  multi-region rotate        │  JP/US/DE/GB/BR (+ locale)
                    │  multi-style bestgo+1024    │
                    │  S0→S9                      │
                    │  on edge_challenge:         │
                    │    remint SID+region        │  NEW in pure-go-register
                    │    new TLS client           │
                    │    restart S0 (same email)  │  never retry challenged POST
                    │  OTP → S10→S14              │
                    └─────────────────────────────┘
```

### Layers

1. **Identity diversity** — fresh SID per attempt; rotate region; prefer ≥2 seed accounts (bestgo+1024).
2. **Session recovery** — edge CF → remint + S0 restart (job 已有；CLI 已补齐).
3. **Admission** — cap simultaneous protocol seats (`internal/admission`, default 200). OTP wait 可放 seat。
4. **Do not** auto-solve CF；detector 保持 non-retryable on same connection.

### CLI flags (new)

```text
-proxy-seed
-proxy-styles bestgo,1024
-proxy-regions JP,US,DE,GB,BR   # must have fingerprint locale
-proxy-ttl 15
-edge-remints 2
```

Fingerprint `ExpectedCountry` follows seed region.

## n=10 true-parallel canary

| Run | Mode | ok/fail | CF | Notes |
|---|---|---|---|---|
| earlier n10p | fixed `-proxy` mint outside | 3/7 | **4** | 30% |
| n10p_seed #1 | `-proxy-seed` + remint | 8/2 | **0** | 2× SG locale missing |
| **n10p_seed #2** | regions JP,US,DE,GB,BR | **8/2** | **0** | OTP + S11 only |

### Latest per-worker (summary_20260724_093829)

| w | ok | token | remints | sec | fail |
|---:|:---:|---:|---:|---:|---|
| 0–5,7–9 | yes | 1754–1765 | 0 | 33–43 | — |
| 6 | no | 0 | 0 | 39 | S11 400 |
| 1 | no | 0 | 0 | 369 | OTP graph_no_openai_code |

- **Success rate 80%** under true parallel (stagger 200ms ≈ simultaneous).
- **Zero edge_challenge** this run (remint budget unused — first SID already good enough with multi-region/style).
- Wall ~43s for successes; total wall 369s dominated by one OTP timeout.

Artifacts: `output/pure_go_register_canary/n10p_seed/`

## Recommended scale path

| Step | Target | Gate |
|---|---|---|
| Now | n=10 parallel ≥70% | **met (80%)** |
| Next | n=25 parallel | CF rate + OTP fail tracked |
| Then | n=50 with admission MaxActive | seat wait vs fail |
| Later | n=100 | only if CF remint recovery rate healthy |

### Ops checklist

1. Keep **≥2** `proxy_seed` available (bestgo + 1024 enabled).
2. Regions ⊆ fingerprint locales: **JP,US,DE,GB,BR** (not SG until locale added).
3. Always `-proxy-seed` for concurrent CLI; never fixed `-proxy` for scale.
4. Prefer daemon `email-protocol-worker` + admission for production 100 seats.
5. Track failure classes: `edge_challenge` / `otp` / `s11_*` / `proxy_dial` separately.

## Code touched

- `cmd/pure-go-register/main.go` — seed mint, edge remint, multi-region, FP country align
- `tools/canary_n10p` — true parallel via `-proxy-seed` (no external fixed proxy)
- 1024 seed id=31858 set `available` for diversity
