from __future__ import annotations

import json
import os
import signal
import subprocess
import socket
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

if sys.version_info[:2] != (3, 13):
    raise SystemExit(
        "Python 3.13 is required. Activate a Python 3.13 venv or run: py -3.13 start.py"
    )


ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "frontend"
DATA = ROOT / "data"
GO_DIR = ROOT / "go-email-protocol"

DEFAULT_WEBUI_PORT = 47718
DEFAULT_GO_WORKER_HOST = "127.0.0.1"
DEFAULT_GO_WORKER_PORT = 18765
DEFAULT_FRONTEND_DEV_PORT = 5173

GO_WORKER_URL = f"http://{DEFAULT_GO_WORKER_HOST}:{DEFAULT_GO_WORKER_PORT}"
GO_WORKER_LOG = DATA / "go-email-protocol-worker.log"
GO_WORKER_DB = DATA / "go-email-protocol-ledger.db"
GO_WORKER_KEY = DATA / "go-email-protocol.key"
GO_WORKER_WORK_ROOT = DATA / "go-email-protocol-jobs"

ENV_DB_FILE = ROOT / "env.db"
DEFAULT_PG_URL = "postgresql://gpt:gpt@127.0.0.1:5432/gpt_register"


def apply_db_env() -> dict[str, str]:
    """Load env.db into os.environ (existing non-empty env wins). Returns applied snapshot.

    Formal flip: presence of env.db means production main DB is Postgres.
    No env.db → leave env alone (SQLite default). Rollback: delete/rename env.db or set backend=sqlite.
    """
    defaults: dict[str, str] = {}
    if ENV_DB_FILE.is_file():
        for raw in ENV_DB_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key:
                defaults[key] = val
        # Fill any missing flip keys so partial files still work
        defaults.setdefault("GPT_REGISTER_DB_BACKEND", "postgres")
        defaults.setdefault("GPT_REGISTER_DATABASE_URL", DEFAULT_PG_URL)
        defaults.setdefault("DATABASE_URL", defaults.get("GPT_REGISTER_DATABASE_URL", DEFAULT_PG_URL))

    applied: dict[str, str] = {}
    for key, val in defaults.items():
        cur = str(os.environ.get(key) or "").strip()
        if cur:
            applied[key] = cur
            continue
        os.environ[key] = val
        applied[key] = val
    if not applied:
        # Report current env even when no file (for check/logs)
        for key in ("GPT_REGISTER_DB_BACKEND", "GPT_REGISTER_DATABASE_URL", "DATABASE_URL"):
            cur = str(os.environ.get(key) or "").strip()
            if cur:
                applied[key] = cur
    return applied


def db_env_summary(applied: dict[str, str] | None = None) -> str:
    snap = applied or {
        "GPT_REGISTER_DB_BACKEND": str(os.environ.get("GPT_REGISTER_DB_BACKEND") or ""),
        "GPT_REGISTER_DATABASE_URL": str(os.environ.get("GPT_REGISTER_DATABASE_URL") or ""),
        "DATABASE_URL": str(os.environ.get("DATABASE_URL") or ""),
    }
    backend = snap.get("GPT_REGISTER_DB_BACKEND") or "(unset)"
    url = snap.get("GPT_REGISTER_DATABASE_URL") or snap.get("DATABASE_URL") or "(unset)"
    # redact password for display
    display = url
    if "://" in url and "@" in url:
        try:
            scheme, rest = url.split("://", 1)
            creds, host = rest.rsplit("@", 1)
            if ":" in creds:
                user = creds.split(":", 1)[0]
                display = f"{scheme}://{user}:***@{host}"
        except ValueError:
            pass
    return f"backend={backend} url={display}"


def _cmd_display(cmd: list[str] | str) -> str:
    if isinstance(cmd, str):
        return cmd
    return " ".join(f'"{part}"' if " " in part else part for part in cmd)


def run(cmd: list[str] | str, *, cwd: Path = ROOT, shell: bool = False) -> None:
    print(f"\n$ {_cmd_display(cmd)}")
    proc = subprocess.run(cmd, cwd=str(cwd), shell=shell)
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)


def popen(
    cmd: list[str] | str,
    *,
    cwd: Path = ROOT,
    shell: bool = False,
    env: dict[str, str] | None = None,
    stdout=None,
    stderr=None,
) -> subprocess.Popen:
    print(f"\n$ {_cmd_display(cmd)}")
    kwargs: dict = {
        "cwd": str(cwd),
        "shell": shell,
        "env": env,
        "stdout": stdout,
        "stderr": stderr,
    }
    if os.name == "nt":
        # Allow graceful CTRL_BREAK_EVENT shutdown of the process tree.
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    return subprocess.Popen(cmd, **kwargs)


def npm_cmd(args: str) -> str:
    return f"npm {args}"


def ensure_frontend_deps() -> None:
    if not (FRONTEND / "node_modules").exists():
        print("\n[frontend] node_modules 不存在，开始 npm install")
        run(npm_cmd("install"), cwd=FRONTEND, shell=True)
    else:
        print("\n[frontend] node_modules 已存在，跳过 npm install")


def _frontend_dist_stale(dist_index: Path) -> bool:
    """True when dist is missing pieces or any frontend source is newer than dist."""
    if not dist_index.is_file():
        return True
    try:
        dist_mtime = dist_index.stat().st_mtime
    except OSError:
        return True

    watch_roots = [
        FRONTEND / "src",
        FRONTEND / "index.html",
        FRONTEND / "package.json",
        FRONTEND / "vite.config.ts",
        FRONTEND / "vite.config.js",
        FRONTEND / "tsconfig.json",
        FRONTEND / "tsconfig.app.json",
    ]
    for root in watch_roots:
        if not root.exists():
            continue
        if root.is_file():
            try:
                if root.stat().st_mtime > dist_mtime:
                    return True
            except OSError:
                return True
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            # skip noise
            if any(part in {"node_modules", "dist", ".git"} for part in path.parts):
                continue
            try:
                if path.stat().st_mtime > dist_mtime:
                    return True
            except OSError:
                continue
    return False


def build_frontend(*, force: bool = False) -> None:
    dist_index = FRONTEND / "dist" / "index.html"
    if force:
        print("\n[frontend] FORCE_BUILD=1，强制重建 dist")
    elif dist_index.exists() and not _frontend_dist_stale(dist_index):
        print("\n[frontend] dist 已是最新，跳过构建（FORCE_BUILD=1 可强制重建）")
        return
    elif dist_index.exists():
        print("\n[frontend] 源码比 dist 新，自动重建")
    else:
        print("\n[frontend] dist 不存在，开始构建")
    ensure_frontend_deps()
    run(npm_cmd("run build"), cwd=FRONTEND, shell=True)


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, int(port)))
        except OSError:
            return False
    return True


def find_free_port(start: int, *, host: str = "127.0.0.1", attempts: int = 50) -> int:
    port = int(start)
    for _ in range(max(1, attempts)):
        if port_free(port, host=host):
            return port
        port += 1
    raise SystemExit(f"从 {start} 起连续 {attempts} 个端口都不可用")


def kill_listeners_on_port(port: int, *, label: str = "port") -> None:
    """Best-effort terminate whatever listens on TCP port (Windows/Unix)."""
    port = int(port)
    if os.name == "nt":
        try:
            out = subprocess.check_output(
                ["netstat", "-ano", "-p", "TCP"],
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if isinstance(out, bytes):
                text = None
                for enc in ("utf-8", "gbk", "cp936", "latin-1"):
                    try:
                        text = out.decode(enc)
                        break
                    except UnicodeDecodeError:
                        continue
                if text is None:
                    text = out.decode("utf-8", errors="replace")
            else:
                text = str(out)
        except Exception as exc:
            print(f"[{label}] netstat 失败，无法清理旧进程: {exc}")
            return
        pids: set[int] = set()
        needle = f":{port}"
        for line in text.splitlines():
            if needle not in line or "LISTENING" not in line.upper():
                continue
            parts = line.split()
            if not parts:
                continue
            # local address may be 127.0.0.1:47718 or 0.0.0.0:47718
            local = ""
            for part in parts:
                if part.endswith(needle) or part.endswith(f"]{needle}"):
                    local = part
                    break
            if not local:
                continue
            try:
                pid = int(parts[-1])
            except ValueError:
                continue
            if pid > 0:
                pids.add(pid)
        for pid in sorted(pids):
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F", "/T"],
                    check=False,
                    capture_output=True,
                    errors="replace",
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                print(f"[{label}] 已终止旧进程 pid={pid}（端口 {port}）")
            except Exception as exc:
                print(f"[{label}] taskkill pid={pid} 失败: {exc}")
        time.sleep(0.4)
        return
    try:
        subprocess.run(["fuser", "-k", f"{port}/tcp"], check=False, capture_output=True)
        time.sleep(0.3)
    except Exception:
        pass


def resolve_webui_port(preferred: int | None = None) -> int:
    env_raw = str(os.environ.get("GPT_REGISTER_BACKEND_PORT") or "").strip()
    if env_raw.isdigit():
        preferred = int(env_raw)
    start = int(preferred or DEFAULT_WEBUI_PORT)
    if not 1 <= start <= 65535:
        raise SystemExit(f"非法 WebUI 端口: {start}")

    reclaim = str(os.environ.get("GPT_REGISTER_RECLAIM_WEBUI") or "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    if reclaim and not port_free(start):
        print(f"[webui] 端口 {start} 被占用，尝试回收旧 WebUI…")
        kill_listeners_on_port(start, label="webui")
        deadline = time.time() + 5
        while time.time() < deadline:
            if port_free(start):
                break
            time.sleep(0.2)

    port = find_free_port(start)
    if port != start:
        print(f"[webui] 端口 {start} 仍被占用，改用 {port}")
    return port


def open_later(url: str, delay: float = 1.5) -> None:
    try:
        time.sleep(delay)
        webbrowser.open(url)
    except Exception:
        pass


def stop_processes(processes: list[subprocess.Popen]) -> None:
    for proc in processes:
        if proc.poll() is not None:
            continue
        try:
            if os.name == "nt":
                proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
            else:
                proc.terminate()
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
    for proc in processes:
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()


def wait_processes(processes: list[subprocess.Popen]) -> None:
    try:
        while True:
            for proc in processes:
                code = proc.poll()
                if code is not None:
                    print(f"\n进程已退出: pid={proc.pid} code={code}")
                    stop_processes([p for p in processes if p is not proc])
                    raise SystemExit(code)
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在停止服务...")
        stop_processes(processes)
        print("已停止。")


def go_worker_binary() -> Path | None:
    if os.name == "nt":
        candidates = [GO_DIR / "email-protocol-worker.exe"]
    else:
        candidates = [GO_DIR / "email-protocol-worker", GO_DIR / "email-protocol-worker.exe"]
    for path in candidates:
        if path.is_file():
            return path
    return None


def go_worker_healthy(*, timeout: float = 1.5) -> dict | None:
    try:
        # Never route loopback health through system proxy (Fiddler etc.).
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(
            f"{GO_WORKER_URL}/health",
            headers={"Accept": "application/json"},
            method="GET",
        )
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            return None
        status = str(data.get("status") or "").lower()
        if status and status not in {"ok", "healthy", "up"} and not data.get("ok", True):
            return None
        return data
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def go_worker_want_pure() -> bool:
    """Default pure-Go live+direct. Set GO_EMAIL_PROTOCOL_PURE_GO=0 for mailat."""
    pure = str(os.environ.get("GO_EMAIL_PROTOCOL_PURE_GO") or "1").strip().lower()
    return pure not in {"0", "false", "no", "off"}


def go_worker_max_active() -> int:
    """Align with TasksService register bucket; no artificial 200/400 product cap."""
    raw = str(os.environ.get("GO_EMAIL_PROTOCOL_MAX_ACTIVE") or "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    # Prefer config.yaml max_register_tasks when present.
    try:
        cfg_path = ROOT / "config.yaml"
        if cfg_path.is_file():
            text = cfg_path.read_text(encoding="utf-8", errors="replace")
            for line in text.splitlines():
                s = line.strip()
                if s.startswith("max_register_tasks:"):
                    val = s.split(":", 1)[1].strip().split("#", 1)[0].strip()
                    return max(1, int(val))
    except Exception:
        pass
    return 200


def go_worker_graph_max_concurrent() -> int:
    """Bound Microsoft Graph HTTP concurrency independently of protocol seats."""
    raw = str(os.environ.get("GO_GRAPH_MAX_CONCURRENT") or "").strip()
    if raw:
        try:
            return max(1, min(int(raw), 256))
        except ValueError:
            pass
    try:
        cfg_path = ROOT / "config.yaml"
        if cfg_path.is_file():
            for line in cfg_path.read_text(encoding="utf-8", errors="replace").splitlines():
                s = line.strip()
                if s.startswith("outlook_graph_max_concurrent:"):
                    val = s.split(":", 1)[1].strip().split("#", 1)[0].strip()
                    return max(1, min(int(val), 256))
    except Exception:
        pass
    return 96


def go_worker_supports_graph_max_flag(binary: Path | None = None) -> bool:
    """Current on-disk worker may lag source; probe -h once (other agents may still be adapting)."""
    path = binary or go_worker_binary()
    if path is None or not path.is_file():
        return False
    try:
        proc = subprocess.run(
            [str(path), "-h"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            cwd=str(ROOT),
        )
        help_text = f"{proc.stdout or ''}{proc.stderr or ''}"
        return "-graph-max-concurrent" in help_text
    except Exception:
        return False

def go_worker_supports_business_db_flag(binary: Path | None = None) -> bool:
    path = binary or go_worker_binary()
    if path is None or not path.is_file():
        return False
    try:
        proc = subprocess.run(
            [str(path), "-h"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            cwd=str(ROOT),
        )
        help_text = f"{proc.stdout or ''}{proc.stderr or ''}"
        return "-business-db" in help_text
    except Exception:
        return False


def go_worker_business_db_path() -> Path:
    """SQLite fallback path for Go writers; env.db Postgres still wins via store.OpenPath."""
    return DATA / "gpt_register.db"


def go_worker_mode_ok(
    health: dict | None,
    *,
    want_pure: bool,
    graph_max_concurrent: int | None = None,
    max_active: int | None = None,
) -> bool:
    """True when live worker matches intended runner/transport/seats.

    graph_max_concurrent is optional: older binaries omit it in /health and do not
    accept -graph-max-concurrent. Soft-skip so mixed-agent rebuilds still boot.
    max_active mismatch forces restart so config 200 is not stuck on old 100 process.
    """
    if not isinstance(health, dict):
        return False
    runner = str(health.get("runner") or "").strip().lower()
    mode = str(health.get("protocol_mode") or "").strip().lower()
    transport = str(health.get("transport") or "").strip().lower()
    if max_active is not None and "max_active" in health:
        try:
            running_max = int(health.get("max_active"))
        except (TypeError, ValueError):
            running_max = None
        if running_max is not None and running_max != int(max_active):
            return False
    if graph_max_concurrent is not None and "graph_max_concurrent" in health:
        try:
            running_graph_max = int(health.get("graph_max_concurrent"))
        except (TypeError, ValueError):
            running_graph_max = None
        if running_graph_max is not None and running_graph_max != int(graph_max_concurrent):
            return False
    # Old workers (pre-0.3) omit runner — treat as mismatch when pure is required.
    if want_pure:
        if not runner:
            return False
        if runner != "protocol":
            return False
        if mode and mode not in {"live", "engine"}:
            return False
        if transport == "fake":
            return False
        # Software hot path needs Go-owned batches; missing feature → restart/rebuild.
        features = health.get("features") or []
        if isinstance(features, list) and features and "email-register-batches" not in features:
            return False
        return True
    # mailat fallback: runner mailat or empty (legacy)
    if runner and runner not in {"mailat", "unknown"}:
        return False
    return True


def kill_go_worker_on_port() -> None:
    """Best-effort terminate whatever listens on the Go worker port (Windows/Unix)."""
    kill_listeners_on_port(DEFAULT_GO_WORKER_PORT, label="go-worker")


def ensure_go_worker() -> subprocess.Popen | None:
    """Start Go email-protocol-worker if missing or wrong runner mode.

    Returns process handle when this call started it; None if a correct worker
    was already running. Never silently reuses a mailat worker when pure-Go is wanted.
    """
    want_pure = go_worker_want_pure()
    max_active = go_worker_max_active()
    graph_max_concurrent = go_worker_graph_max_concurrent()
    health = go_worker_healthy()
    if health is not None and go_worker_mode_ok(
        health, want_pure=want_pure, graph_max_concurrent=graph_max_concurrent, max_active=max_active
    ):
        print(
            f"[go-worker] 已在运行 {GO_WORKER_URL} "
            f"version={health.get('version') or '-'} "
            f"runner={health.get('runner') or '?'} "
            f"mode={health.get('protocol_mode') or '-'} "
            f"transport={health.get('transport') or '-'} "
            f"active={health.get('active_count', '?')}/{health.get('max_active', '?')}"
        )
        return None

    if health is not None:
        print(
            f"[go-worker] 模式不匹配，强制重启: "
            f"want_pure={want_pure} max_active={max_active} got runner={health.get('runner')!r} "
            f"mode={health.get('protocol_mode')!r} transport={health.get('transport')!r} "
            f"max_active_got={health.get('max_active')!r} version={health.get('version')!r}"
        )
        kill_go_worker_on_port()
        # wait until health dies
        deadline = time.time() + 8
        while time.time() < deadline:
            if go_worker_healthy(timeout=0.5) is None:
                break
            time.sleep(0.2)

    binary = go_worker_binary()
    if binary is None:
        raise SystemExit(
            f"未找到 Go worker 可执行文件：{GO_DIR / 'email-protocol-worker.exe'}\n"
            f"请先构建（含 tls-client）：\n"
            f"  cd go-email-protocol && go build -tags tlsclient -o email-protocol-worker.exe ./cmd/email-protocol-worker"
        )

    if not port_free(DEFAULT_GO_WORKER_PORT):
        # Port still occupied after kill attempt.
        kill_go_worker_on_port()
        if not port_free(DEFAULT_GO_WORKER_PORT):
            raise SystemExit(
                f"Go worker 端口 {DEFAULT_GO_WORKER_PORT} 已被占用且无法释放。\n"
                f"常见原因：Fiddler 系统代理劫持 127.0.0.1，或残留进程。\n"
                f"处理：关闭 Fiddler / 手动结束 email-protocol-worker 后重试。"
            )

    DATA.mkdir(parents=True, exist_ok=True)
    GO_WORKER_WORK_ROOT.mkdir(parents=True, exist_ok=True)

    mailat_dir = os.environ.get("MAILAT_PROTOCOL_DIR") or r"E:\project\mailat\mailat\codex_register"
    cmd = [
        str(binary),
        "-addr",
        f"{DEFAULT_GO_WORKER_HOST}:{DEFAULT_GO_WORKER_PORT}",
        "-db",
        str(GO_WORKER_DB),
        "-key",
        str(GO_WORKER_KEY),
        "-work-root",
        str(GO_WORKER_WORK_ROOT),
        "-mailat-dir",
        mailat_dir,
        "-max-active",
        str(max_active),
    ]
    # Only pass when this binary understands it (source may already have the flag).
    if go_worker_supports_graph_max_flag(binary):
        cmd.extend(["-graph-max-concurrent", str(graph_max_concurrent)])
    else:
        print(
            f"[go-worker] 当前 exe 无 -graph-max-concurrent，跳过该参数 "
            f"(config 目标={graph_max_concurrent}；Graph 仍由 Python 侧限流)"
        )
    if go_worker_supports_business_db_flag(binary):
        cmd.extend(["-business-db", str(go_worker_business_db_path())])
    if want_pure:
        # Prefer tls-client (Firefox profile) when binary built with -tags tlsclient;
        # GO_EMAIL_PROTOCOL_TRANSPORT=direct forces stdlib fallback.
        transport = str(os.environ.get("GO_EMAIL_PROTOCOL_TRANSPORT") or "tls").strip().lower()
        if transport not in {"tls", "direct", "fake"}:
            transport = "tls"
        # config.yaml may also set transport
        try:
            cfg_path = ROOT / "config.yaml"
            if cfg_path.is_file() and not os.environ.get("GO_EMAIL_PROTOCOL_TRANSPORT"):
                for line in cfg_path.read_text(encoding="utf-8", errors="replace").splitlines():
                    s = line.strip()
                    if s.startswith("go_email_protocol_transport:"):
                        val = s.split(":", 1)[1].strip().split("#", 1)[0].strip().strip('"').strip("'")
                        if val in {"tls", "direct", "fake"}:
                            transport = val
                        break
        except Exception:
            pass
        cmd.extend(["-pure-go", "-protocol-mode", "live", "-transport", transport])
        print(f"[go-worker] pure-go live + transport={transport} max_active={max_active}")
    else:
        print(f"[go-worker] mailat runner (GO_EMAIL_PROTOCOL_PURE_GO=0) max_active={max_active}")

    log_file = GO_WORKER_LOG.open("a", encoding="utf-8")
    print(f"[go-worker] 启动中 → {GO_WORKER_URL}")
    print(f"[go-worker] 日志: {GO_WORKER_LOG}")
    print(f"[go-worker] argv: {' '.join(cmd)}")
    proc = popen(cmd, cwd=ROOT, stdout=log_file, stderr=subprocess.STDOUT)

    deadline = time.time() + 20
    while time.time() < deadline:
        if proc.poll() is not None:
            log_file.close()
            tail = ""
            try:
                tail = GO_WORKER_LOG.read_text(encoding="utf-8", errors="replace")[-800:]
            except Exception:
                pass
            raise SystemExit(
                f"Go worker 启动失败（exit={proc.returncode}）。日志尾部:\n{tail}"
            )
        health = go_worker_healthy(timeout=0.8)
        if health is not None and go_worker_mode_ok(
            health, want_pure=want_pure, graph_max_concurrent=graph_max_concurrent, max_active=max_active
        ):
            print(
                f"[go-worker] 就绪 version={health.get('version') or '-'} "
                f"runner={health.get('runner') or '?'} "
                f"mode={health.get('protocol_mode') or '-'} "
                f"transport={health.get('transport') or '-'} "
                f"active={health.get('active_count', 0)}/{health.get('max_active', max_active)}"
            )
            return proc
        if health is not None and not go_worker_mode_ok(
            health, want_pure=want_pure, graph_max_concurrent=graph_max_concurrent, max_active=max_active
        ):
            # Started but wrong mode — should not happen if argv correct.
            print(f"[go-worker] 启动后模式仍不对: {health}")
        time.sleep(0.25)

    stop_processes([proc])
    log_file.close()
    raise SystemExit(
        f"Go worker 20s 内未通过模式校验 /health：{GO_WORKER_URL}/health "
        f"(want_pure={want_pure})"
    )


def run_one_click() -> None:
    """One-click: Go worker + WebUI. Default WebUI 47718, +1 if busy."""
    force_build = str(os.environ.get("FORCE_BUILD") or "").strip() in {"1", "true", "yes"}
    build_frontend(force=force_build)

    go_proc = ensure_go_worker()
    webui_port = resolve_webui_port()
    url = f"http://127.0.0.1:{webui_port}"

    print(f"\n一键启动")
    print(f"  WebUI     : {url}")
    print(f"  Go worker : {GO_WORKER_URL}")
    print(f"  Main DB   : {db_env_summary()}")
    print("保持此窗口打开；Ctrl+C 停止全部。")

    backend = popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(webui_port),
        ],
        cwd=ROOT,
    )
    open_later(url)

    procs = [backend]
    if go_proc is not None:
        procs.append(go_proc)
    wait_processes(procs)


def run_dev_split() -> None:
    """Backend + Vite HMR + Go worker."""
    go_proc = ensure_go_worker()
    backend_port = resolve_webui_port()
    frontend_port = find_free_port(DEFAULT_FRONTEND_DEV_PORT)
    if frontend_port == backend_port:
        frontend_port = find_free_port(backend_port + 1)

    ensure_frontend_deps()

    backend = popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(backend_port),
            "--reload",
        ],
        cwd=ROOT,
    )
    frontend_env = os.environ.copy()
    frontend_env["VITE_BACKEND_URL"] = f"http://127.0.0.1:{backend_port}"
    frontend = popen(
        npm_cmd(f"run dev -- --host 127.0.0.1 --port {frontend_port}"),
        cwd=FRONTEND,
        shell=True,
        env=frontend_env,
    )

    url = f"http://127.0.0.1:{frontend_port}"
    print(f"\n热更新模式")
    print(f"  前端      : {url}")
    print(f"  后端 API  : http://127.0.0.1:{backend_port}/api/health")
    print(f"  Go worker : {GO_WORKER_URL}")
    print("保持此窗口打开；Ctrl+C 同时停止。")
    open_later(url, delay=2.0)

    procs = [backend, frontend]
    if go_proc is not None:
        procs.append(go_proc)
    wait_processes(procs)


def check_environment() -> None:
    applied = apply_db_env()
    print(f"项目目录: {ROOT}")
    print(f"Python: {sys.executable}")
    print(f"[db] {db_env_summary(applied)}")
    try:
        from infrastructure.db_backend import resolve_backend, reset_backend_cache

        reset_backend_cache()
        print(f"[db] resolve_backend()={resolve_backend()}")
    except Exception as exc:
        print(f"[db] resolve_backend error: {exc}")
    run([sys.executable, "--version"], cwd=ROOT)
    run("node --version", cwd=ROOT, shell=True)
    run("npm --version", cwd=ROOT, shell=True)
    binary = go_worker_binary()
    if binary is None:
        print("[go-worker] 二进制缺失")
    else:
        print(f"[go-worker] 二进制: {binary}")
        run([str(binary), "version"], cwd=ROOT)
    health = go_worker_healthy()
    if health is None:
        print(f"[go-worker] health: down ({GO_WORKER_URL}/health)")
    else:
        print(f"[go-worker] health: ok {health}")
    run(
        [sys.executable, "-m", "py_compile", "main.py", "application/accounts_service.py", "api/accounts.py", "start.py"],
        cwd=ROOT,
    )
    print("\n环境检查通过。")


def menu() -> str:
    print("\nGPT Register 启动器")
    print("=" * 36)
    print(f"1. 一键启动（Go worker + WebUI，默认端口 {DEFAULT_WEBUI_PORT}，占用 +1）")
    print("2. 热更新开发（后端 --reload + Vite HMR + Go worker）")
    print("3. 环境检查")
    print("0. 退出")
    return input("请选择 [1/2/3/0，直接回车=1]: ").strip() or "1"


def main() -> None:
    os.chdir(ROOT)
    applied = apply_db_env()
    print(f"[db] {db_env_summary(applied)}")
    raw_choice = sys.argv[1].strip() if len(sys.argv) > 1 else menu()
    aliases = {
        "all": "1",
        "prod": "1",
        "one": "1",
        "start": "1",
        "go": "1",
        "dev": "2",
        "split": "2",
        "check": "3",
    }
    choice = aliases.get(raw_choice.lower(), raw_choice)

    # Bare port number still means one-click with that preferred WebUI port.
    if choice not in {"0", "1", "2", "3"} and choice.isdigit() and 1 <= int(choice) <= 65535:
        os.environ["GPT_REGISTER_BACKEND_PORT"] = choice
        choice = "1"

    if choice == "1":
        run_one_click()
    elif choice == "2":
        run_dev_split()
    elif choice == "3":
        check_environment()
    elif choice == "0":
        return
    else:
        print("无效选择。")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
