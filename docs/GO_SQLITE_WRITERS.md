# Go 主库写路径（SQLite / Postgres）

**日期：** 2026-07-18  
**状态：** pgx **已接线**；**生产主库经 `env.db` flip 为 Postgres**（`start.bat` / `start.py` / `with-pg.bat`）。SQLite 文件仅备份。

---

## 1. 写主库的 Go 包

| 包 | 文件 | 用途 | 打开方式 |
|---|---|---|---|
| `internal/proxy` | `pool.go` | lease/release proxy | `store.OpenPath` |
| `internal/mailbox` | `icloud_api.go` | lease/release email | `store.OpenPath` |
| `internal/accounts` | `import.go` | 成功号 accounts + credentials | `store.OpenPath` |
| `internal/ledger` | `ledger.go` | job FSM **另库** | 独立 sqlite |

`pure-go-register` / `batch` 的 `-db` 在 **sqlite 模式**下是主库路径；**postgres 模式**下完全忽略，用 URL，且 URL 缺失时 fail-closed。`email-protocol-worker -db` 是独立 job ledger，不是主业务库选择器。

---

## 2. 后端选择

```
GPT_REGISTER_DB_BACKEND=sqlite|postgres
GPT_REGISTER_DATABASE_URL=postgresql://gpt:gpt@127.0.0.1:5432/gpt_register
# 或 DATABASE_URL=...
# 生产：仓库根 env.db（start.bat/start.py 自动加载）；CLI：with-pg.bat go run ...
```

| 条件 | 结果 |
|---|---|
| backend=postgres 或 URL 为 postgres* | **pgx**，MaxOpenConns=20 |
| 否则 | **sqlite** modernc，WAL + busy_timeout=30s，MaxOpenConns=1 |

`store.MustSQLite` 仅强制 sqlite（测试/特殊路径）；业务写路径用 `OpenPath`。

---

## 3. SQL 差异

| | SQLite | Postgres |
|---|---|---|
| 占位符 | `?` | `store.Rebind` → `$1…` |
| lease 选行 | `ORDER BY RANDOM()` + CAS | `FOR UPDATE SKIP LOCKED` + CAS |
| import PK | `LastInsertId` | `SELECT id WHERE email=?` |
| import 重试 | BUSY/locked/deadlock ×8 | 同左 |

---

## 4. 验收（已通过）

- [x] `ImportRegistered` round-trip on PG（`_smoke_pg_store`）  
- [x] proxy/email lease 32 并发无双租（`_smoke_pg_lease_race`）  
- [x] Python `resolve_backend()==postgres` 可读全量行数  
- [x] 回滚：清 URL / backend=sqlite 回文件  

Dashboard 列表见 pure-Go 新号：切同一 PG 后自然可见（勿 SQLite/PG 双开）。

---

## 5. 相关

- `docs/DB_POSTGRES_AND_CUTOVER.md`  
- `tools/migrate_sqlite_to_pg.py`  
- `go-email-protocol/internal/store/open.go`  
