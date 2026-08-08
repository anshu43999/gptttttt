# pure-Go 邮箱协议：如何使用

[返回 README](../README.md) · [运行手册](operations.md) · [架构](architecture.md)

本文说明**当前默认生产路径**：Dashboard / `start.bat` 拉起 **Go `email-protocol-worker`**，由 worker 自己完成代理铸造、邮箱租约、协议 FSM、Graph 收 OTP、账号写库。Python 只做控制面（提交批次、展示、导出）。

## 1. 架构一句话

```text
操作员 WebUI (127.0.0.1:47718)
  → FastAPI / TasksService
  → POST http://127.0.0.1:18765/v2/email-register-batches
  → Go worker（lease mailbox + mint proxy_seed + pure-Go TLS 协议 + Graph OTP）
  → 成功账号写入 PostgreSQL accounts / account_credentials
  → 可选自动导出 AT 产品行到 at-file/
```

| 角色 | 负责 | 不负责 |
| --- | --- | --- |
| Python Dashboard | 配置、资源导入、批次提交、进度、导出 | 协议 TLS/Sentinel/OTP 轮询实现 |
| Go worker | 并发座位、proxy sticky、注册 FSM、OTP、写库 | UI |
| PostgreSQL | 业务主库（账号/任务/资源池） | Go 内部 ledger（`data/go-email-protocol-ledger.db` 仅 worker 作业账本） |

## 2. 一键启动（推荐）

前置：

1. Python **3.13**、Node.js/npm  
2. 本机 PostgreSQL，库可空  
3. 复制配置：

```powershell
Copy-Item env.db.example env.db
Copy-Item config.example.yaml config.yaml
# 编辑 env.db 里的数据库 URL
# 编辑 config.yaml：不要提交真实密钥
```

可选导入空结构：

```powershell
psql $env:DATABASE_URL -f database/gpt_register_pg_sanitized.sql
```

设置操作员令牌并启动：

```powershell
$env:GPT_REGISTER_OPERATOR_TOKEN = (python -c "import secrets; print(secrets.token_urlsafe(32))")
.\start.bat
# 或: py -3.13 start.py all
```

`start.py` / `start.bat` 会：

1. 加载 `env.db`（Postgres）  
2. 若 `go-email-protocol/email-protocol-worker.exe` 存在，以 **pure-Go + live + tls** 启动 worker（`127.0.0.1:18765`）  
3. 构建/托管前端，WebUI 默认 `http://127.0.0.1:47718`

健康检查：

```text
GET http://127.0.0.1:18765/health
GET http://127.0.0.1:18765/diagnostics   # 仅本机环回
GET http://127.0.0.1:47718/api/health
```

期望 worker：`runner=protocol`（或 pure 相关字段）、`protocol_mode=live`、`transport=tls`（或你配置的 direct）。

### 没有预编译 exe 时

```powershell
cd go-email-protocol
go build -tags tlsclient -o email-protocol-worker.exe ./cmd/email-protocol-worker
cd ..
.\start.bat
```

## 3. 跑 pure-Go 注册前必须准备的资源

### 3.1 邮箱池（Outlook / Hotmail Graph）

- `config.yaml`：`mailbox_provider: outlook_token`  
- 在 **资源池** 导入令牌，格式（四段）：

```text
email----password----client_id----refresh_token
```

Hotmail 与 Outlook.com 同一套 Graph 收码，**没有**单独 hotmail 协议。

导入后确认 `resource_pool` 中对应行 `status=available`（UI 资源页或 SQL）。

### 3.2 代理种子 proxy_seed

pure-Go **不走**本地 HTTP CONNECT bridge；worker 直接拨上游 SOCKS。

- 配置：`proxy_seed_styles: bestgo,1024`（或只 `bestgo`）  
- 在资源池导入 **proxy_seed**（供应商账号），worker 的 `MintSeedSession` 只选：

```text
resource_type=proxy AND provider=proxy_seed AND status=available
```

并从 seed 铸造带 session id 的 sticky `socks5h://...`。

中国大陆出口通常需要系统 **TUN**；不要指望 worker 进程直连境外代理绕过 TUN。

### 3.3 并发与 OTP

| 配置项 | 建议 | 说明 |
| --- | --- | --- |
| `max_register_tasks` / `max_parallel_tasks` | **100** 日常 | 产品层无硬顶；一次打满 200+ S0 在 TUN 下易雪崩 |
| `email_otp_timeout` | 120 | worker 内 Graph 收码预算（秒） |
| `go_email_protocol_transport` | `tls` | 需 `-tags tlsclient` 构建 |
| `email_protocol_backend` | `go` | 默认 |
| `email_protocol_spawn_mode` | `inline` | 控制面直调 Go batch API |

## 4. 从 WebUI 使用

1. 登录（操作员令牌）  
2. **设置**：确认邮箱协议后端为 Go、transport、并发  
3. **资源**：导入 outlook_token + proxy_seed  
4. **注册**：选择邮箱协议 / Go 批量，填写数量与并发  
5. 等待批次完成；成功账号可在账号页查看  
6. **AT 导出**：注册页支持按批次导出产品行到 `at-file/{时间}/`  

产品行格式（五段）：

```text
email----password----client_id----refresh_token----access_token
```

API（需已登录会话）：

```http
POST /api/tasks/batches/export-at-txt
```

## 5. 控制面 API 与 worker API（开发/排障）

### Dashboard → Go batch（Python 封装）

`services/go_registration_batch.py`：

- `POST {go_email_protocol_url}/v2/email-register-batches`  
- 轮询 batch 状态  

payload 关键字段：`count`、`max_concurrent`、`mailbox_provider`、`proxy_styles`、`proxy_regions`、`otp_timeout_seconds`、`email_tries`。

### Worker HTTP（本机）

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/health` | 存活与 runner/mode/transport |
| GET | `/diagnostics` | 座位/计数（环回） |
| POST | `/v2/email-register` | 单任务 |
| GET | `/v2/email-register/{job_id}` | 查询（可 `wait_ms`） |
| POST | `/v2/email-register/{job_id}/otp` | 外部投递 OTP（主路径已内置 Graph） |
| DELETE | `/v2/email-register/{job_id}` | 取消 |
| POST | `/v2/email-register-batches` | 批量（软件主路径） |

鉴权：部分接口使用 worker key / capability（见 worker 启动 `-key`）。

## 6. CLI 金丝雀（可选，协议门禁）

不经过 Dashboard，直接压协议：

```powershell
cd go-email-protocol
# 单条（租 PG 资源池邮箱+代理）
go run ./cmd/pure-go-register -out ../output/pure_go_register

# 批量
go run ./cmd/pure-go-register-batch -n 10 -out ../output/pure_go_register_batch
```

主库为 Postgres 时由 `env.db` / 环境变量选中；不要把生产 token 写进仓库。

容量脚本（维护者）：`scripts/capacity_10min.py`（可 `--styles bestgo`）。

## 7. 失败与邮箱/代理会不会被“拉黑”

### 邮箱（outlook_token）

Go `mailboxStatusForFailure`：

| 失败 | 状态 | 含义 |
| --- | --- | --- |
| 注册成功 | `used` | 消耗 |
| already exists / already used / deleted | `used` | 永久不再租 |
| invalid_grant / refresh 失效 | `disabled` | 令牌废 |
| 网络 / OTP / edge / session 等 | `cooldown` | 临时冷却，**不是**永久黑名单 |

### 代理 seed

设计上 **任务失败不应把 proxy_seed 标 disabled**；mint 只挑 `available`。若 UI/旧逻辑误 disable，需手动改回 `available`。

## 8. 常见问题

| 现象 | 排查 |
| --- | --- |
| `proxy: no available proxy_seed matching approved styles` | 资源池无 `available` 的 proxy_seed，或 `proxy_seed_styles` 与 seed 标签不一致；检查是否被误标 disabled |
| `username/password authentication failed` | 上游代理账密/流量；换 style 或充值 |
| `198.18` / `wsarecv` | Clash TUN 抖动；降并发、检查 TUN |
| OpenAI 500 / session invalid | 上游或会话；先降到 n=5 smoke |
| worker 起不来 | 是否有 `email-protocol-worker.exe`；端口 18765；Fiddler 劫持环回 |
| 有 running 无进程 | 历史僵尸任务；重启 WebUI 并取消无 pid 任务 |
| 邮箱 available=0 | 大量 cooldown/used；冷却策略或补货 |

## 9. 安全边界

- 只绑定 `127.0.0.1`；不要端口转发到公网。  
- `config.yaml`、`env.db`、`data/`、`at-file/`、ledger db 均含敏感数据，**不要**二次分发。  
- 本包提供的 `database/gpt_register_pg_sanitized.sql` **仅结构、零行数据**。  

更多： [负责任使用](responsible-use.md) · [安全与数据处理](security-and-data-handling.md)
