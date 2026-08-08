"""
Sub2API 上传功能
"""

from __future__ import annotations

import base64
import json
import logging
import time
from urllib.parse import quote
from typing import Any, Tuple

try:
    from curl_cffi import requests as cffi_requests
except ImportError:
    import requests as cffi_requests

from .cpa_upload import generate_token_json

logger = logging.getLogger(__name__)

DEFAULT_GROUP_IDS = [2]
DEFAULT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"


def _get_config_value(key: str) -> str:
    try:
        from core.config_store import config_store

        return str(config_store.get(key, "") or "").strip()
    except Exception:
        return ""


def _parse_group_ids(raw: Any, fallback: list[int] | None = None) -> list[int]:
    candidates: list[Any]
    if isinstance(raw, str):
        candidates = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, (list, tuple, set)):
        candidates = list(raw)
    elif raw is None:
        candidates = []
    else:
        candidates = [raw]

    values: list[int] = []
    for item in candidates:
        text = str(item or "").strip()
        if not text:
            continue
        try:
            values.append(int(text))
        except ValueError:
            continue

    return values or list(fallback or DEFAULT_GROUP_IDS)


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    try:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding
        decoded = base64.urlsafe_b64decode(payload)
        data = json.loads(decoded)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _extract_auth(payload: dict[str, Any]) -> dict[str, Any]:
    auth_info = payload.get("https://api.openai.com/auth")
    return auth_info if isinstance(auth_info, dict) else {}


def _extract_organization_id(id_token_payload: dict[str, Any]) -> str:
    auth_info = _extract_auth(id_token_payload)
    organization_id = str(auth_info.get("organization_id") or "").strip()
    if organization_id:
        return organization_id

    organizations = auth_info.get("organizations") or []
    if isinstance(organizations, list):
        for item in organizations:
            if isinstance(item, dict):
                organization_id = str(item.get("id") or "").strip()
                if organization_id:
                    return organization_id
    return ""


def _build_sub2api_account_payload(account, group_ids: list[int] | None = None) -> dict[str, Any]:
    token_data = generate_token_json(account)
    access_token = str(token_data.get("access_token") or "").strip()
    refresh_token = str(token_data.get("refresh_token") or "").strip()
    id_token = str(token_data.get("id_token") or "").strip()
    email = str(token_data.get("email") or getattr(account, "email", "") or "").strip()

    access_payload = _decode_jwt_payload(access_token)
    access_auth = _extract_auth(access_payload)
    expires_at = access_payload.get("exp")
    if not isinstance(expires_at, int) or expires_at <= 0:
        expires_at = int(time.time()) + 863999

    # 关键逻辑：Sub2API 依赖 OpenAI OAuth 结构化字段，这里尽量从现有 token 自动补齐。
    organization_id = _extract_organization_id(_decode_jwt_payload(id_token))

    return {
        "name": email,
        "notes": "",
        "platform": "openai",
        "type": "oauth",
        "credentials": {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_in": 863999,
            "expires_at": expires_at,
            "chatgpt_account_id": str(
                access_auth.get("chatgpt_account_id") or token_data.get("account_id") or ""
            ).strip(),
            "chatgpt_user_id": str(access_auth.get("chatgpt_user_id") or "").strip(),
            "organization_id": organization_id,
            "client_id": str(getattr(account, "client_id", "") or DEFAULT_CLIENT_ID).strip() or DEFAULT_CLIENT_ID,
            "id_token": id_token,
        },
        "extra": {"email": email},
        "group_ids": _parse_group_ids(group_ids),
        "concurrency": 10,
        "priority": 1,
        "auto_pause_on_expired": True,
    }


def upload_to_sub2api(
    account,
    api_url: str | None = None,
    api_key: str | None = None,
    group_ids: list[int] | None = None,
) -> Tuple[bool, str]:
    """上传单个账号到 Sub2API 管理后台。"""
    api_url = str(api_url or _get_config_value("sub2api_api_url")).strip()
    api_key = str(api_key or _get_config_value("sub2api_api_key")).strip()
    resolved_group_ids = _parse_group_ids(
        _get_config_value("sub2api_group_ids") if group_ids is None else group_ids
    )

    if not api_url:
        return False, "Sub2API API URL 未配置"
    if not api_key:
        return False, "Sub2API API Key 未配置"

    payload = _build_sub2api_account_payload(account, group_ids=resolved_group_ids)
    url = f"{api_url.rstrip('/')}/api/v1/admin/accounts"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Referer": f"{api_url.rstrip('/')}/admin/accounts",
        "x-api-key": api_key,
    }

    try:
        response = cffi_requests.post(
            url,
            headers=headers,
            json=payload,
            proxies=None,
            verify=False,
            timeout=30,
            impersonate="chrome110",
        )

        if response.status_code in (200, 201):
            return True, "上传成功"

        error_msg = f"上传失败: HTTP {response.status_code}"
        try:
            detail = response.json()
            if isinstance(detail, dict):
                error_msg = str(
                    detail.get("message")
                    or detail.get("msg")
                    or detail.get("error")
                    or error_msg
                )
        except Exception:
            error_msg = f"{error_msg} - {response.text[:200]}"
        return False, error_msg
    except Exception as exc:
        logger.error("Sub2API 上传异常: %s", exc)
        return False, f"上传异常: {exc}"

def verify_sub2api_upload(
    account,
    api_url: str | None = None,
    api_key: str | None = None,
) -> Tuple[bool, str]:
    """按邮箱/account_id 回查 Sub2API；没有可用查询结果时返回未验证。"""
    api_url = str(api_url or _get_config_value("sub2api_api_url")).strip()
    api_key = str(api_key or _get_config_value("sub2api_api_key")).strip()
    token_data = generate_token_json(account)
    email = str(token_data.get("email") or getattr(account, "email", "") or "").strip().lower()
    account_id = str(token_data.get("account_id") or getattr(account, "account_id", "") or "").strip()
    if not api_url or not api_key:
        return False, "Sub2API 回查配置缺失"
    if not email and not account_id:
        return False, "Sub2API 回查缺少 email/account_id"
    headers = {"Accept": "application/json", "x-api-key": api_key}
    base = api_url.rstrip("/")
    candidates = []
    if email:
        candidates.append(f"{base}/api/v1/admin/accounts?email={quote(email)}")
    if account_id:
        candidates.append(f"{base}/api/v1/admin/accounts?account_id={quote(account_id)}")
    last = ""
    for url in candidates:
        try:
            response = cffi_requests.get(url, headers=headers, proxies=None, verify=False, timeout=30, impersonate="chrome110")
            last = f"HTTP {response.status_code}"
            if response.status_code != 200:
                continue
            data = response.json()
        except Exception as exc:
            last = str(exc)
            continue
        rows = data.get("data") if isinstance(data, dict) else data
        if isinstance(rows, dict):
            rows = rows.get("items") or rows.get("list") or rows.get("records") or [rows]
        if not isinstance(rows, list):
            continue
        for row in rows:
            text = json.dumps(row, ensure_ascii=False).lower() if isinstance(row, dict) else str(row).lower()
            if (email and email in text) or (account_id and account_id.lower() in text):
                return True, "Sub2API 回查成功"
    return False, f"Sub2API 上传后未回查到精确账号: {last or 'no query result'}"
