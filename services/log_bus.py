from __future__ import annotations

from typing import Any

from infrastructure.repositories.tasks_repository import TasksRepository


class LogBus:
    def __init__(self, repo: TasksRepository | None = None):
        self.repo = repo or TasksRepository()

    def emit(self, task_id: str, message: str, *, level: str = "info", event_type: str = "log", data: Any = None) -> None:
        self.repo.add_event(task_id, level, event_type, message, data)
