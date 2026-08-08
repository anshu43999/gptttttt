"""Stats API — FastAPI router."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from application.stats_service import StatsService
from starlette.concurrency import run_in_threadpool


router = APIRouter()

_stats_svc = StatsService()


@router.get("/stats/overview")
async def overview():
    data = await run_in_threadpool(StatsService().overview)
    return {"ok": True, **data}


@router.get("/stats/by-day")
async def by_day(days: int = Query(default=7, ge=1, le=90)):
    return {"ok": True, "items": await run_in_threadpool(StatsService().by_day, days)}


@router.get("/stats/by-proxy")
async def by_proxy():
    return {"ok": True, "items": await run_in_threadpool(StatsService().by_proxy)}


@router.get("/stats/errors")
async def errors(limit: int = Query(default=20, ge=1, le=100)):
    return {"ok": True, "items": await run_in_threadpool(StatsService().errors, limit)}
