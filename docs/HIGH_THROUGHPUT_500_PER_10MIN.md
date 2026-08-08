# 高吞吐产线：10 分钟 ≥500 成功（开发总纲）

> **状态：** 2026-07-24 — OTP seat / 连续补位已落地；**座位默认已抬到 200**（见交付文档）  
> **目标：** 软件路径（Dashboard → TasksService → pure-Go worker）**10 分钟 ≥500 成功注册**，200 座下可继续抬  
> **原则：** 能 Go 的全 Go；OTP 不堵协议 seat；admission 排队不当业务失败；机器不是主矛盾  
> **金标准：** 只有软件路径压测绿才算数；CLI 绿 ≠ 软件绿  
> **当前交付权威：** [`TRUE_200_CONCURRENCY_DELIVERY.md`](TRUE_200_CONCURRENCY_DELIVERY.md) · 入口 [`INDEX.md`](INDEX.md)  
> **注意：** 下文早期段落仍可能出现「100 seat」历史推演；**运行默认以 200 为准**。

---

## 0. 上文结论与已完成工作（必读）

### 0.1 业务与资源基线

| 项 | 结论 |
|---|---|
| 邮箱 | Hotmail/Outlook 同一套 `outlook_token` + Graph；**无需**单独 hotmail 接码协议 |
| 导入格式 | `email----password----client_id----refresh_token` |
| 代理 | **bestgo + 1024**；`proxy_seed_styles: bestgo,1024`；辣椒 sticky disabled |
| 本地桥 | **默认关闭** `mailat_protocol_use_local_bridge: false`；pure-Go **直拨** `socks5://upstream` |
| Python Graph | 曾用 `socks5h/http` 路由；裸 `socks5` 易 SSL EOF（客户端差异，非账号填错） |
| AdsPower | 浏览器 SOCKS 能通 ≠ Python/Go TLS 栈表现一致 |

### 0.2 实测吞吐（软件路径）

| 批次 | n | ok | fail | 墙钟 | 单号 ledger 均值 | 备注 |
|---|---:|---:|---:|---:|---:|---|
| hotmail Python OTP | 10 | 10 | 0 | ~204s | ~43s | 协议~15s + OTP段~28s |
| hotmail **Go 内嵌 OTP** | 10 | **9** | 1 | **~173s** | — | 失败=`chatgpt.com EOF`，非收码 |
| 目标 | — | **500** | — | **≤600s** | — | **50 成功/分钟** |

数量级：

- 100 座历史连续产能约 **54–60 成功/分钟**（capacity 实测 ~545–595/10min）  
- 目标 **50/分钟** 在 100 座已可达量级；200 座目标是 **更高稳态**（待 §TRUE_200 回填）  
- 若单号 45s 且 seat **永远满且零浪费**：200 seat 理论 `200×600/45≈2667` 成功/10min — **上限够**  
- 到不了是因为 **seat 浪费 + 失败率 + 代理/OTP**，不是 Go CPU 算不动  

### 0.3 单号时间结构（ledger）

```text
单号 ~43s
├─ pre-OTP S0–S8     ~15s (35%)  OpenAI 协议 + 发码
└─ OTP→完成          ~28s (65%)  等信 + Graph + S10–S14
```

整批 200s ≠ 单号 200s：

```text
整批墙钟 ≈ 调度/租号/预检 + 并发跑（等最慢）+ admission 重试 + smoke 收尾
并发加速比实测 ~2×（理想应接近 min(n, seats)）
```

### 0.4 已落地代码（2026-07-24）

| 项 | 状态 | 位置 |
|---|---|---|
| Go Graph 令牌接码（CLI） | 早已有 | `internal/mailbox/outlook_token.go` `WaitForOTP` |
| **软件路径 in-worker Go OTP** | **已接** | Create 带 `mailbox_client_id/refresh_token`；`runLiveFromOTP` 调 `WaitForOTPProxy` |
| Python 有 token 时不堵 callback | **已接** | `services/go_email_protocol_runner.py` 仅 poll |
| Graph 代理 socks5/http | **已接** | `WaitForOTPProxy` + `graphHTTPClient` |
| 本地桥默认关 | **已接** | config + mailat/codex/API 默认 false |
| bestgo/1024 锁定 | **已接** | seed style 过滤 + 评分拉平 |
| OTP lookback 防串码 | **已接** | Python 60s / Go 60s（不再 3min） |
| 废 token 禁用 | **已接** | Python `mark_disabled`；Go 失败码上抛 |
| **waiting_for_otp 释放 seat** | **本文 Phase A** | 见 §3 |
| **admission 排队语义** | **本文 Phase A** | 见 §3 |
| **Go Graph 缓存/并发池** | **本文 Phase A** | 见 §3 |

---

## 1. 目标架构（To-Be）

### 1.1 一句话

> **协议 goroutine 只占 seat 做 OpenAI 热路径；等邮件不占全局 seat；OTP 在 Go 内完成；Python 只投递与落库。**

```text
Bulk create
    │
    ▼
TasksService (bucket N) ──租邮箱+SID──► 资源池
    │
    ▼
POST /v2/email-register
    │
    ├─ TryAdmit (协议 seat)
    ├─ S0–S8
    ├─ seal checkpoint
    ├─ status=waiting_for_otp
    ├─ **Release 协议 seat**          ★ Phase A
    ├─ Go WaitForOTPProxy (Graph)     ★ 已做 / 加强缓存
    ├─ TryAdmit 再占 seat             ★ Phase A
    ├─ S10–S14
    └─ Release + 成功入库
```

### 1.2 容量公式

```text
成功/10分钟 ≈ 有效协议并发 × (600 / 平均周期秒) × 成功率
```

| 平均周期 | 成功率 | 需要「有效在途完工」并发 |
|---|---:|---:|
| 45s | 100% | ~38 |
| 45s | 80% | ~48 |
| 30s | 90% | ~28 |

**协议 seat 建议：** 200（OTP 不占 seat 后，100 物理 seat 也能顶更高逻辑在途）。  
**OTP 并发：** Go 内 64～128（信号量），按邮箱串行可选。  
**逻辑在途任务：** 300～500（含 waiting，不占协议 seat）。

### 1.3 机器（纠正夸张说法）

| 场景 | 配置 |
|---|---|
| 100 并发 pure-Go | **现有 8～16 核 / 16～32G 足够**；Go worker 空闲约几十 MB |
| 500/10min 目标 | **先改架构**；32G 内存更舒服，**不是**必须 64G/32 核才能开工 |
| 主瓶颈 | 网络 RTT、Graph、admission、调度，**不是** CPU 算力 |

---

## 2. 问题总表（按优先级）

| ID | 问题 | 证据 | 阶段 |
|---|---|---|---|
| **P0-1** | `waiting_for_otp` 仍占 `admission` seat | `protocol_engine_runner` park 后不 `Release` | A |
| **P0-2** | admission 429 当业务失败 / 狂重试 | ledger 大量 `admission_rejected` | A |
| **P0-3** | Graph 无缓存、并发模型弱 | 每号刷 token；旧 Python 限 20 | A |
| **P0-4** | 软件曾 Python 收码堵线程 | 已改为 Go OTP；保留 fallback | 已完成 |
| **P1-5** | 批启动预检 ipify 超时拖墙钟 | 日志 `proxy_country_check_error` | B |
| **P1-6** | 双账本 TasksService vs Go active | 假满/对不齐 | B |
| **P1-7** | 代理 seed 过少 / 出口单一 | 仅 bestgo+1024 两条 seed | B |
| **P2-8** | 指标与压测台 | 缺 success/min 仪表 | C |

---

## 3. Phase A — 控制面三刀（必须，本迭代）

### 3.1 OTP 放 seat + 恢复再占

**行为：**

1. 进入 `waiting_for_otp` 且 checkpoint 已 seal 后：  
   `m.adm.Release(jobID)` —— **只放 seat，不 `releaseJob`（不毁 runtime）**  
2. OTP 拿到并 ledger → `running` 后、走 S10 前：  
   `m.adm.TryAdmit(seat)`，失败则短退避重试（全局满）  
3. `failJob` / 成功结束仍走现有 `releaseJob`（Release 幂等）

**Seat 字段：** `EmailKey=rec.EmailResourceKey`，`ProxyKey=rt.Proxy.ProxyKey`，`Domain=domainOf(email)`。

**验收：**

- n=100 时 `waiting_for_otp` 很多，但 `active_count` **远小于** waiting 数  
- 新 create 不再被「全员等邮件」堵死  

### 3.2 admission 排队语义（Python）

- `reason=global`：**只退避重试**，不换邮箱、不当最终业务失败（直到次数用尽）  
- `reason=proxy`：换 SID 再试  
- `reason=mailbox`：换邮箱  
- ledger 的 `admission_rejected` 可保留诊断，但 **任务层优先 requeue**  
- 加大 global 退避：`min(15, 0.5 * 2^attempt)`，max attempts 可配置（如 12）

### 3.3 Go Graph 提并发

| 项 | 做法 |
|---|---|
| Token 缓存 | `client_id|sha256(refresh)` → `{token, exp}`，过期前复用 |
| 并发信号量 | 当前 **96**（先测 96，再视 Graph 429/p95 升 128） |
| 代理 | 继续 `WaitForOTPProxy`，与任务 `BridgeURL`（socks5）一致 |
| lookback | 60s（防串码） |

### 3.4 配置建议（Phase A）

```yaml
max_register_tasks: 200
max_parallel_tasks: 200
# worker 启动 -max-active 200（与 DefaultMaxActive 对齐）
# Graph 请求并发：96（独立于 protocol seat；出现 429/p95 恶化退回 64）
outlook_graph_max_concurrent: 96
email_otp_timeout: 240
mailat_protocol_use_local_bridge: false
proxy_seed_styles: bestgo,1024
```

---

## 4. Phase B — 拉吞吐（A 验收后）

1. **禁用/并行化** 国家预检（ipify 超时）  
2. 批模式 `prepare(N)` 先租齐邮箱+SID 再 fire  
3. TasksService drain 扇出提高；与 Go active 对齐显示  
4. 多 proxy seed（≥10 线路）  
5. 可选：waiting 与 protocol 分配额指标  

**验收：** n=100，墙钟 &lt;3min，ok≥80；success/min ≥25。

---

## 5. Phase C — 产线化 → 500/10min

1. OTP 协程池（仍 Go 内嵌，非 Python 服务）  
2. 可选第二 worker 分片  
3. 每日 canary + 废 token 自动剔除  
4. 验收：**连续 3 次 10 分钟 ≥500 成功**

---

## 6. 接口契约（Create）

```json
{
  "task_id": "...",
  "email": "a@hotmail.com",
  "password": "...",
  "mailbox_client_id": "...",
  "mailbox_refresh_token": "...",
  "otp_timeout_seconds": 240,
  "resource_grant": {
    "email_key": "...",
    "proxy_key": "direct-socks:...",
    "bridge": { "url": "socks5://user:pass@host:port", "capability": "direct", "generation": 1 }
  },
  "skip_phone": true
}
```

- 有 `mailbox_*`：worker **Go 收码**，Python 不 SubmitOTP  
- 无凭证：legacy Python `otp_callback` + POST `/otp`  

---

## 7. 状态机（软件 pure-Go）

```text
queued → running(admission)
      → protocol_S0…S8
      → waiting_for_otp  (+ Release seat)
      → [Go Graph poll]
      → running(otp_accepted_go) (+ TryAdmit)
      → protocol_S10…S14
      → protocol_done / failed
```

---

## 8. 指标（必须打）

| 指标 | 含义 |
|---|---|
| `go_active` | admission 持有 seat 数 |
| `go_waiting_otp` | ledger waiting 数（可不占 seat） |
| `success_per_min` | 滚动 1 分钟成功 |
| `admission_reject_total{reason}` | global/proxy/mailbox |
| `otp_latency_seconds` | challenge→code |
| `job_total_seconds` | create→success |

---

## 9. 压测验收表

| 级别 | 命令/动作 | 通过标准 |
|---|---|---|
| L0 | worker `/health` | `runner=protocol` `transport=tls` |
| L1 | n=10 hotmail | ok≥9，墙钟&lt;180s，日志含 `in-worker Graph OTP` |
| L2 | n=50 | ok≥40，墙钟&lt;300s，`active` 在 OTP 高峰 **&lt; waiting** |
| L3 | n=100 | ok≥80，墙钟&lt;240s，admission 假失败不主导 |
| L4 | 10min 持续投递 | **成功≥500** |

记录：`docs/G4_CANARY_LOG.md` + `output/software_smoke_*_summary.json`。

---

## 10. 开发顺序（强制）

1. **本文档 + Phase A 代码**（当前）  
2. hotmail n=10 / n=50 对比 seat 释放前后  
3. Phase B 预检与批启动  
4. Phase C 冲 500/10min  

**禁止：** 只调大 `max_active` 不放 OTP seat；恢复本地桥当默认；用 CLI 数字冒充软件验收。

---

## 11. 风险

| 风险 | 缓解 |
|---|---|
| OpenAI 风控随吞吐上升 | 多 SID、多 seed、可降速 |
| Graph 限流 | 信号量 + token 缓存 + 退避 |
| 放 seat 后 S10 抢不到 seat | 短退避重试；预留 resume 配额（B） |
| PG 锁 | 已 PG；批量写单连接；lease SKIP LOCKED |

---

## 12. 协议质量不变量（吞吐优化的硬边界）

吞吐不得以伪造、混用或丢失协议状态为代价。以下每条都是实现与压测的阻断条件：

| 不变量 | 实现要求 | 违反后的风险 |
|---|---|---|
| **一任务一会话所有权** | 一个 job 只有一个 `Engine`、cookie jar、TLS client、代理租约；只有它的 owner goroutine 可写 | cookie/验证码/指纹串号 |
| **代理黏性** | S0–S14 和该 job 的 Graph 收码都使用同一已租代理；不得在 S8/S10 中间偷偷 rotate | IP/会话不一致、风控 |
| **指纹黏性** | 生成一次并 seal；恢复时恢复同 fingerprint、header order、TLS profile | TLS/UA/headers 前后不一致 |
| **状态单向与 CAS** | ledger 只允许合法迁移，`state_version` CAS；同一 transition 重放必须幂等 | 双完成、错写成功 |
| **OTP 归属** | `not_before=challenge_issued_at`，60s lookback，候选码进入 `reject_codes` 后绝不复用 | 串码 / S10 误验 |
| **资源 fence** | 每次 lease 带 fence/generation；写回与释放必须验证仍是当前租约 | 旧 worker 释放新租约 |
| **网络重试边界** | 只在请求尚未被服务端接受时自动重试；不明结果标 `registration_may_have_succeeded` 后查证 | 重复注册 / 误报失败 |
| **并发边界** | 不允许同一邮箱、同一 proxy seat、同一 live engine 并发跑两次 | 资源冲突、协议污染 |

### 12.1 允许的异步化 vs 禁止的并发化

| 可以并行（有界） | 必须串行 |
|---|---|
| 不同任务的租资源、S0–S8、Graph poll、结果上报 | 同一 job 的 S0→S14 状态转移 |
| 不同邮箱的 token refresh/Graph fetch | 同一邮箱同一时刻的 OTP 消费 |
| 代理健康探测、预热、批量 prepare | 同一代理超过 `MaxPerProxy` 的协议请求 |
| PG 独立 lease claim | 同一资源的 claim/renew/release fence 更新 |

**结论：** 用异步提高的是 *独立 I/O 在途数*，不是把同一个协议会话拆给多个 goroutine。

---

## 13. 异步执行设计（Go 优先）

### 13.1 三个隔离的有界调度域

```text
                         ┌─────────────────────────────┐
Create/Resume ──────────►│ Admission scheduler          │  protocolSeats=200
                         │  - priority: resume > create │
                         └───────┬─────────────┬────────┘
                                 │             │
                       S0–S8/S10–S14      parking transition
                                 │             │
                                 ▼             ▼
                         Protocol goroutines   OTP scheduler
                         (task-local Engine)   graphInFlight=64
                                                 (task-local context)
                                                       │
                                                       ▼
                                                Graph request pool
                                                token cache / transport pool
```

| 域 | 责任 | 上限 | 阻塞时怎么办 |
|---|---|---:|---|
| `protocolSeats` | OpenAI 协议热路径 | 200 初值 | 入 resume/create queue；不占 CPU 自旋 |
| `otpInFlight` | `WaitForOTPProxy` poll/refresh | 64 初值 | 排队但 runtime 保持 parked；超时由 job deadline 终止 |
| `leaseClaims` | PG 邮箱/SID claim | 32 初值 | `SKIP LOCKED` 后返回无资源/短退避 |
| `resultWrites` | PG 成功、释放、指标 | 32 初值 | buffered worker；关键 completion 同步确认 |

**禁止全局无界 `go func`。** 每个 goroutine 必须归属于 job `context.Context`，由 `errgroup`/等待组回收；每个 acquire 必须能因 context deadline 退出。

### 13.2 协议 seat 的异步 lifecycle

```text
Create → acquire protocol seat → S0..S8 → seal checkpoint
      → ledger waiting_for_otp → release protocol seat
      → acquire otp semaphore → Graph poll (异步 I/O)
      → code accepted → enqueue resume (high priority)
      → acquire protocol seat → S10..S14 → finalise/release
```

- `waiting_for_otp` **只释放 admission seat，不关闭** runtime、client、cookie jar、lease 或 context。
- resume 是高优先级：避免持续的新 create 把已有 code 的任务饿死。
- admission 满时不把 OTP 当失败：记录 queue delay；deadline 前退避重试。
- shutdown/recovery：先 durable checkpoint；恢复不得假定内存 connection 仍有效，必须用 sealed checkpoint 重建协议状态，或安全地重走前置状态。

### 13.3 Graph / Outlook 的异步与缓存

1. `tokenCache[clientID|sha256(refresh)] = token, expiresAt`；到期前 2 分钟刷新。
2. 刷新使用 **singleflight**：同 token 的 100 个 waiter 只发一个 refresh 请求。
3. HTTP transport 每个代理 route 分区复用 idle connection；不得跨 proxy 或跨 fingerprint 复用连接。
4. Graph fetch 用可取消 context、请求 deadline、指数退避 + jitter；429/5xx 不忙等。
5. 每邮箱的 OTP code 消费在 mailbox key 上串行化；不同邮箱可以并行。
6. Graph semaphore 默认 64，压测时从 32→64→96 逐档；先看 429、token refresh error 和 p95，不盲目开到无限。

### 13.4 Python 控制面异步化边界

- **主数据面不留 Python callback：** 有 Outlook credentials 时 Python 仅提交 Create、poll job、写业务结果。
- 资源批量 claim、任务投递与状态 poll 应使用 bounded async workers / 连接池；绝不一个任务一个临时 event loop 或线程。
- 旧 Python Graph 仅无 Go credential 时 fallback；该分支单独标 `otp_mode=python_fallback`，不可混入 Go 成功率。

---

## 14. 数据库与 PG 锁设计

### 14.1 原则

1. **事务只做状态与 fence 写入，不在事务内打网络。**
2. 资源 claim 使用 `SELECT … FOR UPDATE SKIP LOCKED`；以稳定顺序（resource type → id）拿锁。
3. job、lease、result 都有唯一业务键 / idempotency key；重复调用返回已有状态。
4. 只对 SQLSTATE `40001`（serialization）和 `40P01`（deadlock）作有限 jitter retry；不能吞其它 DB 错误。
5. lease 释放采用 `WHERE id=$1 AND fence=$2 AND state='leased'`，影响行数为零就是旧 owner，记录而非覆盖。

### 14.2 队列表结构（目标）

| 表/索引 | 用途 |
|---|---|
| `registration_jobs(status, priority DESC, available_at, created_at)` partial index | 取 create/resume queue |
| `resources(kind, status, cooldown_until, score DESC, id)` partial index | `SKIP LOCKED` 批量 claim |
| `resource_leases(resource_id, fence)` unique | fence 归属 |
| `job_events(job_id, state_version)` unique | append-only 审计与幂等 |

### 14.3 拿资源与写结果伪代码

```sql
-- claim: 短事务；无行即没有资源，绝不等待锁
WITH picked AS (
  SELECT id FROM resources
  WHERE kind = $1 AND status = 'available'
  ORDER BY score DESC, id
  FOR UPDATE SKIP LOCKED
  LIMIT $2
)
UPDATE resources r
SET status='leased', fence=r.fence+1, lease_until=$3
FROM picked WHERE r.id=picked.id
RETURNING r.id, r.fence;

-- finalise: 保证同一 job 不会写两次成功
UPDATE registration_jobs
SET status='succeeded', completed_at=now()
WHERE id=$1 AND state_version=$2 AND status='running'
RETURNING id;
```

### 14.4 绝不做的 PG 伪优化

- 不提高全局 lock timeout 来掩盖长事务；应缩短事务。
- 不用 `SELECT` 后应用层再 `UPDATE` claim（竞态）。
- 不对所有查询开串行化隔离；只在确需的临界转移使用 CAS。
- 不把 token/refresh token 写入日志、job event 或指标标签。

---

## 15. 全链路优化清单与优先级

| 优先级 | 优化 | 预期收益 | 质量保护 | 验收 |
|---|---|---|---|---|
| A0 | OTP 释放 protocol seat + resume 高优先 | 消除 OTP 占坑 | runtime/session 不关 | waiting 数大于 active 仍持续完成 |
| A0 | Go Graph token cache + singleflight + semaphore | 降 token 刷新与 Graph 排队 | token key 不泄漏 | p95 OTP 降、429 不升 |
| A0 | admission queue/retry 分类 | 消除假业务失败 | mailbox/proxy reject 仍换资源 | `global` 不直接 fail |
| A1 | 批量 prepare/claim + 异步投递 | 消除启动串行 | fence 与幂等 | n=100 首波起跑时间下降 |
| A1 | 预检并行、缓存或移出关键路径 | 去 ipify 墙钟 | 失败只标风险，不篡改代理 | p95 create 降 |
| A1 | HTTP transport keep-alive（按 route 分片） | 少 TCP/TLS 建连 | 不跨代理/身份共享 | connect time 降、无 session 串扰 |
| A1 | 任务完成写入 batch/outbox | 降 PG 往返 | terminal commit 必须确认 | DB p95/write lock 降 |
| A2 | resume 预留配额（如 20%） | 防饥饿 | 上限固定 | code→S10 p95 下降 |
| A2 | worker 分片 | 线性扩容 | job hash/fence 单 owner | 两 worker 无重复 job |
| A2 | 资源质量评分 | 降 network EOF | score 只来自已验证事件 | 失败率下降 |
| A3 | 自适应并发（AIMD） | 逼近稳定极限 | 每个资源域独立熔断 | 429/EOF 上升即回落 |

### 15.1 明确不优化

- 不改变请求 body 语义、header order、TLS profile、cookie 生命周期来换速度。
- 不把同一任务拆成多客户端并发请求。
- 不为“快”忽略 OTP `not_before`、reject list、lease fence 或成功后查证。
- 不用无限线程/无限 goroutine/无限 PG connection 伪造高并发。

---

## 16. 指标、压测、发布与回滚

### 16.1 必备分桶指标

```text
throughput.success_per_min{otp_mode,proxy_style}
job.stage_latency_seconds{stage=S0_S8|waiting_otp|S10_S14}
admission.queue_seconds{kind=create|resume}
graph.request_seconds{operation=refresh|list}
graph.errors_total{code}
pg.claim_seconds / pg.deadlock_retry_total
active_protocol_seats / waiting_otp / otp_inflight / runnable_resume
protocol_quality_violation_total{invariant}
```

每个阶段记录 start/end monotonic time；绝不将 email、refresh token、proxy credential、OTP 写入 metrics/logs。

### 16.2 渐进压测与门禁

| Gate | 压力 | 成功条件 | 失败动作 |
|---|---|---|---|
| G0 | unit/build | CAS、token cache、seat lifecycle 全绿 | 不发 smoke |
| G1 | n=10 | ok≥9；无 quality violation | 修根因 |
| G2 | n=50 | ≥40 success；p95 OTP 与 queue 指标完整 | 降一档并分析资源域 |
| G3 | n=100 | ≥80；active seat 不被 waiting 吃满 | 分别调 protocol/OTP 额度 |
| G4 | 10 分钟持续投递 | ≥500 success，连续 3 次 | 不把单次峰值称为达标 |

### 16.3 发布/回滚

1. 新 scheduler 有 feature flags：`otp_release_seat`、`go_graph_cache`、`resume_priority`；默认在 canary 开。  
2. 任何 quality violation、重复 finalise、串码信号 → 停止扩容，关闭本轮新功能并保留 ledger 证据。  
3. 回滚不会删 lease/ledger；只停止新任务并让已有 runtime 到可恢复 checkpoint。  
4. 每档压测后记录配置、worker build、seed 组成、资源数量、成功/失败码和 p50/p95。

---

## 17. 资源下限与 500/10min 现实门槛

`500 success` 至少意味着 500 个可用邮箱和对应代理容量；失败率不为零时必须有余量。

| 资源 | 起步建议 | 原因 |
|---|---:|---|
| Outlook token | 650–750 个可验证可用 | 覆盖 20–35% 失败/冷却；每邮箱单任务 |
| 在途任务上限 | 300–500 | waiting 不占 protocol seat |
| protocol seat | 200 | 预留 resume + create |
| Graph in-flight | **96 当前**，通过 G2 后可测 128 | 以 429/p95 决定，不以 CPU 决定 |
| 独立 proxy capacity | ≥200 可同时用的 SID/出口 | `MaxPerProxy` 约束与风控隔离 |
| PG pool | 32–64 短连接 | 再高先看 lock/DB p95 |

资源不足、OpenAI/Graph 限流、出口质量下降时，软件会**安全降吞吐**；这不是调大 goroutine 能替代的。

---

## 18. 修订记录

| 日期 | 内容 |
|---|---|
| 2026-07-24 | 初版：汇总上文、Go OTP 已接、Phase A 定义、500/10min 公式与验收 |
| 2026-07-24 | 设计完成：Phase A 的 seat lifecycle、Graph cache/sem、admission queue；代码尚待逐项验收 |
| 2026-07-24 | 扩展：异步调度域、协议质量不变量、PG 锁与队列、资源下限、渐进压测与回滚 |
| 2026-07-24 | 配置：Graph 96；protocol admission **已抬到 200**（见 TRUE_200）；本文改为指向交付文档 |
