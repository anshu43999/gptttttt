# G4 pure-Go canary log

**默认 backend 仍为 python。** 未达 flip 门槛前不改全局。

## 批次

| 时间 (UTC) | n | ok | fail | 主因 | summary |
|---|---:|---:|---:|---|---|
| 2026-07-18 08:02 | 8 | 6 | 2 | 混合 | `output/pure_go_register_batch/logs_20260718_080253/summary.json` |
| 2026-07-18 14:44 | 8 | **8** | 0 | — | `output/pure_go_register_batch/logs_20260718_144424/summary.json` |
| 2026-07-18 14:50 | 8 | 0 | 8 | **otp_timeout** ×8 | `logs_20260718_145012` |
| 2026-07-18 14:55 | 8 | 0 | 8 | **otp_timeout** ×8 | `logs_20260718_145500` |
| 2026-07-18 17:48 | 3 | **3** | 0 | 降并发 + 新默认 360s | `logs_20260718_174801`（OTP ~1.5min 内到） |
| 2026-07-18 17:56 | 8 | **8** | 0 | 同路径对照（曾 0/8） | `logs_20260718_175600` 81–156s |
| 2026-07-18 17:59 | 12 | **12** | 0 | 更高并发 | `logs_20260718_175900`；1 路代理死重租后 357s 仍成功 |
| 2026-07-18 18:05 | 16 | **16** | 0 | 压到原失败并发 | `logs_20260718_180528` 74–253s 全过 |
| 2026-07-19 04:49 | 8 | **8** | 0 | — | distinct UTC window; 92–157s; `logs_20260719_044955` |
| 2026-07-19 05:50–06:04 | 2 | 0 | 2 | Worker V2 `protocol_step_failed` | `data/go-email-protocol-ledger.db`; dashboard path, insufficient sample |
| 2026-07-19 09:10–09:12 | 1 | **1** | 0 | Worker V2 direct SOCKS | `data/go-email-protocol-ledger.db`, `task_20260719_170628_6c303d`; `protocol_done` |
| 2026-07-19 11:39 | 32 | **31** | 1 | Outlook pure-Go; graph refresh EOF ×1 | `logs_20260719_113936` |
| 2026-07-19 11:43 | 50 | **48** | 2 | Outlook pure-Go; S11 ×2 | `logs_20260719_114336` |
| 2026-07-19 11:47 | 100 | **93** | 7 | Outlook pure-Go; graph/proxy/S11 混合 | `logs_20260719_114719` |

## Outlook 令牌 pure-Go 压测（2026-07-19）

导入：`E:/Download/卡密信息_XJL26071919205X914.txt`（1000 条，`email----password----client_id----refresh_token`）。  
旧 Outlook 池 600 条已清空，替换为这 1000 条。

首轮 n=32 几乎全败：`AADSTS70000 invalid_grant`。**不是号废**，是 Graph refresh scope。  
这批 MSA token 拒绝 `https://graph.microsoft.com/Mail.Read offline_access`，  
接受 `https://graph.microsoft.com/.default offline_access`。  
已修 Go/Python Graph refresh，并重跑。

| 批次 | 结果 | 耗时 | 失败拆分 |
|---|---|---:|---|
| n=32 | **31/32** | 196s | graph refresh 网络 EOF ×1 |
| n=50 | **48/50** | 177s | OpenAI S11 ×2 |
| n=100 | **93/100** | 351s | graph refresh 网络 ×3；S11 ×2；authorize/proxy ×1；S10 OTP validate EOF ×1 |

结论：

- pure-Go + Outlook token + direct SOCKS **能到 100 并发**。
- 成功中位耗时约 **70–75s**。
- 剩余失败主要是 Graph/代理网络抖动和少量 OpenAI S11，不是邮箱池枯竭。
- 这是 **CLI pure-Go** 证据；Dashboard → Worker V2 默认切换门槛仍另算。


## 门槛：已按运营要求 flip 默认 Go

CLI 证据：Outlook pure-Go **31/32 + 48/50 + 93/100**。  
Dashboard Worker V2 此前仅 1 个 direct SOCKS 成功样本；按用户“开始跑号”要求，**默认 backend 已切 go**。

生效配置：

- `email_protocol_backend: go`
- `go_email_protocol_transport: direct`
- `go_email_protocol_mode: pure`
- `mailbox_provider: outlook_token`
- UI Register 默认协议后端 = Go
- `start.py` 默认启动 pure-Go worker（`GO_EMAIL_PROTOCOL_PURE_GO=0` 可回退 mailat）


## Worker V2 诊断修复（2026-07-19）

- 两个失败都发生在 OTP **之前**：S6 返回 `/email-verification`，但响应体不含 `passwordless_signup` 标记。持久化证据只能证明“邮箱已用或会话无效”，不能把两者之一当作已证实根因。
- Worker 原先把完整的已脱敏错误仅写入 ledger `result_json`；V2 状态响应只给 `failure_code`，Python 又优先记录该 code，Dashboard 因而丢失了 S6 细节。
- 已让 V2 `StatusView.message` 返回该已脱敏错误，并让 Python 优先使用 `message`。Go worker 已重新构建、重启并通过 `/health`。
- 回归：`go test ./internal/api ./internal/job -count=1`；`pytest tests/test_mailat_email_protocol_runner.py -q`（18 passed）。
- 后续的 direct SOCKS 实际 canary 为 **1/1 成功**：Worker ledger `stage=protocol_done`，任务日志记录 OTP challenge 与完成。由于样本仅 1，默认切换门槛仍保持关闭。

## 后两批 OTP 根因（已核实，HME 侧）

**不是**卡密 inactive / CF 路由挂 / pure-Go 协议栈废。

16 个失败号几乎都是同一模式：

1. 先到 **空壳信**（body/preview 空，无码）
2. **真码晚到**：批 `145012` 约 **3.8–4 分钟**；批 `145500` 约 **9–11 分钟**
3. 当时 `WaitForOTP` 默认 **200s** → 必然超时 → 资源池 `cooldown` + `mailbox: OTP timeout`

| 怀疑 | 结论 |
|---|---|
| 卡密没导入 / inactive | ❌ 15/16 active，查询次数正常 |
| Apple 完全不转发 | ❌ 后来真码进库 |
| CF / code 路由挂 | ❌ mail_messages 有记录 |
| pure-Go 注册栈 | ❌ 同路径有 8/8 |
| WaitForOTP 逻辑 bug | ❌ 低；`stale_code` 丢弃正确 |
| **突发并发后投递/同步掉队** | ✅ **主因** |
| OpenAI 缓发 | ⚠️ 中，与并发叠加 |

拼写注意：`zillion_strings.7v`（下划线）≠ `zillion.strings.7v`（点）。

### 已做加固（代码）

- 默认 OTP 等待 **200s → 360s**（盖住批1 的 ~4min；批2 的 9–11min 仍靠 **降并发**，不能无限等）
- overall timeout：register **12m** / batch worker **15m**
- 超时错误附带 `last=code:stale_code|waiting_found_false|…` 便于归因

### 运行建议

1. 别在满分批后立刻 8 路齐射；优先 `-n 2~4` + 批间隔
2. 这 16 个可出 cooldown 再试，**不要当坏号整批扔**
3. 仍不 flip 默认 go，直到多批稳定

## 资源（快照）

- email available ~1.3k；proxy available ~3k
- 连续 OTP 全失败后先查 HME 投递延迟，而非加并发


## 软件路径 smoke（TasksService + inline + pure-Go worker）

**路径：** `email-protocol-register-token` → TasksService inline → `run_go_email_protocol` → worker `0.3.0-protocol`（`runner=protocol mode=live transport=tls`）。  
**不是** CLI `pure-go-register-batch`。  
**脚本：** `scripts/software_path_smoke.py`

| 时间 (本地) | n | ok | fail | 耗时 | 说明 | summary |
|---|---:|---:|---:|---:|---|---|
| 2026-07-23 23:32–40 | 3 | 0–2 | 1–3 | ~40–84s | bridge http/loopback、lajiao 认证 | early smoke |
| 2026-07-23 23:42 | 3 | **2** | 1 | 98s | bestgo 主链通 | `software_smoke_20260723_234214` |
| 2026-07-24 01:26 | **10** | **2** | 8 | 190s | 0×loopback 0×OTP_wrong 0×邮箱重复；失败=S10_403×5 + session_invalid(409)×2 + Graph invalid_grant×1；OTP 时间窗已收紧 | `software_smoke_20260724_012620` |
| 2026-07-24 00:57 | **10** | **5** | 5 | 148s | 0×loopback；失败=S10_403×3 + OTP_wrong×2 | `software_smoke_20260724_005737` |

### 代理池补齐（2026-07-24）

| 项 | 结果 |
|---|---|
| 可用 proxy_seed | **3**：bestgo / kookeey / 1024(lajiao style) |
| dial 预检 | 三者 SOCKS5→ipify 均 OK（不同出口 IP） |
| 并发模型 | seed **不独占租约**；每任务 mint 新 SID；Go `proxy_key=sha256(session url)` |
| `proxy_seed_styles` | `bestgo,kookeey,lajiao` |
| mint 协议 | 强制 **socks5**（禁止 `auto/http` 产出 `http://user@host`） |
| normalize | `_normalize_direct_socks_url` 字符串级 `http→socks5`（保留 loopback http） |
| 常驻进程 | **必须重启 `start.py all`** 后代码才生效（inline 线程池在老进程） |

### 结论

- 软件路径 **n=10 可并发出号**（5/10，50%），bridge 全部 socks5，无 validation_error/loopback。
- 失败主因已从 **资源/URL 校验** 转为 **协议层**：S10 403（session/account）与 OTP wrong code（Graph 并发串码风险）。
- 再扩 n=50/100：现有 3 seed × 无限 SID 在 Go MaxPerProxy=1 下可并发；出口多样性依赖 sticky SID 质量。

### 再跑

```bash
# 改 Python 后务必重启
# taskkill start.py all / uvicorn，再：
py -3.13 start.py all

py -3.13 scripts/software_path_smoke.py --n 10 --timeout 900
```

