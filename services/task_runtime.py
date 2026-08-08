"""Task runtime helpers for subprocess and in-process (thread-pool) jobs."""
from __future__ import annotations

import concurrent.futures
import contextlib
import io
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable

from infrastructure.db import now_iso

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Shared pool for email-protocol inline mode (one process, many workers).
_INLINE_POOL: concurrent.futures.ThreadPoolExecutor | None = None
_INLINE_POOL_LOCK = threading.Lock()
_INLINE_POOL_WORKERS = 0


def is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        synchronize = 0x00100000
        wait_timeout = 0x00000102
        handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, int(pid))
        if not handle:
            return False
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == wait_timeout
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def terminate_process_tree(pid: int) -> bool:
    if pid <= 0 or not is_pid_running(pid):
        return False
    if os.name == "nt":
        completed = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return completed.returncode in {0, 128}
    os.kill(pid, 15)
    return True


def subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def subprocess_creationflags() -> int:
    if os.name == "nt":
        return subprocess.CREATE_NEW_PROCESS_GROUP
    return 0


def normalize_spawn_mode(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    if text in {"inline", "thread", "thread_pool", "inprocess", "in_process"}:
        return "inline"
    return "process"


def ensure_inline_pool(max_workers: int) -> concurrent.futures.ThreadPoolExecutor:
    """Process-wide thread pool for email-protocol inline jobs."""
    global _INLINE_POOL, _INLINE_POOL_WORKERS
    workers = max(1, min(int(max_workers or 32), 400))
    with _INLINE_POOL_LOCK:
        if _INLINE_POOL is None or workers > _INLINE_POOL_WORKERS:
            if _INLINE_POOL is not None:
                # Grow: create larger pool; old pool finishes in-flight then GC.
                old = _INLINE_POOL
                _INLINE_POOL = concurrent.futures.ThreadPoolExecutor(
                    max_workers=workers,
                    thread_name_prefix="ep-inline",
                )
                _INLINE_POOL_WORKERS = workers
                try:
                    old.shutdown(wait=False, cancel_futures=False)
                except TypeError:
                    old.shutdown(wait=False)
            else:
                _INLINE_POOL = concurrent.futures.ThreadPoolExecutor(
                    max_workers=workers,
                    thread_name_prefix="ep-inline",
                )
                _INLINE_POOL_WORKERS = workers
        return _INLINE_POOL


class _TeeLog(io.TextIOBase):
    """Write worker prints into the task log file."""

    def __init__(self, log_file: Path, also: io.TextIOBase | None = None):
        super().__init__()
        self._path = log_file
        self._also = also
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, s: str) -> int:  # type: ignore[override]
        if not s:
            return 0
        with self._lock:
            with self._path.open("a", encoding="utf-8", errors="replace") as handle:
                handle.write(s)
            if self._also is not None:
                try:
                    self._also.write(s)
                except Exception:
                    pass
        return len(s)

    def flush(self) -> None:  # type: ignore[override]
        if self._also is not None:
            try:
                self._also.flush()
            except Exception:
                pass


class ManagedTask:
    """Run one pipeline job (subprocess or in-process thread) and mirror state to TasksRepository."""

    def __init__(
        self,
        task_id: str,
        command: list[str],
        log_file: str,
        repo,
        *,
        spawn_mode: str = "process",
        config_path: str = "",
        inline_pool_size: int = 0,
    ):
        self.task_id = task_id
        self.command = list(command)
        self.log_file = Path(log_file)
        self.repo = repo
        self.process: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None
        self._future: concurrent.futures.Future[Any] | None = None
        self._stop_requested = False
        self.spawn_mode = normalize_spawn_mode(spawn_mode)
        self.config_path = str(config_path or "")
        self.inline_pool_size = int(inline_pool_size or 0)

    def _failure_reason(self, exit_code: int) -> str:
        fallback = f"exit code {exit_code}"
        try:
            text = self.log_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return fallback
        for line in reversed(text.splitlines()[-120:]):
            stripped = line.strip()
            if not stripped:
                continue
            for marker in ("流水线异常:", "RuntimeError:", "ValueError:", "ModuleNotFoundError:"):
                if marker in stripped:
                    return stripped.split(marker, 1)[1].strip() or stripped
        return fallback

    def start(self, on_finish: Callable[[int], None] | None = None) -> None:
        if self.spawn_mode == "inline" and self.config_path:
            pool = ensure_inline_pool(self.inline_pool_size or 64)
            self._future = pool.submit(self._run_inline, on_finish)
            return
        self._thread = threading.Thread(target=self._run_process, args=(on_finish,), daemon=True)
        self._thread.start()

    def _mark_running_inline(self) -> None:
        result = self.repo.get(self.task_id).to_dict().get("result") or {}
        if not isinstance(result, dict):
            result = {}
        result["inline"] = 1
        result["spawn_mode"] = "inline"
        result["thread_id"] = int(threading.get_ident())
        # pid=0: capacity uses inline flag, not OS pid.
        result.pop("pid", None)
        self.repo.update(
            self.task_id,
            status="running",
            result=result,
            updated_at=now_iso(),
        )
        self.repo.add_event(
            self.task_id,
            "info",
            "process_started",
            f"inline 线程已启动: thread_id={result['thread_id']}",
            {"spawn_mode": "inline"},
        )

    def _finalize(self, code: int, on_finish: Callable[[int], None] | None) -> None:
        current = self.repo.get(self.task_id).to_dict()
        current_status = str(current.get("status") or "")
        if current_status not in {"running", "starting", "pending", "queued"} or str(current.get("finished_at") or ""):
            self.repo.add_event(
                self.task_id,
                "info",
                "finish_skipped",
                f"任务已是 {current_status or 'unknown'}，跳过退出状态覆盖",
                {"exit_code": code},
            )
            if on_finish:
                on_finish(code)
            return

        if self._stop_requested:
            self.repo.update(
                self.task_id,
                status="cancelled",
                finished_at=now_iso(),
                updated_at=now_iso(),
                result={"exit_code": code, "spawn_mode": self.spawn_mode},
            )
            self.repo.add_event(self.task_id, "warning", "cancelled", "任务已取消", {"exit_code": code})
        elif code == 0:
            prev = current.get("result") if isinstance(current.get("result"), dict) else {}
            result = dict(prev or {})
            result["exit_code"] = code
            result["spawn_mode"] = self.spawn_mode
            self.repo.update(
                self.task_id,
                status="succeeded",
                finished_at=now_iso(),
                updated_at=now_iso(),
                result=result,
            )
            self.repo.add_event(self.task_id, "info", "succeeded", "任务执行成功", {"exit_code": code})
        else:
            reason = self._failure_reason(code)
            self.repo.update(
                self.task_id,
                status="failed",
                finished_at=now_iso(),
                updated_at=now_iso(),
                error=reason,
                retryable=True,
                result={"exit_code": code, "error": reason, "spawn_mode": self.spawn_mode},
            )
            self.repo.add_event(self.task_id, "error", "failed", f"任务执行失败: {reason}", {"exit_code": code, "error": reason})
        if on_finish:
            on_finish(code)

    def _run_inline(self, on_finish: Callable[[int], None] | None) -> None:
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        code = -1
        try:
            current = self.repo.get(self.task_id).to_dict()
            current_status = str(current.get("status") or "")
            if current_status not in {"starting", "running", "pending", "queued"}:
                self.repo.add_event(
                    self.task_id,
                    "info",
                    "start_skipped",
                    f"任务已是 {current_status or 'unknown'}，跳过 inline 启动",
                )
                return
            if current_status != "running":
                self.repo.update(
                    self.task_id,
                    status="starting",
                    started_at=str(current.get("started_at") or "") or now_iso(),
                    updated_at=now_iso(),
                )
            self.repo.add_event(
                self.task_id,
                "info",
                "started",
                "任务开始执行 (inline 线程池 → Go)",
                {"spawn_mode": "inline", "config_path": self.config_path},
            )
            self._mark_running_inline()

            tee = _TeeLog(self.log_file)
            # Redirect print() into task log so resource reporting / OTP logs still work.
            with contextlib.redirect_stdout(tee), contextlib.redirect_stderr(tee):
                if self._stop_requested:
                    code = -2
                else:
                    from services.mailat_email_protocol_task import run as run_email_protocol

                    run_email_protocol(self.config_path, task_id=self.task_id)
                    code = 0
            self._finalize(code, on_finish)
        except Exception as exc:
            try:
                with self.log_file.open("a", encoding="utf-8", errors="replace") as log:
                    log.write(f"流水线异常: {exc}\n")
            except Exception:
                pass
            try:
                self.repo.update(
                    self.task_id,
                    status="failed",
                    finished_at=now_iso(),
                    updated_at=now_iso(),
                    error=str(exc),
                    retryable=True,
                    result={"error": str(exc), "spawn_mode": "inline"},
                )
                self.repo.add_event(self.task_id, "error", "failed", str(exc))
            except Exception:
                pass
            if on_finish:
                on_finish(code if code != 0 else 1)

    def _run_process(self, on_finish: Callable[[int], None] | None) -> None:
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        code = -1
        try:
            # Keep status=starting until the OS process exists. Claim already set
            # starting; only promote to running after Popen succeeds + pid stored.
            current = self.repo.get(self.task_id).to_dict()
            current_status = str(current.get("status") or "")
            if current_status not in {"starting", "running", "pending", "queued"}:
                self.repo.add_event(
                    self.task_id,
                    "info",
                    "start_skipped",
                    f"任务已是 {current_status or 'unknown'}，跳过子进程启动",
                )
                return
            if current_status != "running":
                self.repo.update(
                    self.task_id,
                    status="starting",
                    started_at=str(current.get("started_at") or "") or now_iso(),
                    updated_at=now_iso(),
                )
            self.repo.add_event(self.task_id, "info", "started", "任务开始执行", {"command": self.command})
            with self.log_file.open("a", encoding="utf-8", errors="replace") as log:
                self.process = subprocess.Popen(
                    self.command,
                    cwd=str(PROJECT_ROOT),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=subprocess_env(),
                    creationflags=subprocess_creationflags(),
                )
                result = self.repo.get(self.task_id).to_dict().get("result") or {}
                if not isinstance(result, dict):
                    result = {}
                result["pid"] = self.process.pid
                result["spawn_mode"] = "process"
                self.repo.update(
                    self.task_id,
                    status="running",
                    result=result,
                    updated_at=now_iso(),
                )
                self.repo.add_event(self.task_id, "info", "process_started", f"子进程已启动: pid={self.process.pid}")
                assert self.process.stdout is not None
                for line in self.process.stdout:
                    log.write(line)
                    log.flush()
                    if self._stop_requested:
                        break
                code = self.process.wait()

            self._finalize(code, on_finish)
        except Exception as exc:
            self.repo.update(self.task_id, status="failed", finished_at=now_iso(), updated_at=now_iso(), error=str(exc), retryable=True)
            self.repo.add_event(self.task_id, "error", "failed", str(exc))
            if on_finish:
                on_finish(code if code != 0 else 1)

    def stop(self) -> bool:
        self._stop_requested = True
        if self.process and self.process.poll() is None:
            return terminate_process_tree(self.process.pid)
        # Inline: cooperative only; mark cancel requested in result for observability.
        try:
            current = self.repo.get(self.task_id).to_dict()
            if str(current.get("status") or "") in {"running", "starting"}:
                result = current.get("result") if isinstance(current.get("result"), dict) else {}
                if not isinstance(result, dict):
                    result = {}
                if result.get("inline"):
                    result["cancel_requested"] = 1
                    self.repo.update(self.task_id, result=result, updated_at=now_iso())
        except Exception:
            pass
        return False
