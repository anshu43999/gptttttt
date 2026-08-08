"""Go multi-worker Plus / subscription verify client.

Calls email-protocol-worker:
  POST /v2/plus-verify
  body: { items: [{key, account_id, access_token, proxy}], workers, timeout_ms }
"""
from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener, urlopen

DEFAULT_GO_EMAIL_PROTOCOL_URL = "http://127.0.0.1:18765"


def _first(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        for item in value:
            text = str(item or "").strip()
            if text:
                return text
        return ""
    return str(value).strip()


def go_plus_verify_base_url(config: dict[str, Any] | None = None) -> str:
    cfg = config if isinstance(config, dict) else {}
    return _first(
        cfg.get("go_email_protocol_url")
        or cfg.get("email_protocol_go_url")
        or DEFAULT_GO_EMAIL_PROTOCOL_URL
    ).rstrip("/")


def _http_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = 120.0,
) -> dict[str, Any]:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers, method=method.upper())
    host = (urlparse(url).hostname or "").lower()
    try:
        if host in {"127.0.0.1", "localhost", "::1"}:
            with build_opener(ProxyHandler({})).open(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        else:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        raise RuntimeError(f"Go plus-verify HTTP {exc.code}: {detail[:500]}") from exc
    except URLError as exc:
        raise RuntimeError(
            f"无法连接 Go worker（{url}）：{exc.reason}。请先启动 email-protocol-worker。"
        ) from exc
    if not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise RuntimeError(f"Go plus-verify 返回类型错误: {type(data).__name__}")
    return data


def check_go_plus_verify_available(config: dict[str, Any] | None = None, *, timeout: float = 3.0) -> dict[str, Any]:
    base = go_plus_verify_base_url(config)
    data = _http_json("GET", f"{base}/health", timeout=timeout)
    features = data.get("features") if isinstance(data.get("features"), list) else []
    # Older workers without features still may 404 the route; caller handles that.
    data["_features"] = [str(item) for item in features]
    return data


def go_plus_verify_batch(
    items: list[dict[str, Any]],
    *,
    workers: int = 32,
    timeout_ms: int = 15000,
    config: dict[str, Any] | None = None,
    request_timeout: float | None = None,
) -> dict[str, Any]:
    """Run multi-worker Plus checks via Go.

    Each item: {key, account_id?, access_token, proxy?}
    proxy should be an HTTP CONNECT URL (preferably local bridge http://127.0.0.1:port).
    """
    base = go_plus_verify_base_url(config)
    payload = {
        "items": items,
        "workers": max(1, min(int(workers or 32), 100)),
        "timeout_ms": max(1000, min(int(timeout_ms or 15000), 60000)),
    }
    # Budget: roughly (n/workers)*timeout + overhead, clamped.
    n = max(1, len(items))
    w = int(payload["workers"])
    if request_timeout is None:
        request_timeout = min(900.0, max(60.0, (n / w) * (payload["timeout_ms"] / 1000.0) * 1.5 + 30.0))
    return _http_json("POST", f"{base}/v2/plus-verify", payload, timeout=float(request_timeout))
