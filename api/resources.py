from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from application.resource_pool_service import ResourcePoolService

router = APIRouter()


def get_resource_pool_service() -> ResourcePoolService:
    return ResourcePoolService()


class ImportResourceRequest(BaseModel):
    resource_type: str
    provider: str
    text: str = ""
    # Optional server-local absolute path for huge batches (avoids paste/textarea).
    file_path: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class UpdateResourceStatusRequest(BaseModel):
    status: str
    cooldown_seconds: int = 0
    error: str = ""

class BulkResourceStatusRequest(BaseModel):
    status: str
    resource_ids: list[int] = []
    resource_type: str = ""
    provider: str = ""
    current_status: str = ""
    cooldown_seconds: int = 0
    error: str = ""

class BulkResourceDeleteRequest(BaseModel):
    resource_ids: list[int] = []
    resource_type: str = ""
    provider: str = ""
    current_status: str = ""


class ProxyHealthCheckRequest(BaseModel):
    text: str
    external: bool = False


class CapacityCheckRequest(BaseModel):
    need_phone: int = 0
    need_bind_phone: int = 0
    need_proxy: int = 0
    need_email: int = 0


@router.post('/resources/capacity-check')
def capacity_check(req: CapacityCheckRequest, svc: ResourcePoolService = Depends(get_resource_pool_service)):
    return svc.capacity_summary(need_phone=req.need_phone, need_bind_phone=req.need_bind_phone, need_proxy=req.need_proxy, need_email=req.need_email)


@router.get('/resources/categories')
def resource_categories(svc: ResourcePoolService = Depends(get_resource_pool_service)):
    return {'ok': True, 'items': svc.category_options()}


@router.get('/resources')
def list_resources(resource_type: str = '', provider: str = '', status: str = '', svc: ResourcePoolService = Depends(get_resource_pool_service)):
    return {'ok': True, 'items': svc.list_resources(resource_type, provider, status)}


@router.post('/resources/import')
def import_resources(req: ImportResourceRequest, svc: ResourcePoolService = Depends(get_resource_pool_service)):
    try:
        text = str(req.text or "")
        file_path = str(req.file_path or "").strip()
        if file_path:
            path = Path(file_path)
            if not path.is_file():
                return JSONResponse({'ok': False, 'message': f'导入文件不存在: {file_path}'}, status_code=400)
            # 50MB hard cap — Outlook 卡密 3000 行约 1.7MB。
            if path.stat().st_size > 50 * 1024 * 1024:
                return JSONResponse({'ok': False, 'message': '导入文件超过 50MB 上限'}, status_code=400)
            text = path.read_text(encoding='utf-8-sig', errors='replace')
        if not str(text or "").strip():
            return JSONResponse({'ok': False, 'message': '请提供导入文本或 file_path'}, status_code=400)
        resource_type = 'phone' if req.resource_type == 'bind_phone' else 'email' if req.resource_type == 'icloud_email' else req.resource_type
        if resource_type == 'phone' and req.provider in {'user_phone_url', 'bind_user_phone_url'}:
            count = svc.import_phone_urls(text, provider=req.provider)
        elif resource_type == 'proxy' and req.provider in {'proxy_seed', 'seed', 'lajiao_seed'}:
            count = svc.import_proxy_seeds(
                text,
                protocol=str(req.metadata.get('protocol') or 'socks5'),
                style=str(req.metadata.get('style') or ''),
            )
        elif resource_type == 'proxy' and req.provider == 'lajiao_credentials':
            # Sticky session imports collapse into seed pool.
            count = svc.import_proxy_seeds(
                text,
                protocol=str(req.metadata.get('protocol') or 'socks5'),
            )
        elif resource_type == 'email' and req.provider == 'outlook_token':
            count = svc.import_outlook_tokens(text, provider=req.provider)
        elif resource_type == 'email' and req.provider == 'icloud_api':
            count = svc.import_link_api_mailboxes(text, provider=req.provider)
        elif resource_type == 'email' and req.provider == 'icloud_privacy':
            count = svc.import_icloud_privacy_mailboxes(text, provider=req.provider)
        else:
            return JSONResponse({'ok': False, 'message': '不支持的资源类型或 provider'}, status_code=400)
    except (ValueError, RuntimeError, OSError) as exc:
        return JSONResponse({'ok': False, 'message': str(exc)}, status_code=400)
    return {'ok': True, 'count': count}


@router.post('/resources/proxy-seeds/migrate')
def migrate_proxy_seeds(svc: ResourcePoolService = Depends(get_resource_pool_service)):
    result = svc.migrate_sticky_proxies_to_seeds(disable_legacy=True)
    return {'ok': True, **result}


@router.post('/resources/status/bulk')
def set_resources_status_bulk(req: BulkResourceStatusRequest, svc: ResourcePoolService = Depends(get_resource_pool_service)):
    try:
        count = svc.set_status_bulk(
            resource_ids=req.resource_ids,
            resource_type=req.resource_type,
            provider=req.provider,
            current_status=req.current_status,
            status=req.status,
            cooldown_seconds=req.cooldown_seconds,
            error=req.error,
        )
    except ValueError as exc:
        return JSONResponse({'ok': False, 'message': str(exc)}, status_code=400)
    return {'ok': True, 'count': count}

@router.post('/resources/delete/bulk')
def delete_resources_bulk(req: BulkResourceDeleteRequest, svc: ResourcePoolService = Depends(get_resource_pool_service)):
    count = svc.delete_bulk(
        resource_ids=req.resource_ids,
        resource_type=req.resource_type,
        provider=req.provider,
        current_status=req.current_status,
    )
    return {'ok': True, 'count': count}


@router.post('/resources/proxy/health-check')
def check_proxy_health(req: ProxyHealthCheckRequest, svc: ResourcePoolService = Depends(get_resource_pool_service)):
    try:
        result = svc.check_proxy_health(req.text, external=req.external)
    except ValueError as exc:
        return JSONResponse({'ok': False, 'message': str(exc)}, status_code=400)
    return {'ok': True, **result}


@router.post('/resources/{resource_id}/status')
def set_resource_status(resource_id: int, req: UpdateResourceStatusRequest, svc: ResourcePoolService = Depends(get_resource_pool_service)):
    try:
        svc.set_status(resource_id, req.status, cooldown_seconds=req.cooldown_seconds, error=req.error)
    except ValueError as exc:
        return JSONResponse({'ok': False, 'message': str(exc)}, status_code=400)
    return {'ok': True}


@router.post('/resources/recover-stale')
def recover_stale_resources(lease_ttl_seconds: int = 1800, svc: ResourcePoolService = Depends(get_resource_pool_service)):
    return {'ok': True, 'recovered': svc.recover_stale(lease_ttl_seconds=lease_ttl_seconds)}
