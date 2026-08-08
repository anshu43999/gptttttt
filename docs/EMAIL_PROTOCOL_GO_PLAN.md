# 100 并发邮箱协议 Go 重构蓝图

**更新时间：** 2026-07-15  
**范围：** 仅 `mode=email_protocol` / `email-protocol-register-token` 的 OpenAI/ChatGPT 邮箱协议路径。  
**交付目标：** Go daemon 在 **100 个资源互斥的活跃 job** 下运行完整协议状态机，最终交付 `access_token` 与可恢复的 session state；Python 继续拥有任务、资源、邮箱 OTP 与账号/handoff 落库。

> **权威覆盖（2026-07-18）：** 设备画像与纯 Go 全量指纹终态以  
> **`docs/PURE_GO_FULL_FINGERPRINT_PLAN.md`** 为准（FingerprintBundle **v2**、TransportProfile v2、  
> 无人值守路线 §23）。本文仍是 **FSM / V2 API / bridge / Sentinel 算法 / 100 job 语义** 的基线；  
> 凡本文 §8 Bundle **v1** 字段表与 v2 冲突，**以 v2 为准**（v1 仅作兼容投影 `ToV1`）。  
> 实施顺序与进度勾选：**仅**维护 `PURE_GO_FULL_FINGERPRINT_PLAN.md` §23.6。

> 本文的“全量字段”是指仓库当前生产参考实现 `E:/project/mailat/mailat/codex_register` 中**所有可观察到的**协议、身份、cookie、Sentinel 与 transport 字段。源码证明“字段已发送/消费”，不证明上游当前逐项强制校验。未知字段不得凭空补造，必须先补录兼容 fixture。
 
> **最新 HAR wire 合同：** `docs/LATEST_HAR_PROTOCOL_RECHALLENGE_PLAN.md` 负责注册 HAR 的角色识别、multi-capture contract、Sentinel occurrence/release、离线 ModeLive replay 与重新挑战门禁。本文继续作为 FSM/V2 API/bridge/Sentinel 算法/100 job 语义基线。

---

## 1. 结论与硬边界

### 1.1 要做的是完整 Go 协议，不是 Go 调 Node

最终 Go worker 必须拥有：

- OpenAI/ChatGPT HTTP 状态机；
- 每 job 独立 CookieJar、代理、TLS/HTTP transport、设备画像与 checkpoint；
- Go 实现的 Sentinel requirements/PoW；
- Go 进程内的动态 Sentinel/Turnstile 执行能力；
- durable job ledger、OTP challenge、取消、崩溃恢复、100 job 背压。

现有 TS `openai.ts`、`sentinel.ts` 只作为**协议参考与 fixture oracle**。开发早期可以用它生成脱敏 fixture，但 100 并发生产目标不依赖每 job Node 进程或共享 Node browser context。

### 1.2 必须保留的所有权

| Python 唯一 owner | Go daemon 唯一 owner |
|---|---|
| TasksService admission、resource lease、资源冷却/上报 | 注册协议 FSM、cookie/session、transport、Sentinel、checkpoint |
| credential proxy 原始凭据、proxy preflight、local BridgeManager 与 bridge 生命周期 | 仅以 job grant 指定的 local HTTP CONNECT bridge 出网；不选择/不轮换/不直连 |
| 邮箱 provider、OTP message baseline、OTP 轮询 | OTP challenge 状态、OTP 提交去重与协议推进 |
| accounts / registered / resume / handoff 文件 | 返回版本化 session document，不写 Python 输出目录 |
| UI/API 创建任务、任务取消意图 | daemon job 状态、取消确认、reconcile 结果 |

Go **不**自行选择、轮换、释放或上报 Python 资源；Python **不**在 Go 的结果不明时重跑同一邮箱注册。

### 1.3 100 并发的定义

`100 并发` = 最多 100 个已获资源 grant、仍未终态的 job。`waiting_for_otp` 仍占 1 个 active slot，因为邮箱和代理尚被占用；但它不得占用轮询 goroutine 或持续 HTTP 请求。

现有控制面还做不到 100：

- `config.example.yaml` 当前 `max_parallel_tasks=1`、`max_register_tasks=1`；
- `application/tasks_service.py` 把 global/register bucket 硬限制在 32；
- 只把 Go daemon 开到 100，Python admission 仍会把任务截在 32。

因此 100 并发的必要条件是：**控制面 admit 上限、资源供给、Go daemon 三者一起提升**。

---

## 2. 参考实现与源码基线

| 对象 | 源码 | 本文用途 |
|---|---|---|
| 主邮箱协议 | `E:/project/mailat/mailat/codex_register/src/openai.ts` | endpoint、FSM、headers、cookie、session success 语义 |
| endpoint 常量 | `.../src/constants.ts` | URL 与 OAuth 常量 |
| 设备画像 | `.../src/device-profile.ts` | profile 字段、派生 Client Hints、生成约束 |
| Sentinel | `.../src/sentinel.ts`、`sentinel-browser.ts` | requirements、PoW、SDK/VM、browser context 字段 |
| 当前生产入口 | `.../src/index.ts --at --email --otp` | 实际主链与 session-failure fallback |
| Python task bridge | `services/mailat_email_protocol_task.py` | OTP baseline、handoff 归属 |
| Go client 骨架 | `services/go_email_protocol_runner.py` | 必须升级的 V1 API 与生命周期缺口 |
| 调度/资源 | `application/tasks_service.py`、`application/resource_pool_service.py`、`infrastructure/db.py` | 100 job admission、lease/fence、exit-IP 去重 |

**主链事实：** 当前 `--at` 走 `authRegisterHTTP()`，完成后请求 `GET /api/auth/session`。只有 session 失败时，入口才创建一个新的 client/jar/profile 做既有账号的 Codex OAuth 重新授权；这不是原注册会话的普通重试。

---

## 3. 100 并发总体架构

```mermaid
flowchart LR
    P[Python TasksService\nresource lease / OTP mailbox / BridgeManager] -->|immutable grant + V2 job request| A[Go admission ledger]
    P -->|per-job BridgeGrant\ncapability + expiry| B[Python local HTTP CONNECT bridge\n127.0.0.1 only]
    A -->|max 100 / seats available| W[Go protocol scheduler]
    W --> J1[Job context 1\nprofile + jar + proxy + FSM]
    W --> JN[Job context N\nprofile + jar + proxy + FSM]
    J1 -->|authenticated CONNECT| B
    JN -->|authenticated CONNECT| B
    B -->|credentialed HTTP or SOCKS5 upstream| X[Selected proxy egress]
    X --> O[auth.openai.com / chatgpt.com]
    J1 --> S[Go Sentinel engine\nPoW / SDK realm / VM]
    JN --> S
    J1 -->|waiting_for_otp challenge| P
    P -->|challenge_id + version + code| J1
    J1 -->|versioned result| P
```

### 3.1 Admission 和资源容量公式

```text
admitted = min(
  100,
  PythonControlPlaneGrant,
  availableMailboxSeats,
  distinctProxySeats,
  sum(configuredDomainCaps)
)
```

初始 policy：

| 配置 | 初始值 | 说明 |
|---|---:|---|
| `email_protocol_max_running` | 100 | Go active job 上限 |
| `email_protocol_max_queued` | 100 | durable queue 上限；满时返回 retry hint |
| `email_protocol_proxy_cap` | 1 | 同一代理资源/已知 exit IP 同时仅一个 job |
| `email_protocol_mailbox_account_cap` | 1 | 同一邮箱账号/凭据不并发轮询 |
| `email_protocol_domain_cap` | 1 | **初始保守值**；提高必须有 provider canary 数据 |
| `email_protocol_timeout_seconds` | 900 | job root deadline；不得低于现有 120s floor |
| `email_otp_timeout` | 200 | 不超过 root deadline 剩余时间 |
| `proxy_selection_deadline` | 300s | 对全部候选代理的总 deadline，不是每个候选 300s |

100 active jobs 在 `proxy_cap=1`、`mailbox_account_cap=1` 时，需要至少：

- 100 个可租邮箱 seat；
- 100 个可租 proxy seat；
- 预检后 100 个不同且合规的 exit-IP seat；
- 各邮箱域名 cap 的总和不少于 100。

当前源码没有“邮箱域名并发上限”这个概念；上述 `domain_cap=1` 是待数据验证的初值，不能假定一个 domain 可安全承载 100 个活跃注册。

### 3.2 Scheduler 规则

1. Python 先创建轻量 task；**接近 Go admission 才 lease** 邮箱/代理，不能在长队列时持有 lease。
2. Python 提交 immutable grant：`task_id`、`attempt_id`、email/resource keys、proxy snapshot、lease fence/expiry、预检 exit IP、profile 或 profile seed、deadline。
3. Go 持久化 job 后，按 global/proxy/mailbox/domain seat 原子 admission；不足时保持 `queued`，不接受无限内存 channel。
4. 每个 runnable job 只有一个 FSM owner；任何状态转换在 SQLite transaction 内递增 `state_version`。
5. job 进入 `waiting_for_otp` 时先 checkpoint，再释放协议 worker slot；OTP 到达后重新入 runnable queue。
6. 任意终态、取消或不可恢复错误必须关闭 job transport 的 idle connections。

### 3.3 Context、deadline 与 goroutine 约束

```text
jobCtx = context.WithDeadline(daemonCtx, min(request.deadline, daemonHardMax))
```

- 所有 HTTP、Sentinel、SDK realm、DB、queue wait、timer、取消都使用 `jobCtx`。
- 使用 `context.WithCancelCause` 保存 `user_cancel`、`deadline_exceeded`、`daemon_shutdown`、`transport_error`。
- 建议 stage deadline：proxy selection ≤300s；单 HTTP 请求 ≤30s；控制面请求 ≤60s；OTP ≤200s 且不超过 root 剩余时间。
- Sentinel PoW 循环每 5,000 次检查 context；不能把不可取消的 500,000 次计算塞进一个无界 goroutine。
- OTP waiting 只存状态，不开 100 个 Go mailbox poll goroutine。Python mailbox owner 继续利用现有 message baseline 取码。

### 3.4 每 job 隔离对象

以下对象不得进全局变量、`http.DefaultTransport` 或共享单例：

```go
type JobRuntime struct {
    JobID        string
    AttemptID    int
    Profile      FingerprintBundle
    Jar          http.CookieJar
    Client       ProtocolClient
    Transport    io.Closer
    Proxy        ProxySnapshot
    DeviceID     string            // server-issued oai-did
    CSRFToken    string
    ContinueURL  string
    OTPChallenge OTPChallenge
    Checkpoint   Checkpoint
}
```

现有 TS `OpenAIClient` 每次 constructor 都 `setGlobalDispatcher(...)`，100 并发时后创建任务可覆盖其他任务的代理；Go 实现必须彻底消除这种全局 transport 状态。

---

## 4. Go 模块与接口边界

```text
go-email-protocol/
  cmd/email-protocol-worker/main.go
  internal/
    api/             # V2 HTTP handlers, auth/capability, long-poll
    admission/       # 100-slot scheduler + resource seat guards
    ledger/          # SQLite schema, encrypted checkpoints, migrations
    job/             # typed FSM, state/version transitions, cancellation
    protocol/        # ChatGPT/OpenAI request builders + continuation dispatcher
    fingerprint/     # profile catalog, Client Hints, profile validation
    transport/       # per-job TLS/HTTP client, proxy dialers, redirect policy
    sentinel/        # requirements, PoW, SDK realm, native VM
    session/         # cookie/session serialization and schema validation
    ratelimit/       # per-origin/per-seat bounded limiter
    telemetry/       # redacted events, metrics, trace ring
    cryptostore/     # at-rest encryption for nonterminal secret checkpoints
  migrations/
  testdata/
    protocol-fixtures/
    sentinel-fixtures/
    transport-fixtures/
```

### 4.1 推荐 Go 依赖选择

| 范围 | 选择 | 原因 |
|---|---|---|
| durable ledger | `database/sql` + `modernc.org/sqlite` | Windows 单二进制、事务状态机与 WAL；独立于 Python resource DB |
| 代理 | `net/http` HTTP(S) proxy；`golang.org/x/net/proxy` SOCKS5 | 显式代理、无全局状态 |
| 浏览器 TLS/HTTP profile | `github.com/bogdanfinn/tls-client` | 已确认公开 API 覆盖 ClientProfile（ClientHello、HTTP/2 settings/pseudo-header order）、独立 CookieJar、`SetProxy`、`WithConnectHeaders`、`WithProxyDialerFactory`；可作为 local HTTP CONNECT bridge 的首选客户端，但必须以 **Go toolchain + module graph + wire fixture** 三元组锁定。已验证的 Go 1.22.12 组合为 `tls-client v1.9.1` / `fhttp v0.5.34` / `utls v1.6.5`；`tls-client v1.15.1` 的 module 声明 `go 1.24.1`，在升级 toolchain 前不能被意外引入 |
| 低层 ClientHello | `github.com/refraction-networking/utls` | 只有在 `tls-client` 无法表达已测 profile 时才使用；uTLS 仅调 ClientHello，不自动补齐 HTTP/2/header/cookie 一致性 |
| Sentinel SDK realm | `github.com/dop251/goja` 或等价纯 Go JS runtime | Go 进程内运行版本钉扎的动态 SDK；每 job realm，不能共享 window/navigator state |
| 指标 | `prometheus/client_golang` | 长驻 worker 的可抓取并发/延迟/泄漏指标 |

**TLS 结论：** 不能只把 Go `User-Agent` 改成 Chrome。Go 的 TLS、ALPN、HTTP/2 settings、header order 也属于实际 wire profile。现有 TS 源码没有显式写出这些参数，不能瞎填；要用基线捕获 fixture 生成 `TransportProfile` catalogue。Go 必须验证远端证书，不能复制现有 Node `DEFAULT_INSECURE_TLS=true`。

### 4.2 指纹库的实际能力与非能力

**有可用的 Go transport 指纹库，但没有一个库能单独等价于完整浏览器身份。** 首选 `tls-client`，理由是它把 `uTLS` ClientHello、HTTP/2 SETTINGS / pseudo-header order 和浏览器 profile 放在同一 per-client transport 中；profiles package 公开 Chrome、Firefox、Brave 等 profile，且可读取 ClientHello 与 HTTP/2 设定。它不是只改 `User-Agent` 的库。

对于本项目的 local bridge，已确认的可用接点是：

| `tls-client` API | 用途 | 本项目约束 |
|---|---|---|
| `WithClientProfile(...)` | 选择已测的 TLS + HTTP/2 profile | `TransportProfile` fixture 与 `FingerprintBundle.browser/userAgent/CH` 必须一致；不能按名称猜测 Chrome 版本 |
| `SetProxy("http://127.0.0.1:<port>")` | 指向 Python local HTTP bridge | 只接收已验证的 BridgeGrant URL；不是上游账密 URL |
| `WithConnectHeaders(...)` | 给 HTTPS CONNECT 增加 `Proxy-Authorization: Bearer <bridge capability>` | capability 只在 CONNECT，bridge 校验后剥离；绝不送往 OpenAI 或上游 proxy |
| `WithProxyDialerFactory(...)` | 当 library 内置 proxy path 无法满足 tunnel/observability/retire 语义时注入 job-local dialer | 仍须保留 `CloseIdleConnections`、deadline、max tunnel 与无 direct fallback 不变量 |

这只解决 **network/transport fingerprint** 与 bridge 接入；仍必须由本项目实现和 checkpoint 的部分包括：`FingerprintBundle` 的 OS/viewport/locale/Client Hints 一致性、server `oai-did`、cookie 生命周期、请求字段矩阵、OTP/continuation FSM、Sentinel 的 browser-like realm 和 Turnstile SDK。`uTLS` 仅作为 `tls-client` 无法表示已捕获 ClientHello 时的低层工具；它本身不提供 HTTP/2、cookie、header order 或 local bridge 生命周期。

**选择规则：** 不引入第二套通用 client（例如另一个 TLS-fingerprint wrapper）作为 fallback。先用 `tls-client` 以 per-job client 配合 BridgeGrant；若某一 fixture 不一致，先记录差异并扩展其 profile/dialer adapter，仍无法表达时才评估 `uTLS` 低层实现。每次升级 module/profile 都跑 TLS、ALPN、HTTP/2、CONNECT header、header order、cookie isolation fixture，不能因为 package 声称“Chrome profile”就上线。

### 4.3 Go toolchain 与 module graph release gate

TLS profile 的可重复性依赖编译器和 transitive module；版本不能只写在设计文档或 YAML。Worker 必须在 `go.mod` / `go.sum` 锁定 `tls-client`、`fhttp`、`utls`，并用 `toolchain` / CI 的 `GOTOOLCHAIN=local` 防止构建机静默换 Go 版本。当前经编译和 local-bridge 行为探针验证的兼容组是 **Go 1.22.12 + `tls-client v1.9.1` + `fhttp v0.5.34` + `utls v1.6.5`**；`tls-client v1.15.1` 声明 `go 1.24.1`，所以要升级它必须先做 Go 1.24.1+ 升级 PR、重新捕获全部 wire fixture，不能仅修改 `go get` 版本。

每个 `TransportProfile` 绑定 `go_version`、完整 module graph hash 与 fixture hash。发布 gate 必须依次执行：锁定 toolchain build、`go mod verify`、transport/bridge 单测、受控 TLS endpoint 对拍、协议 fixture。任一个 hash、Go 版本、module 版本或 wire 结果变化都生成新 profile ID；旧 profile 保持可重放或显式 retire，绝不原地覆盖。

设计阶段实际探针也暴露一个不能忽略的事实：stock `profiles.Firefox_135` 在 `tls-client v1.9.1` 已能匹配该 profile 的 HTTP/2 Akamai settings，但发出的默认请求头仍是 `Go-http-client/2.0` 与 `gzip, deflate, br`。因此 **profile 名称不等于浏览器 HTTP request**；所有 OpenAI request header preset 必须由本项目显式构造、按 endpoint fixture 验证。

---

## 5. Durable V2 daemon 协议

V1 的 `{task_id,email,password,proxy_url,skip_phone,timeout_seconds}`、1 秒轮询、一次 OTP、无远程 cancel 无法支撑 100 jobs。实现时升级为 V2。

```text
GET    /health
POST   /v2/email-register
GET    /v2/email-register/{job_id}?wait_ms=0..30000
POST   /v2/email-register/{job_id}/otp
DELETE /v2/email-register/{job_id}
```

### 5.1 创建请求

```json
{
  "task_id": "task_...",
  "attempt_id": 1,
  "idempotency_key": "opaque-per-attempt-key",
  "request_fingerprint": "sha256:immutable-inputs",
  "email": "...",
  "password": "...",
  "resource_grant": {
    "email_key": "...",
    "proxy_key": "opaque stable resource key; never an upstream credential URL",
    "bridge": {
      "bridge_id": "br_...",
      "url": "http://127.0.0.1:18766",
      "capability": "opaque-256-bit-secret",
      "generation": 1,
      "protocol": "http-connect",
      "expires_at": "2026-07-15T00:15:00Z"
    },
    "lease_fence": 17,
    "lease_expires_at": "2026-07-15T00:15:00Z",
    "exit_ip": "...",
    "expected_country": "JP"
  },
  "profile": { "...": "FingerprintBundle v1" },
  "skip_phone": true,
  "deadline_at": "2026-07-15T00:15:00Z"
}
```

同一 `(task_id, attempt_id, request_fingerprint)` 必须返回同一 job；同 task/attempt 不同 fingerprint 必须 `409`。

`bridge.url` 是 Go 唯一允许使用的代理地址；`bridge.capability` 是仅此 job 可用、不可记录的本地 CONNECT 凭据。创建请求、checkpoint 与状态响应均不得出现上游 `user:password@host:port`。同一 `(task_id, attempt_id, request_fingerprint, bridge.generation)` 的重放必须绑定同一个 daemon job；generation 不同则必须 `409`，防止已取消 job 重新拿到新 bridge。

响应：

```json
{
  "job_id": "jr_...",
  "job_capability": "opaque-256-bit-secret",
  "status": "queued",
  "state_version": 1,
  "stage": "admission",
  "retry_after_ms": 1000
}
```

`job_capability` 必须随 job 一起立即被 Python 持久化；之后 GET/OTP/DELETE 都要同时验证 capability。V2 默认只允许受控 loopback worker；worker endpoint 不能由一次注册请求任意指定。

### 5.2 状态与终态结果

```text
queued -> running -> waiting_for_otp -> running -> succeeded
                 \-> cancelling -> cancelled
                 \-> failed
                 \-> reconcile_required
```

每个响应必须带：

```json
{
  "job_id": "jr_...",
  "status": "running|waiting_for_otp|succeeded|failed|cancelled|reconcile_required",
  "state_version": 12,
  "stage": "email_otp_validate",
  "retry_after_ms": 3000,
  "failure_code": "",
  "retryable": false,
  "registration_may_have_succeeded": false
}
```

成功必须同时具备：

1. 非空 `/api/auth/session` `accessToken` 或 `access_token`；
2. schema-valid、大小受限的 session/storage document；
3. email/account_id/plan_type 可从 token 或 response 解析。

Go 返回 document，不返回 daemon-local `session_state_path` 或 `access_token_file`；Python 是唯一 handoff writer。

### 5.3 OTP 请求

进入 waiting 状态时返回：

```json
{
  "challenge_id": "oc_...",
  "state_version": 8,
  "issued_at": "...",
  "deadline_at": "...",
  "retry_after_ms": 3000
}
```

提交：

```json
{
  "challenge_id": "oc_...",
  "state_version": 8,
  "code": "123456"
}
```

- code 只在内存中使用，日志/trace/ledger 不记录原文；
- stale version、重复 challenge、过期 code 必须返回冲突，不推进 FSM；
- 无法确认 OTP POST 是否到达上游时转 `reconcile_required`，不能盲目重放；
- OTP wait 按 `(provider, mailbox-account/email)` 做 one-flight，沿用 Python 当前 message baseline 语义。

### 5.4 取消与恢复

`DELETE` 必须幂等：收到 cancel 先 durable transition 为 `cancelling`，再取消 root context、关闭 idle connections、记录最后安全 checkpoint。Python 只有收到 confirmed terminal state 才能释放/上报资源；cancel ack 丢失或 daemon 重启中断，状态为 `reconcile_required`。

daemon 启动后扫描所有 nonterminal job：只有 request fingerprint、lease fence、deadline 都仍匹配才可继续；否则保留关联资源 fence，等待 Python reconcile。

---

## 6. 完整注册状态机（主 `--at` 邮箱链路）

### 6.1 固定 endpoint

```text
https://chatgpt.com/
https://chatgpt.com/api/auth/csrf
https://chatgpt.com/api/auth/signin/openai
https://auth.openai.com/api/accounts/authorize/continue
https://auth.openai.com/api/accounts/user/register
https://auth.openai.com/api/accounts/email-otp/send
https://auth.openai.com/api/accounts/email-otp/validate
https://auth.openai.com/api/accounts/create_account
https://chatgpt.com/api/auth/callback/openai?...  (server-issued)
https://chatgpt.com/api/auth/session
```

### 6.2 S0–S14 主 FSM

| ID | 请求/本地动作 | 精确 query/body | response 提取与下一步 |
|---|---|---|---|
| S0 | 建立 attempt context | email、password、immutable profile、empty jar、proxy snapshot | 本地校验后 S1 |
| S1 | `GET https://chatgpt.com/`，follow redirect | 无 body | 从 `chatgpt.com`，fallback `openai.com` CookieJar 读取 server-issued `oai-did` 为 `deviceID`；缺失即失败 |
| S2 | 读 CSRF cookie；缺失则 `GET /api/auth/csrf` | 无 body | `__Host-next-auth.csrf-token` URL decode 后取 `|` 前段；否则 JSON `csrfToken`；缺失失败 |
| S3 | `POST /api/auth/signin/openai`，follow | query: `prompt=login`、`ext-oai-did=deviceID`、`auth_session_logging_id=fresh UUID`、`ext-passkey-client-capabilities=0111`、`screen_hint=login_or_signup`、`login_hint=email`; form: `callbackUrl=https://chatgpt.com/`、`csrfToken`、`json=true` | JSON `url` 非空 → S4 |
| S4 | `GET S3.url`，follow | server-issued URL；无新增 query/body | 2xx → S5；所有 Set-Cookie 留同一 jar |
| S5 | Sentinel `authorize_continue` | 见 §9 | 获得一次性 header → S6 |
| S6 | `POST /api/accounts/authorize/continue` | JSON `{username:{kind:"email",value:email},screen_hint:signupScreenHint}` | `continue_url` → continuation dispatcher |
| S7 | `POST /api/accounts/user/register` | JSON `{password,username:email}`；fresh Sentinel flow=`username_password_create` | `continue_url` → dispatcher |
| S8 | `GET /api/accounts/email-otp/send` | 无 query/body | `continue_url` 通常 `/email-verification` → S9 |
| S9 | durable `waiting_for_otp` | 无 OpenAI 请求 | Python mailbox callback 提交 current challenge code → S10 |
| S10 | `POST /api/accounts/email-otp/validate` | JSON `{code}` | `continue_url` 通常 `/about-you` → S11；断连后状态不明 |
| S11 | `POST /api/accounts/create_account` | JSON `{name,birthdate}`；fresh Sentinel flow=`oauth_create_account` | 优先 `page.payload.url`，否则 `continue_url`；ChatGPT callback → S12 |
| S12 | `GET callbackURL`，follow | 仅 server-issued URL | 2xx 且 callback Set-Cookie 落 jar → S13 |
| S13 | `GET /api/auth/session` | 无 body | `accessToken` 优先，否则 `access_token`；非空 → S14 |
| S14 | durable result / handoff document | 不发上游请求 | access token + jar + profile + session schema 通过 → `succeeded` |

### 6.3 continuation dispatcher（不可缺项）

| continuation / page | Go action |
|---|---|
| `/create-account/password` | S7 |
| `https://auth.openai.com/api/accounts/email-otp/send` | S8 |
| `/email-verification` | S9 → S10 |
| `/about-you` | S11 |
| `https://chatgpt.com/api/auth/callback/openai...` | S12 |
| `/add-phone` | 返回 `phone_verification_required`；不能把 `skip_phone` 当成功或绕过标志 |
| `/add-email` | 只在授权/绑定分支实现 add-email FSM |
| `/mfa-challenge/<id>` | 只在既有账号 reauth 且有明确 TOTP secret 时进入 MFA FSM |
| `/workspace...` 或 Codex consent | workspace/consent FSM |
| 未知 continuation | `unexpected_continuation` + redacted checkpoint；**绝不能当 success** |

### 6.4 S15：session-failure 重新授权

若 S13 无 token，当前 Node 的行为是用**新 client / 新 jar / 新 profile**跑已有账号 Codex OAuth 登录，而不是重复 S7/S11。Go 必须把它建模为独立 reauth attempt：

- `GET /oauth/authorize` 带 PKCE；
- continuation 登录、password、可选 MFA、workspace/consent；
- manual redirect，最多 10 hops；
- callback `code` + `state` 匹配后，按两个 token endpoint 依次 exchange。

若这条补偿链失败，账户创建可能已经发生，结果必须 `registration_may_have_succeeded=true` / `reconcile_required`，不得重新注册 email。

---

## 7. 全量请求字段矩阵

### 7.1 Header presets

`B`（每个普通 browser-like request 的基底）：

```text
user-agent: <profile.userAgent>
accept-language: <profile.acceptLanguage>
sec-ch-ua: <derived from same UA/browser>
sec-ch-ua-full-version-list: <derived from same UA/browser>
sec-ch-ua-mobile: <?0|?1 from isMobile>
sec-ch-ua-platform: <"Windows"|"Android">
sec-ch-ua-platform-version: <derived from profile OS>
sec-ch-viewport-width: <quoted profile.viewportWidth>
```

`J(referer, sentinel?)`：`B` 加：

```text
accept: application/json
content-type: application/json
origin: https://auth.openai.com
referer: <previous Auth page>
sec-fetch-dest: empty
sec-fetch-mode: cors
sec-fetch-site: same-origin
openai-sentinel-token: <fresh token only when protected>
```

| State | Header set | 关键例外 |
|---|---|---|
| S1 homepage | B + `accept-encoding:gzip, deflate, br` + `Sec-Fetch document/navigate/none` | — |
| S2 CSRF fallback | B + `accept:application/json` + encoding + ChatGPT referer + `Sec-Fetch empty/cors/same-origin` | — |
| S3 NextAuth sign-in | B + `accept:*/*` + form content type + ChatGPT origin/referer + `Sec-Fetch empty/cors/same-origin` | — |
| S4 authorize page | B + encoding + ChatGPT referer + `Sec-Fetch document/navigate/same-site` | — |
| S6 signup continuation | J(`auth.../log-in-or-create-account?usernameKind=email`, Sentinel) | exact signup body |
| S7 register password | J(`auth.../create-account/password`, Sentinel) | — |
| S8 send email OTP | `accept` + create-password referer + UA + Accept-Language + **six Client Hints** | 没有 source-defined Origin、Content-Type、Sec-Fetch |
| S10 validate OTP | `accept` + JSON content type + Auth origin + email-verification referer + **UA only** | 没有 Accept-Language、Client Hints、Sec-Fetch |
| S11 create account | J(`auth.../about-you`, Sentinel) | — |
| S12 callback | HTML `accept` + about-you referer + B | 没有 source-defined Sec-Fetch |
| S13 session | B + `accept:application/json` + `Sec-Fetch empty/cors/same-origin` + ChatGPT referer | — |

Header key casing/order、HTTP version、TLS/HTTP2 parameters不是 TS 源码显式固定的字段；用 transport fixture 决定，不能从 `new Headers()` 推断。

### 7.2 每个 endpoint 的 query/body 字段

| Endpoint | 方法 | query/body（完整） |
|---|---|---|
| `chatgpt.com/api/auth/csrf` | GET | none |
| `chatgpt.com/api/auth/signin/openai` | POST | query: `prompt`、`ext-oai-did`、`auth_session_logging_id`、`ext-passkey-client-capabilities`、`screen_hint`、`login_hint`; form: `callbackUrl`、`csrfToken`、`json=true` |
| `auth.../authorize/continue` signup | POST | `{username:{kind:"email",value:email},screen_hint}` |
| `auth.../user/register` | POST | `{password,username}` |
| `auth.../email-otp/send` | GET | none |
| `auth.../email-otp/validate` | POST | `{code}` |
| `auth.../create_account` | POST | `{name,birthdate:"YYYY-MM-DD"}` |
| `chatgpt.com/api/auth/session` | GET | none |
| `sentinel.../req` | POST | `{p:requirementsProof,id:oaiDid,flow}` |
| `auth.../oauth/authorize`（reauth） | GET | `client_id`、`response_type=code`、`redirect_uri`、`scope`、`state`、`code_challenge`、`code_challenge_method=S256`、`prompt`、`id_token_add_organizations=true`、`codex_cli_simplified_flow=true`；direct signup 另加 `screen_hint`、`login_hint` |
| OAuth token endpoint | POST form | `grant_type=authorization_code`、`client_id`、`code`、`redirect_uri`、`code_verifier` |
| password verify（reauth） | POST | `{password}` + Sentinel flow=`password_verify` |
| MFA verify（reauth） | POST | `{id,type:"totp",code}` |
| add-phone send / validate | POST | `{phone_number}` / `{code}` |
| add-email send | POST | `{email}` |
| workspace select | POST | `{workspace_id}` |
| consent fallback | POST form | `consent=true` |

### 7.3 Cookie、CSRF、OAuth 与 random 事务字段

| 字段 | 产生方式 | 生命周期 / 约束 |
|---|---|---|
| 全部 CookieJar cookies | response `Set-Cookie` | job-local、跨 `chatgpt.com`/`auth.openai.com` redirect；必须原样按域/路径规则发送，不能 job 共享 |
| `oai-did` / `deviceID` | S1 server Cookie | server-issued、job/session sticky；Sentinel JSON `id` 与 NextAuth `ext-oai-did` 用它；不可与 locally generated `profile.id` 混淆 |
| `__Host-next-auth.csrf-token` | Cookie 或 S2 JSON | URL decode、取 `|` 前段；只用于 S3 当前 session |
| `oai-client-auth-session` | Auth Cookie | reauth workspace 使用；dot 分段首段 URL-safe base64 JSON 解析 workspace |
| `state` | CSPRNG URL-safe string | OAuth transaction sticky；callback 必须完全相等 |
| `code_verifier` | CSPRNG URL-safe string | OAuth transaction sticky；token exchange 使用 |
| `code_challenge` | `base64url(SHA-256(code_verifier))` | OAuth authorize request 派生值 |
| `auth_session_logging_id` | fresh UUID | 每次 S3 打开 sign-in page 生成，不 checkpoint 重放 |
| `name` / `birthdate` | account input | 账户创建前一次生成并 checkpoint；断连后不可无判断地重新生成/发送 |
| `continue_url` / `page.payload.url` | 上游 response | job-local mutable continuation；create-account/MFA 优先 `page.payload.url` |
| callback URL / `code` | 上游 redirect | 单次敏感值；Codex callback 还必须校验 `state` |
| email OTP | Python mailbox | challenge-local transient；绝不记录到日志、metric、trace、长期 ledger |

---

## 8. 全量设备画像字段（FingerprintBundle v1）

### 8.1 持久 schema

```json
{
  "version": 1,
  "id": "uuid",
  "family": "desktop|mobile",
  "browser": "chrome|edge",
  "os": "windows|android",
  "os_version": "...",
  "user_agent": "...",
  "locale": "...",
  "languages": ["..."],
  "accept_language": "...",
  "timezone_id": "...",
  "viewport_width": 0,
  "viewport_height": 0,
  "screen_width": 0,
  "screen_height": 0,
  "outer_width": 0,
  "outer_height": 0,
  "device_scale_factor": 1,
  "hardware_concurrency": 0,
  "device_memory": 0,
  "js_heap_size_limit": 0,
  "platform": "...",
  "vendor": "...",
  "max_touch_points": 0,
  "has_touch": false,
  "is_mobile": false,
  "color_depth": 24,
  "pixel_depth": 24,
  "transport_profile_id": "chrome-profile-version"
}
```

### 8.2 字段清单、生成与一致性

| 字段 | 当前 TS 来源 | wire / runtime 用途 | Go 规则 |
|---|---|---|---|
| `id` | UUID | phone-first OAuth 的 `device_id`/`ext-oai-did`；与 server `oai-did` 不同 | job-sticky；不能复用 default profile ID |
| `family` | desktop/mobile | 约束 browser/OS/viewport/touch 组合 | 只从 catalog 选择合法 tuple |
| `browser` | Chrome/Edge；mobile 仅 Chrome | UA、Sec-CH brand、transport profile | 必须与 UA/CH/TLS profile 一致 |
| `os`,`os_version` | Windows 10 或 Android 12–15 | UA、Sec-CH platform version、browser context | 由 catalog 整套生成 |
| `user_agent` | browser/version/build/model | 所有 B headers、Sentinel req/payload | job-sticky；禁止中途换 UA |
| `locale`,`languages`,`accept_language`,`timezone_id` | 预定义 coherent locale tuple | Accept-Language、Sentinel language/browser realm | 作为一组持久化；不能随机拼接 |
| `viewport_width`,`viewport_height` | predefined viewport pairs | `sec-ch-viewport-width`、Sentinel inner size | 与 screen/DPR 同 tuple |
| `screen_width`,`screen_height` | predefined screen pairs | Sentinel payload/window/screen | 与 viewport 同 tuple |
| `outer_width`,`outer_height` | desktop browser-chrome offset；mobile=viewport | Sentinel window realm | 不跨 job 改变 |
| `device_scale_factor` | viewport tuple | Sentinel DPR/browser realm | 与 mobile/desktop tuple 一致 |
| `hardware_concurrency` | family-specific finite list | Sentinel payload、navigator | job-sticky |
| `device_memory` | family-specific finite list | navigator/SDK realm | job-sticky |
| `js_heap_size_limit` | family-specific finite list | Sentinel payload | job-sticky |
| `platform` | `Win32` / `Linux armv8l` | navigator/SDK realm | 与 OS/mobile 一致 |
| `vendor` | `Google Inc.` | navigator/SDK realm | catalog value |
| `max_touch_points`,`has_touch`,`is_mobile` | desktop 0/false/false；mobile 5/10,true,true | `sec-ch-ua-mobile`、navigator/window realm | 不得与 UA/family 冲突 |
| `color_depth`,`pixel_depth` | 24/24 | Sentinel `screen` realm | catalog value |
| `transport_profile_id` | Go 新增 | TLS/ALPN/H2/header-order catalogue | 不可仅依 UA 推断；必须实测对应 fixture |

当前 TS profile 生成范围：desktop 约 68%，mobile 约 32%；desktop viewport 有 1365×768、1440×900、1536×864、1600×900、1710×1067、1920×1080；mobile 有 360×800、390×844、393×873、412×915、430×932。Go profile catalogue 要保留合法组合，但不能继续用“随机 UA + 未验证 TLS profile”这种不受约束组合。

### 8.3 派生 Client Hints（全部）

由**同一个** profile/UA 一次派生并缓存：

```text
sec-ch-ua
sec-ch-ua-full-version-list
sec-ch-ua-mobile
sec-ch-ua-platform
sec-ch-ua-platform-version
sec-ch-viewport-width
```

- Chrome brand list：`Google Chrome`、`Chromium`、`Not.A/Brand`；
- Edge brand list：`Microsoft Edge`、`Chromium`、`Not.A/Brand`；
- major/full version 都从同一 UA 解析；
- Windows 当前 TS `sec-ch-ua-platform-version` 是 `"15.0.0"`，即使 desktop profile OS version 是 `10.0`。Go 第一版的 fixture 要保留这个当前可观察行为，不能擅自“修正”为另一值；改动需独立 canary。

---

## 9. 全量 Sentinel / Turnstile 字段

### 9.1 SentinelEnv（29 个字段）

`SentinelEnv` 必须从 FingerprintBundle 构造；分层如下：

| 类别 | 字段 |
|---|---|
| identity / locale | `userAgent`、`language`、`languages`、`locale`、`timezoneId` |
| geometry | `screenWidth`、`screenHeight`、`innerWidth`、`innerHeight`、`outerWidth`、`outerHeight`、`devicePixelRatio` |
| navigator capability | `hardwareConcurrency`、`deviceMemory`、`jsHeapSizeLimit`、`platform`、`vendor`、`maxTouchPoints`、`hasTouch`、`isMobile` |
| screen capability | `colorDepth`、`pixelDepth` |
| per Sentinel call | `timeOrigin=Date.now()` |
| compatibility release | `scriptSources`、`buildHash`、`documentKeys`、`windowKeys`、`searchParamKeys` |

job-sticky：除 `timeOrigin` 和 Sentinel call 临时随机值外的 profile 字段。  
release-sticky：`scriptSources/buildHash/documentKeys/windowKeys/searchParamKeys`，由受控 Sentinel release manifest 提供；SDK 更新要先进入 fixture pipeline。

### 9.2 requirements request 与 enforcement header

`T1` requirements request：

```http
POST https://sentinel.openai.com/backend-api/sentinel/req
Content-Type: application/json
User-Agent: <profile.user_agent>

{"p":"<requirements proof>","id":"<server oai-did>","flow":"authorize_continue|password_verify|username_password_create|oauth_create_account"}
```

response 中消费的字段：

```json
{
  "token": "...",
  "proofofwork": {"required": true, "seed": "...", "difficulty": "..."},
  "turnstile": {"dx": "..."}
}
```

`T2` 立即发送到受保护 Auth POST 的 header 值为 JSON 字符串：

```json
{
  "p": "<PoW enforcement proof or null>",
  "t": "<Turnstile dx result or null>",
  "c": "<requirements token>",
  "id": "<server oai-did>",
  "flow": "<same flow>"
}
```

此 token **每个 protected action 新建**，不跨 job、不跨 flow、不跨 challenge 缓存或复用。

### 9.3 requirements / PoW payload（精确 25 项，index 0–24）

当前 `collectFingerprintData()` 输出不是 26 项，而是以下 25 项。Go 需要 fixture 对齐**数组顺序、JSON 编码、base64、hash、比较逻辑**：

| index | 值 |
|---:|---|
| 0 | `screenWidth + screenHeight` |
| 1 | `new Date().toString()` 等价值 |
| 2 | `jsHeapSizeLimit` |
| 3 | 初始随机数，PoW 每次尝试覆盖为 attempt counter |
| 4 | `userAgent` |
| 5 | `scriptSources` 中随机项 |
| 6 | `buildHash` |
| 7 | `language` |
| 8 | `languages.join(",")` |
| 9 | 初始随机数，PoW 每轮覆盖为 elapsed milliseconds |
| 10 | 随机 navigator property：`userAgent−...` / `language−...` / `hardwareConcurrency−...` |
| 11 | `documentKeys` 随机项 |
| 12 | `windowKeys` 随机项 |
| 13 | `performanceNow()` |
| 14 | 当前 Sentinel generator 的 fresh UUID `sid` |
| 15 | `searchParamKeys.join(",")` |
| 16 | `hardwareConcurrency` |
| 17 | `timeOrigin` |
| 18 | literal `0` |
| 19 | literal `1` |
| 20 | literal `1` |
| 21 | literal `0` |
| 22 | literal `0` |
| 23 | literal `0` |
| 24 | literal `1` |

算法约束：

1. fresh generator 生成 requirements seed、SID 与随机项；
2. requirements proof 前缀 `gAAAAAC`；
3. enforcement PoW 按 requirements `seed`/`difficulty`，最多 500,000 attempts；
4. 每次编码 payload，计算 reference `sentinelHashHex(seed + encoded)`，按 reference 的 prefix 比较逻辑判定；
5. enforcement proof 前缀 `gAAAAAB`；
6. 每 5,000 attempts 让出/检查 context；最大次数后不能用假 token，返回 typed Sentinel failure。

### 9.4 SDK / Turnstile 的完整实现要求

若 requirements 有 `turnstile.dx`，不能只做 PoW。Go implementation 必须：

1. 按 `buildHash` 获取、hash pin、版本化保存 SDK fixture；
2. 每 job 创建独立 JS realm（Goja 或等价 Go runtime），注入 job-specific `window`、`document`、`navigator`、`screen`、`location`、storage、`crypto`、timing 与 profile；
3. location 的 frame URL 与 search 参数由 `buildHash`/`searchParamKeys` 产生；
4. 支持 SDK hook 执行；SDK source 或 hook drift 立即 `protocol_incompatible`，不能静默降级；
5. SDK 无法得到结果时，Go 原生实现 `dx` 的 base64 decode → requirements-proof key XOR → JSON instruction program → VM；
6. 每个 realm/VM 完整 job 隔离；不能复用捕获上一个 profile/window 的 cached runner；
7. 动态 Sentinel 资产的 hash、build、fixture coverage 是发布 gate。

参考 TS browser Sentinel 模式还会设置：

```text
sentinel.openai.com cookie: oai-did=<server deviceID>
path=/; secure=true; httpOnly=false; sameSite=None
frame: https://sentinel.openai.com/backend-api/sentinel/frame.html?sv=<buildHash>
context: viewport/screen/DPR/locale/timezone/UA/mobile/touch/accept-language/sec-ch-ua-mobile
```

现有 TS `sentinel-browser.ts` 有 module-global browser/context/page promises，不能作为 100 job 共享实现。Go 版本必须每 job realm/context 隔离；任何 SDK template cache 只能缓存不含 profile/cookie/state 的编译产物。

---

## 10. Transport 指纹、代理与 redirect

### 10.1 TransportProfile v1

```json
{
  "id": "chrome-<version>-h2-v1",
  "baseline_capture_id": "browser-capture:<redacted-id>",
  "browser": "chrome|edge",
  "browser_version": "...",
  "go_version": "go1.22.12",
  "module_graph_fixture": "sha256:...",
  "tls_client_hello_fixture": "sha256:...",
  "tls_extension_order_fixture": "sha256:...",
  "alpn": ["h2", "http/1.1"],
  "http2_settings_fixture": "sha256:...",
  "http2_connection_flow_fixture": "sha256:...",
  "http2_pseudo_header_order": [":method", ":authority", ":scheme", ":path"],
  "header_presets": {
    "document_navigation": "sha256:...",
    "same_origin_fetch": "sha256:...",
    "cross_origin_oauth": "sha256:..."
  },
  "response_content_encoding_fixture": "sha256:...",
  "redirect_max_hops": 10,
  "certificate_validation": true
}
```

这是 Go 新增 schema。TS 参考实现明确了 UA、Client Hints、Cookie、redirect 行为；**未**显式固定 TLS cipher/extension、ALPN、HTTP/2 settings 或 header order。因此这些值必须来自受控抓包 fixture，而非猜一个 JA3 或把随机 UA 贴到任意 `Chrome_*` TLS profile。

### 10.1.1 Wire 对拍协议：先测差异，再写 custom ClientHello

`curl_cffi firefox135` 的实测笔记证明：stock `Firefox_135` 的 cipher、curve 与 HTTP/2 Akamai settings 虽可相同，TLS extensions 仍会少 `session_ticket (35)` 与 `psk_key_exchange_modes (45)`，导致 JA3、JA4、peetprint 不同；只在两个**已捕获的插入位置**补齐后才对拍成功。这是有效的方法论，不是本项目可以直接复制的目标 profile：当前 Go 邮箱协议必须以它自己的 Chrome/Edge + `FingerprintBundle` 基线为准，禁止把 Firefox、PIX header 或支付业务字段移植进 OpenAI 请求。

对每个候选 `TransportProfile` 按以下顺序执行：

1. 用同一 browser/version、同一 OS 画像捕获基线；持久化 ClientHello 的 cipher、extension ID/顺序/参数语义、ALPN，HTTP/2 SETTINGS 顺序、connection WINDOW_UPDATE、priority、pseudo-header order，以及按 endpoint class 分开的请求头原始顺序和值。
2. Go 在无代理的受控 TLS echo endpoint 对拍该基线。JA3/JA4/Akamai/peetprint 只作压缩的报警摘要；验收比较规范化的原始字段。排除每次握手必变的 `client_random`、`session_id` 和 key-share 公钥材料，不能把随机字节当 mismatch。
3. 若 stock profile 不同，复制最接近 profile 的完整 spec，只对 fixture 指出的 extension **缺失、多余或错序**最小修改；每一项记录 baseline diff、插入位置和 profile ID。只有基线顺序固定时才设 `RandomExtensionOrder=false`；不得为方便测试改变一个实际会随机化的 browser 行为。
4. 用 `fhttp.HeaderOrderKey` / `PHeaderOrderKey` 写入 endpoint class 的 header preset。`sec-fetch-user` 只属于有用户激活的 document navigation，不能复制到 API fetch；`zstd` 只能在该 preset 的 response decoder、header fixture 和真实响应读取测试都通过后声明。业务 header 只能在 preset 定义的位置覆盖，不能让 map 遍历决定顺序。
5. 用同一 Go/module lock 通过 fake authenticated bridge 再跑一次。CONNECT capability 必须只出现在 CONNECT；bridge 接受后，origin 侧的捕获必须证明 capability 不存在。任何 mismatch 生成新的 profile 或 fail closed，不能降级到 `net/http`、direct connect 或未认证 bridge。

探针需要至少记录 TLS/HTTP endpoint URL、capture time、profile ID、Go/module hash、网络路径是否经过 bridge 和脱敏结果 hash；不记录 raw cookie、access token、bridge capability 或完整 upstream proxy URL。

### 10.2 每 job transport 构造

```text
one job
  = one CookieJar
  + one tls-client HttpClient / isolated Transport
  + one proxy URL/dialer snapshot
  + one TLS/HTTP TransportProfile
  + one redirect policy
```

约束：

- HTTP(S) proxy 与 SOCKS5 需要显式支持；SOCKS4/4a 没有经过 Go adapter fixture 就拒绝或要求 Python 同机 bridge，禁止悄悄 direct connect；
- `MaxConnsPerHost=2`、`MaxIdleConnsPerHost=1` 是初始防 fan-out 值；FSM 正常串行时每 job 同时只有一个协议请求；
- root cancellation/终态调用 `CloseIdleConnections()`；
- normal stages follow redirects；OAuth manual redirect walker 最多 10 hops；
- 网络异常仅对**明确幂等或可 reconcile**的 stage 做 bounded retry（参考 Node：最多 3 次、1.5s × attempt）；HTTP status 不做盲重试；
- S7/S10/S11/callback 出现“请求已发但响应丢失”时标记 `ambiguous_after_send`，停止自动 replay。

### 10.3 代理一致性

- Python proxy preflight 选择的 `proxy_url`、expected country、exit IP、lease fence 是 immutable job grant；
- 不允许 job 重新挑代理或 fallback direct；
- Python 当前 credential bridge 常监听 `127.0.0.1`，所以 Go worker 首期必须与 Python **同主机 loopback**；远程 Go worker 不能直接使用 Python 主机的 loopback URL；
- 同一已知 exit IP 不并发，沿用 resource pool 的已有排他语义。

### 10.4 本地网络 bridge：现状、目标与 Go 接入规范

#### 当前实现的事实和边界

当前 `CredentialProxyRuntime.start_browser_bridge()` 在发现上游 URL 含账号或密码时，按上游类型启动一个**本机 HTTP proxy**：

| 上游类型 | local bridge 对上游所做的事 | Go 收到的地址 |
|---|---|---|
| `socks5://user:pass@host:port` / `socks5h` | RFC 1929 用户名/密码认证；CONNECT 的目标以 SOCKS domain 发送，由上游解析目标 DNS | `http://127.0.0.1:<ephemeral-port>` |
| `http(s)://user:pass@host:port` | 去掉调用方 `Proxy-Authorization`，注入上游 Basic 认证并原样转发请求；**当前 `https` 分支仍是 plain TCP、没有 TLS wrap，不能视为已验证的 HTTPS-proxy 支持** | `http://127.0.0.1:<ephemeral-port>` |

- listener 固定 `AF_INET 127.0.0.1:0`、`listen(64)`；它不是 SOCKS listener，也不监听 LAN、IPv6 loopback 或远程主机；因此 Go worker 必须在**同一主机、同一网络 namespace**运行。Docker、WSL 或远程 Go 进程不能把 Python 主机的 `127.0.0.1:<port>` 当成自己的 bridge。
- 当前 bridge 支持 HTTP CONNECT 和普通 HTTP forward。邮箱协议首期只应使用 HTTPS 目标的 CONNECT；普通 HTTP forward 不应成为 capability 认证后绕过 header 隔离的旁路。
- 当前实例每个 Python runtime 独占 listener，accept 后为每个连接建 daemon thread；构造后已 bind/listen，`start()` 没有 readiness handshake；`cleanup()` 仅关闭 listener，不等待或追踪既有 relay/upstream socket。`last_error` 也只是 runtime 内的最后字符串，不会以 HTTP error 返回给 Go。
- 当前 V1 在 Python 子进程中持有 runtime，预检后把 local URL 放入 `POST /v1/email-register`，并在本地轮询 return/error 的 `finally` 中 cleanup。Python timeout、task stop 或进程被杀都没有 V1 `DELETE`/terminal acknowledgement；daemon 因而可能比 child 和 bridge 活得更久。这个 V1 生命周期**不得**作为 100 并发生产语义。

#### 目标拓扑：Python bridge owner，Go 只做 bridge client

```text
Python TaskService / BridgeManager (same Windows user + host)
  ├─ owns selected upstream credential, preflight result and resource lease
  ├─ maintains one bounded local HTTP CONNECT listener
  └─ BridgeGrant{bridge_id, capability, generation, expiry} -> Go V2 job

Go job-local transport
  └─ HTTP CONNECT + per-job bridge capability -> 127.0.0.1:<bridge-port>
       └─ BridgeManager resolves capability -> immutable upstream proxy snapshot
            └─ SOCKS5 RFC1929 or HTTP Basic upstream -> Internet
```

决定：为 100 active jobs 把现有的「每个 child 一个 ephemeral listener + 不受控 thread」演进为 Python 常驻 `BridgeManager` 的**一个 bounded listener、每 job 一份 capability 映射**。这样 Go 永远没有上游代理账号密码，Python 仍保留资源/preflight/cooldown owner 身份；100 个 job 不会同时创建 100 个 listener 或让 100 个 Python runtime 各自失去清理责任。

`BridgeManager` 最小契约：

1. `Allocate(grant)` 在 Python 预检成功、Go V2 create 前注册 `(bridge_id, generation, capability_hash) -> {upstream snapshot, lease fence, expiry}`。capability 只能 hash 后留在 bridge map/日志；原值只随加密 V2 request 交给 Go。
2. local endpoint 只接受 `CONNECT host:port HTTP/1.1`；先校验 `Proxy-Authorization: Bearer <capability>` 与未过期 generation，成功后**剥离该 header**，再执行上游 SOCKS5/HTTP 认证。无效/过期/非 CONNECT 返回明确 407/403/405，不可静默直连或回退上游默认代理。
3. `Retire(bridge_id, generation)` 先停止新 CONNECT，再等待该 job 的 active tunnels 到 `bridge_drain_timeout`；超时强制关闭，并把 `bridge_generation_retired` 记入 daemon reconcile evidence。不得仅关 listener 后假定 relay 已结束。
4. bridge 必须是有界 I/O：单 listener、受限 accept queue、每 job `max_tunnels=2`、全局 active tunnel cap、header 上限 64 KiB、dial/read/write deadline 30s、显式 goroutine/socket wait group。bridge 不能为每个 client 建不受限 thread。
5. bridge 与 daemon control API 都只绑定 loopback，运行在同一 Windows 用户身份；control API 使用安装时生成、ACL 保护的 `worker_control_key`。loopback 不是授权边界，不能保留当前「任意本地进程可借 credential egress」的无认证行为。

远程 worker 不在本阶段。将来若必须跨 host，只能部署一个与 Go 同机的受控 bridge agent，并用 mTLS、bridge capability、host identity 和可审计 tunnel；**禁止**暴露 `0.0.0.0`、端口映射 Python 的 loopback，或把原始 upstream credentials 复制给 Go。

#### Go transport 的硬接口

Go 为每个 `JobRuntime` 解析并验证 `BridgeGrant`，然后把它作为唯一 proxy dial path：

```go
type BridgeGrant struct {
    ID         string
    URL        string    // exact http://127.0.0.1:<port>
    Capability []byte    // never log/metric/checkpoint plaintext
    Generation uint64
    ExpiresAt  time.Time
}

type BridgeDialer interface {
    RoundTrip(*http.Request) (*http.Response, error) // HTTPS only, CONNECT required
    CloseIdleConnections()
}
```

构造 gate（每次 create/recover 都做）：

1. URL 必须为 `http`、host 精确为 `127.0.0.1`、显式有效 port；拒绝 `localhost`、`::1`、私网/LAN host、proxy URL userinfo、PAC、SOCKS4/4a 与空 bridge。现有 Python listener 只保证 IPv4 `127.0.0.1`，因此不能把 IPv6 当兼容路径。
2. transport 必须对 HTTPS 发 CONNECT，并仅在 CONNECT 里添加 bridge `Proxy-Authorization: Bearer …`。标准库路径使用 `net/http.Transport.Proxy` + `ProxyConnectHeader`；`tls-client` 的公开 API 已提供 `SetProxy`、`WithConnectHeaders` 与 `WithProxyDialerFactory`，因此它是带指纹的首选 bridge client。仍必须在 transport fixture/真实 fake-bridge integration test 中确认目标版本实际只在 CONNECT 发送 capability、未把它泄漏到 origin/upstream。若不能满足，不得退化到无认证 local proxy；改用能实现该行为的 per-job transport adapter。
3. HTTP request headers、CookieJar、TLS profile 和连接池仍是 job-local；bridge capability 绝不能进入 OpenAI origin header、upstream request、redirect URL、trace、metric label、error text 或 session document。
4. `MaxConnsPerHost=2`、每 job `max_tunnels=2`、终态/cancel/bridge-retired 都调用 `CloseIdleConnections()`；bridge close/error 导致的 request 只可按状态机的 idempotency/reconcile 规则处理，绝不可 direct fallback。
5. daemon 在 durable checkpoint 中仅保存 `bridge_id`、generation、expiry、capability 的加密 form；restart 先向 Python reconcile bridge generation。不存在、过期或已 retired 的 bridge 只能转 `reconcile_required`，不能以旧 job 重开上游连接。

#### V2 交互、取消与时序

```text
1. Python lease + select + preflight upstream through BridgeManager
2. Python Allocate -> BridgeGrant(generation N) -> POST /v2/email-register
3. Go durable-commits {job_id, capability, bridge N} before running FSM
4. Python durable-commits {job_id, job_capability, bridge N} before long-poll
5. Go uses only bridge N; OTP wait retains bridge/lease fence
6. cancel/timeout: Python DELETE(job_id, job_capability) -> waits terminal ACK
7. ACK terminal: Go closes idle connections -> Python Retire(N) -> report/release lease
8. DELETE/ACK lost, Python child crash, daemon restart: retain fence/bridge state; reconcile before report/release
```

Python 的任务进程被 `SIGKILL`/process-tree stop 时，OS 会关闭当前 ephemeral listener；这正是要在 G1 前迁移到常驻 `BridgeManager`、持久化 `job_id/capability/bridge generation` 的原因。新路径中 **任何** Python 本地 timeout 都只是取消意图，绝不能先 cleanup bridge 或报告 resource，再猜测 Go 已停止。

#### Bridge 可观测性与发布门禁

新增 redacted metrics（`bridge_id`、URL、capability、upstream host 都不能作 label）：

```text
email_protocol_bridge_grants{outcome}
email_protocol_bridge_tunnels{state}
email_protocol_bridge_connect_total{outcome,upstream_kind}
email_protocol_bridge_auth_failures_total
email_protocol_bridge_retire_seconds
email_protocol_bridge_forced_close_total
email_protocol_bridge_active_generations
```

发布前必须用 fake HTTP-auth 和 RFC1929 SOCKS5 upstream 做真实 CONNECT integration test：验证每个 job 只到自己的 upstream credential/target；错误 bridge capability 得 407；过期/retired generation 不可重连；cancel、daemon restart、Python child crash 后无 orphan tunnel；100-job barrier 后 listener、tunnel、goroutine/socket 全部回到基线。真实 OpenAI 注册不用于 bridge 压测。

---

## 11. Checkpoint、session 与 secret 处理

### 11.1 Durable JobRecord

```text
job_id, task_id, attempt_id, idempotency_key_hash, request_fingerprint,
status, state_version, stage, normalized_continuation,
created_at, updated_at, deadline_at,
profile_id, transport_profile_id,
email_resource_key, proxy_resource_key, lease_fence, exit_ip_hash,
challenge_id, challenge_issued_at, challenge_deadline,
retry_after_ms, failure_code, retryable, registration_may_have_succeeded,
encrypted_checkpoint, redacted_trace_tail
```

`encrypted_checkpoint` 仅在运行中保存必需密钥材料：密码、cookie jar、server `oai-did`、CSRF、continuation、account input、session。使用安装时生成并受 ACL 保护的 key 加密；metric/log/事件中只允许 hash、stage、status、错误类别。

### 11.2 Versioned session result

```json
{
  "schema_version": 1,
  "email": "...",
  "access_token": "...",
  "account_id": "...",
  "plan_type": "free",
  "obtained_at": "...",
  "profile": {"...": "FingerprintBundle v1"},
  "cookies": [{"name":"...","value":"...","domain":"...","path":"/","expires":0,"httpOnly":true,"secure":true,"sameSite":"Lax"}],
  "origins": []
}
```

验证：JSON object、cookie 字段类型/大小、profile schema、access token 非空且 JWT claims 可解析、总文档大小上限。Python 将它写入现有 task 临时目录和 handoff；Go 不返回文件路径。

---

## 12. 错误分类与 retry 语义

| 分类 | 动作 |
|---|---|
| `transport_transient` | 只重试明确幂等且未发副作用的 stage；bounded jitter；不重新 lease |
| `proxy_preflight_failed` / `proxy_exit_country_mismatch` / `proxy_auth_or_transport` | 终止本 attempt，Python 依据结构化 evidence 进行资源 cooldown/report；不 direct connect |
| `rate_or_risk` | stop attempt，保留 redacted evidence，走 Python cooldown policy |
| `otp_timeout` / `otp_invalid` / `otp_stale_submission` | 当前 challenge/version 处理；不能无限 sleep 或重发旧 code |
| `email_already_used` | email 进入 consumed/fenced；不再次 register |
| `phone_verification_required` | authorized policy terminal；不做绕过 |
| `unexpected_continuation` / `protocol_incompatible` / Sentinel SDK drift | fail closed，冻结该 release/canary，保存 redacted fixture evidence |
| `ambiguous_after_send` / `create_or_response_lost` / `token_missing_after_callback` | `reconcile_required`，`registration_may_have_succeeded=true`，禁止自动创建第二账户 |
| `cancelled` / `deadline_exceeded` | 先远程/本地取消确认；不明结果保持 resource fence |

---

## 13. 100 并发可观测性

禁止用 email、OTP、token、完整 proxy URL、job ID 作为 metric label。

必须提供：

```text
email_protocol_jobs{state,stage}
available_slots
admission_rejected_total{reason=global|queue|proxy|mailbox|domain|lease}
stage_duration_seconds
job_duration_seconds
job_terminal_total{failure_code,retryable,ambiguous}
proxy_preflight_total{outcome,country}
proxy_seat_in_use
otp_challenge_total{outcome}
otp_wait_seconds
otp_submission_total{outcome}
checkpoint_total{operation,outcome}
cancellation_latency_seconds
inflight_http_requests
active_transports
queue_depth
go_goroutines
go_memstats_*
```

每 job 只保留 bounded redacted trace tail；建议上限 64 KiB，100 job 不得积累无界 stdout/stderr 或完整 HTTP body。

---

## 14. 实施顺序：完整 Go 协议路径

| 阶段 | 交付 | 退出 gate |
|---|---|---|
| G0 | fixture recorder：从 TS 参考链提取脱敏 request shape、response continuation、cookie metadata、Sentinel inputs、transport observation | 每个 S/T/L/C state 有 fixture；无 secret 落盘 |
| G1 | Go ledger、V2 API、100-slot admission、per-job jar/transport、OTP challenge/cancel/restart | 100-job synthetic load 无 job/cookie/proxy/OTP cross-talk；crash recovery 绿 |
| G2 | Go 主 S0–S14、全部 headers/query/body、continuation dispatcher、session schema | fixture request builders 与 reference 逐字段对齐；未知 continuation fail closed |
| G3 | Go Sentinel requirements + PoW + cancellation | PoW fixture 对齐；100 job 下 CPU pool 有界、可取消 |
| G4 | Go SDK realm + native Turnstile VM | 每个 pinned build 的 `dx` fixture 对齐；无 Node runtime；SDK drift 阻断发布 |
| G5 | Go S15 reauth / PKCE / callback / workspace | callback state/verifier/10-hop/token fallback tests 绿 |
| G6 | 真实 canary：1 → 5 → 10 → 25 → 50 → 100，资源 cohort 互斥 | access token **且** session document 成功率不低于 TS baseline；无 orphan/reconcile backlog |

不允许同一注册任务双跑比较；注册存在不可逆外部副作用。对比必须使用互斥邮箱、代理、domain cohort。

---

## 15. 必须新增/修改的配置与控制面

```yaml
# Python admission 必须解除当前 32 hard cap 后才生效
max_parallel_tasks: 100
max_register_tasks: 100

email_protocol_backend: go
email_protocol_max_running: 100
email_protocol_max_queued: 100
email_protocol_proxy_cap: 1
email_protocol_mailbox_account_cap: 1
email_protocol_domain_caps:
  example-domain-a: 1
  example-domain-b: 1
email_protocol_timeout_seconds: 900
email_protocol_proxy_selection_deadline_seconds: 300
email_protocol_worker_url: "http://127.0.0.1:18765"
email_protocol_worker_api_version: 2

# V2 local bridge: Python owns upstream credentials; Go receives only a BridgeGrant.
email_protocol_bridge_mode: local_required
email_protocol_bridge_listen: "127.0.0.1:0"
email_protocol_bridge_connect_only: true
email_protocol_bridge_max_active_tunnels: 200
email_protocol_bridge_max_tunnels_per_job: 2
email_protocol_bridge_dial_timeout_seconds: 30
email_protocol_bridge_drain_timeout_seconds: 5
email_protocol_worker_control_key_file: "data/secrets/email_protocol_worker.key"
```

后端必须：

1. 把 global/register scheduler 的 32 hard cap 改成受控、可配置的 100；
2. 把 resource lease 从 task 创建阶段移动到 Go admission 前；
3. 在 preflight 成功后由 Python 常驻 BridgeManager 分配 versioned BridgeGrant；Go V2 只能通过它 CONNECT，且不能收到/记录 upstream credential URL；
4. job 立即落 `job_id/capability/bridge generation`，取消时调用 V2 DELETE 并等待 terminal/reconcile 后才 retire bridge 或报告资源；
5. 用 worker `retry_after` / long-poll，替换 100 个 1 Hz status polls；
6. 只在 `succeeded` 的 session schema 验证后写 registered/resume handoff。

---

## 16. 验收矩阵

### 16.1 协议与指纹

- [ ] S0–S15、T1–T3、C0–C6、L1–L3 全部 typed states；
- [ ] 每 endpoint 的 method、query、body、header preset、cookie scope、response-used 字段有 fixture；
- [ ] FingerprintBundle 全字段、六个 Client Hints、server `oai-did`、CSRF、PKCE、continuation 都按生命周期 checkpoint；
- [ ] 25 项 Sentinel payload 的索引/编码/hash/随机字段顺序 fixture 对齐；
- [ ] 动态 SDK build/hash/Turnstile `dx` 有 release-pinned fixture；不支持的 build fail closed；
- [ ] transport profile 与 UA/CH/browser/profile catalogue 一致；不以“改 UA”代替 wire profile。
- [ ] 每个 profile 的 `go version`、`go.mod/go.sum` module graph hash、基线 capture ID、ClientHello extension order、ALPN、HTTP/2 SETTINGS/flow/pseudo-header order、endpoint-class header order 都有版本化 fixture；JA3/JA4/Akamai/peetprint 仅作报警摘要，不以随机 `client_random` / session ID / key-share 判定失败；
- [ ] stock profile 与 baseline 不同的每个 extension delta 都有最小 custom-spec diff、插入位置和回归测试；不得复制不属于当前 Chrome/Edge baseline 的 Firefox/curl_cffi/PIX 字段；
- [ ] document navigation、same-origin API fetch、cross-origin OAuth 的 header preset 分别对拍；`sec-fetch-user`、`zstd` 等语义 header 只有其请求类型与 decoder fixture 同时满足时才出现；
- [ ] locked Go toolchain build、`go mod verify`、无代理 TLS echo probe 与 fake authenticated bridge probe 均通过；升级 Go 或任一 TLS transport module 后必须重新生成 profile ID 与全部 wire fixture；

### 16.2 100 job load

- [ ] barrier 启动 100 个 admissible job，观察 `running <= 100`、queue 有界、终态无 goroutine/connection leak；
- [ ] 同一个 proxy resource/exit IP、mailbox account、email、domain cap 不会被两个 active job 超额使用；
- [ ] 若资源不够，返回 admission backpressure，不复用资源、不 direct connect；
- [ ] 100 个 job 同时进入 OTP 时，没有重复 mailbox wait 或重复 OTP submit；
- [ ] 每个 job 的 CookieJar、profile ID、server `oai-did`、Sentinel header、continuation、session token 无 cross-talk。

### 16.3 故障与恢复

- [ ] 在 proxy preflight、S1、S6、S7、S8、S9、S10、S11、S12、S13、checkpoint 前后取消；终止后不再发请求；
- [ ] 在每个外部 request 前后 kill/restart；重放 create 必须返回原 job/result 或 reconcile，绝不创建第二账户；
- [ ] wrong/expired/replayed OTP、daemon restart waiting OTP、cancel ack lost、lease fence 变化全部有 deterministic test；
- [ ] callback 或 account create 后 token/session 缺失时不是成功，也不能重跑注册；
- [ ] success 断言为 `access_token + validated session document`，不是“页面跳转到了 callback”。

### 16.4 本地 bridge

- [ ] Go worker、Python BridgeManager 在同一 host/network namespace；Go 对非 `http://127.0.0.1:<port>` 的 BridgeGrant fail closed；
- [ ] 100 job barrier：每条 CONNECT 只映射到拥有其 capability/generation 的 upstream proxy；没有 proxy key、exit IP、cookie 或 tunnel cross-talk；
- [ ] fake SOCKS5 RFC1929 与 fake HTTP Basic upstream 均验证认证、目标 host/port、CONNECT response relay；bridge capability 不会被转发到 upstream；
- [ ] wrong/expired/replayed capability、retired generation、plain HTTP forward、SOCKS4/4a、direct fallback 都有 deterministic rejection test；
- [ ] queued/running/waiting-OTP cancel、Python child crash、daemon restart、lost DELETE ACK 都保持 lease/bridge fence，reconcile 后准确 retire/report；
- [ ] terminal 后 active tunnels、open sockets、bridge goroutines、Go idle connections 回到基线；capability、完整 bridge/upstream URL 从日志、metrics、checkpoint plaintext、session result 中均不可见。
- [ ] fake bridge 精确断言 Go 发出 `CONNECT <target>:443`、携带正确 `Proxy-Authorization: Bearer <capability>`；fake origin 断言该 capability 不会穿过 tunnel。该测试覆盖 `tls-client.WithConnectHeaders` 的实际 wire 行为，不能只做 API 编译检查；

---

## 17. 当前已知参考实现差异（开发必须消除）

| 当前 TS/Python 行为 | Go / V2 目标 |
|---|---|
| 每个 Python task spawn Node child；task cwd 隔离 | 一个 100-slot Go daemon，job-local runtime/ledger 隔离 |
| `setGlobalDispatcher` 从 global config 读 proxy | 每 job immutable BridgeGrant / isolated client；Go 无 proxy 选择和直连 fallback |
| authenticated proxy 的 Python child 创建一个无本地认证的 ephemeral bridge | Python 常驻、bounded BridgeManager；per-job capability/generation、CONNECT-only、明确 retire/reconcile |
| Python local timeout/kill 不会发送 Go cancel，bridge 可能先于 daemon 消失 | V2 DELETE + terminal ACK/reconcile 后才 close bridge/report lease |
| `DEFAULT_INSECURE_TLS=true` | certificate validation=true |
| default profile 在 module import 时生成一次 | 每 job 生成并 durable checkpoint profile |
| browser Sentinel 有 global browser/context/page | 每 job Go realm/context；不可共享 profile/cookie |
| Go V1 仅一次 OTP、1 Hz polling、无 cancel | V2 challenge/version/long-poll/cancel/reconcile |
| session path 可从 daemon 返回 | Go 返回 schema-valid document；Python 写文件 |
| 当前 scheduler hard cap 32 | Python + Go admission 一致支持 100 |

---

## 18. Source reference index

- `E:/project/mailat/mailat/codex_register/src/openai.ts:237-282` — current per-client jar/global dispatcher; `:764-822` — primary registration chain; `:890-933` — OAuth/PKCE; `:1062-1251` — continuation/password/email OTP; `:1462-1529` — create/callback/session; `:1834-2021` — bootstrap/CSRF/NextAuth/header builder; `:2058-2189` — retry behavior.
- `E:/project/mailat/mailat/codex_register/src/device-profile.ts:3-245` — complete profile schema/catalog/Client Hints.
- `E:/project/mailat/mailat/codex_register/src/sentinel.ts:27-195` — SentinelEnv; `:198-342` — req/final token/25-item PoW payload; `:356-569` — SDK then VM path; `:1136-1437` — navigator/window/screen realm.
- `E:/project/mailat/mailat/codex_register/src/sentinel-browser.ts:6-204` — browser Sentinel context/cookie/frame fields and global-state limitation.
- `E:/project/GPT Register/application/tasks_service.py:45-67,218-238,519-571,573-593` — existing 32 cap, admission, cancellation.
- `E:/project/GPT Register/application/resource_pool_service.py:611-744,652-669,790-998` and `infrastructure/db.py:857-1000` — resource lease/exit-IP exclusion/recovery.
- `E:/project/GPT Register/services/go_email_protocol_runner.py:197-263` — V1 create/poll/OTP lifecycle to replace.
- `E:/project/GPT Register/services/mailat_email_protocol_task.py:115-131,162-202` — mailbox baseline and Python handoff ownership.
- `E:/project/GPT Register/core/proxy/credential_runtime.py:21-292,773-814` — 当前 `127.0.0.1` HTTP-to-SOCKS/HTTP credential bridge 的监听、CONNECT、认证、relay 与 cleanup 语义；它是 V2 BridgeManager 的兼容基线，不是 100-job 的最终生命周期实现。
- `E:/project/GPT Register/services/mailat_email_protocol_runner.py:219-261,338-461` 与 `services/go_email_protocol_runner.py:177-265` — selected upstream URL / runtime local bridge URL 的分离、preflight、V1 handoff 和本地 finally cleanup。
- `E:/project/GPT Register/tests/test_proxy_runtime.py:139-233` — 当前 SOCKS5 RFC1929、HTTP Basic upstream bridge integration baseline；V2 必须把它扩大为 100-job/cancel/restart isolation tests。
- `https://pkg.go.dev/net/http#Transport` — 标准库 `Transport.Proxy` 与 `ProxyConnectHeader` 的 CONNECT header 机制；只证明标准库路径，浏览器 TLS transport 仍须单独 fixture 验证。
- `https://pkg.go.dev/github.com/bogdanfinn/tls-client` 与 `https://pkg.go.dev/github.com/bogdanfinn/tls-client/profiles` — 已确认 `ClientProfile`、`SetProxy`、`WithConnectHeaders`、`WithProxyDialerFactory`、CookieJar 与 TLS/HTTP2 profile API；这些是 local bridge + wire profile 的候选实现，仍需项目 fixture 验证。
- `https://github.com/refraction-networking/utls` — low-level ClientHello capability reference; not a complete browser transport solution by itself.
- `E:/Download/go_tls_fingerprint_align_notes.md:1-422` — 2026-07-15 的 `curl_cffi firefox135` ↔ Go `tls-client` 对拍记录：提供 extension-diff、HTTP/2/header-order、proxy normalization 与 peet.ws 方法论。其 Firefox/PIX-specific profile、headers 与业务结论不能直接用于本项目。
