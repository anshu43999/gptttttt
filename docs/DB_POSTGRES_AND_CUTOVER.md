# Postgres 切换 + G4/H 门槛

**日期：** 2026-07-18  
**生产主库：** **Postgres**（`env.db` 正式 flip）  
**SQLite 文件：** `data/gpt_register.db` **仅备份**，勿与 PG 双写。

---

## 1. 现状

| 库 | 路径/方式 | 用途 | 状态 |
|---|---|---|---|
| **Postgres 16** | `127.0.0.1:5432` db=`gpt_register` user=`gpt`/`gpt` | **生产主业务库** | **flip ON** |
| SQLite 备份 | `data/gpt_register.db` | 回滚 / 再 import 源 | 保留，不默认写 |
| Go ledger | `data/go-email-protocol-ledger.db` | job FSM | 独立 sqlite，不迁 |

### 全量对账（flip 前）

| 表 | SQLite | PG |
|---|---:|---:|
| accounts | 2551 | 2551 |
| account_credentials | 2422 | 2422 |
| resource_pool | 5627 | 5627 |
| tasks | 4295 | 4295 |
| task_events | 53681 | 53681 |
| sms_activations | 282531 | 282531 |
| **合计导入** | | **378180 rows** |

`tasks.account_id_ref`：混类型 → PG **TEXT**。

---

## 2. 正式 flip 怎么落地

### 配置文件

| 文件 | 作用 |
|---|---|
| **`env.db`** | 生产主库 = postgres + URL（存在即 flip） |
| **`env.db.bat`** | cmd 加载 `env.db`（已有 env 非空则不覆盖） |
| **`start.bat`** | 启动前 `call env.db.bat` |
| **`start.py`** | `apply_db_env()` 读 `env.db` → 注入子进程（uvicorn / go worker） |
| **`go-email-protocol/with-pg.bat`** | CLI：`with-pg.bat go run ./cmd/...` |

`env.db` 内容：

```
GPT_REGISTER_DB_BACKEND=postgres
GPT_REGISTER_DATABASE_URL=postgresql://gpt:gpt@127.0.0.1:5432/gpt_register
DATABASE_URL=postgresql://gpt:gpt@127.0.0.1:5432/gpt_register
```

### 启动

```bat
start.bat
REM 或
start.bat check
```

启动日志应出现：

```
[db] backend=postgres url=postgresql://gpt:***@127.0.0.1:5432/gpt_register
```

`start.py check` 另有：`resolve_backend()=postgres`。

### pure-Go CLI

```bat
cd go-email-protocol
with-pg.bat go run ./cmd/pure-go-register-batch -n 3 -db ../data/gpt_register.db
```

`env.db`（或 `with-pg.bat`）选中 postgres 后，`-db` **完全忽略**；它不能成为 SQLite 回退。PG URL 缺失或连通性检查失败时启动直接报错，绝不静默切 SQLite。

### 行为

| 侧 | 行为 |
|---|---|
| Python `db_backend` | env → **PG** |
| Go `store.OpenPath` | env → **pgx** |
| Go lease | `FOR UPDATE SKIP LOCKED` |
| 子进程 | 继承 start.py 注入的 env（`os.environ`） |

### 回滚（立刻回 SQLite）

任选其一，**重启所有写主库进程**：

1. 改 `env.db`：`GPT_REGISTER_DB_BACKEND=sqlite` 并清空 URL；或  
2. **重命名/删除 `env.db`**，再 `set GPT_REGISTER_DB_BACKEND=sqlite`；或  
3. 启动前强制：`set GPT_REGISTER_DB_BACKEND=sqlite`（非空覆盖 `env.db`）

SQLite 文件未删。**不要**一边 Dashboard 写 PG、一边裸 `go run` 无 env 写 SQLite。

### 再同步 SQLite → PG

```bat
py -3.13 -m pip install "psycopg[binary]>=3.1"
py -3.13 tools/migrate_sqlite_to_pg.py import --db data/gpt_register.db --url "postgresql://gpt:gpt@127.0.0.1:5432/gpt_register" --drop --batch 500
```

---

## 3. 检查清单

| # | 项 | 状态 |
|---|---|---|
| 1 | 全量 import + 行数对账 | **DONE** |
| 2 | Go `pgx` 接线 | **DONE** |
| 3 | accounts/proxy/mailbox `OpenPath` | **DONE** |
| 4 | PG lease SKIP LOCKED + 32 并发无双租 | **DONE** |
| 5 | SQLite WAL（未 flip 时） | **DONE** |
| 6 | **生产启动脚本 flip**（`env.db` + start.*） | **DONE** |
| 7 | 操作员重启 Dashboard / worker 吃新 env | **需你重启一次** |

---

## 4. 冒烟（flip 后已过）

```bat
py -3.13 start.py check
REM → resolve_backend()=postgres

py -3.13 -c "from start import apply_db_env; from infrastructure.db_backend import ...; accounts=2551"

cd go-email-protocol
with-pg.bat go run ./cmd/_smoke_pg_store
with-pg.bat go run ./cmd/_smoke_pg_lease_race
```

---

## 5. G4 / Phase H

| | |
|---|---|
| G4 默认 go | **仍未 flip**（成功率 + OTP） |
| Phase H | 写库 PG 前置已满足；仍卡 OTP/资源/阶梯真压 |

---

## 6. 相关文件

- `env.db` / `env.db.bat` / `start.bat` / `start.py`  
- `go-email-protocol/with-pg.bat`  
- `infrastructure/db_backend.py`  
- `tools/migrate_sqlite_to_pg.py`  
- `go-email-protocol/internal/store/open.go`  
- `docs/GO_SQLITE_WRITERS.md`  
