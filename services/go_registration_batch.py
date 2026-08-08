"""Thin control-plane client for Go-owned email registration batches.

Python only submits policy + polls aggregate status. Lease/proxy/OTP/protocol
and account import run inside email-protocol-worker.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

DEFAULT_GO_URL = "http://127.0.0.1:18765"


def _opener():
    # Never route loopback through system proxy (Fiddler).
    return build_opener(ProxyHandler({}))


def _base_url(config: dict[str, Any] | None = None) -> str:
    cfg = config or {}
    for key in ("go_email_protocol_url", "go_worker_url", "email_protocol_go_url"):
        raw = str(cfg.get(key) or "").strip()
        if raw:
            return raw.rstrip("/")
    return DEFAULT_GO_URL


def worker_supports_batches(config: dict[str, Any] | None = None, *, timeout: float = 2.0) -> bool:
    url = _base_url(config) + "/health"
    try:
        req = Request(url, headers={"Accept": "application/json"}, method="GET")
        with _opener().open(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return False
    features = body.get("features") or []
    if isinstance(features, list) and "email-register-batches" in features:
        return True
    return False


def _proxy_styles(config: dict[str, Any]) -> list[str]:
    raw = config.get("proxy_seed_styles") or config.get("proxy_styles") or "bestgo,1024"
    if isinstance(raw, (list, tuple)):
        parts = [str(x).strip() for x in raw]
    else:
        parts = [p.strip() for p in str(raw).split(",")]
    out = [p for p in parts if p]
    return out or ["bestgo", "1024"]


def _mailbox_provider(config: dict[str, Any]) -> str:
    raw = str(config.get("mailbox_provider") or "outlook_token").strip().lower()
    if raw in {"", "outlook", "hotmail", "graph", "outlook_token"}:
        return "outlook_token"
    return raw


def start_go_registration_batch(
    *,
    count: int,
    config: dict[str, Any],
    batch_id: str = "",
    max_concurrent: int | None = None,
) -> dict[str, Any]:
    """POST /v2/email-register-batches and return the worker snapshot."""
    n = max(1, int(count or 0))
    bid = str(batch_id or "").strip() or f"go_batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    if max_concurrent is None:
        try:
            max_concurrent = int(config.get("max_register_tasks") or config.get("go_batch_max_concurrent") or 200)
        except Exception:
            max_concurrent = 200
    max_concurrent = max(1, min(int(max_concurrent), n))
    try:
        otp_timeout = int(config.get("email_otp_timeout") or config.get("otp_timeout_seconds") or 120)
    except Exception:
        otp_timeout = 120
    otp_timeout = max(60, min(240, int(otp_timeout)))
    try:
        timeout_seconds = int(config.get("go_batch_timeout_seconds") or (otp_timeout + 90))
    except Exception:
        timeout_seconds = otp_timeout + 90
    region_raw = str(
        config.get("proxy_regions")
        or config.get("proxy_region")
        or config.get("proxy_expected_country")
        or config.get("lajiao_proxy_expected_country")
        or config.get("lajiao_proxy_regions")
        or "JP,US,DE,GB,BR"
    ).strip().upper() or "JP,US,DE,GB,BR"
    # Keep full multi-region CSV for Go bulk (worker spreads + remint rotates).
    regions = [p.strip() for p in region_raw.split(",") if p.strip() and len(p.strip()) == 2]
    if not regions:
        regions = ["JP", "US", "DE", "GB", "BR"]
    region = regions[0]
    try:
        email_tries = int(config.get("email_tries") or config.get("go_batch_email_tries") or 5)
    except Exception:
        email_tries = 5
    email_tries = max(1, min(20, email_tries))
    payload = {
        "batch_id": bid,
        "count": n,
        "max_concurrent": max_concurrent,
        "mailbox_provider": _mailbox_provider(config),
        "proxy_styles": _proxy_styles(config),
        "proxy_region": ",".join(regions),
        "proxy_regions": regions,
        "proxy_ttl_seconds": int(config.get("proxy_ttl_seconds") or 15),
        "otp_timeout_seconds": otp_timeout,
        "timeout_seconds": timeout_seconds,
        "email_tries": email_tries,
        "skip_phone": bool(config.get("mailat_protocol_skip_phone", True)),
    }
    url = _base_url(config) + "/v2/email-register-batches"
    data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST")
    try:
        with _opener().open(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = str(exc)
        raise RuntimeError(f"Go batch create failed HTTP {exc.code}: {detail[:400]}") from exc
    except URLError as exc:
        raise RuntimeError(f"Go batch create unreachable: {exc}") from exc


def get_go_registration_batch(batch_id: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    url = _base_url(config) + f"/v2/email-register-batches/{batch_id}"
    req = Request(url, headers={"Accept": "application/json"}, method="GET")
    with _opener().open(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def cancel_go_registration_batch(batch_id: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    url = _base_url(config) + f"/v2/email-register-batches/{batch_id}"
    req = Request(url, headers={"Accept": "application/json"}, method="DELETE")
    with _opener().open(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def wait_go_registration_batch(
    batch_id: str,
    *,
    config: dict[str, Any] | None = None,
    timeout_s: float = 900,
    poll_s: float = 2.0,
) -> dict[str, Any]:
    deadline = time.time() + max(30.0, float(timeout_s))
    last: dict[str, Any] = {}
    while time.time() < deadline:
        last = get_go_registration_batch(batch_id, config)
        if last.get("done"):
            return last
        time.sleep(max(0.5, float(poll_s)))
    return last


def try_start_go_batch_register(
    data: dict[str, Any],
    overrides: dict[str, Any],
    count: int,
    *,
    force: bool = False,
) -> int | None:
    """Return created count when Go batch path is used; None to let caller fall back."""
    from application.config_service import ConfigService

    total = max(0, int(count or 0))
    if total <= 0:
        return 0
    config_service = ConfigService(base_config=str(data.get("config") or "config.yaml"))
    base = config_service.merged_config()
    base.update(overrides or {})
    backend = str(base.get("email_protocol_backend") or overrides.get("email_protocol_backend") or "go").strip().lower()
    if not force and backend not in {"go", "golang", "go_worker", "go_daemon", ""}:
        # Empty defaults to go for pure path when env wants pure; keep None only for explicit python.
        if backend in {"python", "mailat", "node"}:
            return None
    if not worker_supports_batches(base):
        if force:
            raise RuntimeError(
                "Go batch registration required but worker is missing email-register-batches "
                f"(url={_base_url(base)})"
            )
        return None
    batch_id = str(overrides.get("batch_id") or base.get("batch_id") or "").strip()
    try:
        max_c = int(base.get("max_register_tasks") or 200)
    except Exception:
        max_c = 200
    # Operator threads from UI land in max_register_tasks; prefer that over stale defaults.
    try:
        if int(base.get("register_threads") or 0) > 0:
            max_c = max(max_c, int(base.get("register_threads") or 0))
    except Exception:
        pass
    view = start_go_registration_batch(count=total, config=base, batch_id=batch_id, max_concurrent=max_c)
    created = int(view.get("count") or 0)
    if created <= 0 and isinstance(view.get("task_ids"), list):
        created = len(view["task_ids"])
    return created if created > 0 else total
