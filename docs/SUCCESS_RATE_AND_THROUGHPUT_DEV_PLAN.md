# 成功率 + 产出效率：彻底提升开发计划

> **状态：** 调研完成，作为下一阶段主开发合同（2026-07-26）  
> **证据权威：** 源码 + canary/capacity 产物；**不以记忆 / STATUS.md 为准**  
> **入口索引：** [`INDEX.md`](INDEX.md)  
> **关联：**  
> - [`TRUE_200_CONCURRENCY_DELIVERY.md`](TRUE_200_CONCURRENCY_DELIVERY.md) — 200 座已交付代码  
> - [`LAB_CANARY_VS_MAIN_MERGE_PLAN.md`](LAB_CANARY_VS_MAIN_MERGE_PLAN.md) — canary vs 主网根因  
> - [`HIGH_THROUGHPUT_500_PER_10MIN.md`](HIGH_THROUGHPUT_500_PER_10MIN.md) — 产能公式  
> - [`TRUE_100_CONCURRENCY_GAP_AND_WORKLIST.md`](TRUE_100_CONCURRENCY_GAP_AND_WORKLIST.md) — 历史债（只读）

## 0b. 2026-07-26 已落地（代码 + L0/L1）

| ID | 状态 | 说明 |
|---|---|---|
| A1 | **done** | go backend force batch fail-closed |
| A2 | **done** | health 要求 `email-register-batches` |
| B1 | **done** | OTP 旁路 email_tries 换邮 |
| B2 | **done** | early abort 65s / firstWait / resend remain |
| B3 | **done** | remint 换 style + ExpectedCountry on spawn |
| D1 | **done** | outlook available 充足；1024 seed 恢复 available |
| **T1 transport reclass** | **done** | `HTTP response to HTTPS` / wsarecv / GOAWAY → `proxy_or_network` + remint；post-OTP 非 S11 一次 client rebuild |
| L0 100×10min | **done** | **804/105**（88.4%）~783/10min；`cap_20260726_080916_4cb452` |
| **L1 120×10min** | **done** | **965/16**（**98.4%**）**93.6/min ~936/10min**；`cap_20260726_094058_6fe27f` |

### L0 失败结构（ledger，同窗 ~179 failed）

| failure_code | n | 主因 |
|---|---:|---|
| protocol_step_failed | 61 | 大量 **wsarecv / HTTP→HTTPS** 未归网 |
| otp_timeout | 59 | 死邮/无信 + 少量 Graph 经坏代理 |
| ambiguous_after_send | 43 | create_account 上 **wsarecv / HTTP→HTTPS**（未 remint） |
| email_already_used | 11 | 正常换邮沉淀 |
| session_invalid / proxy_or_network | 3+2 | 少 |

中段 pulse（~t=274–311）：以 **otp + transport ambiguous** 为主，非协议体回归。

**T1 后 L1：** fail 从 105→**16**，成功率 88%→**98%**，产能 783→**936/10min**。

**L1 剩余失败（ledger 同窗，batch 口径 16 / ledger ~80+ 含跨窗）：** 主因 **`otp_timeout` / `graph_no_openai_code`（空信冷却，非死邮）**；残留 `protocol_step_failed` 含 bare `EOF`（T1b 已归网）、`S14 missing access_token`、偶发 `S3 missing csrf`。

**2026-07-26 续：**
- capacity：`GO_EMAIL_PROTOCOL_MAX_ACTIVE=max(concurrent,env)` 自动对齐；summary 内嵌 `failure_breakdown`
- 独立脚本：`scripts/aggregate_ledger_failures.py`
- T1b：bare `: EOF` → `proxy_or_network`
- **L2 150：暂不跑**。L1 已 98%+；下一杠杆是邮箱质量，不是座位。需 >1000/10min 时再分波 L2。
- **邮箱 OTP 空信语义：** `graph_no_openai_code` **不是死邮** → `cooldown`+`cooldown_until`（6h），lease 优先 fail_count 低
- **config.yaml**：`max_register_tasks/max_parallel_tasks=120`，`proxy_seed_styles=bestgo,1024`
- **canary 回归：** `canary_n10p -n 100` → **97/100**（≥95 通过）`summary_20260726_024024.json`

---

## 0. 一句话

**协议体已经是同一套 pure-Go `ModeLive`。**  
彻底提高成功率与产出，不是再写第二套协议，而是：

1. **产品热路径只剩 Go batch worker**（Python 只下单/看进度）  
2. **把 canary 已证明的恢复策略吃干净**（已大部分合入，剩 OTP 尾与观测）  
3. **用连续补位 + 稳态座位**，禁止 200 齐射打穿出口  
4. **失败结构可分类 + 邮箱池不空**，否则谈效率是空话

---

## 1. 现有架构（As-Is，有源码依据）

### 1.1 目标产品链（应成为唯一热路径）

```text
UI / API / capacity_10min
        │
        ▼
TasksService.start_email_protocol_register(_many)
        │  prefer try_start_go_batch_register
        ▼
POST /v2/email-register-batches   (services/go_registration_batch.py 薄客户端)
        │
        ▼
email-protocol-worker  (single process)
  admission.MaxActive (config/start → 默认 200)
  job.Manager.StartBulk
        │
        ├─ lease outlook_token
        ├─ MintSeedSession(styles=bestgo,1024, region∈JP,US,DE,GB,BR)
        ├─ prepareProfile(ExpectedCountry=seed.Region)
        ├─ protocol.Engine ModeLive  S0→S9
        │     edge/EOF/timeout → SessionRemints=2 + NextSeedRegion
        ├─ waiting_for_otp → Release seat → Graph OTP (inbox+junk)
        │     early abort ~50s → S8 resend → 再等
        ├─ S10→S14；S11 5xx 清 SO 再试一次
        └─ accounts.ImportRegistered + mailbox MarkUsed + taskstore
```

**协议体位置：** `go-email-protocol/internal/protocol`（CLI 与 worker **共用**）。  
**不采用：** 生产默认 `N × pure-go-register.exe`（lab 金标准可保留）。

### 1.2 旁路 / 历史路径（必须降级或关闭）

| 路径 | 入口 | 问题 |
|---|---|---|
| Python inline 单 job | `mailat_email_protocol_task` → `run_go_email_protocol` | Python 选邮/mint 代理/OTP callback；与 canary 外壳不一致 |
| mailat / Node 协议 | `email_protocol_backend=python` | 历史翻车；ledger 混跑 |
| CLI 多进程齐射 | `canary_n10p` / `pure-go-register` | 协议金标准；**不是** Dashboard 生产模型 |
| hybrid_pipeline / 浏览器注册 | 独立产品线 | **不是** email-protocol 主链 |

### 1.3 Canary 金标准（协议）

| 项 | 值 | 产物 |
|---|---|---|
| n=100 真并行 seed（历史） | **98/100**，wall ~169s | `output/pure_go_register_canary/n100p_seed/summary_20260724_103914.json` |
| n=100 回归 2026-07-26 | **97/100**，wall ~213s | `output/pure_go_register_canary/n100p_seed/summary_20260726_024024.json` |
| 成功 token | access_token 非空 | 同上 |
| 默认策略 | proxy-seed bestgo+1024；regions JP,US,DE,GB,BR；edge-remints 2；email-tries；OTP 宽预算 | `cmd/pure-go-register` + `tools/canary_n10p` |

### 1.4 主网产能现状（连续补位）

| 跑次 | concurrent | 窗口 | ok | fail | rate/min | 备注 |
|---|---:|---:|---:|---:|---:|---|
| 历史 100 座 | 100 | 10min | 559 | 185 | ~54.5 | TUN on |
| 短烟 50 座 | 50 | ~1min | 42 | 2 | ~40 | 加速后健康 |
| **200 齐射** | 200 | ~5min 中止 | ~147 | **800+** | 崩 | 出口/TUN 打穿，`go_active→0` |
| 100 座恢复 | 100 | 10min | 552 | 234 | 53.5 | 分波+熔断后 |

**结论：** 100 座稳态 ~530–560/10min；**200 齐射不可用**；座位 200 可保留作上限，稳态先 80–120 再爬。

---

## 2. 架构对比：已经对齐 vs 仍拖后腿

### 2.1 协议体 / 挑战语义

| 层 | CLI canary | 主网 worker | 差异 |
|---|---|---|---|
| FSM / wire / Sentinel | `protocol.Engine` ModeLive | **同一包** | **无第二套协议** |
| Edge CF | 检测 only；禁止同连接重放 | 同 | 同 |
| S11 body | `randomAboutYouProfile` | 同 | 同 |
| Graph OTP 扫 junk | 有 | 有 | 同 |

### 2.2 控制面（M1 已合入，源码现状）

| 能力 | Canary | 主网代码现状（2026-07-24+） | 仍开着的洞 |
|---|---|---|---|
| multi-region | JP,US,DE,GB,BR | bulk + `go_registration_batch` 默认同 | 需确认运行中 worker/config 真生效 |
| SessionRemints | 2 | `RunnerConfig.SessionRemints=2`；`runProtocolEngine` 默认 2 | diagnostics 未暴露，难运维确认 |
| rotate 换区 | Next region | `rotateRuntimeProxy` + `NextSeedRegion` | style 轮换（bestgo↔1024）未做 |
| email-tries | 5–20 | bulk 默认 5；`isBulkEmailRetryable` | **OTP 旁路失败后** 换邮弱（Q3） |
| OTP 预算 | CLI 常 360s | 主网 120s + ~50s early abort + resend | 误杀 otp_timeout 占比仍高（Q1） |
| 唯一热路径 | 单进程 CLI | TasksService **prefer** batch | 仍可 **静默 fallback** 到 Python inline |
| 落库 | CLI ImportRegistered | bulk 已 Import | inline 仍走 orchestrator JSON |
| 座位 | N 进程 | admission 200 | 200 齐射会塌；需稳态爬坡策略 |
| 启动守卫 | 每次显式 pure-go | `ensure_go_worker` 校验 runner/mode/max_active | 二进制必须 `-tags tlsclient` |

### 2.3 失败率差在哪（不是协议“慢一倍”）

成功路径 p50 两边都约 **30–45s**。  
主网产能低于理论值的主因：

```text
有效产出 ≈ 稳态有效座位 × (600 / 周期) × 成功率

成功率 ↓（OTP 误杀 / 出口 EOF / 邮箱 used）→ 座位被失败占用或空转
补位策略错（200 齐射 / free 狂补）→ 雪崩
邮箱 available=0 → 整窗废
```

**杠杆排序：** 成功率 > 座位不空 > remint/换邮吃失败 > 加座位。

---

## 3. 目标（可验收）

### 3.1 成功率

| 门禁 | 目标 |
|---|---|
| 协议齐射 | `canary_n10p -n 100` **≥95%**（金标准不回退） |
| 软件路径 100 座连续 | 10min 成功率 **≥80%**（ok/(ok+fail)），失败可分类 |
| 软件路径稳态 100–120 座 | 成功率 **≥ canary 主网历史**，且无 unknown 主导 |

### 3.2 产出效率

| 阶梯 | concurrent | 窗口 | 成功目标 | 备注 |
|---|---:|---:|---|---|
| L0 | 100 | 10min | **≥500** | 对齐历史 ~545 |
| L1 | 100–120 | 10min | **≥550** 且成功率 ≥80% | 稳态优先 |
| L2 | 爬到 150 | 10min | ≥ L1 且不雪崩 | 分波 prime |
| L3 | 上限 200 | 10min | 仅当 L2 稳后再冲；**禁止首波 200 齐射** | 见 §5.2 |

### 3.3 产品形态

```text
email_protocol_backend=go 时：
  只允许 /v2/email-register-batches
  worker runner=protocol mode=live transport=tls
  禁止静默掉 mailat / Python 协议
```

---

## 4. 开发工作分解（按优先级）

### 阶段 A — 热路径锁死（成功率地基，P0）

**目标：** 软件路径永远走 Go bulk；错 worker 起不来。

| ID | 改动 | 文件/符号 | 验收 |
|---|---|---|---|
| A1 | `backend=go` 时 batch 失败 **fail closed**，禁止静默 inline | `tasks_service.py` `start_email_protocol_register(_many)`；`try_start_go_batch_register` | 关 worker 时 UI/API 明确失败，无 mailat stage |
| A2 | health 硬校验：`runner=protocol` + `mode=live` + `transport∈{tls,direct}` + `features` 含 batches + `max_active` 对齐 | `start.py` `ensure_go_worker` / `go_worker_mode_ok` | 错进程必杀重启 |
| A3 | 构建/启动文档与脚本强制 `-tags tlsclient` | `start.py`、构建 bat/README 一句 | `/health transport=tls` |
| A4 | Dashboard 默认 `email_protocol_backend=go`；去掉“看起来像 python 协议”的默认 | `config.yaml` / UI Register | 新任务日志只见 go batch |

**不做：** 重写协议；把 Dashboard 整站 Go 化。

---

### 阶段 B — 失败恢复吃干净（成功率主杠杆，P0–P1）

**目标：** 主网失败结构逼近 canary（edge/EOF 可救；used/死邮可换）。

| ID | 改动 | 文件/符号 | 验收 |
|---|---|---|---|
| B1 | **OTP 旁路失败可 re-lease**（`otp_timeout` / `graph_no_openai` / invalid_grant）在 bulk task 层计入 `email_tries`，换区再开 | `bulk.go` `waitBulkOTPAndFinish` / `isBulkEmailRetryable`；与 CLI `isOTPMailboxRetryable` 对齐 | capacity 失败中 otp 换邮后 ok 上升；同邮箱不立刻再 lease |
| B2 | OTP early abort **50s → 65s**；resend 后等待 **55–60s**；总预算 **120–150s**（禁止默认 360s） | `mailbox/outlook_token.go` early abort 常量；`protocol_engine_runner.go` firstWait | otp_timeout 占比下降且 10min 产能不降 |
| B3 | remint 时 **style 轮换** bestgo↔1024（region 已有） | `rotateRuntimeProxy` / `proxypool` | remint 日志出现 style 切换 |
| B4 | `session_invalid` 在 S10 后：明确策略 = 记失败 + bulk 可换邮重开（勿死磕同 cookie） | `runLiveFromOTP` / bulk 分类 | session_invalid 不无限占座 |
| B5 | providers **EOF** 继续走 SessionRemints（已有）；补单元测试防回归 | `protocol_engine_runner_test` | 单测覆盖 needRestart 分支 |

**已在代码、只做确认不重复造轮：**

- SessionRemints=2  
- multi-region bulk + rotate  
- email_tries 对 S11 used / 协议前 OTP 类字符串  
- S11 5xx SO remint ×1  

---

### 阶段 C — 产出效率（座位与补位，P0 运维 + P1 代码）

**目标：** 稳态高 in_flight，不雪崩。

| ID | 改动 | 文件/符号 | 验收 |
|---|---|---|---|
| C1 | capacity **分波 prime**（先 60–80）+ **失败率熔断停补**（已有雏形则固化默认） | `scripts/capacity_10min.py` | 不再出现 200 齐射 `go_active→0` 雪崩 |
| C2 | Dashboard/API 大批次同样：`max_concurrent` 默认 **min(配置, 120)** 或分波；200 仅作 ceiling | `go_registration_batch` / TasksService | 默认不齐射 200 |
| C3 | 空座 refill：半空时大块补，失败率高时缩小（已有参数则写死推荐值） | capacity + bulk 文档 | 10min 平均 in_flight ≥ 0.7× 目标座 |
| C4 | Graph 限流可观测：health 暴露 graph 使用；429 时文档回退 64 | worker diagnostics | 运维可调 |
| C5 | bulk stagger 保持 **≤400ms**（已改）；禁止回 1500ms | `bulk.go` | 首波占座快 |

**产能公式（写进验收）：**

```text
10min_ok ≈ steady_seats × (600 / cycle_s) × success_rate

例：100 × (600/50) × 0.85 ≈ 1020 理论上限
实测 L0：~55/min × 10 ≈ 550（含失败与空座）
```

---

### 阶段 D — 资源与运维门槛（否则一切无效，P0）

| ID | 动作 | 门槛 |
|---|---|---|
| D1 | 跑前检查 `outlook_token` available | 稳态 ≥ concurrent×2；冲 10min ≥ concurrent×4（email_tries 会烧邮） |
| D2 | 恢复误标 `paused_for_hotmail_canary_*` / 可回收 cooldown | 有脚本；写入操作手册 |
| D3 | `proxy_seed` styles 必须 **bestgo+1024**；辣椒禁用主链 | config `proxy_seed_styles` |
| D4 | 国内 **TUN 开**；禁止关 TUN 直连当银弹 | INDEX 红线 |
| D5 | 池空时 capacity **fail fast**，不宣称策略失败 | capacity 前置检查 |

---

### 阶段 E — 观测与验收工程（P1）

| ID | 改动 | 验收 |
|---|---|---|
| E1 | capacity summary：peak_go_active、region histogram、failure_code 占比 | 每次 10min 可贴表 |
| E2 | health/diagnostics：`session_remints`、default regions、graph_max | curl 可见 |
| E3 | ledger 聚合：`protocol_s0_restart_*` 计数 / remint 救活率 | 脚本或 SQL |
| E4 | 软件路径 smoke：`scripts/software_path_smoke.py` n=10→50 | 不靠 CLI 交差 |
| E5 | 协议回归：`canary_n10p -n 100` 写入 G4 / TRUE_200 表 | ≥95% |

---

### 阶段 F — 明确不做 / 延后（P2+）

| 项 | 原因 |
|---|---|
| 再写一套协议 / 改 wire 当产能银弹 | 协议已能 98%；治理债另走 LATEST_HAR |
| 生产 200× CLI 进程 | 无连续补位/共享 lease 观测 |
| OTP 默认 360s | 拖死号、占资源；主网用短预算+resend+换邮 |
| 并发 300+ 齐射 | 出口塌缩已实证 |
| CF 自动解题 | 策略禁止；remint 新 SID |
| HAR R0–R10 挡产能 | 并行治理，不阻塞本计划 A–E |
| 恢复 Python 协议热路径 | 失误率与双路径元凶 |

---

## 5. 推荐实施顺序（执行日历）

```text
Week 切片（可压缩，但顺序别反）:

Day 0  资源盘点 D1–D5 + health 红线 A2/A3
Day 1  A1 fail-closed batch + A4 默认 go
Day 1–2  B1 OTP 旁路 re-lease + B2 early abort 调参
Day 2  C1/C2 分波与默认 concurrent 上限；E1 失败结构
Day 3  L0 capacity 100×10min 回填数字
Day 4  B3 style 轮换 + E2 diagnostics
Day 5  L1 稳态 100–120；协议 canary 回归
之后   L2 爬 150；HAR 治理并行；禁止未回填宣称 200@10min 达标
```

---

## 6. 文件与符号清单（开发时只动这些）

### 6.1 必改 / 优先

```text
application/tasks_service.py          # A1 fail-closed
services/go_registration_batch.py     # C2 默认 concurrent 上限；regions/email_tries 已齐
start.py                              # A2/A3 已有则加固
go-email-protocol/internal/job/bulk.go
go-email-protocol/internal/job/protocol_engine_runner.go
go-email-protocol/internal/mailbox/outlook_token.go   # B2 early abort
go-email-protocol/internal/proxy/seed.go              # B3 style rotate 辅助
go-email-protocol/cmd/email-protocol-worker/main.go   # diagnostics / SessionRemints
scripts/capacity_10min.py             # C1/E1
config.yaml                           # 红线默认值
docs/INDEX.md                         # 权威入口挂本文件
docs/TRUE_200_CONCURRENCY_DELIVERY.md # 回填实测
```

### 6.2 只读金标准（对照，不当分叉实现）

```text
go-email-protocol/cmd/pure-go-register/main.go
go-email-protocol/tools/canary_n10p/
go-email-protocol/internal/protocol/   # 协议体共用
```

### 6.3 禁止再扩

```text
services/mailat_email_protocol_runner.py   # 热路径禁止
vendor/mailat-codex-register               # 非 go backend
hybrid_pipeline / full_pipeline 浏览器注册 # 非本计划范围
```

---

## 7. 验收矩阵（完成定义）

| # | 门禁 | 命令/方法 | 通过 |
|---|---|---|---|
| 1 | Worker 模式 | `GET /health` | runner=protocol, mode=live, transport=tls, max_active=配置, features 含 batches |
| 2 | 热路径 | 关 worker 再点注册 | **失败明确**，无 inline/mailat 成功假象 |
| 3 | 协议不回退 | `canary_n10p -n 100` | ok≥95 |
| 4 | 产能 L0 | `capacity_10min --minutes 10 --concurrent 100 --region JP,US,DE,GB,BR` | ok≥500；成功率≥80% 优先 |
| 5 | 失败结构 | summary / ledger | 主因 ∈ {otp, proxy_or_network, email_used, session_invalid}；remint stage 可见 |
| 6 | 无雪崩 | 任何 ≥150 座跑 | 不得 `go_active→0` 后狂补；需熔断 |
| 7 | 邮箱门槛 | 跑前 SQL/API | available 满足 §D1 |
| 8 | 文档 | INDEX + 本文件 + TRUE_200 表 | 数字回填；不写「HAR 全绿」 |

---

## 8. 决策记录（2026-07-26）

| 决策 | 选择 | 理由 |
|---|---|---|
| 协议体 | **不再分叉**；CLI/worker 共用 Engine | canary 98% 已证 |
| 产品热路径 | **仅 Go batch** | 双路径是失误率主因 |
| 并发模型 | **单 worker + admission**；CLI 多进程仅 lab | 连续补位 / 共享资源 |
| 座位 | ceiling 200；**稳态先 100–120** | 200 齐射已塌缩实证 |
| 成功率优先 | remint/换邮/OTP 调参 > 加座 | 公式与实测一致 |
| OTP | 短预算 + resend + **换邮**；不回 360s 默认 | 成功 ~30s；死邮应换 |
| 代理 | bestgo+1024 + 多 region + socks5h | canary 同款 |
| TUN | 国内必须开 | 关则 0 成功 |
| HAR 治理 | 并行 P2 | 不挡 A–E 产能 |

---

## 9. 与旧文档关系

| 文档 | 关系 |
|---|---|
| TRUE_200 | **座位与 M1 代码交付**仍有效；本文件补「彻底成功率 + 稳态产出」执行合同，并吸收 200 塌缩教训 |
| LAB_CANARY | 根因分析保留；M1 已合部分不再重复开发 |
| TRUE_100 GAP | 历史；双路径/mailat 项由本文件 A 阶段关闭 |
| LATEST_HAR | 协议治理债；**不得**用本计划完成度冒充 R-gate |

---

## 10. 下一步（立即执行）

1. **先做 A1 + D1**：fail-closed batch + 邮箱池盘点。  
2. **再做 B1 + B2**：OTP 旁路 re-lease + early abort 65s。  
3. **跑 L0**：100 座 10min，回填 TRUE_200 / 本文件 §3。  
4. 数字达标后再动 C 爬坡与 B3 style 轮换。

**禁止：** 未完成 A/D 就开 200 齐射；用 CLI 98% 代替软件路径验收。

---

**完。**
