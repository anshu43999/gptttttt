# Lab Canary 与主网合并 / 更快路径开发计划

> **状态：** 调研结论 + M1 策略已合入代码；**200 座见 TRUE_200**（2026-07-24）  
> **日期：** 2026-07-24  
> **范围：** pure-Go 注册协议体、CLI canary、email-protocol-worker 主网、连续产能  
> **证据权威：** 源码 + 实测 summary + 既有计划文档；**不以记忆/STATUS 为准**  
> **入口：** [`INDEX.md`](INDEX.md) · 交付：[`TRUE_200_CONCURRENCY_DELIVERY.md`](TRUE_200_CONCURRENCY_DELIVERY.md)
> **关联文档：**
>
> | 文档 | 角色 |
> |---|---|
> | [`LATEST_HAR_PROTOCOL_RECHALLENGE_PLAN.md`](LATEST_HAR_PROTOCOL_RECHALLENGE_PLAN.md) | **最新协议体 / wire 合同**（d17/d24 HAR 重新挑战） |
> | [`PURE_GO_FULL_FINGERPRINT_PLAN.md`](PURE_GO_FULL_FINGERPRINT_PLAN.md) | 指纹 / Sentinel / pure-Go 终态 |
> | [`TRUE_100_CONCURRENCY_GAP_AND_WORKLIST.md`](TRUE_100_CONCURRENCY_GAP_AND_WORKLIST.md) | CLI 稳 vs 软件路径差距 |
> | [`HIGH_THROUGHPUT_500_PER_10MIN.md`](HIGH_THROUGHPUT_500_PER_10MIN.md) | 产能目标与调度不变量 |
> | [`EMAIL_PROTOCOL_GO_PLAN.md`](EMAIL_PROTOCOL_GO_PLAN.md) | FSM / V2 API / admission 基线 |
> | [`TRUE_200_CONCURRENCY_DELIVERY.md`](TRUE_200_CONCURRENCY_DELIVERY.md) | **200 座交付合同（本轮落地）** |

---

## 0. 一句话结论

1. **Lab/canary 并没有“单号协议快一倍”**——成功任务 p50 仍约 **30–40s**，与主网成功路径同量级。  
2. canary **失败率极低（98/100）**，是因为 **调度更简单 + 代理/重试策略更完整 + 统计是齐射墙钟**，不是另一套神秘协议。  
3. 主网要更快，应 **对齐 canary 的代理/remint/多 region 策略**，并坚持 **连续补位产能 KPI**；不要再分叉第二套协议体。  
4. **协议体权威文档是 `LATEST_HAR_PROTOCOL_RECHALLENGE_PLAN.md`**（2026-07-24 HAR 重新挑战）。该文档 **R0–R10 复选框几乎全未勾**（0 done / 129 todo），但 **live pure-Go 路径已能出号**——文档门禁与 live 能力 **脱节**，合并时必须显式处理，不能假装“文档全绿”或“文档作废”。

---

## 1. 你忘了的那份文档是哪份？完成度如何？

### 1.1 权威文档

**主文档：`docs/LATEST_HAR_PROTOCOL_RECHALLENGE_PLAN.md`**

- 日期：2026-07-24  
- 案例：`cases/2026-07-24-har-rechallenge/assessment.state.json`  
- 证据 HAR：
  - **H17** 注册基线（Firefox 150 / pt-BR / case-001 来源）
  - **H18** Plus/Stripe checkout（**禁止**当注册基线）
  - **H24** 最新成功注册（Firefox 150 / ja-JP / create_account 200）

配套历史：

- `PURE_GO_FULL_FINGERPRINT_PLAN.md` — Bundle/TLS/Sentinel 实现进度（大量已勾）  
- `TRUE_100_CONCURRENCY_*` — 100 并发与 CLI vs 软件差距  
- `EMAIL_PROTOCOL_GO_PLAN.md` — FSM/API 基线  

### 1.2 文档完成度（诚实账）

| 文档 | 复选框 done | todo | 解读 |
|---|---:|---:|---|
| **LATEST_HAR_PROTOCOL_RECHALLENGE_PLAN** | **0** | **129** | 合同写全了，**R0–R10 工程门禁基本未落地勾选** |
| PURE_GO_FULL_FINGERPRINT_PLAN | 53 | 28 | 指纹/Sentinel/live 主链大量已做；SO 字节级 HAR 等未办 |
| TRUE_100_CONCURRENCY_GAP_AND_WORKLIST | 22 | 59 | CLI 100 稳、软件路径仍有债 |

`assessment.state.json` 仍写 `phase: analysis`、规划期禁止高速率 live——与今天 **已跑 n=100 canary / 10min capacity** 的事实并存。

### 1.3 文档 vs 现实（关键矛盾）

```text
文档要求：
  HAR contract → 离线 replay → wire gate → 1→5→10→25 canary → 再 100 load

现实：
  pure-Go live + tlsclient 已能注册
  CLI canary_n10p -n 100 → 98/100
  主网 capacity 10min → ~545/10min（开 TUN）
  但 R0–R10 机器门禁 / contract release / offline replay 多未按文档完成
```

**合理态度：**

- **协议实现能力**：已可用（以 canary 为金标准实测）  
- **协议治理能力**（contract/replay/promotion）：文档要求的大部分 **未完成**  
- 合并主网时：**live 路径以 canary 行为为准**；**治理债单独排期**，不阻塞“把 canary 策略合进 daemon”，但 **不得宣称 HAR wire gate 已全绿**

---

## 2. 两条路径对照（更快 / 更稳）

### 2.1 架构

```text
【Lab Canary】tools/canary_n10p
  → N 个 pure-go-register.exe 真并行
  → 每进程：proxy-seed mint SID + S0–S14 + 进程内 OTP
  → edge remint ≤2 + 多 region 轮换 + email-tries
  → 无 Dashboard / 无 bulk HTTP / 无 PG task 状态机

【主网】scripts/capacity_10min + email-protocol-worker
  → 单 daemon + admission(100)
  → bulk/capacity 连续补位
  → OTP 旁路不占 seat
  → PG tasks + accounts 落库
  → 代理/OTP/remint 策略弱于 canary（见 §3）
```

### 2.2 实测（同日、同项目）

| 指标 | CLI canary n=100 | 主网 capacity（开 TUN） |
|---|---:|---:|
| 成功/提交 | **98/100（98%）** | 3min: 189/296（~81%）；10min: 559/805（~70%） |
| 墙钟 | **169s** 齐射结束 | 连续补位 3–10min |
| 成功耗时 p50 | **~40s** | **~40s**（同量级） |
| 成功耗时 p90 | **~58s** | 略高 / 长尾更多 |
| 折合产能 | 齐射口径 ~35/min | **连续 ~55–60/min（~545–595/10min）** |
| 失败形态 | 2× providers EOF | OTP 超时 + 198.18/wsarecv + session_invalid + callback |

### 2.3 “为啥 canary 时间少 / 失败少？”

**不是单号协议更快，而是：**

| 因素 | Canary | 主网缺什么 |
|---|---|---|
| **成功率** | 98% | 常 70–85%；失败吃座位 |
| **调度** | 100 进程齐射，墙钟≈最慢任务 | 有 HTTP/admission/OTP 旁路/多波 refill；尾部仍空座 |
| **代理** | `proxy-seed` + **多 region** JP,US,DE,GB,BR | 常单区 JP/VN；region 轮换弱 |
| **edge remint** | **预算 2**，且实测 5 次 remint 全救活 | 多为 1 次；EOF 未完全等同 canary |
| **邮箱重试** | `-email-tries 5`（CLI 默认更高） | bulk 路径换邮弱 |
| **OTP 策略** | canary 用 **360s** 宽预算（成功仍早退） | 主网 **120s + 50s 无信 early abort**——失败砍得更狠，OTP 失败占比更高 |
| **控制面** | 无 PG task / Dashboard | 有；正确但增加抖动面 |
| **统计** | 固定 n 齐射墙钟，观感“快” | 连续产能 KPI 更真实，但失败结构更脏 |
| **协议体** | 同一 pure-Go Engine + tlsclient | **应同一套**；不得再分叉 |

**成功路径 p50 都是 ~40s** → 再堆并发到 300 **解决不了**“感觉慢”；解决的是 **失败率 + 空座 + 代理策略**。

---

## 3. 主网相对 canary 缺什么（差距清单）

### 3.1 已有（主网不弱的部分）

- [x] pure-Go live + tlsclient  
- [x] 进程内 Graph OTP（不依赖 Python 收码）  
- [x] OTP 不占 admission seat  
- [x] bulk 内 OTP 旁路、协议 worker 可补位  
- [x] `capacity_10min` 连续补位  
- [x] socks5h mint  
- [x] edge → 换 SID + S0（部分）  
- [x] EOF/timeout → 换 SID（已加，需与 canary 预算对齐）  
- [x] 短 OTP + 无信 early abort + S8 resend  

### 3.2 明确落后 canary（应合并）

| ID | 缺口 | Canary 行为 | 主网现状 | 优先级 |
|---|---|---|---|---|
| G1 | **多 region 轮换** | `JP,US,DE,GB,BR`，remint 换区 | 默认单区 | **P0** |
| G2 | **edge remint 预算** | 2 | 多为 1 | **P0** |
| G3 | **providers/EOF 级 remint** | 进程级 email-tries + remint 组合 | 有 EOF remint，但未证明与 canary 同等覆盖 | **P0** |
| G4 | **email-tries / 死邮换号** | 5–20 | bulk 单邮失败即终态偏多 | **P1** |
| G5 | **OTP 失败策略平衡** | 宽预算少误杀 | early abort 50s 抬高 otp_timeout 占比 | **P1**（调参，非加长到 6min） |
| G6 | **CLI/worker 同一 bootstrap gate** | CLI 可绕过部分 gate（文档批评点） | worker 有 SDK drift 等 | **P1**（治理） |
| G7 | **HAR contract / offline replay 门禁** | 文档要求 | 几乎未勾 | **P2**（治理，不阻塞策略合并） |
| G8 | **diagnostics 暴露 contract/profile/pin** | 文档要求 | health 仅 runner/mode/seats | **P2** |

### 3.3 主网独有、canary 没有（必须保留）

- Dashboard / PG `tasks` / `accounts` 落库  
- 连续 10 分钟产能 KPI（`capacity_10min`）  
- admission 全局 seat、OTP 释放 seat  
- 软件路径可观测（batch status / waiting_for_otp）  

**合并原则：canary 的“打法”（多 region / remint / email-tries）进 daemon。** 100 进程齐射 **短时 lab 完全可接受**（本机实测 n=100 墙钟 169s、无明显机器瓶颈）；不进 Dashboard 生产默认，是因为 **连续补位 / 共享资源 / 可观测 / 落库**，不是因为“100 进程机器扛不住”。

---

## 4. 协议体是否“最新”？

### 4.1 实现层（live）

- 使用 pure-Go `protocol.Engine` + Sentinel pin `20260219f9f6` + tlsclient Firefox 路径  
- H24 时代 live 已能 **稳定出 access_token**（canary 98%、capacity 可 500+/10min 量级）  
- **结论：生产可用的协议体 ≈ lab pure-Go 最新 live 路径**

### 4.2 治理层（文档 R0–R10）

按 `LATEST_HAR_PROTOCOL_RECHALLENGE_PLAN`：

| 阶段 | 文档要求 | 现状（调研） |
|---|---|---|
| R0 证据/运行事实 | 未勾 | 部分口头收敛，无机器 gate |
| R1 HAR contract | 未勾 | 缺完整 ingest/contract 发布 |
| R2 Sentinel release manifest | 未勾 | pin 有，loader/source set 合同不完整 |
| R3 真离线 replay | 未勾 | fixture 非完整 wire replay |
| R4 按 contract 修 wire | 未勾 | live 能过，但无 contract 证明 |
| R5 Transport wire gate | 未勾 | tlsclient 在用；direct 仍存在 |
| R6 统一 bootstrap | 未勾 | CLI/worker 仍可能不一致 |
| R7 CF 因果 | 部分实现 | 有 `edge_challenge_required` 检测；remint 在 CLI 更强 |
| R8 Offline rechallenge gate | 未勾 | — |
| R9 Live 1→25 canary | 部分 | **直接有 n=100 CLI canary**，但非文档阶梯门禁 |
| R10 TRUE_100 合流 | 部分 | 并发能力有；wire gate 未按文档关闭 |

**不得对外宣称“已按 LATEST_HAR 文档全部门禁完成”。**  
**可以宣称“live pure-Go 与 lab canary 同协议栈，且 canary n=100 实证 98%”。**

---

## 5. 其它仍存在的问题（调研）

### 5.1 代理 / 网络环境

| 问题 | 证据 | 影响 |
|---|---|---|
| **TUN/fake-ip 与直连两难** | 开 TUN：有 198.18 但能出号；关 TUN：ruleset 拒 chatgpt → 0 成功 | 国内环境必须挂 IP；不能简单 PROCESS DIRECT 上游 |
| bestgo/1024 ruleset | 关 TUN 后 `connection not allowed by ruleset` | 代理产品策略，非协议 bug |
| 高并发半截断 | validate/callback `wsarecv` / EOF | 失败主因之一 |

### 5.2 OTP

| 问题 | 证据 | 影响 |
|---|---|---|
| 成功 OTP ~30s | ledger p50 | 正常 |
| 主网 early abort ~50s 无信 | 失败日志 `after 50s ... no openai mail` | 降长尾，但抬 otp_timeout 计数 |
| canary OTP 360s | canary 参数 | 少误杀，拖死号更久（canary 可接受） |

**合理中间态：** 总预算 120s；无信 **60–70s** 再 resend（略宽于 50s）；resend 后 50–60s；**禁止**回到 6 分钟。

### 5.3 会话

| 问题 | 证据 | 影响 |
|---|---|---|
| S10/S11 `session no longer valid` | capacity 失败 | OTP 等到了也废；需换号/整段重开 |

### 5.4 控制面

| 问题 | 影响 |
|---|---|
| 单批 n=100 wall 当 KPI | 严重低估连续产能 |
| CLI canary 与 worker 参数默认不一致 | 同协议不同打法 → 失败率差一截 |
| HAR 文档门禁未落地 | 无法机器证明“未漂移” |

### 5.5 明确不要做的“假更快”

- 并发拉到 200–300 齐射当银弹（成功率掉则更慢）  
- OTP 加长到 3–6 分钟  
- 再养一套 Python 协议  
- 关 TUN 强行直连国外入口（国内会挂）  
- **把“机器扛不住 100 进程”当理由**——本机实测不成立；若选多进程，要先解决 **PG/邮箱/代理 lease 争用 + 连续补位**，不是先谈 CPU

---

## 6. 合理“更快”方案（对比后的选择）

### 6.1 产能公式（务实）

```text
10 分钟成功数 ≈ 有效座位 × (600 / 单号有效周期) × 成功率

单号有效周期 ≈ 协议+OTP 成功耗时（~40–60s）+ 调度空座浪费
```

| 杠杆 | 作用 | canary 启示 |
|---|---|---|
| **成功率** | 最大 | 98% vs 80% |
| **连续补位、座位不空** | 大 | capacity 已证明 500+/10min 量级 |
| **多 region + remint** | 大 | canary 核心 |
| **OTP 误杀↓** | 中 | 略放宽 early abort，不回到长超时 |
| **加并发 >100** | 小/负 | 成功率掉则更慢 |

### 6.2 推荐运行模型（主网）

```text
email-protocol-worker（单进程）
  admission.max_active = 200
  proxy-seed + socks5h
  regions = JP,US,DE,GB,BR（可配）
  edge_remints = 2
  EOF/timeout remint = 与 edge 同预算
  email_tries (bulk) = 5
  OTP: 成功即返回；无信 early abort + resend；总预算 ~120–150s
  capacity_10min：始终维持 in_flight≈200
KPI：滚动 10 分钟成功数（100 座历史 ~545；200 座待测）
门禁：canary_n10p -n 25/100 作协议回归（不替代产能）
```

---

## 7. 合并工作分解（可执行）

### M0 — 认知与门禁（本文落地）

- [x] 标明协议权威文档与完成度诚实账  
- [x] canary vs 主网更快对比  
- [x] 主网缺口列表  
- [x] 发版/文档禁止写“HAR R0–R10 已完成”（见 INDEX + LATEST_HAR 0/129）  
- [x] 文档入口 [`INDEX.md`](INDEX.md)

### M1 — 策略对齐 canary（P0）— **代码已合 2026-07-24**

- [x] worker/bulk/capacity 默认 **`proxy_regions=JP,US,DE,GB,BR`**（可配置）  
- [x] **edge remint 预算 = 2**；region 轮换与 canary 一致  
- [x] **EOF / providers / timeout** 与 edge 同一 remint 通道（预算 2）  
- [x] bulk **email 失败可换邮**（默认 email_tries=5）  
- [x] capacity 默认 concurrent=200、多 region、styles 对齐  
- [ ] 验收：`canary_n10p -n 100` ≥95%（本轮后重跑）  
- [ ] 验收：`capacity_10min --concurrent 200` 数字写回 TRUE_200 §1.4  

### M2 — OTP 调参（P1）

- [ ] 无 OpenAI 信 early abort：**50s → 65s**（降误杀）  
- [ ] resend 后等待：**50s → 55–60s**  
- [ ] 总预算保持 **120–150s**；禁止 360s 作为主网默认  
- [ ] 验收：otp_timeout 占比下降，且 10min 产能不回退  

### M3 — 协议治理债（P2，不阻塞 M1）

按 `LATEST_HAR_PROTOCOL_RECHALLENGE_PLAN` 原顺序推进，但 **与产能并行、不挡 200**：

- [ ] R1–R3 离线 contract/replay 骨架  
- [ ] R6 CLI/worker 同一 bootstrap  
- [ ] diagnostics 暴露 release/profile/pin（无秘密）  
- [ ] 1→5→10→25 文档阶梯 canary 报告格式（可包装现有 canary_n10p）  

### M4 — 环境说明（运维）

- [x] 国内默认 **开 TUN** 跑产能（实测；写入 INDEX / TRUE_200 红线）  
- [x] 文档写明：关 TUN 可能导致上游 ruleset 拒 OpenAI  
- [x] 不推荐 worker DIRECT 国外代理入口  
- [x] 止血/回滚开关见 TRUE_200 §9  

---

## 8. 验收矩阵

| 门禁 | 命令/方法 | 通过标准 |
|---|---|---|
| 协议齐射 | `canary_n10p -n 100 ...` | ok≥95，无大面积 edge/OTP 新类 |
| 连续产能 | `capacity_10min --minutes 10 --concurrent 100 --region JP` | **≥500 成功 / 10min** |
| 失败结构 | ledger/PG 聚合 | 198.18/wsarecv、otp_timeout、session_invalid 可分类；无 unknown 主导 |
| 协议声称 | 文档/发版 | 未完成 R-gate 不得写“HAR 全对齐” |
| 单号耗时 | 成功任务 | p50 仍应在 30–50s；异常飙升即回归 |

---

## 9. 决策记录（2026-07-24）

| 决策 | 选择 | 理由 |
|---|---|---|
| 协议金标准 | **CLI pure-go-register + canary_n10p** | 98/100 实证 |
| 产能金标准 | **capacity_10min 连续补位** | 真实 10 分钟窗口 |
| 协议文档权威 | **LATEST_HAR_PROTOCOL_RECHALLENGE_PLAN** | 2026-07-24 HAR 合同 |
| 文档完成度 | **治理未完成，live 可用** | 0/129 vs 能出号 |
| 生产模型 | **默认单 worker 200 seat**；**lab 100 进程齐射保留** | 机器压力不是瓶颈；连续产能/共享资源用 daemon；见 TRUE_200 |
| 更快路径 | **成功率 + 连续补位 + canary 代理策略** | 非盲目加并发 |
| TUN | **开着跑**（国内） | 关 TUN 曾 0 成功 |
| OTP | **短预算 + resend，略放宽 early abort** | 成功 ~30s；禁止 6min |

---

## 10. 附录：关键路径与产物

| 路径 | 用途 |
|---|---|
| `go-email-protocol/tools/canary_n10p` | N 进程真并行 canary |
| `go-email-protocol/cmd/pure-go-register` | 单号协议 + proxy-seed + edge remint |
| `go-email-protocol/cmd/email-protocol-worker` | 主网 daemon |
| `scripts/capacity_10min.py` | 10 分钟连续补位产能 |
| `scripts/software_path_smoke.py` | 软件路径单批 smoke |
| `output/pure_go_register_canary/n100p_seed/summary_20260724_103914.json` | canary 98/100 |
| `output/cap_20260724_180620_e3d491_summary.json` | capacity ~545/10min |
| `output/cap_20260724_182941_1ec177_summary.json` | capacity 3min ~595/10min 投影 |
| `cases/2026-07-24-har-rechallenge/assessment.state.json` | HAR 案例状态 |

---

## 11. 下一步（只做合理的）

1. **M1 策略合并**（多 region、remint=2、EOF/email-tries）→ 再跑 canary100 + capacity10min  
2. **M2 OTP 微调**（early abort 50→65s）  
3. **M3** 按 LATEST_HAR 补治理，不挡产能  
4. 不再讨论“再加 300 并发”直到失败结构接近 canary  

**完。**
