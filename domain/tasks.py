from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TaskSummary:
    id: str
    task_type: str
    status: str
    created_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    updated_at: str = ""
    error: str = ""
    retryable: bool = False
    params: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    command: list[str] = field(default_factory=list)
    log_file: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskSummary":
        return cls(
            id=str(data.get("id") or ""),
            task_type=str(data.get("task_type") or data.get("type") or ""),
            status=str(data.get("status") or ""),
            created_at=str(data.get("created_at") or ""),
            started_at=str(data.get("started_at") or ""),
            finished_at=str(data.get("finished_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            error=str(data.get("error") or ""),
            retryable=bool(data.get("retryable")),
            params=data.get("params") if isinstance(data.get("params"), dict) else {},
            result=data.get("result") if isinstance(data.get("result"), dict) else {},
            command=data.get("command") if isinstance(data.get("command"), list) else [],
            log_file=str(data.get("log_file") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.task_type,
            "task_type": self.task_type,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "updated_at": self.updated_at,
            "error": self.error,
            "retryable": self.retryable,
            "params": dict(self.params),
            "result": dict(self.result),
            "command": list(self.command),
            "log_file": self.log_file,
        }


@dataclass
class TaskEvent:
    id: int
    task_id: str
    timestamp: str
    level: str
    event_type: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)
