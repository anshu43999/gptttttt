"""Provider management — FastAPI router."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from api.deps import get_providers_service
from application.providers_service import ProvidersService

router = APIRouter()


class TestProviderRequest(BaseModel):
    provider_type: str
    provider_name: str
    data: dict[str, Any] = {}



class SaveProviderRequest(BaseModel):
    provider_type: str
    provider_name: str
    enabled: bool = True
    settings: dict[str, Any] = {}

@router.get("/providers")
def list_providers(svc: ProvidersService = Depends(get_providers_service)):
    return {"ok": True, "items": svc.list_providers()}


@router.post("/providers/test")
def test_provider(req: TestProviderRequest, svc: ProvidersService = Depends(get_providers_service)):
    try:
        return svc.test_provider(req.provider_type, req.provider_name, req.data)
    except Exception as exc:
        return JSONResponse({"ok": False, "provider_type": req.provider_type, "message": str(exc)}, status_code=200)


@router.post("/providers")
def save_provider(req: SaveProviderRequest, svc: ProvidersService = Depends(get_providers_service)):
    try:
        provider = svc.save_provider(req.provider_type, req.provider_name, req.settings, req.enabled)
    except (ValueError, RuntimeError) as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
    return {"ok": True, "provider": provider}
