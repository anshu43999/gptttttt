> 交付包使用说明见仓库根目录 [docs/go-protocol-usage.md](../docs/go-protocol-usage.md)。
> 软件默认：`start.bat` 以 `-pure-go -protocol-mode live -transport tls` 启动本目录 `email-protocol-worker.exe`。

# go-email-protocol

Go worker for GPT Register email-protocol path.

**规格权威：**

| 文档 | 用途 |
|---|---|
| [`docs/PURE_GO_FULL_FINGERPRINT_PLAN.md`](../docs/PURE_GO_FULL_FINGERPRINT_PLAN.md) | **终态 + 无人值守路线 §23 + 进度表 §23.6** |
| [`docs/EMAIL_PROTOCOL_GO_PLAN.md`](../docs/EMAIL_PROTOCOL_GO_PLAN.md) | FSM / V2 API / bridge / Sentinel 基线（Bundle 以 v2 为准） |
| [`docs/TRUE_100_CONCURRENCY_FINGERPRINT_PLAN.md`](../docs/TRUE_100_CONCURRENCY_FINGERPRINT_PLAN.md) | 100 并发与隔离 |
| [`docs/LATEST_HAR_PROTOCOL_RECHALLENGE_PLAN.md`](../docs/LATEST_HAR_PROTOCOL_RECHALLENGE_PLAN.md) | **最新 d17/d24 HAR wire contract、Sentinel release、离线 replay、canary 与 rollback** |

## Phases

| Phase | Status | Notes |
|---|---|---|
| G0 fixtures | done | catalogue + redaction + golden |
| G1 V2 daemon | done | ledger, admission 100, API, synthetic runner |
| **A** FingerprintBundle v2 | **done** | generate/freeze/validate |
| **B** Create bind | **done** | force Bundle / server generate legacy stub |
| **C** HeaderPreset | **done** | ordered presets + OTP sparse + Datadog opt-in |
| **D** tls-client | **partial** | dep + factory + `-tags tlsclient`; **default still fake**; echo 对拍未做 |
| **E** FSM | **done (CLI)** | live S0–S14 pure-Go CLI; worker live opt-in via `-pure-go` |
| **F** Sentinel | **partial** | pin+drift + PoW + Turnstile/SDK 路径；字节级对拍未做 |
| **G** Python canary / cutover | **in progress** | CLI concurrent 6/8 SUCCESS; worker V2 pure-Go smoke S6/OTP; **default backend still not go** |
| **H** 100 load + retire Node | pending | |

## Pure-Go CLI（主业务库）

生产根目录的 `env.db` 会自动选中 Postgres；此时 `-db` 参数被忽略，不能写回 SQLite 备份。仅在显式 `GPT_REGISTER_DB_BACKEND=sqlite` 时，`-db` 才是 SQLite 文件路径。

```bash
# single: leases email + proxy from PostgreSQL resource_pool, imports accounts on success
go run ./cmd/pure-go-register -db "../data/gpt_register.db" -out "../output/pure_go_register"

# batch N (each worker leases its own proxy from PostgreSQL pool)
go run ./cmd/pure-go-register-batch -n 10 -db "../data/gpt_register.db" \
  -bin ./bin/pure-go-register.exe -out "../output/pure_go_register_batch"
```

Worker pure-Go (opt-in; default remains mailat):

```bash
go run ./cmd/email-protocol-worker -pure-go -protocol-mode=live -transport=direct \
  -addr 127.0.0.1:18765 -db ./data/ledger.db -key ./data/worker.key
```

## Run tests

```bash
cd go-email-protocol
go mod tidy
GOTOOLCHAIN=local go test ./... -count=1
# optional tls factory compile:
GOTOOLCHAIN=local go test -tags tlsclient ./internal/transport/ -count=1
```

Worker:

```bash
# default: fake transport + mailat runner (existing production-adjacent path)
go run ./cmd/email-protocol-worker -addr 127.0.0.1:18080 -db ./data/ledger.db -key ./data/worker.key

# synthetic G1 only
go run ./cmd/email-protocol-worker -synthetic -transport=fake ...

# tls transport requires build tag (still no live FSM until Phase E3):
go run -tags tlsclient ./cmd/email-protocol-worker -transport=tls -synthetic ...
```

## API surface

- `GET /health`
- `GET /diagnostics` — loopback-only aggregate admission counters; no job data or secrets
- `POST /v2/email-register`
- `GET /v2/email-register/{job_id}?wait_ms=0..30000`
- `POST /v2/email-register/{job_id}/otp`
- `DELETE /v2/email-register/{job_id}`

Auth: `X-Job-Capability` or `Authorization: Bearer <job_capability>`.

## Non-goals until gates pass

- Do **not** change Python `email_protocol.backend` default to Go.
- Do **not** delete Node/mailat protocol path.
- Do **not** default `-transport=tls` until live FSM + canary green.
