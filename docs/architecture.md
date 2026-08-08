# 架构与信任边界

[返回 README](../README.md) · [pure-Go 协议用法](go-protocol-usage.md) · [运行手册](operations.md)

## 目标

本机操作员仪表盘：导入邮箱/代理资源 → **pure-Go** 批量注册 ChatGPT → 落库 access/refresh token → 可选 Plus/导出。  
不是公网 SaaS；不绑定 `0.0.0.0`；不提供多租户。

```mermaid
flowchart LR
    op[本地操作员] --> ui[WebUI 127.0.0.1:47718]
    ui --> api[FastAPI]
    api --> pg[(PostgreSQL 主库)]
    api --> go[Go email-protocol-worker :18765]
    go --> pg
    go --> proxy[上游 SOCKS proxy_seed]
    go --> graph[Outlook Graph OTP]
    go --> openai[OpenAI 注册协议]
```

## 组件

| 组件 | 职责 |
| --- | --- |
| `start.py` / `start.bat` | 加载 env.db、拉起 Go worker、构建/托管前端 |
| `main.py` + `api/*` | 本地 API；操作员会话 |
| `application/*` | 任务、资源池、账号、导出 |
| `services/go_registration_batch.py` | 向 Go 提交/轮询 batch |
| `go-email-protocol/` | pure-Go worker：租约、代理、FSM、OTP、写库 |
| `infrastructure/db.py` | PG/SQLite 初始化与迁移 |
| `database/gpt_register_pg_sanitized.sql` | **仅**空结构，供新环境 |

## 默认协议路径

1. `email_protocol_backend: go`  
2. `email_protocol_spawn_mode: inline`  
3. worker：`-pure-go -protocol-mode live -transport tls`  
4. 邮箱：`outlook_token`；代理：`proxy_seed` + `proxy_seed_styles`  
5. **无**本地 bridge（`mailat_protocol_use_local_bridge: false`）

旧 mailat/Node 路径仍可能存在于代码中，**不是**默认交付路径。

## 信任边界

- 环回 Host / 对端校验；API `no-store`  
- 操作员令牌仅进程环境  
- 出站 TLS 校验保持开启  
- 真实 `config.yaml` / 数据库 / 导出 不得进入 git 或对外 zip  

变更时不要引入第二套认证，不要放宽环回绑定。
