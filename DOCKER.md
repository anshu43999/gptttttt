# Docker 部署说明

这个部署方案把 FastAPI 后端、静态前端、Go email-protocol worker 放在同一个容器里启动。这样现有系统默认的 `http://127.0.0.1:18765` worker 地址可以继续使用，不需要额外改业务配置。

## 服务器部署

```bash
cd /opt/apps/fucccccckgpt
cp docker.env.example .env
mkdir -p data output
docker compose up -d --build
```

默认访问地址：

```text
http://服务器IP:47718/
```

如果你要换端口，编辑 `.env`：

```bash
GPT_REGISTER_PUBLIC_PORT=47719
```

然后重启：

```bash
docker compose up -d
```

## 低内存服务器建议

2G 内存服务器建议保持默认并发：

```bash
GO_EMAIL_PROTOCOL_MAX_ACTIVE=1
GO_GRAPH_MAX_CONCURRENT=4
```

如果内存更大，可以逐步调高，不建议一次调很多。

## 数据持久化

以下目录会保留在宿主机：

```text
./data   SQLite 数据库、Go worker ledger/key、任务数据
./output 运行输出
```

升级代码前不要删除这两个目录。

## 常用命令

```bash
# 构建并启动
docker compose up -d --build

# 查看状态
docker compose ps

# 查看日志
docker compose logs -f --tail=200

# 停止
docker compose down

# 重启
docker compose restart
```

## 健康检查

```bash
curl http://127.0.0.1:47718/api/health
```

返回 `{"status":"ok"...}` 表示 Web 服务正常。Go worker 在容器内部监听 `127.0.0.1:18765`，不需要对公网开放。