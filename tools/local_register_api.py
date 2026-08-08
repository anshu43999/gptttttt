from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from core import account_store
from api.context import DashboardContext
from api import accounts as accounts_api
from api import config as config_api
from api import providers as providers_api
from api import register as register_api
from api import tasks as tasks_api

CTX = DashboardContext(PROJECT_ROOT)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def json_response(handler: BaseHTTPRequestHandler, payload: dict, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def text_response(handler: BaseHTTPRequestHandler, text: str, content_type: str = "text/html; charset=utf-8") -> None:
    body = text.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def bytes_response(handler: BaseHTTPRequestHandler, body: bytes, content_type: str) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def content_type_for(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".html": "text/html; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".ico": "image/x-icon",
        ".json": "application/json; charset=utf-8",
    }.get(suffix, "application/octet-stream")


def serve_ui(handler: BaseHTTPRequestHandler, path: str) -> bool:
    if path in {"/", "/dashboard"}:
        index = CTX.ui_path if CTX.ui_path.exists() else CTX.legacy_ui_path
        text_response(handler, index.read_text(encoding="utf-8"))
        return True
    if path.startswith("/assets/") and CTX.ui_dist_path.exists():
        asset = (CTX.ui_dist_path / path.lstrip("/")).resolve()
        try:
            asset.relative_to(CTX.ui_dist_path.resolve())
        except ValueError:
            return False
        if asset.exists() and asset.is_file():
            bytes_response(handler, asset.read_bytes(), content_type_for(asset))
            return True
    return False


def read_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length).decode("utf-8", errors="replace")
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}




class Handler(BaseHTTPRequestHandler):
    server_version = "GPTRegisterDashboard/0.1"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[dashboard] {self.address_string()} {fmt % args}")

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if serve_ui(self, path):
            return
        if path == "/api/health":
            json_response(self, {"ok": True, "time": now_iso()})
            return
        if path == "/api/summary" or path == "/api/dashboard/summary":
            json_response(self, {"ok": True, "summary": CTX.summary()})
            return
        if path == "/api/accounts":
            json_response(self, accounts_api.list_accounts(CTX))
            return
        if path.startswith("/api/accounts/") and path.endswith("/tokens"):
            payload, status = accounts_api.reveal_tokens(CTX, path.split("/")[-2])
            json_response(self, payload, status)
            return
        if path.startswith("/api/accounts/") and "/artifact/" in path:
            parts = path.split("/")
            payload, status = accounts_api.artifact(CTX, parts[3], parts[-1])
            json_response(self, payload, status)
            return
        if path.startswith("/api/accounts/"):
            key = path.rsplit("/", 1)[-1]
            payload, status = accounts_api.get_account(CTX, key)
            json_response(self, payload, status)
            return
        if path == "/api/tasks":
            json_response(self, tasks_api.list_tasks(CTX))
            return
        if path.startswith("/api/tasks/") and path.endswith("/stream"):
            tasks_api.write_stream_response(CTX, self, path.split("/")[-2])
            return
        if path.startswith("/api/tasks/") and path.endswith("/log"):
            tasks_api.write_log_response(CTX, self, path.split("/")[-2])
            return
        if path.startswith("/api/tasks/") and path.endswith("/events"):
            qs = parse_qs(urlsplit(self.path).query)
            since_id = int((qs.get("since_id") or ["0"])[0] or 0)
            json_response(self, tasks_api.events(CTX, path.split("/")[-2], since_id))
            return
        if path.startswith("/api/tasks/"):
            payload, status = tasks_api.get_task(CTX, path.rsplit("/", 1)[-1])
            json_response(self, payload, status)
            return
        if path == "/api/config":
            json_response(self, config_api.get_config(CTX))
            return
        if path == "/api/providers":
            json_response(self, providers_api.list_providers(CTX))
            return
        json_response(self, {"ok": False, "message": "not found"}, 404)

    def do_DELETE(self) -> None:
        path = urlsplit(self.path).path
        if path.startswith("/api/accounts/"):
            payload, status = accounts_api.archive_account(CTX, path.rsplit("/", 1)[-1])
            json_response(self, payload, status)
            return
        json_response(self, {"ok": False, "message": "not found"}, 404)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        try:
            data = read_body(self)
            if path == "/api/import-legacy":
                json_response(self, {"ok": True, "count": CTX.import_legacy(copy_artifacts=True)})
                return
            if path == "/api/register/start":
                json_response(self, register_api.start_register(CTX, data))
                return
            if path == "/api/resume/start":
                json_response(self, register_api.start_resume(CTX, data))
                return
            if path.startswith("/api/accounts/") and path.endswith("/mark-plus"):
                payload, status = accounts_api.mark_plus(CTX, path.split("/")[-2])
                json_response(self, payload, status)
                return
            if path.startswith("/api/accounts/") and path.endswith("/resume-oauth"):
                payload, status = accounts_api.resume_oauth(CTX, path.split("/")[-2], data)
                json_response(self, payload, status)
                return
            if path.startswith("/api/accounts/") and path.endswith("/check-plus"):
                payload, status = accounts_api.check_plus(CTX, path.split("/")[-2])
                json_response(self, payload, status)
                return
            if path.startswith("/api/accounts/") and path.endswith("/export-product"):
                payload, status = accounts_api.export_product(CTX, path.split("/")[-2])
                json_response(self, payload, status)
                return
            if path.startswith("/api/providers/") and path.endswith("/test"):
                parts = path.split("/")
                json_response(self, providers_api.test_provider(CTX, parts[-3], parts[-2], data))
                return
            if path == "/api/config":
                json_response(self, config_api.save_config(CTX, data))
                return
            if path.startswith("/api/tasks/") and (path.endswith("/stop") or path.endswith("/cancel")):
                json_response(self, tasks_api.stop(CTX, path.split("/")[-2]))
                return
            if path.startswith("/api/tasks/") and path.endswith("/retry"):
                payload, status = tasks_api.retry(CTX, path.split("/")[-2])
                json_response(self, payload, status)
                return
            json_response(self, {"ok": False, "message": "not found"}, 404)
        except Exception as exc:
            json_response(self, {"ok": False, "message": str(exc)}, 500)


def run(host: str = "127.0.0.1", port: int = 8788) -> None:
    account_store.import_legacy_outputs(copy_artifacts=False)
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"GPT Register Dashboard: http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run(os.environ.get("GPT_REGISTER_DASHBOARD_HOST", "127.0.0.1"), int(os.environ.get("GPT_REGISTER_DASHBOARD_PORT", "8788")))
