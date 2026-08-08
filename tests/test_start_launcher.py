from __future__ import annotations

import os
import importlib.util
import socket
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_start():
    path = ROOT / "start.py"
    spec = importlib.util.spec_from_file_location("gpt_register_start", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Avoid SystemExit on non-3.13 CI if any — start.py checks version at import.
    # Tests run under project Python 3.13.
    spec.loader.exec_module(mod)
    return mod


def test_default_ports_and_worker_url():
    start = _load_start()
    assert start.DEFAULT_WEBUI_PORT == 47718
    assert start.DEFAULT_GO_WORKER_PORT == 18765
    assert start.GO_WORKER_URL == "http://127.0.0.1:18765"


def test_find_free_port_returns_open_port():
    start = _load_start()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        busy = sock.getsockname()[1]
        # hold busy open while probing from busy
        free = start.find_free_port(busy, attempts=5)
    assert free >= busy
    assert free != busy or start.port_free(free)
    # free port must actually bind
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as check:
        check.bind(("127.0.0.1", free))


def test_resolve_webui_port_uses_env(monkeypatch):
    start = _load_start()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        preferred = sock.getsockname()[1]
    # preferred free
    monkeypatch.setenv("GPT_REGISTER_BACKEND_PORT", str(preferred))
    assert start.resolve_webui_port() == preferred


def test_go_worker_binary_prefers_exe_on_windows(monkeypatch, tmp_path):
    start = _load_start()
    fake_dir = tmp_path / "go-email-protocol"
    fake_dir.mkdir()
    exe = fake_dir / "email-protocol-worker.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(start, "GO_DIR", fake_dir)
    monkeypatch.setattr(start.os, "name", "nt")
    assert start.go_worker_binary() == exe


def test_go_worker_healthy_false_when_down(monkeypatch):
    start = _load_start()
    monkeypatch.setattr(start, "GO_WORKER_URL", "http://127.0.0.1:9")
    assert start.go_worker_healthy(timeout=0.2) is None


def test_apply_db_env_loads_env_db(monkeypatch, tmp_path):
    start = _load_start()
    env_file = tmp_path / "env.db"
    env_file.write_text(
        "\n".join(
            [
                "# comment",
                "GPT_REGISTER_DB_BACKEND=postgres",
                "GPT_REGISTER_DATABASE_URL=postgresql://gpt:gpt@127.0.0.1:5432/gpt_register",
                "DATABASE_URL=postgresql://gpt:gpt@127.0.0.1:5432/gpt_register",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(start, "ENV_DB_FILE", env_file)
    monkeypatch.delenv("GPT_REGISTER_DB_BACKEND", raising=False)
    monkeypatch.delenv("GPT_REGISTER_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    applied = start.apply_db_env()
    assert applied["GPT_REGISTER_DB_BACKEND"] == "postgres"
    assert "gpt_register" in applied["GPT_REGISTER_DATABASE_URL"]
    assert os.environ["GPT_REGISTER_DB_BACKEND"] == "postgres"


def test_apply_db_env_keeps_existing(monkeypatch, tmp_path):
    start = _load_start()
    env_file = tmp_path / "env.db"
    env_file.write_text("GPT_REGISTER_DB_BACKEND=postgres\n", encoding="utf-8")
    monkeypatch.setattr(start, "ENV_DB_FILE", env_file)
    monkeypatch.setenv("GPT_REGISTER_DB_BACKEND", "sqlite")
    monkeypatch.delenv("GPT_REGISTER_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    applied = start.apply_db_env()
    assert applied["GPT_REGISTER_DB_BACKEND"] == "sqlite"


def test_apply_db_env_noop_without_file(monkeypatch, tmp_path):
    start = _load_start()
    monkeypatch.setattr(start, "ENV_DB_FILE", tmp_path / "missing.env.db")
    monkeypatch.delenv("GPT_REGISTER_DB_BACKEND", raising=False)
    monkeypatch.delenv("GPT_REGISTER_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    applied = start.apply_db_env()
    assert "GPT_REGISTER_DB_BACKEND" not in applied
    assert not os.environ.get("GPT_REGISTER_DB_BACKEND")
