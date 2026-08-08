# 本地运行手册

[返回 README](../README.md) · [pure-Go 协议用法](go-protocol-usage.md) · [架构](architecture.md)

## 前提

- Windows：Python **3.13**、Node.js、npm；可选 Go 1.22+（仅需自行编译 worker 时）
- 本机 PostgreSQL（推荐）或显式 SQLite 回退
- 仅可信操作员、仅本机目录；对外部账号/数据有书面授权

## 安全最小启动

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --require-virtualenv -r requirements.txt
npm --prefix frontend ci
Copy-Item env.db.example env.db
Copy-Item config.example.yaml config.yaml
# 编辑 env.db / config.yaml（真实密钥只留在本机）
$env:GPT_REGISTER_OPERATOR_TOKEN = (python -c "import secrets; print(secrets.token_urlsafe(32))")
python start.py check
.\start.bat
```

可选预建空库结构：

```powershell
psql $env:DATABASE_URL -f database/gpt_register_pg_sanitized.sql
```

`start.bat` / `python start.py all` 会启动：

1. Go `email-protocol-worker` → `http://127.0.0.1:18765`（pure-Go live）
2. WebUI → `http://127.0.0.1:47718`

详细协议资源导入与批次操作见 [pure-Go 协议用法](go-protocol-usage.md)。

### 常用 start.bat

| 命令 | 作用 |
| --- | --- |
| `start.bat` / `start.bat all` | 一键 Go worker + WebUI |
| `start.bat restart` | 释放 47718、强制前端重建、启动 |
| `start.bat rebuild` | 强制前端重建后启动 |
| `start.bat dev` | 后端 reload + Vite HMR |
| `start.bat check` | 工具链检查 |
| `start.bat 47718` | 指定 WebUI 端口 |

## 配置与密钥

| 变量 | 必需 | 用途 |
| --- | --- | --- |
| `GPT_REGISTER_OPERATOR_TOKEN` | 是 | 本地登录 |
| `GPT_REGISTER_SESSION_SECRET` | 否 | 独立会话签名 |
| `GPT_REGISTER_EMAIL_WEBHOOK_TOKEN` | 否 | 邮件 webhook；未设则拒绝回调 |
| `GPT_REGISTER_DB_BACKEND` | 推荐 | `postgres`（env.db） |
| `GPT_REGISTER_DATABASE_URL` / `DATABASE_URL` | 推荐 | PG URL |

不要把令牌写入 YAML、前端、截图或聊天。

## 数据

- 主库：PostgreSQL（`env.db`）
- Worker ledger：`data/go-email-protocol-ledger.db`（作业账本，非业务主库）
- 导出：`at-file/`、`output/`
- 均可能含账号与令牌；打包给他人前必须删除

## 故障表

| 现象 | 先查 |
| --- | --- |
| 启动失败 | Python 3.13、OPERATOR_TOKEN、端口、PG 连通 |
| Go worker 失败 | `go-email-protocol/email-protocol-worker.exe` 是否存在；`data/go-email-protocol-worker.log` |
| 前端空白 | `npm --prefix frontend ci` 与 rebuild |
| 注册全失败 | 邮箱 available、proxy_seed available、TUN、styles |
| 登录失败 | 当前 shell 的 OPERATOR_TOKEN 是否与启动进程一致 |

## 发布/再分发前

扫描：无 `config.yaml` 实值、无 `data/*.db` 业务库、无 `at-file` 产品、无 HAR/日志。只允许 schema-only SQL。见 [release-audit](release-audit.md)。
