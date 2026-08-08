"""Configuration API — FastAPI router."""
from __future__ import annotations
import threading


from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api.deps import get_config_service, get_tasks_service
from application.config_service import ConfigService
from application.tasks_service import TasksService

router = APIRouter()


class SaveConfigRequest(BaseModel):
    model_config = {"extra": "allow"}


@router.get("/config")
def get_config(config_svc: ConfigService = Depends(get_config_service)):
    return {"ok": True, "config": config_svc.file_config(), "db_config": config_svc.db_config()}


@router.post("/config")
def save_config(data: SaveConfigRequest, config_svc: ConfigService = Depends(get_config_service),
                tasks_svc: TasksService = Depends(get_tasks_service)):
    saved = config_svc.save_overrides(data.model_dump())
    if any(key in data.model_dump() for key in ("max_parallel_tasks", "max_register_tasks", "max_oauth_tasks", "max_maintenance_tasks")):
        tasks_svc.reload_limits()
        if hasattr(tasks_svc, "drain_queue"):
            threading.Thread(target=tasks_svc.drain_queue, daemon=True).start()
    return {"ok": True, "config": config_svc.db_config()}
