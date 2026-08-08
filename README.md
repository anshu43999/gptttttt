# GPT Register（fucccccckgpt 脱敏交付包）

> 面向**可信本地操作员**的源码包。不是公共服务、托管产品或任何第三方官方工具。

本目录从生产工程同步并**脱敏**：不含账号、令牌、代理实值、任务日志与业务行数据；附带 **schema-only** PostgreSQL 结构文件，便于空库初始化。

## 你要的主路径：pure-Go 邮箱协议

**默认**用 Go worker 批量注册（不是旧 Node/mailat）：

1. `start.bat` 拉起 `email-protocol-worker`（`127.0.0.1:18765`）+ WebUI（`127.0.0.1:47718`）
2. 资源池导入 **Outlook/Hotmail token** + **proxy_seed**
3. 注册页提交批量任务；worker 内完成协议 + Graph 收码
4. 成功账号可导出 AT 产品行到 `at-file/`

**完整步骤与排障 → [docs/go-protocol-usage.md](docs/go-protocol-usage.md)**（必读）

其它文档：

| 主题 | 文档 |
| --- | --- |
| 文档总索引 | [docs/INDEX.md](docs/INDEX.md) |
| 安装启动 | [docs/operations.md](docs/operations.md) |
| 架构边界 | [docs/architecture.md](docs/architecture.md) |
| 安全与数据 | [docs/security-and-data-handling.md](docs/security-and-data-handling.md) |
| 负责任使用 | [docs/responsible-use.md](docs/responsible-use.md) |

## 5 分钟启动

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
npm --prefix frontend ci

Copy-Item env.db.example env.db
Copy-Item config.example.yaml config.yaml
# 按本机修改 env.db 中 PostgreSQL URL；config.yaml 填空密钥即可先启动

# 可选：导入空表结构
# psql "postgresql://gpt:gpt@127.0.0.1:5432/gpt_register" -f database/gpt_register_pg_sanitized.sql

$env:GPT_REGISTER_OPERATOR_TOKEN = (python -c "import secrets; print(secrets.token_urlsafe(32))")
.\start.bat
```

浏览器打开 `http://127.0.0.1:47718`，用上面的操作员令牌登录。

### 预编译 Go worker

包内应包含：

```text
go-email-protocol/email-protocol-worker.exe
```

若缺失，在已安装 Go 的机器上：

```powershell
cd go-email-protocol
go build -tags tlsclient -o email-protocol-worker.exe ./cmd/email-protocol-worker
```

## 数据库

- **主库：PostgreSQL**（`env.db`）
- 脱敏结构：`database/gpt_register_pg_sanitized.sql`（**零行数据**）
- 应用启动仍会跑 `infrastructure/db.py` 迁移补列
- SQLite 仅显式回退/开发

清理干净的库 = 空 `resource_pool` / 空 `accounts` / 空 `tasks`。自行导入邮箱与代理后再跑注册。

## 目录要点

```text
start.bat / start.py     一键启动
config.example.yaml      配置模板（无密钥）
env.db.example           PG 模板
database/                空 schema
go-email-protocol/       Go worker 源码 + exe
frontend/                React UI
api/ application/ ...    Python 控制面
docs/go-protocol-usage.md
```

## 不要做的事

- 不要把本机填好的 `config.yaml`、`env.db`、`data/`、`at-file/` 再打给别人  
- 不要对公网暴露端口  
- 不要在未授权系统上使用  

## 许可证

第一方源码见 [LICENSE](LICENSE)；第三方见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
# gptttttt
