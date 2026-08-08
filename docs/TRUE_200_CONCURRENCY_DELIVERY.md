# True 200 并发交付与持续开发合同

> **状态：** 代码已落地；**座位门禁已绿**；**200@10min 产能数字待回填**（2026-07-24）  
> **目标产物：** 软件 pure-Go 路径 **真实 200 协议座位** + canary 级代理/remint 策略 + 连续产能  
> **金标准：**  
> 1. `/health` → `max_active=200` ✅（已测）  
> 2. `capacity_10min --concurrent 200` 稳态 `in_flight≈200` 且 10min 成功数可测 ⏳  
> 3. canary `n=100` 仍 ≥95%（协议不回退） ⏳（历史 98/100；本轮后未重跑）  
> **入口索引：** [`INDEX.md`](INDEX.md)  
> **关联：** [`LAB_CANARY_VS_MAIN_MERGE_PLAN.md`](LAB_CANARY_VS_MAIN_MERGE_PLAN.md)、[`HIGH_THROUGHPUT_500_PER_10MIN.md`](HIGH_THROUGHPUT_500_PER_10MIN.md)、[`LATEST_HAR_PROTOCOL_RECHALLENGE_PLAN.md`](LATEST_HAR_PROTOCOL_RECHALLENGE_PLAN.md)

---

## 0. 为什么「别人可以、我们不行」

### 0.1 不是机器扛不住

本机 canary：**100 进程齐射**，墙钟 ~169s，**98/100**。  
CPU/内存不是瓶颈；瓶颈是 **代理 RTT + OpenAI + Graph OTP + 失败率**。

### 0.2 真正差在哪

| 维度 | Canary（人家/我们 lab） | 旧主网 | 本轮后主网 |
|---|---|---|---|
| 协议体 | pure-Go + tlsclient | 同 | 同 |
| 座位 | 100 进程 | **config 锁 100** | **200** |
| remint | **2** + 换区 | **1** + 同区 | **2** + 多区轮换 |
| region | JP,US,DE,GB,BR | 单区 JP/VN | **多区 CSV** |
| 换邮 | email-tries 5–20 | 基本 1 次死 | **默认 5** |
| 调度 | 齐射墙钟 | 连续补位弱于策略 | 连续补位 + 策略对齐 |
| 成功率 | ~98% | ~70–85% | 目标逼近 canary |

**结论：** 不是「不能 200」，是 **座位被 config/start 锁在 100**，且 **失败恢复策略弱于 canary** → 同样 100 座也更慢。

### 0.3 产能公式（仍适用）

```text
10 分钟成功 ≈ 有效座位 × (600 / 单号周期) × 成功率

200 座 × (600/45) × 0.85 ≈ 2266 理论上限（理想）
200 座 × (600/50) × 0.70 ≈ 1680 较现实上界
实测目标：先稳住 200 座不空 + 成功率 ≥ canary 主网历史，再冲 1000+/10min
```

**加座位只在「成功率不崩 + 座位不空」时有效。**

---

## 1. 本轮已交付（代码）

### 1.1 200 座位链路

| 层 | 文件 | 变更 |
|---|---|---|
| Go admission | `internal/admission` | `DefaultMaxActive=200`（已有） |
| Worker 启动 | `cmd/email-protocol-worker` | `-graph-max-concurrent` 默认 **96**；`SessionRemints: 2` |
| 配置 | `config.yaml` | `max_parallel_tasks/max_register_tasks: **200**` |
| start | `start.py` | max_active 默认 **200**；graph 默认 **96**；**max_active 不一致强制重启** |
| TasksService | `application/tasks_service.py` | claim/start/lease fan-out 上限 **200**（原 160/128） |
| Bulk cap | `internal/job/bulk.go` | `MaxConcurrent` 仍 clamp **1..200** |

### 1.2 Canary 策略合并

| 策略 | 实现 |
|---|---|
| remint 预算 2 | `RunnerConfig.SessionRemints`；`runProtocolEngine` 默认 2 |
| 多 region 轮换 | `proxy.DefaultSeedRegions/Parse/Next/Pick`；`rotateRuntimeProxy` 换区 mint |
| bulk 多 region | `ProxyRegions` + `proxy_region` CSV；任务 hash 选区；换邮时轮换 |
| email-tries | bulk 默认 **5**；`AttemptID=try` 新 job；already-used/OTP 死邮可换 |
| Python 批客户端 | `go_registration_batch` 默认 region `JP,US,DE,GB,BR` + `email_tries` |
| capacity/smoke | 默认 concurrent **200**、多 region、email_tries=5 |

### 1.2b 启动加速（2026-07-24 续）

| 项 | 旧 | 新 | 目的 |
|---|---|---|---|
| bulk 任务 stagger | 0–1500ms | **0–400ms** | 首波更快占座 |
| email-try 间隔 | 400ms | **200ms** | 换邮少空等 |
| remint 后暂停 | 800ms | **300ms** | 失败恢复更快重开 S0 |
| capacity 轮询 | 3s | **1s** | 空座更快看见 |
| capacity 默认 refill | min(20,free) | **min(free, max(50, concurrent/3))**；半空时更大 | 200 座补位不碎成 1–2 个小批 |

短烟：`concurrent=50` ~62s → **ok=42 fail=2 ~40/min**（`output/cap_20260724_192452_6bcefb_summary.json`）。

### 1.3 构建

```text
cd go-email-protocol
go build -tags tlsclient -o email-protocol-worker.exe ./cmd/email-protocol-worker
go test ./internal/job/ ./internal/proxy/ -count=1   # OK
py -3.13 -m py_compile scripts/capacity_10min.py services/go_registration_batch.py start.py ...
```

---

### 产能（含 200 塌缩实锤）

| 跑次 | concurrent | minutes | ok | fail | rate/min | proj_10m | summary 路径 | 备注 |
|---|---:|---:|---:|---:|---:|---:|---|---|
| 历史 100 座 | 100 | 10 | 559 | 185 | ~54.5 | ~545 | `output/cap_20260724_180620_e3d491*` | TUN on |
| 历史 100 座 | 100 | 3 | 189 | 45 | ~59.5 | ~595 | `output/cap_20260724_182941_1ec177*` | 投影 |
| 短烟 20 座 | 20 | 0.5 | 17 | 0 | 16.2 | ~162 | `output/cap_20260724_192039_358a19_summary.json` | 多 region |
| 短烟 50 座 | 50 | ~1 | 42 | 2 | ~40 | ~403 | `output/cap_20260724_192452_6bcefb_summary.json` | 加速后 |
| **200 齐射塌缩** | 200 | ~5 中止 | **~147** | **~800+** | 崩 | 崩 | run `cap_20260724_192943_38f422` | go_active→0；已 cancel |
| **100 座恢复后** | **100** | **10** | **552** | **234** | **53.5** | **~535** | `output/cap_20260724_194546_7a1a0d_summary.json` | 分波+熔断；邮箱恢复后 |
| **L0 100 座（SUCCESS 后）** | **100** | **10** | **804** | **105** | **78.3** | **~783** | `output/cap_20260726_080916_4cb452_summary.json` | multi-region + B1/B2/B3；成功率 88.4% |
| **L1 120 座 + T1 transport** | **120** | **10** | **965** | **16** | **93.6** | **~936** | `output/cap_20260726_094058_6fe27f_summary.json` | HTTP→HTTPS/wsarecv remint；成功率 **98.4%** |
| L2 150（暂缓） | 150 | 10 | _ | _ | _ | _ | _ | L1 已 98%+ ~936/10min；下一杠杆邮箱质量，非座位。需 >1000/10min 再分波 |
| 200 座 L2（分波+熔断） | 200 | 10 | _ | _ | _ | _ | _ | 待邮箱/出口稳后再爬 |

**200 塌缩结论（2026-07-24）：**

1. 首波 `submit(200)` + 快 stagger → 同时打满 TUN/fake-ip（`198.18.0.1` wsarecv/wsasend）。  
2. `go_active` 一度 ~150，随后 **掉到 0**，capacity 仍按 free 狂补 → **失败雪崩**。  
3. 失败主因：`proxy_or_network` / `protocol_step_failed`(198.18) / `ambiguous_after_send`(timeout) / `otp_timeout`。  
4. **不是协议体突然变差**；是 **出口并发打穿**。50 座仍健康（42/44）。  
5. 修复：capacity **分波 prime（先 60）+ 失败熔断停补**；默认 refill 块改小。  
6. **座位 200 仍保留**；稳态目标 in_flight 先 **80–120**，再爬坡，勿再 200 齐射。

**100 全灭假象（同日稍后）：** `outlook_token available=0`，**4327** 封被标  
`cooldown` + `last_error=paused_for_hotmail_canary_20260724`（人工/脚本暂停，不是协议）。  
已 `UPDATE … SET status=available` 恢复。恢复后 **n=3 → 3/0 成功**。  
跑产能前必须检查：`select status,count(*) … outlook_token`。


### 协议 canary

| 跑次 | n | ok | fail | wall_s | summary |
|---|---:|---:|---:|---:|---|
| 历史 seed | 100 | 98 | 2 | 169 | `output/pure_go_register_canary/n100p_seed/summary_20260724_103914.json` |
| **T1+cooldown 后回归** | **100** | **97** | **3** | **~213** | `output/pure_go_register_canary/n100p_seed/summary_20260726_024024.json` |

2026-07-26 回归失败：`S14 missing access_token`×1、providers `EOF`×1、OTP validate timeout×1。**ok=97≥95 门禁通过**。

### 失败结构模板（每次 capacity 后填占比）

| 类别 | count | % | 备注 |
|---|---:|---:|---|
| otp_timeout / no openai mail |  |  |  |
| proxy/network EOF / wsarecv / 198.18 |  |  |  |
| edge_challenge → remint 恢复 |  |  | stage `protocol_s0_restart_*` |
| session_invalid |  |  |  |
| email_already_used（换邮后） |  |  |  |
| other / unknown |  |  | 必须趋近 0 |

### 资源下限（库内快照 2026-07-24）

| 资源 | 当前 | 200 座建议 | 说明 |
|---|---|---|---|
| `outlook_token` available | **366** | 稳态 ≥400；冲 10min 建议 ≥800 | email_tries=5 会加速消耗 used/cooldown |
| `outlook_token` used+cooldown | ~13k 历史 | — | 正常沉淀 |
| `proxy_seed` available | **2**（bestgo+含 lajiao 标签混杂） | ≥2 且 **styles 含 bestgo+1024** | seed **不独占**；靠 mint SID 扩并发 |
| `lajiao_credentials` | disabled 为主 | 主链不用 | 勿当默认 |
| Graph concurrent | 96 | 96；429 则 64 | 独立于 protocol seat |
| TUN | 必须开（国内） | 开 | 关 TUN 曾 0 成功 |

**硬门槛：** `outlook available < concurrent` 时不要宣称「策略失败」——先灌邮箱。  
**proxy_seed 行数少不是阻塞：** 与 canary 相同，一账户多 SID。

## 2. 操作员怎么跑（必须重启）

旧 worker 若仍是 `max_active=100`，`ensure_go_worker` 现在会因 **max_active 不匹配** 强制重启。

```bat
:: 1) 确认二进制已重建
cd /d E:\project\GPT Register\go-email-protocol
go build -tags tlsclient -o email-protocol-worker.exe ./cmd/email-protocol-worker

:: 2) 重启软件路径（Dashboard / start.py）
cd /d E:\project\GPT Register
py -3.13 start.py

:: 3) 健康检查
curl -s http://127.0.0.1:18765/health
:: 期望: max_active=200, graph_max_concurrent=96, features 含 email-register-batches

:: 4) 连续产能（200 座）
py -3.13 scripts/capacity_10min.py --minutes 10 --concurrent 200 --region JP,US,DE,GB,BR --otp-timeout 120

:: 5) 协议回归（可选）
cd go-email-protocol
go run ./tools/canary_n10p -n 100
```

**国内：** TUN 保持开启（关 TUN 曾 0 成功）。

---

## 3. 架构（交付后）

```text
Python (thin)
  capacity_10min / Dashboard
       │  POST /v2/email-register-batches
       │  max_concurrent≤200
       │  proxy_regions=[JP,US,DE,GB,BR]
       │  email_tries=5
       ▼
email-protocol-worker (single process)
  admission.MaxActive = 200
  bulk workers = max_concurrent
       │  lease outlook_token
       │  MintSeedSession(region_i) socks5h
       │  Create → protocol S0..S9
       │  edge/EOF → remint ≤2 + NextSeedRegion
       │  waiting_for_otp → Release seat → Graph OTP
       │  S10..S14 → import accounts
       ▼
  continuous refill: free seats → next task
```

**不采用：** 生产默认 200×`pure-go-register.exe`（lab 可；连续补位/lease/观测用 daemon）。

---

## 4. 仍存在的问题（诚实清单）

### 4.1 P0 运行时（上线后盯）

| ID | 问题 | 信号 | 处理 |
|---|---|---|---|
| R1 | 200 座下代理 EOF/198.18 上升 | capacity fail 结构 | 保持 TUN；styles=bestgo,1024；看 remint 是否吃掉 |
| R2 | Graph 429 / OTP 变慢 | graph p95、otp_timeout | graph_max 96→64 回退；勿盲目加 |
| R3 | 邮箱池不够 200 并行 | mailbox lease 失败 | 先灌 outlook_token；email_tries 会加速消耗 |
| R4 | 旧进程未重启 | health max_active≠200 | start 已强制；手动 kill 端口 18765 |

### 4.2 P1 策略/质量

| ID | 问题 | 说明 |
|---|---|---|
| Q1 | OTP early abort ~50s | 可调 65s 降误杀（未在本轮改协议 OTP 内核） |
| Q2 | session_invalid 仍可能烧号 | remint=2 覆盖部分；S10 后仍可能废 |
| Q3 | bulk 换邮不重试「已进 OTP 后」死码 | OTP 旁路失败当前终态；可后续在 finishBulk 加 re-lease |
| Q4 | canary OTP 360s vs 主网 120s | 主网保持短预算；不回 6min |

### 4.3 P2 治理（不挡 200）

| ID | 问题 | 文档 |
|---|---|---|
| G1 | HAR R0–R10 复选框几乎未勾 | LATEST_HAR… |
| G2 | CLI/worker bootstrap 未完全统一 | 同左 |
| G3 | diagnostics 缺 contract/pin | 同左 |

### 4.4 明确不做

- 并发 300+ 当银弹  
- 关 TUN 直连国外入口  
- 恢复 Python 协议热路径  
- 宣称 HAR wire gate 全绿  

---

## 5. 验收矩阵

| 门禁 | 方法 | 通过 |
|---|---|---|
| 座位 | `GET /health` | `max_active=200`，`graph_max_concurrent=96` |
| 配置 | `config.yaml` | register/parallel = 200 |
| 重启语义 | 改 max_register 后 ensure | 旧 worker 被杀并拉起 |
| 协议 | canary_n10p -n 100 | ok≥95 |
| 产能 | capacity_10min -c 200 -m 10 | 记录 ok/fail/rate；目标先 **≥历史 100 座的 1.5×**，再冲 500+/10min 旧目标的抬升版 |
| 失败结构 | ledger 聚合 | edge remint 有 `protocol_s0_restart_*`；无 unknown 主导 |
| 多 region | bulk 日志 / seed region | 成功任务 region 分布非单点 |

### 建议 KPI 阶梯

| 阶梯 | concurrent | 窗口 | 成功目标（务实） |
|---|---:|---:|---|
| L0 | 100 | 3min | 对齐旧 ~60/min |
| L1 | 200 | 3min | ≥ 1.3× L0 速率 |
| L2 | 200 | 10min | ≥800（若成功率≥80%） |
| L3 | 200 | 10min soak | 失败可分类、无进程泄漏 |

---

## 6. 后续开发 backlog（持续）

### M-A 观测（半天）

- [ ] capacity 输出 peak_go_active / region histogram  
- [ ] health/diagnostics 暴露 session_remints、default_regions  
- [ ] ledger 聚合脚本：`protocol_s0_restart_*` 计数  

### M-B OTP（0.5–1 天）

- [ ] early abort 50→65s  
- [ ] finishBulk OTP 死邮 → email re-lease 新 attempt（可选）  
- [ ] Graph p50/p95 打点  

### M-C 代理（持续）

- [ ] remint 时 style 也轮换（bestgo↔1024）  
- [ ] 失败 region 冷却（短时避开烂区）  
- [ ] 198.18 专项：Clash 规则 vs socks5h 路径文档  

### M-D HAR 治理（并行，不挡产能）

- [ ] 按 LATEST_HAR R1–R3 离线骨架  
- [ ] 不把 load gate 当 wire 证明  

---

## 7. 决策记录

| 决策 | 选择 | 理由 |
|---|---|---|
| 生产并发 | **200 seat 单 worker** | 用户要实际产物；admission 已支持 |
| Lab 100 进程 | **保留** | 机器无压力；协议金标准 |
| remint | **2** | canary 实证 |
| region | **JP,US,DE,GB,BR** | canary 默认 |
| email-tries | **5** | canary_n10p；bulk 默认 |
| Graph | **96** | 与 config 对齐；可回退 64 |
| 更快手段 | 成功率+补位+策略 | 非盲目 300 |

---

## 8. 变更文件清单

```text
go-email-protocol/internal/job/types.go              # SessionRemints
go-email-protocol/internal/job/protocol_engine_runner.go  # remint=2, multi-region rotate
go-email-protocol/internal/job/bulk.go               # email-tries, multi-region
go-email-protocol/internal/proxy/seed.go             # region helpers
go-email-protocol/cmd/email-protocol-worker/main.go  # graph 96, SessionRemints 2
config.yaml                                          # 200/200
start.py                                             # defaults + max_active restart
application/tasks_service.py                         # fan-out 200
services/go_registration_batch.py                    # multi-region + email_tries
scripts/capacity_10min.py                            # concurrent 200 defaults
scripts/software_path_smoke.py                       # multi-region defaults
docs/TRUE_200_CONCURRENCY_DELIVERY.md                # 本文
docs/LAB_CANARY_VS_MAIN_MERGE_PLAN.md                # 先前对比（已修正多进程压力表述）
```

---

## 9. 止血 / 回滚开关（运维）

按严重程度从轻到重。**先降风险，再查协议。**

### 9.1 软降级（不改代码）

| 症状 | 动作 |
|---|---|
| 失败率飙、代理 EOF 多 | `capacity --concurrent 100` 或 150；region 先 `JP` 单区对比 |
| Graph 429 / OTP 变慢 | env `GO_GRAPH_MAX_CONCURRENT=64` 后重启 worker |
| 邮箱租约失败 | 停投递；导入 outlook_token；看 available 是否 < concurrent |
| 疑似策略回归 | 跑 `canary_n10p -n 25`；若 canary 也烂 → 协议/代理环境；若 canary 好 → 主网调度 |

### 9.2 配置回滚到 100 座

```yaml
# config.yaml
max_parallel_tasks: 100
max_register_tasks: 100
```

```bat
set GO_EMAIL_PROTOCOL_MAX_ACTIVE=100
set GO_GRAPH_MAX_CONCURRENT=64
py -3.13 -c "import start; start.ensure_go_worker()"
:: 确认 /health max_active=100
```

### 9.3 策略回滚（单区 / 少换邮）

```bat
py -3.13 scripts/capacity_10min.py --concurrent 100 --region JP --otp-timeout 120
:: 或 overrides: proxy_region=JP, email_tries=1
```

代码默认仍是多区+tries=5；回滚用 **调用参数/配置覆盖**，不必立刻 revert git。

### 9.4 环境红线（不要「修协议」）

- 国内 **TUN 开**；关 TUN 导致 ruleset 拒 OpenAI 时先恢复 TUN  
- `proxy_seed_styles=bestgo,1024`；不要默认辣椒  
- 出网 **socks5h**；不要 PROCESS DIRECT 国外入口当银弹  

### 9.5 何时才改代码回退

- canary n=100 **跌破 90%** 且与本轮 remint/region 变更时间对齐  
- 200 座出现 **跨 job 串号 / 空 token 成功**（质量事故）  
- 否则优先调 concurrent / graph / region / 资源池  

---

## 10. 一句话

**200 并发产物 = 座位打开到 200 + canary 的 remint/多区/换邮合进 daemon + 连续补位。**  
机器从来不是主因；**旧 config 锁 100 + remint=1 + 单区** 才是「别人可以我们不行」的根。

**座位门禁已绿。下一步：填 §1.4 产能表（`capacity_10min --concurrent 200`）。**
