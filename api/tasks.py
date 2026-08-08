"""Tasks API — FastAPI router with SSE streaming support."""
from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from api.deps import get_tasks_service
from application.tasks_service import TasksService
from starlette.concurrency import run_in_threadpool



def _read_task_log_tail(task_id: str, tail_bytes: int) -> PlainTextResponse | dict[str, str | bool]:
    path = TASK_ROOT / f"{task_id}.log"
    if not path.exists():
        return {"ok": False, "message": "log not found"}
    size = path.stat().st_size
    with path.open("rb") as handle:
        handle.seek(max(0, size - max(1, min(tail_bytes, 2_000_000))))
        text = handle.read().decode("utf-8", errors="replace")
    return PlainTextResponse(text)

def _read_task_stream_updates(task_id: str, since_id: int, log_offset: int, svc: TasksService) -> tuple[list[dict], list[str], int, str]:
    path = TASK_ROOT / f"{task_id}.log"
    next_offset = log_offset
    rows = svc.task_events(task_id, since_id)
    lines: list[str] = []
    if path.exists():
        size = path.stat().st_size
        if size < next_offset:
            next_offset = 0
        if size > next_offset:
            with path.open("rb") as handle:
                handle.seek(next_offset)
                chunk = handle.read(size - next_offset).decode("utf-8", errors="replace")
            next_offset = size
            lines = [line for line in chunk.splitlines() if line.strip()]
    task = svc.get_task(task_id)
    return rows, lines, next_offset, str(task.get("status") or "")




router = APIRouter()
PROJECT_ROOT = Path(__file__).resolve().parents[1]
TASK_ROOT = PROJECT_ROOT / "data" / "tasks"


class RetryRequest(BaseModel):
    pass

class ExportTaskBatchesRequest(BaseModel):
    batch_ids: list[str] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=list)
    only_succeeded: bool = True
    archive_after_export: bool = False


class ExportTaskBatchesAtRequest(BaseModel):
    batch_ids: list[str] = Field(default_factory=list)
    only_succeeded: bool = True
    archive_after_export: bool = False
    # 0 = single growing file; >0 = accounts per split file
    chunk_size: int = 0
    # empty = project at-file/
    write_dir: str = ""
    # empty = now stamp YYYY-MM-DD-HH-MM-SS
    stamp: str = ""
    # realtime: only flush accounts not yet marked at_exported
    only_unexported: bool = True



@router.get("/tasks")
async def list_tasks(status: str = "", limit: int = 50, offset: int = 0, svc: TasksService = Depends(get_tasks_service)):
    # Never block the list endpoint on drain (resource prep can be slow), but always
    # kick a background drain so queued tasks recover after restart / missed async start.
    if hasattr(svc, "drain_queue_async"):
        svc.drain_queue_async()
    elif hasattr(svc, "drain_queue"):
        threading.Thread(target=svc.drain_queue, daemon=True).start()
    try:
        items = await run_in_threadpool(
            svc.list_tasks,
            status=status,
            limit=limit,
            offset=offset,
            drain_queue=False,
            reconcile_stale=False,
        )
    except TypeError:
        items = await run_in_threadpool(svc.list_tasks)
        if status:
            items = [item for item in items if str(item.get("status") or "") == status]
        parsed_offset = max(0, int(offset or 0))
        parsed_limit = max(1, min(int(limit or 50), 500))
        items = items[parsed_offset:parsed_offset + parsed_limit]
    counts: dict[str, int] = {}
    if hasattr(svc, "task_status_counts"):
        try:
            counts = await run_in_threadpool(svc.task_status_counts)
        except Exception:
            counts = {}
    return {
        "ok": True,
        "items": items,
        "counts": counts,
        "limit": max(1, min(int(limit or 50), 500)),
        "offset": max(0, int(offset or 0)),
    }


@router.get("/tasks/batches")
async def list_task_batches(limit: int = 20, since: str = "", svc: TasksService = Depends(get_tasks_service)):
    """Lightweight batch summary for the Tasks page — no per-task card payload."""
    if hasattr(svc, "drain_queue_async"):
        svc.drain_queue_async()
    counts: dict[str, int] = {}
    if hasattr(svc, "task_status_counts"):
        try:
            counts = await run_in_threadpool(svc.task_status_counts)
        except Exception:
            counts = {}
    batches: list[dict] = []
    if hasattr(svc, "task_batch_summaries"):
        try:
            batches = await run_in_threadpool(svc.task_batch_summaries, limit=limit, since=since)
        except Exception:
            batches = []
    active = int(counts.get("running") or 0) + int(counts.get("starting") or 0)
    queued = int(counts.get("queued") or 0) + int(counts.get("pending") or 0)
    succeeded = int(counts.get("succeeded") or 0)
    failed = int(counts.get("failed") or 0) + int(counts.get("interrupted") or 0)
    total = sum(int(v or 0) for v in counts.values())
    return {
        "ok": True,
        "counts": counts,
        "summary": {
            "total": total,
            "running": active,
            "queued": queued,
            "succeeded": succeeded,
            "failed": failed,
            "active": active + queued,
            "starting": int(counts.get("starting") or 0),
        },
        "batches": batches,
        "limit": max(1, min(int(limit or 20), 100)),
    }


@router.get("/tasks/scheduler-health")
async def tasks_scheduler_health(svc: TasksService = Depends(get_tasks_service)):
    """Real concurrency: alive_pid / nopid / starting / go active. Detect fake running."""
    if hasattr(svc, "ensure_reconcile_loop"):
        try:
            svc.ensure_reconcile_loop()
        except Exception:
            pass
    if hasattr(svc, "scheduler_health"):
        return await run_in_threadpool(svc.scheduler_health)
    return {"ok": False, "message": "scheduler_health unavailable"}



@router.post("/tasks/batches/export")
async def export_task_batches(req: ExportTaskBatchesRequest, svc: TasksService = Depends(get_tasks_service)):
    """Export accounts created by selected register task batches; sync export_status."""
    batch_ids = [str(item or "").strip() for item in (req.batch_ids or []) if str(item or "").strip()]
    if not batch_ids:
        return JSONResponse({"ok": False, "message": "请至少选择一个批次", "count": 0, "products": []}, status_code=400)
    if not hasattr(svc, "export_batch_accounts"):
        return JSONResponse({"ok": False, "message": "当前服务不支持批次导出"}, status_code=501)
    result = await run_in_threadpool(
        svc.export_batch_accounts,
        batch_ids,
        req.fields or None,
        only_succeeded=bool(req.only_succeeded),
        archive_after_export=bool(req.archive_after_export),
    )
    status = 200 if result.get("ok") else 400
    if status >= 400:
        return JSONResponse(result, status_code=status)
    return result


@router.post("/tasks/batches/export-at-txt")
async def export_task_batches_at_txt(req: ExportTaskBatchesAtRequest, svc: TasksService = Depends(get_tasks_service)):
    """Incremental AT export for register batches under at-file/{stamp}/.

    Designed for realtime polling: only_unexported=True flushes newly succeeded
    accounts into rolling split files. Empty new_count is still HTTP 200.
    """
    batch_ids = [str(item or "").strip() for item in (req.batch_ids or []) if str(item or "").strip()]
    if not batch_ids:
        return JSONResponse({"ok": False, "message": "请至少选择一个批次", "count": 0, "files": []}, status_code=400)
    if not hasattr(svc, "export_batch_at_products_txt"):
        return JSONResponse({"ok": False, "message": "当前服务不支持批次 AT 导出"}, status_code=501)
    write_dir = str(req.write_dir or "").strip() or str(PROJECT_ROOT / "at-file")
    chunk = int(req.chunk_size or 0)
    result = await run_in_threadpool(
        svc.export_batch_at_products_txt,
        batch_ids,
        only_succeeded=bool(req.only_succeeded),
        archive_after_export=bool(req.archive_after_export),
        chunk_size=chunk,
        write_dir=write_dir,
        stamp=str(req.stamp or "").strip() or None,
        only_unexported=bool(req.only_unexported),
    )
    if result.get("ok") is False:
        return JSONResponse(result, status_code=400)
    return result


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, svc: TasksService = Depends(get_tasks_service)):
    task = await run_in_threadpool(svc.get_task, task_id)
    if not task.get("id"):
        return {"ok": False, "message": "task not found"}
    return {"ok": True, "task": task}


@router.get("/tasks/{task_id}/events")
async def task_events(task_id: str, since_id: int = 0, svc: TasksService = Depends(get_tasks_service)):
    return {"ok": True, "items": await run_in_threadpool(svc.task_events, task_id, since_id)}


@router.get("/tasks/{task_id}/logs")
async def task_log(task_id: str, tail_bytes: int = 300_000):
    return await run_in_threadpool(_read_task_log_tail, task_id, tail_bytes)


@router.get("/tasks/{task_id}/logs/stream")
async def task_log_stream(task_id: str, request: Request, svc: TasksService = Depends(get_tasks_service)):
    async def event_stream():
        since_id = 0
        path = TASK_ROOT / f"{task_id}.log"
        log_offset = await run_in_threadpool(lambda: path.stat().st_size if path.exists() else 0)
        while True:
            if await request.is_disconnected():
                return
            events, log_lines, log_offset, status = await run_in_threadpool(_read_task_stream_updates, task_id, since_id, log_offset, svc)
            for event in events:
                since_id = max(since_id, int(event.get("id") or 0))
                if str(event.get("event_type") or "") == "log":
                    continue
                payload = json.dumps(event, ensure_ascii=False)
                yield f"id: {since_id}\ndata: {payload}\n\n"
            for line in log_lines:
                payload = json.dumps({"timestamp": "", "level": "info", "event_type": "log", "message": line}, ensure_ascii=False)
                yield f"data: {payload}\n\n"
            if status in {"succeeded", "failed", "cancelled", "interrupted"}:
                yield f"event: done\ndata: {json.dumps({'status': status})}\n\n"
                return
            await asyncio.sleep(1)

    return StreamingResponse(event_stream(), media_type="text/event-stream")



@router.post("/tasks/stop-all")
async def stop_all_tasks(svc: TasksService = Depends(get_tasks_service)):
    return {"ok": True, **await run_in_threadpool(svc.stop_all)}

@router.post("/tasks/{task_id}/stop")
async def stop_task(task_id: str, svc: TasksService = Depends(get_tasks_service)):
    return {"ok": True, "stopped": await run_in_threadpool(svc.stop, task_id)}


@router.post("/tasks/{task_id}/retry")
async def retry_task(task_id: str, svc: TasksService = Depends(get_tasks_service)):
    task = await run_in_threadpool(svc.retry, task_id)
    if not task:
        return {"ok": False, "message": "task not retryable"}
    return {"ok": True, "task": task}
