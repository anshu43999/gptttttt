from __future__ import annotations
import base64

import datetime as _dt
import yaml
import select
import socket
import struct
import threading
import re
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunparse, urlunsplit, unquote


class ProxyBridgeFailure(OSError):
    pass


class LocalHttpToSocksBridge:
    def __init__(self, upstream_host: str, upstream_port: int, username: str, password: str):
        self.upstream_host = upstream_host
        self.upstream_port = int(upstream_port)
        self.username = username.encode("utf-8")
        self.password = password.encode("utf-8")
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(64)
        self._server.settimeout(1.0)
        self.port = int(self._server.getsockname()[1])
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self.last_error = ""

    @property
    def server_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
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
                client, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _read_http_header(self, client: socket.socket) -> bytes:
        data = bytearray()
        while b"\r\n\r\n" not in data and len(data) < 65536:
            chunk = client.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
        return bytes(data)

    def _read_exact(self, sock: socket.socket, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = sock.recv(size - len(data))
            if not chunk:
                raise OSError("socket closed")
            data.extend(chunk)
        return bytes(data)

    def _connect_upstream(self, host: str, port: int) -> socket.socket:
        upstream = socket.create_connection((self.upstream_host, self.upstream_port), timeout=30)
        upstream.settimeout(30)
        upstream.sendall(b"\x05\x01\x02")
        if self._read_exact(upstream, 2) != b"\x05\x02":
            raise ProxyBridgeFailure("SOCKS5 upstream authentication method rejected")
        upstream.sendall(b"\x01" + bytes([len(self.username)]) + self.username + bytes([len(self.password)]) + self.password)
        if self._read_exact(upstream, 2) != b"\x01\x00":
            raise ProxyBridgeFailure("SOCKS5 upstream credentials rejected")
        host_bytes = host.encode("idna")
        upstream.sendall(b"\x05\x01\x00\x03" + bytes([len(host_bytes)]) + host_bytes + struct.pack("!H", int(port)))
        response = self._read_exact(upstream, 4)
        if response[1] != 0:
            raise ProxyBridgeFailure("SOCKS5 upstream connection rejected")
        atyp = response[3]
        if atyp == 1:
            self._read_exact(upstream, 4)
        elif atyp == 3:
            self._read_exact(upstream, self._read_exact(upstream, 1)[0])
        elif atyp == 4:
            self._read_exact(upstream, 16)
        self._read_exact(upstream, 2)
        return upstream

    def _handle(self, client: socket.socket) -> None:
        upstream = None
        try:
            client.settimeout(30)
            header = self._read_http_header(client)
            if not header:
                return
            head, _, body = header.partition(b"\r\n\r\n")
            lines = head.split(b"\r\n")
            parts = lines[0].decode("latin1", errors="replace").split(" ", 2)
            if len(parts) != 3:
                return
            method, target, version = parts
            if method.upper() == "CONNECT":
                host, _, port_text = target.rpartition(":")
                upstream = self._connect_upstream(host, int(port_text or "443"))
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                self._relay(client, upstream)
                return

            parsed = urlsplit(target)
            host = parsed.hostname or ""
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            if not host:
                for line in lines[1:]:
                    if line.lower().startswith(b"host:"):
                        host_value = line.split(b":", 1)[1].strip().decode("latin1")
                        host, _, port_text = host_value.partition(":")
                        port = int(port_text or port)
                        break
            if not host:
                return
            upstream = self._connect_upstream(host, int(port))
            request_target = parsed.path or "/"
            if parsed.query:
                request_target += "?" + parsed.query
            request = f"{method} {request_target} {version}\r\n".encode("latin1") + b"\r\n".join(lines[1:]) + b"\r\n\r\n" + body
            upstream.sendall(request)
            self._relay(client, upstream)
        except ProxyBridgeFailure as exc:
            self.last_error = str(exc)
        except Exception as exc:
            self.last_error = type(exc).__name__
        finally:
            try:
                client.close()
            except Exception:
                pass
            if upstream:
                try:
                    upstream.close()
                except Exception:
                    pass

    def _relay(self, left: socket.socket, right: socket.socket) -> None:
        try:
            left.setblocking(False)
            right.setblocking(False)
            sockets = [left, right]
            while not self._closed.is_set():
                readable, _, _ = select.select(sockets, [], [], 0.5)
                if not readable:
                    continue
                for source in readable:
                    target = right if source is left else left
                    try:
                        data = source.recv(65536)
                    except BlockingIOError:
                        continue
                    if not data:
                        return
                    target.sendall(data)
        except Exception:
            pass
        finally:
            for sock in (left, right):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass



class LocalHttpToHttpBridge:
    def __init__(self, upstream_host: str, upstream_port: int, username: str, password: str):
        self.upstream_host = upstream_host
        self.upstream_port = int(upstream_port)
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        self.proxy_authorization = f"Proxy-Authorization: Basic {token}\r\n".encode("latin1")
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(64)
        self._server.settimeout(1.0)
        self.port = int(self._server.getsockname()[1])
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self.last_error = ""

    @property
    def server_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
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
                client, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _read_http_header(self, client: socket.socket) -> bytes:
        data = bytearray()
        while b"\r\n\r\n" not in data and len(data) < 65536:
            chunk = client.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
        return bytes(data)

    def _handle(self, client: socket.socket) -> None:
        upstream = None
        try:
            client.settimeout(30)
            header = self._read_http_header(client)
            if not header:
                return
            head, _, body = header.partition(b"\r\n\r\n")
            lines = head.split(b"\r\n")
            parts = lines[0].decode("latin1", errors="replace").split(" ", 2)
            if len(parts) != 3:
                return
            method, target, version = parts
            upstream = socket.create_connection((self.upstream_host, self.upstream_port), timeout=30)
            upstream.settimeout(30)
            filtered = [line for line in lines[1:] if not line.lower().startswith(b"proxy-authorization:")]
            request = f"{method} {target} {version}\r\n".encode("latin1") + self.proxy_authorization + b"\r\n".join(filtered) + b"\r\n\r\n" + body
            upstream.sendall(request)
            self._relay(client, upstream)
        except Exception as exc:
            self.last_error = type(exc).__name__
        finally:
            try:
                client.close()
            except Exception:
                pass
            if upstream:
                try:
                    upstream.close()
                except Exception:
                    pass

    def _relay(self, left: socket.socket, right: socket.socket) -> None:
        try:
            left.setblocking(False)
            right.setblocking(False)
            sockets = [left, right]
            while not self._closed.is_set():
                readable, _, _ = select.select(sockets, [], [], 0.5)
                if not readable:
                    continue
                for source in readable:
                    target = right if source is left else left
                    try:
                        data = source.recv(65536)
                    except BlockingIOError:
                        continue
                    if not data:
                        return
                    target.sendall(data)
        except Exception:
            pass
        finally:
            for sock in (left, right):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass



class CredentialProxyRuntime:
    def __init__(self, config: dict[str, Any], *, log_fn: Callable[[str], None] | None = None):
        self.config = config
        self.log_fn = log_fn or (lambda _msg: None)
        self._used_proxy_ips: set[str] = set()
        self._proxy_candidates: list[str] = []
        self._proxy_candidate_index = 0
        self._bridges: list[LocalHttpToSocksBridge] = []
        self._bridge_by_target: dict[str, LocalHttpToSocksBridge] = {}

    def log(self, message: str) -> None:
        self.log_fn(message)

    @staticmethod
    def country_from_proxy_zone(proxy: str) -> str:
        text = str(proxy or "").strip()
        if not text:
            return ""
        parsed = urlsplit(text if "://" in text else f"//{text}")
        username = unquote(parsed.username or "")
        for value in (username, unquote(text)):
            match = re.search(r"(?:^|[_-])(?:custom[_-])?(?:zone|region)[_-]([A-Za-z]{2})(?=[_-]|$)", value, flags=re.IGNORECASE)
            if match:
                return match.group(1).upper()
        return ""

    def expected_country_for(self, proxy: str) -> str:
        configured = str(self.config.get("lajiao_proxy_expected_country") or "").strip().upper()
        return configured or self.country_from_proxy_zone(proxy)

    def _config_int(self, key: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
        try:
            value = int(self.config.get(key, default) or default)
        except Exception:
            value = default
        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    def _use_credentials_mode(self) -> bool:
        mode = str(self.config.get("lajiao_proxy_mode") or "").strip().lower()
        if mode in {"credential", "credentials", "account", "auth"}:
            return True
        return not mode and bool(str(self.config.get("lajiao_proxy_credentials") or "").strip() or str(self.config.get("lajiao_proxy_credentials_file") or "").strip())

    def _credential_protocol_for(self, _value: str, protocol: str) -> str:
        protocol = str(protocol or "auto").strip().lower() or "auto"
        if protocol in {"auto", "socks5", "socks5h", "http", "https"}:
            return protocol
        return "auto"

    def _credential_protocols_for(self, value: str, protocol: str) -> list[str]:
        protocol = self._credential_protocol_for(value, protocol)
        if protocol == "auto":
            host = str(urlsplit(value if "://" in value else f"//{value}").hostname or "").lower()
            if host.endswith("kookeey.info") or host.endswith("kookeey.com") or "lajiao" in host:
                return ["socks5", "http"]
            return ["socks5", "http"]
        return [protocol]

    def credential_candidates(self) -> list[str]:
        rows: list[str] = []
        raw = self.config.get("lajiao_proxy_credentials") or ""
        if isinstance(raw, (list, tuple)):
            rows.extend(str(item or "") for item in raw)
        else:
            rows.extend(str(raw).replace("\r", "\n").split("\n"))
        file_path = str(self.config.get("lajiao_proxy_credentials_file") or "").strip()
        if file_path:
            path = Path(file_path)
            if not path.exists():
                raise RuntimeError(f"账号密码代理文件不存在: {file_path}")
            rows.extend(path.read_text(encoding="utf-8").splitlines())
        candidates: list[str] = []
        for row in rows:
            value = row.strip().strip('"').strip("'")
            if not value or value.startswith("#"):
                continue
            if "://" not in value:
                protocols = self._credential_protocols_for(value, str(self.config.get("lajiao_proxy_credential_protocol") or "auto"))
                values = [protocol + "://" + value for protocol in protocols]
            else:
                parsed = urlsplit(value)
                protocol = self._credential_protocol_for(value, parsed.scheme)
                values = [value] if protocol == "auto" or protocol == parsed.scheme else [urlunsplit(parsed._replace(scheme=protocol))]
            for candidate in values:
                if candidate not in candidates:
                    candidates.append(candidate)
        if not candidates:
            raise RuntimeError("代理账密模式未配置代理账号或文件")
        return candidates

    def _proxy_resource_key(self, value: str) -> str:
        value = str(value or "").strip()
        if "://" in value:
            parsed = urlsplit(value)
            if parsed.netloc:
                return parsed.netloc.strip()
        return value

    def _same_proxy_resource(self, left: str, right: str) -> bool:
        return self._proxy_resource_key(left) == self._proxy_resource_key(right)

    def _dashboard_task_id(self) -> str:
        return str(self.config.get("dashboard_task_id") or "").strip()

    def _task_config_path(self) -> Path:
        explicit = str(self.config.get("_task_config_path") or "").strip()
        if explicit:
            return Path(explicit)
        task_id = self._dashboard_task_id()
        return Path("data") / "tasks" / f"{task_id}_config.yaml"

    def _persist_resource_leases(self) -> None:
        task_id = self._dashboard_task_id()
        if not task_id:
            return
        path = self._task_config_path()
        if not path.exists():
            return
        try:
            current = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            current["resource_leases"] = self.config.get("resource_leases") or []
            path.write_text(yaml.safe_dump(current, allow_unicode=True, sort_keys=False), encoding="utf-8")
        except Exception as exc:
            self.log(f"  动态代理租约写回失败: {exc}")

    def _append_proxy_lease(self, provider: str, key: str) -> None:
        leases = self.config.get("resource_leases") if isinstance(self.config.get("resource_leases"), list) else []
        if not any(isinstance(item, dict) and item.get("type") == "proxy" and self._same_proxy_resource(str(item.get("key") or ""), key) for item in leases):
            leases.append({"type": "proxy", "provider": provider, "key": key})
        self.config["resource_leases"] = leases
        self._persist_resource_leases()

    def _remove_proxy_lease(self, key: str) -> None:
        leases = self.config.get("resource_leases") if isinstance(self.config.get("resource_leases"), list) else []
        self.config["resource_leases"] = [
            item for item in leases
            if not (isinstance(item, dict) and item.get("type") == "proxy" and self._same_proxy_resource(str(item.get("key") or ""), key))
        ]
        self._persist_resource_leases()

    def _resource_pool_db_path(self) -> str:
        return str(self.config.get("resource_pool_db_path") or self.config.get("_resource_pool_db_path") or "").strip()

    def _lease_next_pool_proxy(self) -> str:
        task_id = self._dashboard_task_id()
        if not task_id or not self._use_credentials_mode():
            return ""
        try:
            from infrastructure.repositories.resource_pool_repository import ResourcePoolRepository

            region = str(self.config.get("lajiao_proxy_regions") or self.config.get("lajiao_proxy_expected_country") or "")
            region_value = region.split(",")[0].strip() if region else ""
            lease = ResourcePoolRepository(self._resource_pool_db_path() or None).lease("proxy", "lajiao_credentials", task_id, region=region_value)
            if not lease.resource_key:
                return ""
            proxy = str(lease.payload.get("url") or lease.resource_key)
            self._append_proxy_lease(lease.provider, lease.resource_key)
            return proxy
        except Exception as exc:
            self.log(f"  动态租用代理失败: {exc}")
            return ""

    def _cooldown_probe_failed_proxy(self, proxy: str) -> None:
        task_id = self._dashboard_task_id()
        key = self._proxy_resource_key(proxy)
        if not task_id or not key:
            return
        try:
            from infrastructure.repositories.resource_pool_repository import ResourcePoolRepository

            repo = ResourcePoolRepository(self._resource_pool_db_path() or None)
            cooldown_until = (_dt.datetime.now() + _dt.timedelta(seconds=1800)).replace(microsecond=0).isoformat()
            repo.report(task_id, key, success=False, cooldown_until=cooldown_until, error="proxy probe failed before registration")
            self._remove_proxy_lease(key)
        except Exception as exc:
            self.log(f"  代理探针失败冷却写回失败: {exc}")

    def fetch_api_candidates(self) -> list[str]:
        import requests

        api_url = str(self.config.get("lajiao_proxy_api_url") or "http://api.lajiaohttp.com/api/extract_ip")
        parsed_url = urlparse(api_url)
        params = None
        if parsed_url.query:
            query = dict(parse_qsl(parsed_url.query, keep_blank_values=True))
            region = str(self.config.get("lajiao_proxy_regions") or "JP").strip() or "JP"
            if query.get("regions") != region:
                query["regions"] = region
                api_url = urlunparse(parsed_url._replace(query=urlencode(query)))
        else:
            params = {
                "regions": str(self.config.get("lajiao_proxy_regions") or "JP"),
                "num": str(int(self.config.get("lajiao_proxy_num", 3) or 3)),
                "protocol": str(self.config.get("lajiao_proxy_protocol") or "socks5"),
                "type": str(self.config.get("lajiao_proxy_type") or "json"),
                "cate": str(self.config.get("lajiao_proxy_cate") or "2"),
                "t": str(self.config.get("lajiao_proxy_t") or "10"),
                "lb": str(self.config.get("lajiao_proxy_lb") or "1"),
            }
        session = requests.Session()
        session.trust_env = False
        response = session.get(api_url, params=params, timeout=30)
        response.raise_for_status()
        text = (response.text or "").strip()
        candidates: list[str] = []
        content_type = str(response.headers.get("content-type") or "").lower()
        wants_json = (params or {}).get("type") == "json" or "application/json" in content_type or text.startswith("{")
        if wants_json:
            data = response.json()
            if not data.get("success", data.get("code") == 0):
                raise RuntimeError(f"代理 API 提取失败: {data}")
            for item in data.get("data") or []:
                if isinstance(item, str):
                    proxy = item.strip()
                elif isinstance(item, dict):
                    proxy = f"{item.get('ip')}:{item.get('port')}"
                else:
                    proxy = ""
                if proxy and "None" not in proxy and proxy not in candidates:
                    candidates.append(proxy)
        else:
            for line in text.replace("\r", "\n").split("\n"):
                proxy = line.strip()
                if proxy and ":" in proxy and "None" not in proxy and proxy not in candidates:
                    candidates.append(proxy)
        if not candidates:
            raise RuntimeError(f"代理 API 提取失败: 无候选: {text[:200]}")
        return candidates

    @staticmethod
    def _credential_protocol_for_host(proxy: str, configured_protocol: str = "") -> str:
        value = str(proxy or "").strip().lower()
        parts = urlsplit(value if "://" in value else f"//{value}")
        host = str(parts.hostname or "").lower()
        if host.endswith("kookeey.info") or host.endswith("kookeey.com"):
            return "socks5"
        protocol = str(configured_protocol or "auto").strip().lower()
        if protocol and protocol != "auto":
            return protocol
        return "socks5"

    def check_url(self, proxy: str) -> str:
        value = str(proxy or "").strip()
        if not value:
            return ""
        if "://" not in value:
            if self._use_credentials_mode():
                protocol = self._credential_protocol_for_host(value, str(self.config.get("lajiao_proxy_credential_protocol") or ""))
                return protocol + "://" + value
            return "socks5h://" + value
        if value.startswith("socks5://"):
            return "socks5h://" + value[len("socks5://"):]
        return value

    def runtime_url(self, proxy: str) -> str:
        value = str(proxy or "").strip()
        if not value:
            return ""
        if "://" not in value:
            if self._use_credentials_mode():
                protocol = self._credential_protocol_for_host(value, str(self.config.get("lajiao_proxy_credential_protocol") or ""))
                if protocol in {"http", "https"}:
                    return protocol + "://" + value
                return "socks5h://" + value
            return "socks5://" + value
        return value

    def _check_proxy_via_local_bridge(self, proxy: str) -> tuple[str, Callable[[], None]]:
        check_proxy_url = self.check_url(proxy)
        parts = urlsplit(check_proxy_url)
        scheme = (parts.scheme or "").lower()
        if scheme in {"http", "https"} and parts.hostname and parts.port and "@" in check_proxy_url:
            bridge = LocalHttpToHttpBridge(parts.hostname, int(parts.port), unquote(parts.username or ""), unquote(parts.password or ""))
            bridge.start()
            return bridge.server_url, bridge.close
        runtime_proxy_url = self.runtime_url(proxy)
        parts = urlsplit(runtime_proxy_url)
        scheme = (parts.scheme or "").lower()
        if scheme in {"socks5", "socks5h"} and parts.hostname and parts.port and "@" in runtime_proxy_url:
            bridge = LocalHttpToSocksBridge(parts.hostname, int(parts.port), unquote(parts.username or ""), unquote(parts.password or ""))
            bridge.start()
            return bridge.server_url, bridge.close
        return self.check_url(proxy), lambda: None

    def _has_openai_proxy_risk(self, url: str, status_code: int, text: str) -> str:
        lower_url = str(url or "").lower()
        lower_text = str(text or "").lower()
        if status_code in {403, 407, 429}:
            return f"status={status_code}"
        markers = (
            "cf_chl_",
            "cloudflare",
            "turnstile",
            "captcha",
            "verify you are human",
            "security check",
            "access denied",
            "too many requests",
            "rate limit",
            "suspicious",
            "unusual activity",
        )
        for marker in markers:
            if marker in lower_url or marker in lower_text:
                return marker
        return ""

    def _check_openai_surface_with_requests(self, proxy: str, proxies: dict[str, str], timeout: int) -> bool:
        import requests

        checks = (
            ("https://chatgpt.com/", None, False),
            ("https://chatgpt.com/api/auth/csrf", {"accept": "application/json", "referer": "https://chatgpt.com/"}, True),
            ("https://auth.openai.com/", None, False),
            ("https://auth.openai.com/api/accounts/authorize?client_id=probe&redirect_uri=https%3A%2F%2Fchatgpt.com%2Fapi%2Fauth%2Fcallback%2Fopenai&response_type=code&scope=openid%20email%20profile&state=proxy-probe&screen_hint=signup&prompt=login", {"accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "referer": "https://chatgpt.com/"}, False),
        )
        for url, headers, require_csrf in checks:
            try:
                response = requests.get(url, proxies=proxies, timeout=timeout, allow_redirects=True, headers=headers)
            except Exception as exc:
                self.log(f"  代理 OpenAI 预检失败 {proxy}: {url} -> {exc}")
                return False
            risk = self._has_openai_proxy_risk(str(getattr(response, "url", url) or url), int(getattr(response, "status_code", 0) or 0), str(getattr(response, "text", "") or "")[:20000])
            if risk:
                self.log(f"  代理 OpenAI 预检风控，跳过 {proxy}: {url} -> {risk}")
                return False
            if require_csrf and "csrfToken" not in str(getattr(response, "text", "") or ""):
                self.log(f"  代理 OpenAI 预检失败 {proxy}: csrf body missing token")
                return False
        return True
    def check(self, proxy: str) -> tuple[bool, str]:
        if self._use_credentials_mode():
            import requests

            timeout = self._config_int("lajiao_proxy_timeout", 15, minimum=1)
            proxy_url, cleanup_probe_bridge = self._check_proxy_via_local_bridge(proxy)
            proxies = {"http": proxy_url, "https": proxy_url}
            exit_ip = ""
            ip_errors: list[str] = []
            try:
                for check_url in (
                    "https://api.ipify.org?format=json",
                    "https://ipinfo.io/ip",
                    "https://icanhazip.com",
                    "https://ifconfig.me/ip",
                    "http://api.ipify.org?format=json",
                ):
                    try:
                        response = requests.get(check_url, proxies=proxies, timeout=timeout)
                        if response.status_code != 200:
                            ip_errors.append(f"{check_url} -> {response.status_code}")
                            continue
                        try:
                            exit_ip = str(response.json().get("ip") or "")
                        except Exception:
                            exit_ip = (response.text or "").strip().split()[0][:80]
                        if exit_ip:
                            break
                    except Exception as exc:
                        ip_errors.append(f"{check_url} -> {exc}")
                expected_country = self.expected_country_for(proxy)
                region_hint = self.country_from_proxy_zone(proxy)
                if not exit_ip:
                    self.log(f"  代理不可用 {proxy}: IP 检测失败，无法校验国家; {'; '.join(ip_errors[:2])}")
                    return False, exit_ip
                if expected_country and exit_ip:
                    try:
                        geo_response = requests.get(f"https://ipinfo.io/{exit_ip}/json", proxies=proxies, timeout=timeout)
                        if geo_response.status_code != 200:
                            self.log(f"  代理国家校验失败 {proxy}: ipinfo -> {geo_response.status_code}")
                            return False, exit_ip
                        actual_country = str((geo_response.json() or {}).get("country") or "").strip().upper()
                        if actual_country != expected_country:
                            self.log(f"  代理国家不匹配，跳过: exit_ip={exit_ip} actual={actual_country or '?'} expected={expected_country}")
                            return False, exit_ip
                    except Exception as exc:
                        if region_hint == expected_country:
                            self.log(f"  代理国家接口无响应，按账号 region 标记放行: exit_ip={exit_ip} region={region_hint} error={exc}")
                        else:
                            self.log(f"  代理国家校验失败 {proxy}: {exc}")
                            return False, exit_ip
                if exit_ip and exit_ip in self._used_proxy_ips:
                    self.log(f"  代理出口 IP 已用过，跳过: {exit_ip}")
                    return False, exit_ip
                if not self._check_openai_surface_with_requests(proxy, proxies, timeout):
                    return False, exit_ip
                return True, exit_ip
            finally:
                cleanup_probe_bridge()

        from curl_cffi import requests as curl_requests

        timeout = self._config_int("lajiao_proxy_timeout", 15, minimum=1)
        bridge_url, close_bridge = self._check_proxy_via_local_bridge(proxy)
        proxy_url = bridge_url
        exit_ip = ""
        try:
            for url in (
                "https://api.ipify.org?format=json",
                "https://chatgpt.com/auth/login",
                "https://chatgpt.com/api/auth/csrf",
                "https://auth.openai.com/",
            ):
                try:
                    headers = {"accept": "application/json", "referer": "https://chatgpt.com/"} if "api/auth/csrf" in url else None
                    response = curl_requests.get(url, proxy=proxy_url, timeout=timeout, impersonate="chrome", allow_redirects=False, headers=headers)
                except Exception as exc:
                    self.log(f"  账号密码代理不可用 {proxy}: {url} -> {exc}")
                    return False, exit_ip
                if response.status_code != 200:
                    self.log(f"  账号密码代理不可用 {proxy}: {url} -> {response.status_code}")
                    return False, exit_ip
                if "api/auth/csrf" in url and "csrfToken" not in (response.text or ""):
                    self.log(f"  账号密码代理不可用 {proxy}: csrf body missing token")
                    return False, exit_ip
                if "api.ipify.org" in url:
                    try:
                        exit_ip = str(response.json().get("ip") or "")
                    except Exception:
                        exit_ip = (response.text or "").strip()[:80]
        finally:
            close_bridge()
        return True, exit_ip

    def select(self) -> tuple[str, str]:
        if not self.config.get("rotate_proxy_each_attempt"):
            proxy_url = str(self.config.get("proxy") or "")
            return proxy_url, ""
        credentials_mode = self._use_credentials_mode()
        dynamic_pool = credentials_mode and bool(self._dashboard_task_id())
        max_batches = self._config_int("lajiao_proxy_max_candidates", 24, minimum=1) if dynamic_pool else (1 if credentials_mode else self._config_int("lajiao_proxy_max_batches", 3, minimum=1))
        max_candidates = self._config_int("lajiao_proxy_max_candidates", 24, minimum=1)
        deadline_seconds = self._config_int("lajiao_proxy_select_deadline", 180, minimum=15)
        deadline = time.time() + deadline_seconds
        batches = 0
        checked = 0
        while time.time() < deadline and checked < max_candidates:
            if self._proxy_candidate_index >= len(self._proxy_candidates):
                if batches >= max_batches:
                    break
                if dynamic_pool:
                    leased_proxy = self._lease_next_pool_proxy()
                    self._proxy_candidates = [leased_proxy] if leased_proxy else []
                else:
                    self._proxy_candidates = self.credential_candidates() if credentials_mode else self.fetch_api_candidates()
                self._proxy_candidate_index = 0
                batches += 1
                mode_label = "账号密码动态租约" if dynamic_pool else ("账号密码" if credentials_mode else "API")
                self.log(f"  已加载代理{mode_label}候选: {len(self._proxy_candidates)} 个 batch={batches}/{max_batches}")
                if not self._proxy_candidates:
                    break
            proxy = self._proxy_candidates[self._proxy_candidate_index]
            self._proxy_candidate_index += 1
            checked += 1
            if self.config.get("lajiao_proxy_skip_check"):
                proxy_url = self.runtime_url(proxy)
                self.config["proxy"] = proxy_url
                self.config["_camoufox_geoip_ip"] = ""
                self.log(f"  使用新代理: {proxy_url} exit_ip=skip_check")
                return proxy_url, ""
            ok, exit_ip = self.check(proxy)
            if not ok:
                self._cooldown_probe_failed_proxy(proxy)
                continue
            if exit_ip:
                self._used_proxy_ips.add(exit_ip)
            proxy_url = self.runtime_url(proxy)
            self.config["proxy"] = proxy_url
            self.config["_camoufox_geoip_ip"] = exit_ip
            self.log(f"  使用新代理: {proxy_url} exit_ip={exit_ip}")
            return proxy_url, exit_ip
        raise RuntimeError(f"代理池耗尽或超时: checked={checked}, batches={batches}, deadline={deadline_seconds}s")

    def start_browser_bridge(self, runtime_proxy_url: str) -> str:
        value = str(runtime_proxy_url or "").strip()
        if value and "://" not in value:
            protocol = str(self.config.get("lajiao_proxy_credential_protocol") or "socks5").strip().lower()
            if protocol not in {"http", "https", "socks5", "socks5h"}:
                protocol = "socks5"
            value = f"{protocol}://{value}"
        parts = urlsplit(value)
        scheme = (parts.scheme or "").lower()
        if scheme not in {"http", "https", "socks5", "socks5h"} or not parts.hostname or not parts.port:
            return value
        username = unquote(parts.username or "")
        password = unquote(parts.password or "")
        if not username and not password:
            return value
        existing = self._bridge_by_target.get(value)
        if existing:
            return existing.server_url
        if scheme in {"http", "https"}:
            bridge = LocalHttpToHttpBridge(parts.hostname, int(parts.port), username, password)
            bridge_kind = "HTTP 认证"
        else:
            bridge = LocalHttpToSocksBridge(parts.hostname, int(parts.port), username, password)
            bridge_kind = "SOCKS5 认证"
        bridge.start()
        self._bridges.append(bridge)
        self._bridge_by_target[value] = bridge
        self.log(f"  已启动本地 {bridge_kind}桥: {bridge.server_url} -> {parts.hostname}:{parts.port}")
        return bridge.server_url

    def browser_bridge_error(self, runtime_proxy_url: str) -> str:
        bridge = self._bridge_by_target.get(str(runtime_proxy_url or "").strip())
        return str(getattr(bridge, "last_error", "") or "").strip()

    def cleanup(self) -> None:
        for bridge in list(self._bridges):
            try:
                bridge.close()
            except Exception:
                pass
        self._bridges.clear()
        self._bridge_by_target.clear()
