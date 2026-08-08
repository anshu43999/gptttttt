"""定时调度器 — 驱动 LifecycleService 周期任务."""
from __future__ import annotations

import threading
import time

from application.lifecycle_service import LifecycleService


class Scheduler:
    _instance = None
    _running = False
    _tasks: dict[str, dict] = {
        "check_validity":  {"interval": 6 * 3600,  "fn": "check_validity"},
        "refresh_tokens":  {"interval": 12 * 3600, "fn": "refresh_tokens"},
        "flag_expiring":   {"interval": 24 * 3600, "fn": "flag_expiring_trials"},
    }

    @classmethod
    def start(cls):
        if cls._running:
            return
        cls._running = True
        svc = LifecycleService()
        for name, cfg in cls._tasks.items():
            fn = getattr(svc, cfg["fn"])
            t = threading.Thread(
                target=cls._run_loop, args=(name, cfg["interval"], fn), daemon=True
            )
            t.start()

    @classmethod
    def stop(cls):
        cls._running = False

    @classmethod
    def _run_loop(cls, name, interval, fn):
        while cls._running:
            try:
                fn()
            except Exception:
                pass
            time.sleep(interval)
