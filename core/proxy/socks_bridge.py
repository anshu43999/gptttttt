"""
SOCKS5 认证本地桥 — 从 full_pipeline.py _LocalSocks5Bridge 完整迁移。

解决 Camoufox/Playwright 不支持带用户名密码认证的 SOCKS5 代理。
启动本地 SOCKS5 服务 (127.0.0.1:随机端口), 无认证,
上游用提供的用户名密码进行 SOCKS5 认证握手 + CONNECT 命令。

RFC 1928 SOCKS Protocol Version 5
"""
from __future__ import annotations

import select
import socket
import struct
import threading
from urllib.parse import urlparse


class LocalSocksBridge:
    def __init__(self, upstream_host: str, upstream_port: int,
                 username: str = "", password: str = ""):
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.username = username.encode() if username else b""
        self.password = password.encode() if password else b""
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(64)
        self._server.settimeout(1.0)
        self.port: int = self._server.getsockname()[1]
        self.local_url: str = f"socks5://127.0.0.1:{self.port}"
        self._closed = threading.Event()

    @classmethod
    def from_proxy_url(cls, proxy_url: str) -> "LocalSocksBridge":
        parsed = urlparse(proxy_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 1080
        username = parsed.username or ""
        password = parsed.password or ""
        return cls(host, port, username, password)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._closed.set()
        try:
            self._server.close()
        except Exception:
            pass

    def _serve(self) -> None:
        while not self._closed.is_set():
            try:
                readable, _, _ = select.select([self._server], [], [], 1.0)
                if readable:
                    client_sock, _ = self._server.accept()
                    threading.Thread(target=self._handle, args=(client_sock,),
                                     daemon=True).start()
            except (socket.timeout, OSError):
                continue
            except Exception:
                break

    def _handle(self, client: socket.socket) -> None:
        upstream: socket.socket | None = None
        try:
            client.settimeout(30)
            # 1. read greeting: VER=5, NMETHODS
            greeting = client.recv(2)
            if len(greeting) < 2: return
            ver, nmethods = greeting[0], greeting[1]
            methods = client.recv(nmethods)
            # 2. reply: VER=5, METHOD=0 (no auth)
            client.sendall(b"\x05\x00")

            # 3. read request: VER, CMD, RSV, ATYP, DST
            header = client.recv(4)
            if len(header) < 4: return
            ver, cmd, _, atyp = header[0], header[1], header[2], header[3]
            if ver != 5 or cmd != 1:  # only CONNECT
                client.sendall(b"\x05\x07\x00\x01" + socket.inet_aton("0.0.0.0") + b"\x00\x00")
                return

            # 4. parse destination
            if atyp == 1:    # IPv4
                dst = client.recv(4)
                host = socket.inet_ntoa(dst)
                port_raw = client.recv(2)
                port = struct.unpack(">H", port_raw)[0]
            elif atyp == 3:  # domain name
                name_len = client.recv(1)[0]
                host = client.recv(name_len).decode()
                port_raw = client.recv(2)
                port = struct.unpack(">H", port_raw)[0]
            else:
                client.sendall(b"\x05\x08\x00\x01" + socket.inet_aton("0.0.0.0") + b"\x00\x00")
                return

            # 5. connect upstream
            upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            upstream.settimeout(30)
            upstream.connect((self.upstream_host, self.upstream_port))

            # 6. SOCKS5 auth handshake with upstream
            if self.username and self.password:
                upstream.sendall(b"\x05\x01\x02")  # VER=5, 1 method, user/pass
                up_resp = upstream.recv(2)
                if up_resp != b"\x05\x02":
                    client.sendall(b"\x05\x01\x00\x01" + socket.inet_aton("0.0.0.0") + b"\x00\x00")
                    return
                # user/pass sub-negotiation
                user_bytes = self.username
                pass_bytes = self.password
                up_auth = b"\x01" + bytes([len(user_bytes)]) + user_bytes + bytes([len(pass_bytes)]) + pass_bytes
                upstream.sendall(up_auth)
                up_auth_resp = upstream.recv(2)
                if up_auth_resp != b"\x01\x00":
                    client.sendall(b"\x05\x01\x00\x01" + socket.inet_aton("0.0.0.0") + b"\x00\x00")
                    return
            else:
                upstream.sendall(b"\x05\x01\x00")  # VER=5, 1 method, no auth
                up_resp = upstream.recv(2)
                if up_resp != b"\x05\x00":
                    client.sendall(b"\x05\x01\x00\x01" + socket.inet_aton("0.0.0.0") + b"\x00\x00")
                    return

            # 7. CONNECT upstream
            if isinstance(host, str):
                upstream.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host.encode() + struct.pack(">H", port))
            else:
                upstream.sendall(b"\x05\x01\x00\x01" + socket.inet_aton(host) + struct.pack(">H", port))
            up_conn_resp = upstream.recv(10)
            if len(up_conn_resp) < 10 or up_conn_resp[1] != 0:
                client.sendall(b"\x05\x04\x00\x01" + socket.inet_aton("0.0.0.0") + b"\x00\x00")
                return

            # 8. reply success to client
            client.sendall(b"\x05\x00\x00\x01" + socket.inet_aton("0.0.0.0") + b"\x00\x00")

            # 9. bidirectional relay
            self._relay(client, upstream)

        except Exception:
            pass
        finally:
            try: client.close()
            except Exception: pass
            if upstream:
                try: upstream.close()
                except Exception: pass

    @staticmethod
    def _relay(a: socket.socket, b: socket.socket) -> None:
        def _copy(src, dst, stop):
            try:
                while not stop.is_set():
                    readable, _, _ = select.select([src], [], [], 1.0)
                    if readable:
                        data = src.recv(8192)
                        if not data: break
                        dst.sendall(data)
                    if stop.is_set(): break
            except Exception:
                pass

        stop = threading.Event()
        t1 = threading.Thread(target=_copy, args=(a, b, stop), daemon=True)
        t2 = threading.Thread(target=_copy, args=(b, a, stop), daemon=True)
        t1.start(); t2.start()
        t1.join(timeout=300); t2.join(timeout=300)
        stop.set()
