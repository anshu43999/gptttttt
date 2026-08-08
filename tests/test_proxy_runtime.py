from __future__ import annotations

import socket
import struct
import threading
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.proxy.credential_runtime import CredentialProxyRuntime, LocalHttpToHttpBridge, LocalHttpToSocksBridge


def test_credential_proxy_url_policy_and_bridge_start() -> None:
    runtime = CredentialProxyRuntime({"lajiao_proxy_mode": "credentials", "lajiao_proxy_credential_protocol": "http"})
    proxy = "http://user:pass@la.residential.rayobyte.com:8000"
    assert runtime.check_url(proxy) == proxy
    assert runtime.runtime_url(proxy) == proxy
    bridge_url = runtime.start_browser_bridge(proxy)
    try:
        assert bridge_url.startswith("http://127.0.0.1:")
    finally:
        runtime.cleanup()


def test_credential_proxy_bridge_wraps_bare_credentials() -> None:
    runtime = CredentialProxyRuntime({"lajiao_proxy_mode": "credentials", "lajiao_proxy_credential_protocol": "socks5"})
    bridge_url = runtime.start_browser_bridge("user:pass@127.0.0.1:9")
    try:
        assert bridge_url.startswith("http://127.0.0.1:")
    finally:
        runtime.cleanup()

def test_credential_proxy_expected_country_honors_manual_country_before_credentials_tags() -> None:
    runtime = CredentialProxyRuntime({"lajiao_proxy_expected_country": "US"})

    for proxy in (
        "http://customer-custom_zone_TR:secret@proxy.example:8000",
        "http://customer-region-JP:secret@proxy.example:8000",
    ):
        assert runtime.expected_country_for(proxy) == "US"


def test_credential_proxy_expected_country_derives_credentials_tags_without_manual_country() -> None:
    runtime = CredentialProxyRuntime({})

    for proxy, expected_country in (
        ("http://customer-custom_zone_TR:secret@proxy.example:8000", "TR"),
        ("http://customer-region-JP:secret@proxy.example:8000", "JP"),
    ):
        assert runtime.expected_country_for(proxy) == expected_country


class FakeSocks5Server:
    def __init__(self):
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(1)
        self.port = int(self._server.getsockname()[1])
        self.ready = threading.Event()
        self.done = threading.Event()
        self.username = ""
        self.password = ""
        self.target_host = ""
        self.target_port = 0
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> None:
        self._thread.start()
        self.ready.wait(2)

    def close(self) -> None:
        try:
            self._server.close()
        except Exception:
            pass

    def _read_exact(self, sock: socket.socket, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = sock.recv(size - len(data))
            if not chunk:
                raise OSError("socket closed")
            data.extend(chunk)
        return bytes(data)

    def _serve(self) -> None:
        self.ready.set()
        conn, _ = self._server.accept()
        with conn:
            greeting = self._read_exact(conn, 3)
            assert greeting == b"\x05\x01\x02"
            conn.sendall(b"\x05\x02")
            version = self._read_exact(conn, 1)
            assert version == b"\x01"
            user_len = self._read_exact(conn, 1)[0]
            self.username = self._read_exact(conn, user_len).decode("utf-8")
            pass_len = self._read_exact(conn, 1)[0]
            self.password = self._read_exact(conn, pass_len).decode("utf-8")
            conn.sendall(b"\x01\x00")
            header = self._read_exact(conn, 4)
            assert header[:3] == b"\x05\x01\x00"
            atyp = header[3]
            if atyp == 3:
                host_len = self._read_exact(conn, 1)[0]
                self.target_host = self._read_exact(conn, host_len).decode("idna")
            elif atyp == 1:
                self.target_host = socket.inet_ntoa(self._read_exact(conn, 4))
            else:
                raise AssertionError(f"unexpected atyp {atyp}")
            self.target_port = struct.unpack("!H", self._read_exact(conn, 2))[0]
            conn.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            self.done.set()
            conn.recv(1)


class RejectingSocks5Server(FakeSocks5Server):
    def __init__(self):
        super().__init__()
        self.auth_rejected = threading.Event()

    def _serve(self) -> None:
        self.ready.set()
        conn, _ = self._server.accept()
        with conn:
            if self._read_exact(conn, 3) != b"\x05\x01\x02":
                return
            conn.sendall(b"\x05\x02")
            if self._read_exact(conn, 1) != b"\x01":
                return
            self._read_exact(conn, self._read_exact(conn, 1)[0])
            self._read_exact(conn, self._read_exact(conn, 1)[0])
            conn.sendall(b"\x01\x01")
            self.auth_rejected.set()


def test_local_http_to_socks_bridge_performs_socks5_auth_and_connect() -> None:
    upstream = FakeSocks5Server()
    upstream.start()
    bridge = LocalHttpToSocksBridge("127.0.0.1", upstream.port, "user1", "pass1")
    bridge.start()
    try:
        with socket.create_connection(("127.0.0.1", bridge.port), timeout=3) as client:
            client.sendall(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
            response = client.recv(128)
        assert b"200 Connection Established" in response
        assert upstream.done.wait(2)
        assert upstream.username == "user1"
        assert upstream.password == "pass1"
        assert upstream.target_host == "example.com"
        assert upstream.target_port == 443
    finally:
        bridge.close()
        upstream.close()


def test_credential_proxy_runtime_reports_redacted_socks_auth_rejection() -> None:
    upstream = RejectingSocks5Server()
    upstream.start()
    runtime = CredentialProxyRuntime({})
    candidate = f"socks5://bridge-user:bridge-password@127.0.0.1:{upstream.port}"
    bridge_url = runtime.start_browser_bridge(candidate)
    bridge_port = int(bridge_url.rsplit(":", 1)[1])
    try:
        with socket.create_connection(("127.0.0.1", bridge_port), timeout=3) as client:
            client.sendall(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
            client.recv(128)

        assert upstream.auth_rejected.wait(2)
        error = runtime.browser_bridge_error(candidate)
        assert "SOCKS5 upstream credentials rejected" in error
        assert "bridge-user" not in error
        assert "bridge-password" not in error
    finally:
        runtime.cleanup()
        upstream.close()


class FakeHttpProxyServer:
    def __init__(self):
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(1)
        self.port = int(self._server.getsockname()[1])
        self.ready = threading.Event()
        self.done = threading.Event()
        self.request = b""
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> None:
        self._thread.start()
        self.ready.wait(2)

    def close(self) -> None:
        try:
            self._server.close()
        except Exception:
            pass

    def _serve(self) -> None:
        self.ready.set()
        conn, _ = self._server.accept()
        with conn:
            data = bytearray()
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data.extend(chunk)
            self.request = bytes(data)
            conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self.done.set()


def test_local_http_to_http_bridge_adds_proxy_authorization() -> None:
    upstream = FakeHttpProxyServer()
    upstream.start()
    bridge = LocalHttpToHttpBridge("127.0.0.1", upstream.port, "user1", "pass1")
    bridge.start()
    try:
        with socket.create_connection(("127.0.0.1", bridge.port), timeout=3) as client:
            client.sendall(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
            response = client.recv(128)
        assert b"200 Connection Established" in response
        assert upstream.done.wait(2)
        assert b"CONNECT example.com:443 HTTP/1.1" in upstream.request
        assert b"Proxy-Authorization: Basic dXNlcjE6cGFzczE=" in upstream.request
    finally:
        bridge.close()
        upstream.close()


def test_credential_proxy_check_rejects_openai_cloudflare(monkeypatch) -> None:
    calls = []

    class FakeResponse:
        def __init__(self, url: str, status_code: int, text: str = ""):
            self.url = url
            self.status_code = status_code
            self.text = text

        def json(self):
            return {"ip": "1.2.3.4"}

    def fake_get(url, **kwargs):
        calls.append(url)
        if "api.ipify.org" in url:
            return FakeResponse(url, 200, '{"ip":"1.2.3.4"}')
        return FakeResponse("https://chatgpt.com/?__cf_chl_rt_tk=blocked", 403, "Cloudflare")

    logs = []
    monkeypatch.setattr("requests.get", fake_get)

    runtime = CredentialProxyRuntime({"lajiao_proxy_mode": "credentials", "lajiao_proxy_credential_protocol": "http"}, log_fn=logs.append)

    ok, exit_ip = runtime.check("http://proxy.local:8080")

    assert ok is False
    assert exit_ip == "1.2.3.4"
    assert any("OpenAI 预检风控" in item for item in logs)
    assert "https://chatgpt.com/" in calls
