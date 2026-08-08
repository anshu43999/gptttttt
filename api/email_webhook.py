"""Email webhook API — Cloudflare Worker callback + OTP query."""
from __future__ import annotations

from fastapi import APIRouter, Request

from infrastructure.db import insert_email_otp, get_latest_email_otp, consume_email_otp

router = APIRouter()


@router.post("/receive-email")
async def receive_email(request: Request):
    """Cloudflare Worker → POST webhook. 提取验证码存入 DB."""
    data = await request.json()
    to_addr = str(data.get("to") or "").strip().lower()
    subject = str(data.get("subject") or "")
    body = str(data.get("text") or data.get("html") or "")
    full = f"{subject} {body}"

    import re
    m = re.search(r'\b(\d{6})\b', full)
    code = m.group(1) if m else ""

    if to_addr and code:
        insert_email_otp(to_addr, code, subject=subject, body=full)

    return {"ok": True, "email": to_addr, "code_found": bool(code)}


@router.get("/email-otp/{email}")
async def get_email_otp(email: str):
    """查询最新未消费的 OTP (注册流程轮询)."""
    otp = get_latest_email_otp(email.strip().lower())
    if otp:
        code = otp.get("code", "")
        if code:
            consume_email_otp(email, code, consumed_by="registration")
            return {"ok": True, "code": code}
    return {"ok": False, "message": "no otp found"}
