"""
GPT Register — ChatGPT 手机号注册 + Plus 激活全链路编排。

流程:
  1. HeroSMS 获取手机号
  2. Camoufox 浏览器自动化注册 (phone-first)
  3. 获取 accessToken
  4. [手动] iceaix CDK 激活 Plus
  5. OAuth PKCE 绑定 Outlook 邮箱 → 获取 refresh_token
  6. 格式化输出 + 上传 Sub2API

用法:
  python full_pipeline.py --config config.yaml
  python full_pipeline.py --config config.yaml --headed  # 显示浏览器
  python full_pipeline.py --config config.yaml --step register  # 只跑注册
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import uuid
import socket
import struct
import threading
import select
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

try:
    from platforms.chatgpt.iceaix_client import configure_utf8_stdio
    configure_utf8_stdio()
except Exception:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 添加项目根目录到路径
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
from core.base_sms import extract_verification_code
from core.sms.activation_pool import LocalActivationPool


def _patch_playwright_firefox_pageerror_dispatcher() -> None:
    """Work around Playwright Firefox driver crash on page errors without location."""
    try:
        import playwright  # type: ignore
        driver_root = Path(playwright.__file__).resolve().parent / "driver" / "package" / "lib" / "coreBundle.js"
        text = driver_root.read_text(encoding="utf-8")
        broken = """location: {\n              url: pageError.location.url,\n              line: pageError.location.lineNumber,\n              column: pageError.location.columnNumber\n            }"""
        fixed = """location: {\n              url: pageError.location ? pageError.location.url : \"\",\n              line: pageError.location ? pageError.location.lineNumber : 0,\n              column: pageError.location ? pageError.location.columnNumber : 0\n            }"""
        if broken in text:
            driver_root.write_text(text.replace(broken, fixed), encoding="utf-8")
    except Exception:
        pass


_patch_playwright_firefox_pageerror_dispatcher()

import yaml
from core import account_store
from core.browser.session import BrowserSession


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    # === 接码服务 ===
    "sms_provider": "herosms_api",
    "sms_api_key": "",
    "sms_country": "187",       # HeroSMS 国家代码；只控制接码国家，不控制代理 IP
    "sms_service": "dr",         # dr=ChatGPT (HeroSMS 服务代码)
    
    # === 代理 ===
    "proxy": "",                 # ChatGPT 注册用代理；自动轮换时由代码写入
    "proxy_jp": "",              # 日本代理 (试用检测/激活用)
    "lajiao_proxy_regions": "JP", # 默认使用日本出口 IP
    
    # === ChatGPT 注册 ===
    "country_code": "1",         # 注册手机号国家拨号码
    "country_name": "United States",
    "use_camoufox": False,       # 兼容旧字段；新注册优先 browser_engine
    "headed": False,             # 是否显示浏览器窗口
    "browser_mode": "protocol",  # protocol | headless | headed
    "browser_engine": "patchright",      # patchright | playwright | camoufox
    "browser_channel": "chrome",         # chrome | chromium
    "browser_profile_mode": "per_task",  # per_task | none
    "browser_no_viewport": True,
    "email_register_flow": "fast",       # fast | legacy
    "locale": "ja-JP",
    "timezone_id": "Asia/Tokyo",
    "accept_language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    "sms_code_timeout": 600,     # 等待短信验证码秒数；OpenAI 密码提交后可能已占号，必须等晚到码
    "rotate_proxy_each_attempt": False,
    "lajiao_proxy_api_url": "http://api.lajiaohttp.com/api/extract_ip",
    "lajiao_proxy_num": 3,
    "lajiao_proxy_timeout": 15,
    "lajiao_proxy_mode": "api",     # api | credentials
    "lajiao_proxy_credentials": "",  # 每行一个 user:pass@host:port 或 socks5://user:pass@host:port
    "lajiao_proxy_credentials_file": "",
    "lajiao_proxy_expected_country": "",  # 为空不校验；如 IN/JP 则强制出口国家匹配
    "camoufox_geoip": True,       # 用代理出口 IP 自动匹配时区/语言/地理指纹
    "camoufox_humanize": True,    # 鼠标轨迹 humanize，避免过快自动化特征
    "camoufox_enable_cache": False,
    
    # === PPXY / iceaix ===
    "iceaix_api_key": "",        # PPXY API Key (api_xxx) — 为空则走手动 Plus 模式
    "iceaix_base_url": "https://plus.iceaix.com",
    "paypal_phone": "",          # PayPal 日区手机号 (固定)
    "iceaix_sms_api": "",        # PayPal 接码 API；自动 Plus 必填
    "iceaix_job_timeout": 300,
    "iceaix_poll_interval": 3,
    "iceaix_otp_timeout": 60,
    "iceaix_pplink_retry": 20,
    "iceaix_job_create_attempts": 3,
    "iceaix_proxy": "",          # iceaix US 代理；默认空=服务端默认
    "iceaix_proxy_jp": "",       # iceaix JP 代理；默认空=服务端默认
    "iceaix_allow_paid_no_trial": False,
    "plus_verify_retries": 6,
    "plus_verify_interval": 10,
    
    # === OAuth 绑定 ===
    "outlook_email": "",         # 要绑定的 Outlook 邮箱
    "outlook_token_order_file": "",  # Outlook Graph 令牌文件: email----password----client_id----refresh_token
    "outlook_web_otp": True,      # Graph 不可用且配置密码时，使用 Outlook Web 读取验证码
    "outlook_password": "",      # Outlook 密码 (如果用不到可为空)
    "oauth_client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
    "save_token_pool": False,    # register-token 辅助索引；默认关闭，避免额外明文 token 文件
    "oauth_redirect_uri": "http://localhost:1455/auth/callback",
    "oauth_callback_mode": "cpa", # local | cpa；cpa 模式只把 callback 提交给 CPA，不在本地换 refresh_token
    "cpa_base_url": "",
    "cpa_management_key": "",
    "outlook_failed_retryable_limit": 2,  # 同一 Outlook 邮箱 retryable 失败达到次数后进入 cooldown
    "outlook_cooldown_hours": 24,
    # === 邮箱绑定 Provider ===
    "mailbox_provider": "icloud_api",  # icloud_api | outlook_token | icloud_privacy | forwarded_domain | cfworker_admin_api
    "mailbox_domain": "",        # 转发域名，例如 @example.com；生成 random@example.com 后由域名转发到 IMAP 邮箱
    "mailbox_imap_user": "",     # 163/126/QQ/Gmail 等转发收件箱账号
    "mailbox_imap_pass": "",     # IMAP 授权码/应用密码
    "mailbox_imap_host": "",     # 可空；163/126/QQ/Gmail/Outlook 自动推断
    "mailbox_imap_port": 993,
    "cfworker_api_url": "",
    "cfworker_admin_token": "",
    "cfworker_domain": "",
    "cfworker_fingerprint": "",
    "icloud_api_order_file": "",
    "icloud_api_order_text": "",
    "outlook_max_attempts_per_run": 3,
    "herosms_cancel_on_timeout": False,  # 超时默认保留 activation，避免晚到码被立刻取消
    "prepare_registration_before_phone": True,  # 租号前先验证代理/OpenAI 注册环境
    "precheck_phone_before_sms": True,          # 租号后先确认号码进入新账号密码页，再发送短信
    "herosms_presend_cancel_delay": 75,         # 未发送短信的旧号预检失败后，延迟释放 activation
    "herosms_max_price": 0.0999,              # HeroSMS 硬上限；只租 0.1 美刀以下的号
    "sms_activation_pool_enabled": True,       # 本地租号池/黑名单，跨 dashboard 子进程阻止旧 activation 复用
    "sms_activation_release_grace_seconds": 120,  # OpenAI 发短信后至少等待该秒数再尝试释放 HeroSMS activation
    "rotate_proxy_each_browser_launch": True,  # 每次新开浏览器窗口前重新选择 JP 代理并重建本地桥
    "sms_activation_block_ttl_seconds": 86400,    # 本地黑名单保留时长，避免同日重复拿旧号
    "sms_activation_pool_file": "",           # 默认 data/sms_activation_pool.json
    "sms_first_poll_delay": 12,   # OpenAI 请求短信后先延迟，降低接码平台旧状态/空轮询误判
    "sms_poll_interval": 5,
    "sms_preserve_password_on_timeout": True,
    "email_otp_timeout": 300,
    "email_otp_poll_interval": 5,

    
    # === 上传 ===
    "sub2api_url": "",           # Sub2API 管理后台 URL
    "sub2api_admin_key": "",     # Sub2API Admin API Key
    
    # === 输出 ===
    "output_dir": "output",
    "save_session": True,        # 保存 session JSON
    "save_tokens": True,         # 保存 token JSON
}


def load_config(config_path: str) -> dict:
    """加载 YAML 配置文件，与默认配置合并。"""
    from core.config_loader import load_config as _load_config

    return _load_config(config_path)


class _LocalSocks5Bridge:
    def __init__(self, upstream_host: str, upstream_port: int, username: str, password: str):
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.username = username.encode("utf-8")
        self.password = password.encode("utf-8")
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(64)
        self._server.settimeout(1.0)
        self.port = int(self._server.getsockname()[1])
        self.server_url = f"socks5://127.0.0.1:{self.port}"
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

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

    def _read_exact(self, sock: socket.socket, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = sock.recv(size - len(data))
            if not chunk:
                raise OSError("socket closed")
            data.extend(chunk)
        return bytes(data)

    def _handle(self, client: socket.socket) -> None:
        upstream = None
        try:
            client.settimeout(30)
            header = self._read_exact(client, 2)
            if header[0] != 5:
                return
            methods = self._read_exact(client, header[1])
            client.sendall(b"\x05\x00" if b"\x00" in methods else b"\x05\xff")
            request = self._read_exact(client, 4)
            if request[:3] != b"\x05\x01\x00":
                client.sendall(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
                return
            atyp = request[3]
            if atyp == 1:
                host = socket.inet_ntoa(self._read_exact(client, 4))
            elif atyp == 3:
                host = self._read_exact(client, self._read_exact(client, 1)[0]).decode("idna")
            elif atyp == 4:
                host = socket.inet_ntop(socket.AF_INET6, self._read_exact(client, 16))
            else:
                client.sendall(b"\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00")
                return
            port = struct.unpack("!H", self._read_exact(client, 2))[0]
            upstream = socket.create_connection((self.upstream_host, self.upstream_port), timeout=30)
            upstream.settimeout(30)
            upstream.sendall(b"\x05\x01\x02")
            auth_method = self._read_exact(upstream, 2)
            if auth_method != b"\x05\x02":
                client.sendall(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")
                return
            upstream.sendall(b"\x01" + bytes([len(self.username)]) + self.username + bytes([len(self.password)]) + self.password)
            if self._read_exact(upstream, 2) != b"\x01\x00":
                client.sendall(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")
                return
            host_bytes = host.encode("idna")
            upstream.sendall(b"\x05\x01\x00\x03" + bytes([len(host_bytes)]) + host_bytes + struct.pack("!H", port))
            response = self._read_exact(upstream, 4)
            if response[1] != 0:
                client.sendall(b"\x05" + response[1:2] + b"\x00\x01\x00\x00\x00\x00\x00\x00")
                return
            if response[3] == 1:
                bound = self._read_exact(upstream, 4)
            elif response[3] == 3:
                bound = self._read_exact(upstream, self._read_exact(upstream, 1)[0])
            elif response[3] == 4:
                bound = self._read_exact(upstream, 16)
            else:
                bound = b"\x00\x00\x00\x00"
            bound_port = self._read_exact(upstream, 2)
            client.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00" + bound_port)
            self._relay(client, upstream)
        except Exception:
            pass
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
        left.setblocking(True)
        right.setblocking(True)

        def pump(source: socket.socket, target: socket.socket) -> None:
            try:
                while not self._closed.is_set():
                    data = source.recv(65536)
                    if not data:
                        break
                    target.sendall(data)
            except Exception:
                pass
            finally:
                for sock in (source, target):
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except Exception:
                        pass

        t1 = threading.Thread(target=pump, args=(left, right), daemon=True)
        t2 = threading.Thread(target=pump, args=(right, left), daemon=True)
        t1.start()
        t2.start()
        while (t1.is_alive() or t2.is_alive()) and not self._closed.is_set():
            t1.join(0.5)
            t2.join(0.5)


class _LocalHttpToSocksBridge:
    def __init__(self, upstream_host: str, upstream_port: int, username: str, password: str):
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.username = username.encode("utf-8")
        self.password = password.encode("utf-8")
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(64)
        self._server.settimeout(1.0)
        self.port = int(self._server.getsockname()[1])
        self.server_url = f"http://127.0.0.1:{self.port}"
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

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

    def _connect_upstream(self, host: str, port: int) -> socket.socket:
        upstream = socket.create_connection((self.upstream_host, self.upstream_port), timeout=30)
        upstream.settimeout(30)
        upstream.sendall(b"\x05\x01\x02")
        if upstream.recv(2) != b"\x05\x02":
            raise OSError("upstream socks auth method rejected")
        upstream.sendall(b"\x01" + bytes([len(self.username)]) + self.username + bytes([len(self.password)]) + self.password)
        if upstream.recv(2) != b"\x01\x00":
            raise OSError("upstream socks authentication failed")
        host_bytes = host.encode("idna")
        upstream.sendall(b"\x05\x01\x00\x03" + bytes([len(host_bytes)]) + host_bytes + struct.pack("!H", int(port)))
        response = upstream.recv(4)
        if len(response) != 4 or response[1] != 0:
            raise OSError(f"upstream socks connect failed: {response!r}")
        atyp = response[3]
        if atyp == 1:
            upstream.recv(4)
        elif atyp == 3:
            upstream.recv(upstream.recv(1)[0])
        elif atyp == 4:
            upstream.recv(16)
        upstream.recv(2)
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
            first = lines[0].decode("latin1", errors="replace")
            parts = first.split(" ", 2)
            if len(parts) != 3:
                return
            method, target, version = parts
            if method.upper() == "CONNECT":
                host, _, port_text = target.rpartition(":")
                upstream = self._connect_upstream(host, int(port_text or "443"))
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                self._relay(client, upstream)
                return
            from urllib.parse import urlsplit
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
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            if not parsed.scheme:
                path = target
            rewritten = f"{method} {path} {version}".encode("latin1")
            upstream.sendall(b"\r\n".join([rewritten] + lines[1:]) + b"\r\n\r\n" + body)
            self._relay(client, upstream)
        except Exception:
            try:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            except Exception:
                pass
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
        _LocalSocks5Bridge._relay(self, left, right)




# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class RegisterPipeline:
    """ChatGPT 手机号注册 + Plus 激活全链路。"""

    def __init__(self, config: dict):
        self.config = config
        self.sms_provider = None
        self.page = None
        self.browser = None
        self._proxy_candidates = []
        self._proxy_candidate_index = 0
        self._used_proxy_ips = set()
        self._local_socks_bridge = None
        self._local_socks_bridge_target = ""
        self.playwright_instance = None
        self.browser_context = None
        self._camoufox_ctx = None
        self._bad_phone_exceptions = []
        self._bad_activation_ids = set()
        self._last_sms_wait_status = ""
        self._registration_device_id = ""
        self._registration_csrf_token = ""
        self._prechecked_phone_number = ""
        self._prechecked_create_account_url = ""
        self.activation_pool = LocalActivationPool.from_config(self.config)
        debug_dir = Path(self.config.get("output_dir", "output")) / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        self.debug_log_file = debug_dir / f"register_debug_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{os.getpid()}.log"
        self.result = {
            "success": False,
            "status": "",
            "phone_number": "",
            "activation_id": "",
            "access_token": "",
            "refresh_token": "",
            "id_token": "",
            "session_token": "",
            "account_id": "",
            "email": "",
            "plan_type": "",
            "password": "",
            "iceaix_job_id": "",
            "iceaix_status": "",
            "iceaix_result_code": "",
            "iceaix_error_message": "",
            "iceaix_billing_status": "",
            "iceaix_resource_mode": "",
            "registration_proxy": "",
            "registration_proxy_exit_ip": "",
            "subscription_check_proxy": "",
            "subscription_check_source": "",
            "failed_step": "",
            "failure_reason": "",
            "retryable": False,
            "next_action": "",
            "outlook_email": "",
            "failed_file": "",
            "cpa_callback_url": "",
            "cpa_submit_status": 0,
            "cpa_submit_body": "",
            "resume_file": "",
            "final_file": "",
            "text_file": "",
            "steps": [],
        }

    def log(self, msg: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {msg}"
        print(line)
        try:
            with open(self.debug_log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def _debug_page_state(self, label: str) -> None:
        try:
            url = str(self.page.url or "") if self.page else ""
            title = self.page.title() if self.page else ""
            body = ""
            if self.page:
                body = self.page.evaluate("() => (document.body?.innerText || '').slice(0, 800)")
            self.log(f"  [debug:{label}] url={url[:220]} title={title[:120]} body={body[:240].replace(chr(10), ' ')}")
        except Exception as exc:
            self.log(f"  [debug:{label}] snapshot failed: {exc}")

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

    def _config_bool(self, key: str, default: bool = False) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _activation_pool_enabled(self) -> bool:
        return bool(getattr(self.activation_pool, "enabled", False))

    def _sync_activation_pool_exclusions(self, configured_exceptions: list[str]) -> list[str]:
        if not self._activation_pool_enabled():
            return list(configured_exceptions) + list(self._bad_phone_exceptions)
        pool_provider = str(self.config.get("sms_provider") or "")
        try:
            for record in self.activation_pool.snapshot():
                if record.status not in {"reserved", "post_send_pending", "blocked", "release_pending"}:
                    continue
                if pool_provider and record.provider != pool_provider:
                    continue
                if record.activation_id:
                    self._bad_activation_ids.add(record.activation_id)
            for prefix in self.activation_pool.blocked_phone_exceptions(provider=pool_provider, limit=3):
                if prefix and prefix not in self._bad_phone_exceptions:
                    self._bad_phone_exceptions.append(prefix)
        except Exception as exc:
            self.log(f"  本地接码池读取失败，继续使用内存黑名单: {exc}")
        return list(configured_exceptions) + list(self._bad_phone_exceptions)

    def _activation_pool_reserve_current(self, *, service: str = "", country: str = "") -> None:
        if not self._activation_pool_enabled():
            return
        activation_id = str(self.result.get("activation_id") or "").strip()
        phone_number = str(self.result.get("phone_number") or "").strip()
        if not activation_id and not phone_number:
            return
        try:
            self.activation_pool.reserve(
                provider=str(self.config.get("sms_provider") or ""),
                activation_id=activation_id,
                phone_number=phone_number,
                service=str(service or self.config.get("sms_service") or ""),
                country=str(country or self.config.get("sms_country") or ""),
                proxy_exit_ip=str(self.result.get("registration_proxy_exit_ip") or ""),
            )
        except Exception as exc:
            self.log(f"  本地接码池 reserve 失败: {exc}")

    def _activation_pool_block_current(self, reason: str, *, post_send: bool = False, release_pending: bool = False) -> None:
        if not self._activation_pool_enabled():
            return
        activation_id = str(self.result.get("activation_id") or "").strip()
        phone_number = str(self.result.get("phone_number") or "").strip()
        if not activation_id:
            return
        try:
            if post_send:
                self.activation_pool.mark_post_send(activation_id, reason=reason)
            elif release_pending:
                self.activation_pool.mark_release_pending(activation_id, reason=reason)
            else:
                self.activation_pool.block(activation_id, phone_number=phone_number, reason=reason)
        except Exception as exc:
            self.log(f"  本地接码池标记失败: {exc}")

    def _activation_pool_mark_released(self, activation_id: str, reason: str = "released") -> None:
        if not self._activation_pool_enabled() or not activation_id:
            return
        try:
            self.activation_pool.mark_released(activation_id, reason=reason)
        except Exception as exc:
            self.log(f"  本地接码池 release 标记失败: {exc}")

    def _activation_pool_mark_completed(self, activation_id: str, reason: str = "registration succeeded") -> None:
        if not self._activation_pool_enabled() or not activation_id:
            return
        try:
            self.activation_pool.mark_completed(activation_id, reason=reason)
        except Exception as exc:
            self.log(f"  本地接码池完成标记失败: {exc}")

    def _release_due_pool_activations(self) -> None:
        if not self._activation_pool_enabled() or not self.sms_provider:
            return
        cancel = getattr(self.sms_provider, "cancel_activation", None)
        if not callable(cancel):
            return
        try:
            due_records = self.activation_pool.releasable(provider=str(self.config.get("sms_provider") or ""), limit=10)
        except Exception as exc:
            self.log(f"  本地接码池待释放查询失败: {exc}")
            return
        for record in due_records:
            try:
                if cancel(record.activation_id):
                    self.activation_pool.mark_released(record.activation_id, reason="local pool delayed release")
                    self.log(f"  本地接码池已延迟释放 activation: {record.activation_id}")
            except Exception as exc:
                self.log(f"  本地接码池释放失败: {record.activation_id} {exc}")

    def _set_failure(self, status: str, *, step: str, reason: str, retryable: bool = False, next_action: str = "") -> None:
        self.result["success"] = False
        self.result["status"] = status
        self.result["failed_step"] = step
        self.result["failure_reason"] = reason
        self.result["retryable"] = bool(retryable)
        self.result["next_action"] = next_action

    def _clear_registration_auth_state(self) -> None:
        self._registration_device_id = ""
        self._registration_csrf_token = ""

    def _is_phone_retryable_registration_error(self, error_text: str) -> bool:
        text = str(error_text or "").lower()
        if not text:
            return False
        markers = (
            "phone_already_registered",
            "already registered",
            "already used",
            "incorrect phone",
            "phone number or password",
            "existe uma conta para este número",
            "existe uma conta para este numero",
            "já existe uma conta para este número",
            "ja existe uma conta para este numero",
            "unsupported phone",
            "phone number is not supported",
            "unable to verify phone",
            "sms timeout",
            "sms 超时",
            "sms activation cancelled",
            "activation cancelled",
            "activation 已失效",
            "missing_activation",
            "账号已被使用",
        )
        return any(marker in text for marker in markers)

    def _registration_page_indicates_existing_phone(self) -> tuple[bool, str]:
        if not self.page:
            return False, ""
        url = str(self.page.url or "")
        lower_url = url.lower()
        if "auth.openai.com/log-in" in lower_url:
            return True, f"authorize resolved to existing-account login page: {url[:160]}"
        body = ""
        try:
            body = self.page.evaluate("() => (document.body?.innerText || '').slice(0, 900)")
        except Exception:
            body = ""
        body_lower = body.lower()
        markers = (
            "already registered",
            "already used",
            "incorrect phone",
            "phone number or password",
            "existe uma conta para este número",
            "existe uma conta para este numero",
            "já existe uma conta para este número",
            "ja existe uma conta para este numero",
            "账号已被使用",
        )
        if any(marker in body_lower for marker in markers):
            return True, body[:240]
        return False, ""

    def _registration_page_is_create_account_password(self) -> bool:
        if not self.page:
            return False
        url = str(self.page.url or "").lower()
        return "auth.openai.com/create-account/password" in url

    def _registration_page_accepts_password(self) -> bool:
        if self._registration_page_is_create_account_password():
            return True
        if not self.page or not self._config_bool("force_signup_from_login_password", False):
            return False
        url = str(self.page.url or "").lower()
        return "auth.openai.com/log-in/password" in url

    def _force_signup_from_login_password(self) -> bool:
        if not self.page or not self._config_bool("force_signup_from_login_password", False):
            return False
        url = str(self.page.url or "").lower()
        if "auth.openai.com/log-in/password" not in url:
            return False
        selectors = (
            "text=サインアップ",
            "text=Sign up",
            "text=新規登録",
            "text=注册",
            "text=创建账号",
            "a[href*='create-account']",
            "button:has-text('Sign up')",
        )
        for selector in selectors:
            try:
                target = self.page.locator(selector).first
                if target.count() <= 0:
                    continue
                target.click(timeout=2500)
                self._wait_for_auth_password_state(timeout=20)
                if self._registration_page_is_create_account_password():
                    self.log(f"  已从登录密码页强制切换到注册页: {selector}")
                    return True
            except Exception:
                continue
        try:
            clicked = self.page.evaluate(
                """
                () => {
                  const needles = ['サインアップ', 'Sign up', '新規登録', '注册', '创建账号'];
                  const nodes = [...document.querySelectorAll('a,button,[role="button"]')];
                  const node = nodes.find(el => needles.some(n => (el.innerText || el.textContent || '').includes(n)));
                  if (!node) return false;
                  node.click();
                  return true;
                }
                """
            )
            if clicked:
                self._wait_for_auth_password_state(timeout=20)
                if self._registration_page_is_create_account_password():
                    self.log("  已从登录密码页强制切换到注册页: dom-text")
                    return True
        except Exception:
            pass
        return False

    def _wait_for_auth_password_state(self, *, timeout: int = 35) -> None:
        if not self.page:
            return
        deadline = time.time() + timeout
        while time.time() < deadline:
            url = str(self.page.url or "")
            lower_url = url.lower()
            if "auth.openai.com/log-in" in lower_url or "auth.openai.com/create-account/password" in lower_url:
                break
            if "auth.openai.com" in lower_url and "challenge" not in lower_url:
                break
            time.sleep(1.5)

    def _discard_current_activation_after_precheck_failure(self, reason: str) -> None:
        activation_id = str(self.result.get("activation_id") or "").strip()
        phone_number = str(self.result.get("phone_number") or "").strip()
        if not activation_id:
            return
        self._activation_pool_block_current(reason, release_pending=False)
        self.log(f"  预检未发短信，跳过释放旧 activation，直接换号: phone={phone_number} activation_id={activation_id} reason={reason[:120]}")

    def _classify_authorize_url(self, value: str, *, source: str) -> tuple[str, str] | None:
        from urllib.parse import urlparse

        candidate = str(value or "").strip()
        if not candidate:
            return None
        parsed = urlparse(candidate)
        host = parsed.netloc.lower()
        path = parsed.path.lower()
        if host == "auth.openai.com" and path.startswith("/create-account/password"):
            return "new", candidate
        if host == "auth.openai.com" and path.startswith("/log-in"):
            return "registered", f"{source} resolved to existing-account login page: {candidate[:160]}"
        if host == "auth.openai.com" and any(marker in path for marker in ("add-phone", "email-verification", "email-otp", "about-you", "choose-an-account")):
            return "registered", f"{source} skipped password and reached {candidate[:160]}"
        if host.endswith("chatgpt.com") and not path.startswith("/auth/"):
            return "registered", f"{source} reached ChatGPT home: {candidate[:160]}"
        return None

    def _precheck_authorize_redirect_by_request(self, redirect_url: str) -> tuple[str, str]:
        """Follow the authorize redirect without navigating the visible browser tab."""
        target = str(redirect_url or "").strip()
        if not target:
            return "unknown", "empty authorize URL"
        classified = self._classify_authorize_url(target, source="authorize URL")
        if classified:
            return classified
        if not self.browser_context:
            return "unknown", "missing browser context"
        try:
            response = self.browser_context.request.get(
                target,
                headers={
                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                    "accept-language": "en-US,en;q=0.9",
                    "referer": "https://chatgpt.com/",
                    "upgrade-insecure-requests": "1",
                },
                timeout=45000,
            )
        except Exception as exc:
            return "unknown", f"authorize request failed: {str(exc)[:180]}"

        final_url = str(getattr(response, "url", "") or target)
        classified = self._classify_authorize_url(final_url, source="authorize request")
        if classified:
            return classified
        try:
            status = int(getattr(response, "status", 0) or 0)
        except Exception:
            status = 0
        return "unknown", f"unexpected authorize request state: status={status} url={final_url[:160]}"

    def _precheck_phone_registration_state(self, phone_number: str) -> tuple[str, str]:
        """Resolve OpenAI's phone authorize state before sending SMS without showing login pages."""
        from platforms.chatgpt.browser_register import _derive_registration_state_from_page, _extract_auth_error_text, _start_browser_signin

        self._prechecked_phone_number = ""
        self._prechecked_create_account_url = ""
        device_id, csrf = self.step_prepare_registration_environment(reset_auth=False)
        redirect_url = _start_browser_signin(self.page, phone_number, device_id, csrf, screen_hint=str(self.config.get("registration_screen_hint") or "signup"))
        self._clear_registration_auth_state()
        if not redirect_url:
            return "unknown", "browser signin failed: no redirect URL"

        request_state, request_detail = self._precheck_authorize_redirect_by_request(redirect_url)
        self.log(f"  手机号预检请求态: state={request_state} detail={request_detail[:140]}")
        if request_state != "new":
            return request_state, request_detail

        try:
            self.page.goto(request_detail, wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:
            current_url = str(self.page.url or "")
            if "auth.openai.com/create-account/password" in current_url:
                self.log(f"  预检 create-account 打开超时但已到注册密码页，继续判定: {current_url[:120]}")
            else:
                return "unknown", f"create-account navigation failed after request precheck: {str(exc)[:180]}"
        self._wait_for_auth_password_state()
        self._debug_page_state("phone-precheck-after-create-account-request")
        state = _derive_registration_state_from_page(self.page)
        page_type = str(state.get("page_type") or "").strip()
        state_url = str(state.get("current_url") or state.get("continue_url") or self.page.url or "")
        self.log(f"  手机号预检页面态: page_type={page_type or 'unknown'} url={state_url[:140]}")
        if page_type == "login_password":
            return "registered", f"create-account request opened login page: {state_url[:160]}"
        error_text = _extract_auth_error_text(self.page)
        if error_text:
            existing_by_error, existing_detail = self._registration_page_indicates_existing_phone()
            if existing_by_error:
                return "registered", existing_detail
            return "unknown", f"auth error: {error_text[:240]}"
        existing_phone, existing_detail = self._registration_page_indicates_existing_phone()
        if existing_phone:
            return "registered", existing_detail
        if page_type in {"create_account_password", "password"} or self._registration_page_is_create_account_password():
            self._prechecked_phone_number = str(phone_number or "")
            self._prechecked_create_account_url = state_url or str(self.page.url or "")
            return "new", self._prechecked_create_account_url
        if page_type in {"add_phone", "email_otp_verification", "about_you", "choose_account", "chatgpt_home"}:
            return "registered", f"authorize skipped password and reached {page_type}: {state_url[:160]}"
        return "unknown", f"unexpected auth state: page_type={page_type or 'unknown'} url={state_url[:160]}"

    def _get_registration_csrf_token(self) -> str:
        from platforms.chatgpt.browser_register import _get_browser_csrf_token

        try:
            token = _get_browser_csrf_token(self.page)
        except Exception as exc:
            self.log(f"  [debug] browser csrf fetch failed: {str(exc)[:160]}")
            token = ""
        if token:
            return token
        if not self.browser_context:
            return ""
        try:
            response = self.browser_context.request.get(
                "https://chatgpt.com/api/auth/csrf",
                headers={"accept": "application/json", "referer": "https://chatgpt.com/auth/login"},
                timeout=30000,
            )
            data = response.json()
            if isinstance(data, dict):
                return str(data.get("csrfToken") or "").strip()
        except Exception as exc:
            self.log(f"  [debug] context csrf fallback failed: {str(exc)[:160]}")
        return ""

    def _prepare_registration_environment_once(self, *, reset_auth: bool, headed: bool) -> tuple[str, str]:
        from platforms.chatgpt.browser_register import _seed_browser_device_id

        if self.page is None:
            proxy = self._select_fresh_proxy_for_attempt()
            if proxy and not self.result.get("registration_proxy"):
                self.result["registration_proxy"] = proxy
                self.config["_proxy_selected_for_next_browser_launch"] = True
            self.config["_force_fresh_browser_context"] = True
            self.log("  注册前先验证代理和 OpenAI 注册环境...")
            self._launch_camoufox(headed=headed) if self.config.get("use_camoufox", True) else self._launch_playwright(headed=headed)
        else:
            self.log("  复用当前浏览器/IP，重置 OpenAI 注册入口...")

        device_id = str(uuid.uuid4())
        _seed_browser_device_id(self.page, device_id)
        try:
            self.page.goto("https://chatgpt.com/auth/login", wait_until="domcontentloaded", timeout=90000)
        except Exception as exc:
            current_url = str(self.page.url or "")
            if current_url.startswith("https://chatgpt.com/auth/login"):
                self.log(f"  注册入口加载超时但已到登录页，继续复用当前窗口: {current_url[:120]}")
            else:
                try:
                    self.page.goto("https://chatgpt.com/auth/login", wait_until="commit", timeout=30000)
                    self.log("  注册入口 domcontentloaded 超时，commit 已成功，继续")
                except Exception:
                    raise exc
        self._debug_page_state("registration-env-login")
        csrf = self._get_registration_csrf_token()
        if not csrf:
            for csrf_attempt in range(4):
                self.log(f"  [debug] csrf fetch retry {csrf_attempt + 1}/4")
                time.sleep(1.5 if csrf_attempt == 0 else 2.5)
                csrf = self._get_registration_csrf_token()
                if csrf:
                    break
        if not csrf:
            self._debug_page_state("registration-env-csrf-empty")
            self._clear_registration_auth_state()
            raise RuntimeError("registration environment check failed: csrf token unavailable")
        self._registration_device_id = device_id
        self._registration_csrf_token = csrf
        self.log("  注册环境验证通过: ChatGPT login + csrf OK")
        return device_id, csrf

    def step_prepare_registration_environment(self, *, reset_auth: bool = False) -> tuple[str, str]:
        """Select/verify the registration proxy and prepare an auth page before renting a phone."""
        if self._registration_device_id and self._registration_csrf_token and self.page and not reset_auth:
            return self._registration_device_id, self._registration_csrf_token

        headed = self.config.get("headed", False)
        attempts = 2 if self.page is None else 1
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                return self._prepare_registration_environment_once(reset_auth=reset_auth, headed=headed)
            except Exception as exc:
                last_exc = exc
                if self.result.get("phone_number") or attempt >= attempts - 1:
                    raise
                self.log(f"  注册环境初始化失败，重开浏览器重试: {str(exc)[:180]}")
                self._cleanup()
                self.result["registration_proxy"] = ""
                self.result["registration_proxy_exit_ip"] = ""
        raise last_exc or RuntimeError("registration environment check failed")

    def _attach_page_debug_events(self, page) -> None:
        try:
            def _remember_oauth_callback(url: str) -> None:
                value = str(url or "")
                if "state=" not in value or ("code=" not in value and "error=" not in value):
                    return
                if "/auth/callback" not in value and "localhost" not in value:
                    return
                try:
                    setattr(page, "_omp_last_oauth_callback_url", value)
                except Exception:
                    pass
            if not self._config_bool("debug_browser_events", False):
                def _frame_navigated_basic(frame):
                    if frame != page.main_frame:
                        return
                    _remember_oauth_callback(frame.url)
                    self.log(f"  [navigated] {frame.url[:220]}")
                page.on("framenavigated", _frame_navigated_basic)
                page.on("close", lambda: self.log("  [page] closed"))
                try:
                    page.on("crash", lambda: self.log("  [page] crash"))
                except Exception:
                    pass
                return

            def _request_failed(req):
                try:
                    _remember_oauth_callback(req.url)
                except Exception:
                    pass
                failure = ""
                try:
                    value = req.failure
                    if callable(value):
                        value = value()
                    failure = str(value or "")
                except Exception as exc:
                    failure = f"failure-read-error: {exc}"
                self.log(f"  [requestfailed] {req.method} {req.url[:220]} -> {failure[:240]}")

            page.on("pageerror", lambda exc: self.log(f"  [pageerror] {str(exc)[:500]}"))
            page.on("console", lambda msg: self.log(f"  [console:{msg.type}] {str(msg.text)[:500]}") if msg.type in ("error", "warning") else None)
            page.on("requestfailed", _request_failed)
            def _frame_navigated_debug(frame):
                if frame != page.main_frame:
                    return
                _remember_oauth_callback(frame.url)
                self.log(f"  [navigated] {frame.url[:220]}")
            page.on("framenavigated", _frame_navigated_debug)
            page.on("close", lambda: self.log("  [page] closed"))
            try:
                page.on("crash", lambda: self.log("  [page] crash"))
            except Exception:
                pass
        except Exception as exc:
            self.log(f"  [debug] attach page events failed: {exc}")

    def _ensure_phone_sms_send_clicked(self) -> tuple[bool, str]:
        """Ensure OpenAI actually requested the phone OTP before polling HeroSMS."""
        if not self.page:
            return False, "missing page"
        from platforms.chatgpt.browser_register import (
            PHONE_SEND_SELECTORS,
            OTP_INPUT_SELECTORS,
            _click_first,
            _extract_auth_error_text,
            _wait_for_any_selector,
        )

        try:
            if _wait_for_any_selector(self.page, OTP_INPUT_SELECTORS, timeout=1):
                return True, "otp input already visible"
        except Exception:
            pass

        error_text = _extract_auth_error_text(self.page)
        if error_text:
            return False, error_text[:300]

        page_url = str(self.page.url or "")
        if "password" in page_url and "contact-verification" not in page_url:
            return False, f"仍停留密码页，不能点击发送短信: url={page_url[:120]}"

        clicked = _click_first(self.page, PHONE_SEND_SELECTORS, timeout=8)
        if clicked:
            self.log(f"  已点击发送短信按钮: {clicked}")
        else:
            try:
                body = self.page.evaluate("() => (document.body?.innerText || '').slice(0, 700)")
            except Exception:
                body = ""
            if re.search(r"sent|enviado|code|c[oó]digo|验证码", body, flags=re.I):
                return True, "page text indicates code sent"
            return False, f"未找到发送短信按钮: url={str(self.page.url)[:120]} body={body[:240]}"

        deadline = time.time() + 15
        last_body = ""
        while time.time() < deadline:
            try:
                if _wait_for_any_selector(self.page, OTP_INPUT_SELECTORS, timeout=1):
                    return True, "otp input visible after send"
            except Exception:
                pass
            error_text = _extract_auth_error_text(self.page)
            if error_text:
                return False, error_text[:300]
            try:
                last_body = self.page.evaluate("() => (document.body?.innerText || '').slice(0, 700)")
            except Exception:
                last_body = ""
            if re.search(r"sent|enviado|resend|reenviar|code|c[oó]digo", last_body, flags=re.I):
                return True, "page text indicates code sent after click"
            time.sleep(1)
        return False, f"发送短信后未出现验证码输入框: url={str(self.page.url)[:120]} body={last_body[:240]}"


    def _mark_current_phone_bad(self, reason: str) -> None:
        activation_id = str(self.result.get("activation_id") or "").strip()
        phone_number = str(self.result.get("phone_number") or "").strip()
        digits = "".join(ch for ch in phone_number if ch.isdigit())
        if activation_id:
            self._bad_activation_ids.add(activation_id)
        if digits and digits not in self._bad_phone_exceptions:
            self._bad_phone_exceptions.append(digits)
        self._activation_pool_block_current(reason)
        if self.sms_provider and activation_id:
            try:
                self.sms_provider.mark_send_failed(activation_id, reason)
            except Exception:
                pass

    def _lajiao_credential_protocol_for(self, proxy_value: str, configured_protocol: str = "") -> str:
        from urllib.parse import urlsplit

        value = str(proxy_value or "").strip().lower()
        parsed = urlsplit(value if "://" in value else f"//{value}")
        host = str(parsed.hostname or "").lower()
        protocol = str(configured_protocol or "").strip().lower()
        if (not protocol or protocol in {"socks5", "socks5h"}) and (host.endswith("lajiaohttp.net") or host.endswith("lajiaohttp.com")):
            return "http"
        return protocol or "http"

    def _is_lajiao_http_gateway(self, proxy_value: str) -> bool:
        from urllib.parse import urlsplit

        value = str(proxy_value or "").strip().lower()
        parsed = urlsplit(value if "://" in value else f"//{value}")
        host = str(parsed.hostname or "").lower()
        return host.endswith("lajiaohttp.net") or host.endswith("lajiaohttp.com")


    def _credential_proxy_candidates(self) -> list[str]:
        from core.proxy.credential_runtime import CredentialProxyRuntime

        return CredentialProxyRuntime(self.config, log_fn=self.log).credential_candidates()

    def _proxy_check_url(self, proxy: str) -> str:
        from core.proxy.credential_runtime import CredentialProxyRuntime

        return CredentialProxyRuntime(self.config, log_fn=self.log).check_url(proxy)

    def _proxy_runtime_url(self, proxy: str) -> str:
        from core.proxy.credential_runtime import CredentialProxyRuntime

        return CredentialProxyRuntime(self.config, log_fn=self.log).runtime_url(proxy)

    def _use_lajiao_credentials_mode(self) -> bool:
        mode = str(self.config.get("lajiao_proxy_mode") or "").strip().lower()
        if mode in {"credential", "credentials", "account", "auth"}:
            return True
        if not mode and (str(self.config.get("lajiao_proxy_credentials") or "").strip() or str(self.config.get("lajiao_proxy_credentials_file") or "").strip()):
            return True
        return False

    def _fetch_lajiao_proxy_candidates(self) -> list[str]:
        import requests
        from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

        api_url = str(self.config.get("lajiao_proxy_api_url") or "http://api.lajiaohttp.com/api/extract_ip")
        parsed_url = urlparse(api_url)
        query_has_params = bool(parsed_url.query)
        params = None
        if query_has_params:
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
        candidates = []
        content_type = str(response.headers.get("content-type") or "").lower()
        wants_json = (params or {}).get("type") == "json" or "application/json" in content_type or text.startswith("{")
        if wants_json:
            data = response.json()
            if not data.get("success", data.get("code") == 0):
                raise RuntimeError(f"辣椒 HTTP 代理提取失败: {data}")
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
            raise RuntimeError(f"辣椒 HTTP 代理提取失败: 无候选: {text[:200]}")
        return candidates

    def _check_lajiao_proxy(self, proxy: str) -> tuple[bool, str]:
        from core.proxy.credential_runtime import CredentialProxyRuntime

        runtime = CredentialProxyRuntime(self.config, log_fn=self.log)
        runtime._used_proxy_ips = self._used_proxy_ips
        ok, exit_ip = runtime.check(proxy)
        self._used_proxy_ips = runtime._used_proxy_ips
        return ok, exit_ip

    def _select_fresh_proxy_for_attempt(self) -> str:
        if not self.config.get("rotate_proxy_each_attempt"):
            proxy_url = str(self.config.get("proxy") or "")
            if proxy_url:
                ok, exit_ip = self._check_lajiao_proxy(proxy_url)
                if not ok:
                    self._set_failure("proxy_precheck_failed", step="select_proxy", reason=f"代理 OpenAI 探针失败: {proxy_url}", retryable=True, next_action="更换干净代理或开启代理轮换")
                    raise RuntimeError(self.result["failure_reason"])
                self.result["registration_proxy"] = proxy_url
                if exit_ip:
                    self.result["registration_proxy_exit_ip"] = exit_ip
                    self.config["_camoufox_geoip_ip"] = exit_ip
                self.log(f"  注册代理 OpenAI 探针通过: proxy={proxy_url} exit_ip={exit_ip or '-'}")
            return proxy_url
        from core.proxy.credential_runtime import CredentialProxyRuntime

        runtime = CredentialProxyRuntime(self.config, log_fn=self.log)
        runtime._used_proxy_ips = self._used_proxy_ips
        runtime._proxy_candidates = self._proxy_candidates
        runtime._proxy_candidate_index = self._proxy_candidate_index
        try:
            proxy_url, exit_ip = runtime.select()
        except RuntimeError as exc:
            self._set_failure("proxy_exhausted", step="select_proxy", reason=str(exc), retryable=True, next_action="稍后重试或检查辣椒代理池质量")
            raise RuntimeError(self.result["failure_reason"])
        self._used_proxy_ips = runtime._used_proxy_ips
        self._proxy_candidates = runtime._proxy_candidates
        self._proxy_candidate_index = runtime._proxy_candidate_index
        self.result["registration_proxy"] = proxy_url
        self.result["registration_proxy_exit_ip"] = exit_ip
        return proxy_url

    def _select_fresh_proxy_for_subscription_check(self) -> str:
        if not self.config.get("rotate_proxy_each_attempt"):
            return str(self.config.get("proxy") or "")
        try:
            candidates = self._credential_proxy_candidates() if self._use_lajiao_credentials_mode() else self._fetch_lajiao_proxy_candidates()
        except Exception as exc:
            self.log(f"  实时订阅检查加载辣椒 HTTP 代理失败: {exc}")
            return ""
        for proxy in candidates:
            ok, exit_ip = self._check_lajiao_proxy(proxy)
            if not ok:
                continue
            proxy_url = self._proxy_runtime_url(proxy)
            self.config["_camoufox_geoip_ip"] = exit_ip
            self.log(f"  实时订阅检查使用新辣椒 HTTP 代理: {proxy_url} exit_ip={exit_ip}")
            return proxy_url
        return ""


    # ------------------------------------------------------------------
    # Step 1: 获取手机号
    # ------------------------------------------------------------------

    def step_get_phone_number(self) -> str:
        """获取注册手机号。"""
        self.log("=" * 60)
        self.log("Step 1: 获取手机号")
        self.log("=" * 60)

        provider_key = str(self.config.get("sms_provider") or "herosms_api").strip().lower()

        if self._config_bool("prepare_registration_before_phone", True):
            self.step_prepare_registration_environment(reset_auth=True)

        from core.base_sms import HeroSmsProvider, create_sms_provider, hero_sms_cache_file

        sms_config = dict(self.config)
        configured_exceptions = sms_config.get("herosms_phone_exceptions") or sms_config.get("sms_phone_exceptions") or []
        if isinstance(configured_exceptions, str):
            configured_exceptions = [item.strip() for item in configured_exceptions.replace("\n", ",").split(",") if item.strip()]
        sms_config["herosms_phone_exceptions"] = self._sync_activation_pool_exclusions(list(configured_exceptions))
        if provider_key in {"herosms", "herosms_api"}:
            self.sms_provider = HeroSmsProvider.from_config(sms_config)
        else:
            self.sms_provider = create_sms_provider(provider_key, sms_config)

        if isinstance(self.sms_provider, HeroSmsProvider):
            try:
                balance = self.sms_provider.get_balance()
                self.log(f"  HeroSMS 余额: {balance}")
            except Exception as e:
                self.log(f"  查余额失败: {e}")
            try:
                for active in self.sms_provider.get_active_activations(limit=20):
                    active_id = str(active.get("activationId") or "").strip()
                    active_phone = str(active.get("phoneNumber") or "").strip()
                    if active_id:
                        self._bad_activation_ids.add(active_id)
                    if active_phone and active_phone not in self._bad_phone_exceptions:
                        self._bad_phone_exceptions.append(active_phone)
                self.sms_provider.phone_exceptions = self.sms_provider._normalize_phone_exceptions(
                    list(configured_exceptions) + list(self._bad_phone_exceptions)
                )
            except Exception as e:
                self.log(f"  读取 active 激活失败，继续租号: {e}")
            self._release_due_pool_activations()

        cache_file = hero_sms_cache_file()
        if cache_file.exists() and not self._config_bool("register_reuse_phone_to_max", True):
            cache_file.unlink(missing_ok=True)

        service = self.config.get("sms_service", "dr")
        country = self.config.get("sms_country", "33")
        accepted_result = None
        last_error = ""
        for rent_attempt in range(60):
            try:
                candidate_result = self.sms_provider.get_number(service=service, country=country)
            except Exception as exc:
                last_error = str(exc)
                if "NO_NUMBERS" in last_error or "Not Found" in last_error:
                    if len(self._bad_phone_exceptions) > 3:
                        self._bad_phone_exceptions = self._bad_phone_exceptions[-3:]
                        self.sms_provider.phone_exceptions = self.sms_provider._normalize_phone_exceptions(
                            list(configured_exceptions) + list(self._bad_phone_exceptions)
                        )
                        self.log("  HeroSMS 无号，已收缩 phoneException 到最近 3 个坏前缀后重试")
                    self.log(f"  HeroSMS 暂无号码，等待后重试 {rent_attempt + 1}/60: {last_error[:160]}")
                    time.sleep(30)
                    continue
                raise
            phone_number = str(getattr(candidate_result, "phone_number", "") or "").strip()
            activation_id = str(getattr(candidate_result, "activation_id", "") or "").strip()
            digits = "".join(ch for ch in phone_number if ch.isdigit())
            self.result["phone_number"] = phone_number
            self.result["activation_id"] = activation_id
            pool_blocked, pool_record = self.activation_pool.is_blocked(activation_id=activation_id, phone_number=phone_number, provider=str(self.config.get("sms_provider") or "")) if self._activation_pool_enabled() else (False, None)
            if pool_blocked or (isinstance(self.sms_provider, HeroSmsProvider) and (activation_id in self._bad_activation_ids or digits in self._bad_phone_exceptions)):
                pool_reason = f" pool_status={pool_record.status}" if pool_record else ""
                self.log(f"  HeroSMS 返回旧号，加入排除后重取: {phone_number} / {activation_id}{pool_reason}")
                if digits:
                    self._bad_phone_exceptions.append(digits)
                self.sms_provider.phone_exceptions = self.sms_provider._normalize_phone_exceptions(self._bad_phone_exceptions)
                clear_cache = getattr(self.sms_provider, "_clear_cache", None)
                if callable(clear_cache):
                    clear_cache()
                cache_file.unlink(missing_ok=True)
                continue
            self._activation_pool_reserve_current(service=str(service), country=str(country))
            if self._config_bool("precheck_phone_before_sms", True):
                state, detail = self._precheck_phone_registration_state(phone_number)
                self.log(f"  手机号注册态预检: state={state} detail={detail[:180]}")
                if state == "new":
                    accepted_result = candidate_result
                    break
                self._mark_current_phone_bad(f"phone precheck {state}: {detail}")
                self._discard_current_activation_after_precheck_failure(detail)
                last_error = f"phone precheck {state}: {detail}"
                self.log("  手机号预检未通过，保持当前浏览器/IP，继续换下一个号")
                continue
            accepted_result = candidate_result
            break
        if accepted_result is None:
            raise RuntimeError(f"接码取号失败: 等待后仍无非排除号码: {last_error[:200]}")
        result = accepted_result
        phone_number = str(getattr(result, "phone_number", "") or "").strip()
        activation_id = str(getattr(result, "activation_id", "") or "").strip()

        if not phone_number:
            raise RuntimeError("接码取号失败: 无可用的号码")
        self.log(f"  已获取手机号: {phone_number}")
        self.log(f"  Activation ID: {activation_id}")
        self.result["phone_number"] = phone_number
        self.result["activation_id"] = activation_id
        self.result["steps"].append("get_phone_number")

        return phone_number
    def step_hybrid_register(self, phone_number: str) -> dict:

        """Hybrid 注册: Camoufox 完成 OpenAI 授权、密码、短信和 token 获取。"""
        self.log("=" * 60)
        self.log("Step 2: Hybrid 注册 ChatGPT")
        self.log("=" * 60)

        from platforms.chatgpt.browser_register import _start_browser_signin
        from platforms.chatgpt.phone_register import PWD_INPUT, SMS_INPUT, SUBMIT, continue_after_sms
        from platforms.chatgpt.utils import generate_random_password

        password = generate_random_password(16)
        self.result["password"] = password
        self.result["generated_chatgpt_password"] = password
        max_retries = int(self.config.get("phone_retry_limit", 6) or 6)

        for attempt in range(max_retries):
            phone_submitted = False
            if attempt > 0:
                self.log(f"  重试 #{attempt + 1}/{max_retries}...")

            try:
                prechecked = (
                    self._prechecked_phone_number == str(phone_number or "")
                    and self.page is not None
                    and self._registration_page_is_create_account_password()
                )
                if prechecked:
                    self.log(f"  [debug] attempt={attempt + 1}/{max_retries} 使用预检后的 create-account/password 页面 phone={self.result.get('phone_number')} activation={self.result.get('activation_id')} proxy={self.config.get('proxy')} exit_ip={self.result.get('registration_proxy_exit_ip')}")
                else:
                    device_id, csrf = self.step_prepare_registration_environment(reset_auth=False)
                    self.log(f"  [debug] attempt={attempt + 1}/{max_retries} phone={self.result.get('phone_number')} activation={self.result.get('activation_id')} proxy={self.config.get('proxy')} exit_ip={self.result.get('registration_proxy_exit_ip')}")
                    redirect_url = _start_browser_signin(self.page, phone_number, device_id, csrf, screen_hint=str(self.config.get("registration_screen_hint") or "signup"))
                    self._clear_registration_auth_state()
                    self.log(f"  [debug] signin redirect={'yes' if redirect_url else 'empty'} url={str(redirect_url)[:180]}")
                    phone_submitted = True
                    if not redirect_url:
                        raise RuntimeError("browser signin failed: no redirect URL")
                    self.page.goto(redirect_url, wait_until="domcontentloaded", timeout=45000)
                    self._debug_page_state("after-auth-redirect-goto")
                    self._wait_for_auth_password_state()

                existing_phone, existing_detail = self._registration_page_indicates_existing_phone()
                if existing_phone:
                    if self._config_bool("force_signup_from_login_password", False):
                        self.log("  强制注册模式: 不点击 signup，直接在登录密码页输入随机密码")
                        existing_phone = False
                        existing_detail = ""
                    elif self._force_signup_from_login_password():
                        existing_phone = False
                        existing_detail = ""
                    else:
                        phone_submitted = True
                        raise RuntimeError(f"PHONE_ALREADY_REGISTERED: {existing_detail}")
                if not self._registration_page_accepts_password():
                    raise RuntimeError(f"unexpected auth step before password entry: url={str(self.page.url or '')[:160]}")
                phone_submitted = True
                browser_cookies = self.browser_context.cookies()
                self.log(f"  授权态 cookies: {len(browser_cookies)}")

                password_visible = False
                try:
                    self.page.locator(PWD_INPUT).first.wait_for(state="visible", timeout=8000)
                    password_visible = True
                except Exception:
                    password_visible = False

                if password_visible:
                    self.log("  浏览器已进入密码步骤，使用 Camoufox 提交密码并等待短信。")
                    password_input = None
                    try:
                        input_count = self.page.locator(PWD_INPUT).count()
                    except Exception:
                        input_count = 1
                    for input_index in range(max(1, input_count)):
                        candidate = self.page.locator(PWD_INPUT).nth(input_index)
                        try:
                            if candidate.is_visible(timeout=1200):
                                password_input = candidate
                                break
                        except Exception:
                            continue
                    if password_input is None:
                        raise RuntimeError("no visible password input")
                    password_input.click(timeout=4000)
                    time.sleep(0.2)
                    try:
                        password_input.fill("")
                    except Exception:
                        pass
                    password_input.type(password, delay=75)
                    time.sleep(0.5)
                    try:
                        filled_len = len(str(password_input.input_value(timeout=1000) or ""))
                    except Exception:
                        filled_len = 0
                    self.log(f"  密码输入完成: len={filled_len} inputs={input_count}")
                    if filled_len < 8:
                        raise RuntimeError("visible password input did not accept typed value")

                    submit_error = ""
                    for submit_attempt in range(3):
                        try:
                            submit_button = None
                            try:
                                button_count = self.page.locator(SUBMIT).count()
                            except Exception:
                                button_count = 1
                            for button_index in range(max(1, button_count)):
                                candidate = self.page.locator(SUBMIT).nth(button_index)
                                try:
                                    if candidate.is_visible(timeout=1000) and candidate.is_enabled():
                                        submit_button = candidate
                                        break
                                except Exception:
                                    continue
                            if submit_button is None:
                                submit_button = self.page.locator("button:has-text('続行'), button:has-text('Continue')").first
                                submit_button.wait_for(state="visible", timeout=4000)
                            submitted_by = "locator.click"
                            submit_button.scroll_into_view_if_needed(timeout=3000)
                            try:
                                box = submit_button.bounding_box(timeout=3000)
                            except Exception:
                                box = None
                            if box:
                                self.page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=8)
                                time.sleep(0.15)
                                self.page.mouse.down()
                                time.sleep(0.08)
                                self.page.mouse.up()
                                submitted_by = "mouse.click"
                            else:
                                submit_button.click(timeout=8000)
                            self.log(f"  密码提交触发: {submitted_by}")
                        except Exception as exc:
                            submit_error = str(exc)
                            self.log(f"  密码提交普通点击失败，尝试回车/JS: {exc}")
                            try:
                                password_input.press("Enter", timeout=3000)
                            except Exception:
                                submitted = self.page.evaluate(
                                    """
                                    () => {
                                        const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
                                        const buttons = Array.from(document.querySelectorAll('button'));
                                        const button = buttons.find(b => visible(b) && !b.disabled && b.getAttribute('aria-disabled') !== 'true' && /続行|continue|continuar/i.test((b.innerText || b.textContent || '').trim()))
                                            || buttons.find(b => visible(b) && !b.disabled && b.getAttribute('aria-disabled') !== 'true' && b.type === 'submit');
                                        if (button) { button.scrollIntoView({block: 'center'}); button.click(); return 'button.click'; }
                                        const input = Array.from(document.querySelectorAll('input[type="password"]')).find(visible);
                                        const form = input?.form || input?.closest?.('form') || document.querySelector('form');
                                        if (form?.requestSubmit) { form.requestSubmit(); return 'requestSubmit'; }
                                        return '';
                                    }
                                    """
                                )
                                if not submitted:
                                    raise RuntimeError("password submit unavailable") from exc
                                self.log(f"  密码提交触发: {submitted}")
                        deadline_transition = time.time() + 18
                        body_after_submit = ""
                        while time.time() < deadline_transition:
                            current_url = str(self.page.url or "")
                            try:
                                body_after_submit = self.page.evaluate("() => (document.body?.innerText || '').slice(0, 500)")
                            except Exception:
                                body_after_submit = ""
                            body_lower = body_after_submit.lower()
                            if "contact-verification" in current_url:
                                break
                            if (
                                "incorrect phone number or password" in body_lower
                                or "incorrect phone" in body_lower
                                or "phone number or password" in body_lower
                                or "já existe uma conta para este número de telefone" in body_lower
                                or "ja existe uma conta para este numero de telefone" in body_lower
                                or "existe uma conta para este número" in body_lower
                                or "existe uma conta para este numero" in body_lower
                            ):
                                if self._config_bool("force_signup_from_login_password", False):
                                    submit_error = "login-password page rejected random password"
                                    break
                                raise RuntimeError("PHONE_ALREADY_REGISTERED: OpenAI says this phone already has an account")
                            time.sleep(1)
                        if "contact-verification" in str(self.page.url or ""):
                            break
                        submit_error = f"password submit stayed on {str(self.page.url)[:120]} body={body_after_submit[:200]}"
                        self.log(f"  密码提交未转场，重试 {submit_attempt + 1}/3: {submit_error[:180]}")
                    if "contact-verification" not in str(self.page.url or ""):
                        raise RuntimeError(submit_error or f"password submit did not reach contact-verification: url={str(self.page.url)[:120]}")

                    time.sleep(1.5)
                    used_markers = (
                        "incorrect phone number or password",
                        "incorrect phone",
                        "phone number or password",
                        "already registered",
                        "already used",
                        "já existe uma conta para este número de telefone",
                        "ja existe uma conta para este numero de telefone",
                        "existe uma conta para este número",
                        "existe uma conta para este numero",
                        "账号已被使用",
                    )
                    body = ""
                    account_used = False
                    deadline = time.time() + 25
                    while time.time() < deadline:
                        try:
                            body = self.page.evaluate("() => (document.body?.innerText || '').slice(0, 800)")
                        except Exception:
                            body = ""
                        if any(marker in body.lower() for marker in used_markers):
                            account_used = True
                            break
                        if "contact-verification" in str(self.page.url or "") or "code" in body.lower() or "código" in body.lower():
                            break
                        time.sleep(1)
                    if account_used:
                        self.log("  该手机号已被使用，复用当前 IP/窗口换号重试")
                        self._mark_current_phone_bad("account used")
                        if attempt < max_retries - 1:
                            phone_number = self.step_get_phone_number()
                        continue
                    sms_send_ok, sms_send_detail = self._ensure_phone_sms_send_clicked()
                    self.log(f"  发送短信确认: ok={sms_send_ok} detail={sms_send_detail[:180]}")
                    if not sms_send_ok:
                        raise RuntimeError(f"OpenAI 未确认发送短信: {sms_send_detail}")
                    self._activation_pool_block_current("OpenAI requested SMS", post_send=True)

                    activation_id = self.result.get("activation_id", "")
                    first_delay = self._config_int("sms_first_poll_delay", 12, minimum=0, maximum=120)
                    poll_interval = self._config_int("sms_poll_interval", 5, minimum=2, maximum=30)
                    sms_timeout = self._config_int("sms_code_timeout", 600, minimum=120, maximum=1800)
                    if first_delay:
                        self.log(f"  OpenAI 已请求短信，延迟 {first_delay}s 后开始接码轮询，避免旧状态误判")
                        time.sleep(first_delay)
                    browser_result = None
                    sms_deadline = time.time() + sms_timeout
                    while time.time() < sms_deadline:
                        remaining = max(poll_interval, int(sms_deadline - time.time()))
                        code = self._wait_for_sms_code(activation_id, timeout=remaining, poll_interval=poll_interval)

                        if not code:
                            reason = self._last_sms_wait_status or "sms timeout"
                            self.log(f"  SMS 等待结束但未拿到可用验证码: {reason}；保留已提交密码的账号记录")
                            break

                        self._debug_page_state("before-continue-after-sms")
                        browser_result = continue_after_sms(self.page, code, log=self.log)
                        if browser_result.success and browser_result.access_token:
                            self._activation_pool_mark_completed(str(activation_id), "registration succeeded")
                            break
                        error_text = browser_result.error or "access token missing"
                        lowered_error = error_text.lower()
                        if any(marker in lowered_error for marker in ("invalid", "incorrect", "验证码", "verification code", "sms code", "otp")):
                            self.log(f"  SMS 验证码被页面拒绝，标记旧码并继续等待新码: {error_text[:180]}")
                            marker = getattr(self.sms_provider, "mark_code_failed", None)
                            if callable(marker):
                                marker(activation_id, error_text)
                            continue
                        raise RuntimeError(f"browser SMS completion failed: {error_text}")
                    if browser_result is None or not browser_result.success or not browser_result.access_token:
                        timeout_reason = (browser_result.error if browser_result else "sms timeout after password submitted") or "access token missing"
                        self._set_failure("sms_code_pending", step="sms_code", reason=timeout_reason, retryable=True, next_action="该手机号可能已被 OpenAI 创建/占用；用失败记录里的 phone/password 后续人工登录或继续验证码流程")
                        self._activation_pool_block_current("sms timeout after password submitted", post_send=True)
                        raise RuntimeError(timeout_reason)

                    access_token = browser_result.access_token
                    self.result["access_token"] = access_token
                    self.result["chatgpt_access_token_initial"] = access_token
                    self.result["account_id"] = browser_result.account_id or ""
                    self.result["email"] = browser_result.email or ""
                    self.result["plan_type"] = browser_result.plan_type or "free"
                    self.result["password"] = password
                    self.result["session_token"] = ""
                    self.result["steps"].append("hybrid_browser_password_register")
                    self.log(f"  Access Token: {access_token[:50]}...")
                    return {"access_token": access_token, "password": password, "session_token": ""}

                try:
                    body = self.page.evaluate("() => (document.body?.innerText || '').slice(0, 500)")
                except Exception:
                    body = ""
                raise RuntimeError(f"browser did not reach password step: url={str(self.page.url)[:120]} body={body[:200]}")
            except Exception as exc:
                error_text = str(exc)
                if self.result.get("status") == "sms_code_pending" or self.result.get("failed_step") == "sms_code":
                    self.log(f"  密码已提交但短信未完成，当前号进入本地黑名单并换号重试: {error_text}")
                    activation_id = str(self.result.get("activation_id") or "").strip()
                    phone_digits = "".join(ch for ch in str(self.result.get("phone_number") or "") if ch.isdigit())
                    if activation_id:
                        self._bad_activation_ids.add(activation_id)
                    if phone_digits and phone_digits not in self._bad_phone_exceptions:
                        self._bad_phone_exceptions.append(phone_digits)
                    self.result.setdefault("sms_timeout_handoffs", []).append({"phone_number": self.result.get("phone_number"), "activation_id": activation_id, "password": self.result.get("password"), "reason": error_text})
                    if attempt >= max_retries - 1:
                        self.log("  已达到换号重试上限，保留最后一个超时号供人工晚到码处理")
                        raise
                    self.result["status"] = "retrying_phone"
                    self.result["failed_step"] = ""
                    self.result["failure_reason"] = ""
                    self.result["retryable"] = False
                    self.result["next_action"] = ""
                    self._clear_registration_auth_state()
                    phone_number = self.step_get_phone_number()
                    continue
                phone_is_terminal = phone_submitted and self._is_phone_retryable_registration_error(error_text)
                if phone_is_terminal:
                    self.log(f"  本次手机号不可用，复用当前 IP/窗口换号: {error_text}")
                    self._mark_current_phone_bad(error_text)
                    if attempt < max_retries - 1:
                        phone_number = self.step_get_phone_number()
                else:
                    self.log(f"  注册环境/代理异常，关闭窗口并准备换 IP: {error_text}")
                    self._cleanup()
                    self._clear_registration_auth_state()
                    if attempt >= max_retries - 1:
                        raise
                    continue
                if attempt >= max_retries - 1:
                    raise
        raise RuntimeError("Hybrid 注册失败: 多次换号后仍未成功")

    def _wait_for_sms_code(self, activation_id: str, *, timeout: int = 180, poll_interval: float = 3.0) -> str:
        self._last_sms_wait_status = "timeout"
        if not activation_id:
            self._last_sms_wait_status = "missing_activation"
            return ""
        waiter = getattr(self.sms_provider, "wait_for_code", None)
        if callable(waiter):
            result = waiter(activation_id, timeout=timeout, poll_interval=int(poll_interval))
            if isinstance(result, dict):
                code = str(result.get("code") or "").strip()
                if code:
                    self.log(f"  SMS: {code}")
                    return code

            if not self._config_bool("herosms_cancel_on_timeout", False):
                self._activation_pool_block_current("sms timeout after password submitted", post_send=True)
                stop_reuse = getattr(self.sms_provider, "_stop_reuse", None)
                if callable(stop_reuse):
                    stop_reuse("sms timeout after password submitted")
                self.log(f"  HeroSMS 未收到验证码，保留 activation 等待晚到码但停止本地复用: {activation_id}")
                return ""
            try:
                if self.sms_provider.cancel_activation(activation_id):
                    self._activation_pool_mark_released(activation_id, "sms timeout cancel")
                self.log(f"  HeroSMS 未收到验证码，已取消 activation: {activation_id}")
            except Exception as exc:
                self._activation_pool_block_current("sms timeout cancel failed", release_pending=True)
                self.log(f"  HeroSMS 未收到验证码，取消 activation 失败: {activation_id} {exc}")

        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self.sms_provider.get_status(activation_id)
            if not isinstance(status, dict):
                status = {"status": "unknown", "raw": str(status)}
            state = str(status.get("status") or "").strip().lower()
            if state == "cancel":
                self._last_sms_wait_status = "cancel"
                self.log(f"  HeroSMS activation 已释放/取消: {activation_id}")
                return ""
            if state not in {"wait_code", "wait_retry", "wait_resend", "ok"}:
                self._last_sms_wait_status = "unknown"
                self.log(f"  HeroSMS 状态异常: {activation_id} {status}")
                return ""
            code = status.get("code", "")
            if code and str(code).strip():
                code = str(code).strip()
                self.log(f"  SMS: {code}")
                return code
            time.sleep(poll_interval)
        if not self._config_bool("herosms_cancel_on_timeout", False):
            self._activation_pool_block_current("sms timeout after password submitted", post_send=True)
            stop_reuse = getattr(self.sms_provider, "_stop_reuse", None)
            if callable(stop_reuse):
                stop_reuse("sms timeout after password submitted")
            self.log(f"  HeroSMS 未收到验证码，保留 activation 等待晚到码但停止本地复用: {activation_id}")
            return ""
        try:
            if self.sms_provider.cancel_activation(activation_id):
                self._activation_pool_mark_released(activation_id, "sms timeout cancel")
            self.log(f"  HeroSMS 未收到验证码，已取消 activation: {activation_id}")
        except Exception as exc:
            self._activation_pool_block_current("sms timeout cancel failed", release_pending=True)
            self.log(f"  HeroSMS 未收到验证码，取消 activation 失败: {activation_id} {exc}")
        return ""

    def _sync_protocol_cookies_to_browser(self, reg) -> None:
        if not self.browser_context:
            return
        cookies = []
        for cookie in reg.s.cookies.jar:
            domain = cookie.domain or ".chatgpt.com"
            item = {
                "name": cookie.name,
                "value": cookie.value,
                "domain": domain,
                "path": cookie.path or "/",
                "httpOnly": bool(getattr(cookie, "_rest", {}).get("HttpOnly") or getattr(cookie, "_rest", {}).get("httponly")),
                "secure": bool(cookie.secure),
                "sameSite": "Lax",
            }
            if cookie.expires:
                item["expires"] = int(cookie.expires)
            cookies.append(item)
        if cookies:
            try:
                self.browser_context.add_cookies(cookies)
                self.log(f"  协议 cookies 已同步到浏览器: {len(cookies)}")
            except Exception as exc:
                self.log(f"  同步 cookies 到浏览器失败: {exc}")

    def step_browser_register(self, phone_number: str) -> dict:
        """注册 ChatGPT；默认走 hybrid，必要时可用 browser 旧路径。"""
        mode = str(self.config.get("browser_mode") or "hybrid").strip().lower()
        if mode in {"hybrid", "protocol"}:
            return self.step_hybrid_register(phone_number)
        self.log("=" * 60)
        self.log("Step 2: 浏览器注册 ChatGPT")
        self.log("=" * 60)

        headed = self.config.get("headed", False)
        use_camoufox = self.config.get("use_camoufox", True)

        if use_camoufox:
            self._launch_camoufox(headed=headed)
        else:
            self._launch_playwright(headed=headed)

        page = self.page

        try:
            # 生成密码
            from platforms.chatgpt.utils import generate_random_password
            password = generate_random_password()

            # 运行手机号注册
            from platforms.chatgpt.phone_register import phone_registration_flow, wait_for_sms_and_continue, PhoneResult

            country_code = self.config.get("country_code", "1")
            country_name = self.config.get("country_name", "United States")

            # 重试循环: 如果号码已注册，换号重试
            max_retries = 3
            for attempt in range(max_retries):
                if attempt > 0:
                    self.log(f"  重试 #{attempt+1}...")
                    phone_number = self.step_get_phone_number()
                
                result = phone_registration_flow(
                    page, phone_number, password,
                    country_code=country_code, country_name=country_name,
                    headed=headed, log=self.log,
                )
                
                if result.error == "PHONE_ALREADY_REGISTERED":
                    self.log(f"  号码已被注册，换号重试 ({attempt+1}/{max_retries})")
                    self._mark_current_phone_bad("already registered")
                    continue
                
                if not result.success and result.error == "AWAITING_SMS_CODE":
                    self.log("  等待 SMS 验证码...")
                    activation_id = self.result.get("activation_id", "")
                    result = wait_for_sms_and_continue(
                        page, self.sms_provider, activation_id,
                        timeout=120, log=self.log,
                    )
                break  # got result

            self.log(f"  结果: success={result.success} token={'YES' if result.access_token else 'NO'}")
            if not result.success:
                raise RuntimeError(f"注册失败: {result.error}")
            
            self.log(f"  Access Token: {result.access_token[:50]}...")
            self.result["access_token"] = result.access_token
            self.result["account_id"] = result.account_id
            self.result["email"] = result.email
            self.result["plan_type"] = result.plan_type
            self.result["password"] = password

            if self.config.get("save_session", True):
                self._save_session_json(result)

            self.result["steps"].append("browser_register")
            return {"access_token": result.access_token, "password": password}
        except Exception as e:
            self.log(f"  注册异常: {e}")
            raise

    # ------------------------------------------------------------------
    # Step 3: iceaix Plus 激活
    # ------------------------------------------------------------------
    def step_activate_plus(self, access_token: str) -> bool:
        """激活 ChatGPT Plus 试用。"""
        self.log("=" * 60)
        self.log("Step 3: 激活 Plus 试用")
        self.log("=" * 60)


        api_key = str(self.config.get("iceaix_api_key", "") or "").strip()
        paypal_phone = str(self.config.get("paypal_phone", "") or "").strip()
        sms_api = str(self.config.get("iceaix_sms_api", "") or "").strip()

        if api_key:
            if not paypal_phone:
                self._set_failure(
                    "paypal_phone_required",
                    step="activate_plus",
                    reason="配置了 iceaix_api_key 但缺少 paypal_phone",
                    retryable=False,
                    next_action="在 config.yaml 配置 paypal_phone",
                )
                return False
            if not sms_api:
                self._set_failure(
                    "paypal_sms_api_required",
                    step="activate_plus",
                    reason="配置了 iceaix_api_key 但缺少 iceaix_sms_api，不能宣称自动 Plus",
                    retryable=False,
                    next_action="配置 iceaix_sms_api，或改用 register-token 手动 Plus 流程",
                )
                return False
            return self._activate_via_api(access_token, api_key, paypal_phone)
        return self._activate_via_cdk(access_token)

    def _activate_via_api(self, access_token: str, api_key: str, paypal_phone: str) -> bool:
        """通过 PPXY API 自动激活。"""
        from platforms.chatgpt.iceaix_client import (
            CreateJobRequest,
            IceAixClient,
            JobStatus,
        )

        client = IceAixClient(api_key=api_key, base_url=self.config.get("iceaix_base_url", "https://plus.iceaix.com"), verbose=True)
        sms_api = str(self.config.get("iceaix_sms_api") or "").strip()
        client_ref = f"gpt-register-{uuid.uuid4().hex[:12]}"

        try:
            account = client.get_account()
            self.log(f"  PPXY 余额: {account.quota_remaining}/{account.quota_total} reserved={account.quota_reserved} concurrency={account.concurrency_limit}")
            if not account.can_create_job:
                self._set_failure("iceaix_quota_insufficient", step="activate_plus", reason=f"PPXY 额度不足: {account.quota_remaining}", retryable=False)
                return False
        except Exception as e:
            self._set_failure("iceaix_account_check_failed", step="activate_plus", reason=str(e), retryable=True, next_action="检查 iceaix API Key/网络后重试")
            self.log(f"  额度检查失败: {e}")
            return False

        try:
            trial = client.check_trial(access_token, proxy_jp=str(self.config.get("iceaix_proxy_jp") or ""))
            self.log(f"  试用资格: eligible={trial.eligible} status={trial.status} code={trial.result_code} | {trial.message}")
            if trial.blocked:
                self._set_failure("plus_trial_blocked", step="trial_check", reason=trial.message or trial.result_code or "trial blocked", retryable=False)
                return False
            if not trial.eligible and not self.config.get("iceaix_allow_paid_no_trial"):
                self._set_failure(
                    "plus_no_trial",
                    step="trial_check",
                    reason=trial.message or trial.result_code or "no trial eligibility",
                    retryable=False,
                    next_action="如确认接受非试用成本，设置 iceaix_allow_paid_no_trial=true 后重试",
                )
                return False
        except Exception as e:
            if self._config_bool("iceaix_allow_paid_no_trial") or self._config_bool("iceaix_continue_on_trial_check_error"):
                self.log(f"  试用检测异常: {e}，配置允许非试用付费，继续创建任务")
            else:
                self._set_failure(
                    "plus_trial_check_failed",
                    step="trial_check",
                    reason=str(e),
                    retryable=True,
                    next_action="检查 iceaix 试用检测接口/API Key/网络；如确认接受非试用成本，设置 iceaix_allow_paid_no_trial=true 后重试",
                )
                self.log(f"  试用检测异常，停止创建 PPXY 任务: {e}")
                return False
        request = CreateJobRequest(
            input_token=access_token,
            phone=paypal_phone,
            sms_api=sms_api,
            client_ref=client_ref,
            proxy=str(self.config.get("iceaix_proxy") or ""),
            proxy_jp=str(self.config.get("iceaix_proxy_jp") or ""),
            pplink_retry=self._config_int("iceaix_pplink_retry", 20, minimum=0),
            otp_timeout=self._config_int("iceaix_otp_timeout", 60, minimum=10),
        )

        max_job_attempts = self._config_int("iceaix_job_create_attempts", 3, minimum=1)
        final_job = None
        last_retryable_reason = ""
        for job_attempt in range(1, max_job_attempts + 1):
            if job_attempt > 1:
                self.log(f"  PPXY 临时失败重试 #{job_attempt}/{max_job_attempts}: {last_retryable_reason or '-'}")
                request.client_ref = f"gpt-register-{uuid.uuid4().hex[:12]}"
            try:
                job = client.create_job(request, idempotency_key=request.client_ref)
                self.result["iceaix_job_id"] = job.job_id
                self.result["iceaix_status"] = job.status.value
                self.result["iceaix_resource_mode"] = job.resource_mode
                if not job.job_id:
                    self._set_failure("iceaix_job_create_failed", step="activate_plus", reason="PPXY 未返回 job_id", retryable=True)
                    return False
                self.log(f"  PPXY 任务已创建: job_id={job.job_id} ref={request.client_ref} mode={job.resource_mode or '-'}")
                final_job = client.wait_for_job(
                    job.job_id,
                    timeout=self._config_int("iceaix_job_timeout", 300, minimum=30),
                    poll_interval=self._config_int("iceaix_poll_interval", 3, minimum=1),
                )
            except Exception as e:
                self._set_failure("iceaix_job_error", step="activate_plus", reason=str(e), retryable=True, next_action="检查 PPXY 任务状态或稍后重试")
                self.log(f"  PPXY 任务异常: {e}")
                return False

            self.result["iceaix_job_id"] = final_job.job_id or self.result.get("iceaix_job_id", "")
            self.result["iceaix_status"] = final_job.status.value
            self.result["iceaix_result_code"] = final_job.result_code
            self.result["iceaix_error_message"] = final_job.error_message
            self.result["iceaix_billing_status"] = final_job.billing_status
            self.result["iceaix_resource_mode"] = final_job.resource_mode or self.result.get("iceaix_resource_mode", "")

            if final_job.otp_pending or final_job.status == JobStatus.OTP_PENDING:
                self._set_failure(
                    "paypal_otp_pending",
                    step="activate_plus",
                    reason="PPXY 任务等待 PayPal OTP，iceaix_sms_api 没有及时返回新验证码",
                    retryable=True,
                    next_action="确认 PayPal 接码 API 是否为该手机号的新验证码订单后重试",
                )
                self.log(f"  PPXY 任务等待 OTP: job_id={final_job.job_id}")
                return False

            if final_job.is_success or str(final_job.result_code or "").upper() == "ALREADY_PAID":
                self.log(f"  Plus 激活任务成功! job_id={final_job.job_id} code={final_job.result_code or final_job.status.value}")
                self.result["steps"].append("activate_plus")
                return True

            code = str(final_job.result_code or final_job.status.value or "failed").lower()
            error_text = str(final_job.error_message or "")
            transient_link_failure = "开通链接准备失败" in error_text or "稍后重试" in error_text or "retry" in error_text.lower()
            retryable = code in {"timeout", "internal_error", "queued", "running"} or transient_link_failure
            if retryable and job_attempt < max_job_attempts:
                last_retryable_reason = error_text or final_job.result_code or final_job.status.value
                continue
            break

        if final_job is None:
            self._set_failure("iceaix_job_error", step="activate_plus", reason="PPXY 未返回最终任务状态", retryable=True, next_action="稍后重试")
            return False

        code = str(final_job.result_code or final_job.status.value or "failed").lower()
        error_text = str(final_job.error_message or "")
        oas_recoverable = str(final_job.error_message or final_job.result_code or "").upper() == "OAS_ERROR"
        retryable = code in {"timeout", "internal_error", "queued", "running"} or transient_link_failure or oas_recoverable
        next_action = "稍后用 --step activate --resume-file <registered_account.json> 重试 Plus 激活；OAS_ERROR 可能需等待 PPXY 多地区/账单国家更新" if retryable else ""
        status = {
            "no_trial": "plus_failed_no_trial",
            "blocked": "plus_failed_blocked",
            "invalid_input": "plus_failed_invalid_input",
            "timeout": "plus_failed_timeout",
            "internal_error": "plus_failed_internal",
        }.get(code, "plus_failed")
        self._set_failure(status, step="activate_plus", reason=final_job.error_message or final_job.result_code or final_job.status.value, retryable=retryable, next_action=next_action)
        self.log(f"  Plus 激活失败: {final_job.status.value} | {final_job.result_code} | {final_job.error_message}")
        return False

    def _activate_via_cdk(self, access_token: str) -> bool:
        """CDK 手动激活模式。"""
        from platforms.chatgpt.iceaix_client import manual_cdk_activation_required

        msg = manual_cdk_activation_required(access_token)
        print(msg)
        # 非交互环境无法完成 CDK 手动步骤
        if not sys.stdin.isatty():
            self.log("[CDK] 非交互模式，无法完成手动激活")
            self.result["steps"].append("activate_plus_cdk_skipped")
            return False

        self.log("等待手动激活完成...")
        try:
            response = input("  按 Enter 继续 (q 退出): ").strip()
            if response.lower() == "q":
                return False
        except (EOFError, OSError):
            self.log("[CDK] 无法读取输入，跳过")
            self.result["steps"].append("activate_plus_cdk_skipped")
            return False

        self.result["steps"].append("activate_plus_cdk_pending")
        return False

    # ------------------------------------------------------------------
    # Step 4: OAuth 绑定邮箱 + 获取 refresh_token
    # ------------------------------------------------------------------

    def step_oauth_bind_email(self) -> dict:
        """通过 OAuth PKCE 绑定 Outlook 邮箱，获取 refresh_token。"""
        self.log("=" * 60)
        self.log("Step 4: OAuth 绑定邮箱 + 获取 refresh_token")
        self.log("=" * 60)

        outlook_email = self.config.get("outlook_email", "")
        if not outlook_email:
            self.log("  未配置 outlook_email，跳过 OAuth 绑定")
            self.result["steps"].append("oauth_skipped")
            return {}

        from platforms.chatgpt.oauth_client import OAuthClient

        oauth = OAuthClient(
            config=self.config,
            proxy=self.config.get("proxy", ""),
            verbose=True,
            browser_mode=self.config.get("browser_mode", "protocol"),
        )

        # 承接前序浏览器的 session/cookies
        if self.page:
            try:
                cookies = self.result.get("session_cookies", {})
                oauth.adopt_browser_context(
                    session=None,  # oauth_client 会创建自己的 session
                    device_id=str(uuid.uuid4()),
                )
            except Exception as e:
                self.log(f"  承接浏览器上下文失败: {e}")

        # 执行 login_and_get_tokens (登录已注册账号 → OAuth → 绑定邮箱 → 获取 refresh_token)
        password = self.result.get("password", "")
        result = oauth.login_and_get_tokens(
            email=self.result.get("email") or outlook_email,
            password=password,
            device_id=str(uuid.uuid4()),
            complete_about_you_if_needed=True,
            allow_phone_verification=False,  # 已经注册完，不需要再验证
            screen_hint="login",
        )

        if result and result.get("access_token"):
            self.log(f"  OAuth 成功! Refresh Token: {str(result.get('refresh_token', ''))[:30]}...")
            self.result["refresh_token"] = result.get("refresh_token", "")
            self.result["access_token"] = result.get("access_token", "")  # 更新为 OAuth token
            self.result["id_token"] = result.get("id_token", "")
            self.result["account_id"] = result.get("account_id") or self.result.get("account_id", "")
            self.result["email"] = result.get("email") or self.result.get("email") or outlook_email

            if self.config.get("save_tokens", True):
                self._save_tokens_json(result)

            self.result["steps"].append("oauth_bind_email")
            return result
        else:
            self.log("  OAuth 失败")
            return {}

    def _oauth_callback_mode(self) -> str:
        return str(self.config.get("oauth_callback_mode") or "local").strip().lower()

    def _request_cpa_codex_auth_url(self) -> str:
        import requests

        base_url = str(self.config.get("cpa_base_url") or os.getenv("CPA_BASE_URL") or "").strip().rstrip("/")
        management_key = str(self.config.get("cpa_management_key") or os.getenv("CPA_MANAGEMENT_KEY") or "").strip()
        if not base_url:
            raise RuntimeError("CPA 绑定模式缺少 cpa_base_url/CPA_BASE_URL")
        if not management_key:
            raise RuntimeError("CPA 绑定模式缺少 cpa_management_key/CPA_MANAGEMENT_KEY")
        session = requests.Session()
        session.trust_env = False
        response = session.get(
            f"{base_url}/v0/management/codex-auth-url",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {management_key}",
                "X-Management-Key": management_key,
            },
            timeout=20,
            verify=False,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"CPA codex-auth-url 失败: status={response.status_code} body={(response.text or '')[:300]}")
        data = response.json()
        authorize_url = str(data.get("url") or data.get("auth_url") or data.get("authUrl") or (data.get("data") or {}).get("url") or (data.get("data") or {}).get("auth_url") or (data.get("data") or {}).get("authUrl") or "").strip()
        if not authorize_url.startswith("http"):
            raise RuntimeError(f"CPA codex-auth-url 未返回有效 URL: {str(data)[:300]}")
        self.log(f"  CPA authorize URL 已获取: {authorize_url[:100]}...")
        return authorize_url

    def _submit_cpa_oauth_callback(self, callback_url: str, oauth_start, proxy: str | None) -> dict:
        import requests

        base_url = str(self.config.get("cpa_base_url") or os.getenv("CPA_BASE_URL") or "").strip().rstrip("/")
        management_key = str(self.config.get("cpa_management_key") or os.getenv("CPA_MANAGEMENT_KEY") or "").strip()
        if not base_url or not management_key:
            raise RuntimeError("CPA 绑定模式缺少 cpa_base_url 或 cpa_management_key")
        session = requests.Session()
        session.trust_env = False
        response = session.post(
            f"{base_url}/v0/management/oauth-callback",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {management_key}",
                "X-Management-Key": management_key,
            },
            json={"provider": "codex", "redirect_url": callback_url},
            timeout=30,
            verify=False,
        )
        body = response.text or ""
        self.result["cpa_callback_url"] = callback_url
        self.result["cpa_submit_status"] = int(response.status_code)
        self.result["cpa_submit_body"] = body[:1000]
        if response.status_code >= 400:
            raise RuntimeError(f"CPA oauth-callback 失败: status={response.status_code} body={body[:300]}")
        self.log(f"  CPA oauth-callback 已提交: status={response.status_code}")
        return {
            "type": "codex_cpa",
            "cpa_submitted": True,
            "cpa_status": int(response.status_code),
            "cpa_body": body[:1000],
            "callback_url": callback_url,
        }

    def _resume_oauth_browser_config(self, *, headed: bool, storage_state_path: str) -> dict[str, Any]:
        session_config = dict(self.config)
        locale, timezone_id = self._browser_locale_for_proxy_region()
        browser_locale = str(session_config.get("browser_locale") or session_config.get("locale") or locale or "").strip()
        browser_timezone = str(session_config.get("browser_timezone") or session_config.get("timezone_id") or timezone_id or "").strip()
        session_config["headed"] = bool(headed)
        session_config["_log_fn"] = self.log
        if browser_locale:
            session_config["browser_locale"] = browser_locale
        if browser_timezone:
            session_config["browser_timezone"] = browser_timezone
        if storage_state_path:
            session_config["browser_storage_state_path"] = storage_state_path
            session_config["_browser_storage_state"] = storage_state_path
        else:
            session_config.pop("browser_storage_state_path", None)
            session_config.pop("_browser_storage_state", None)
        engine = str(
            session_config.get("browser_engine")
            or ("camoufox" if session_config.get("use_camoufox", True) else "playwright")
        ).strip().lower()
        profile_mode = str(session_config.get("browser_profile_mode") or "").strip().lower()
        profile_dir = str(session_config.get("browser_profile_dir") or "").strip()
        if engine == "patchright" and profile_mode == "per_task" and not profile_dir:
            profile_key = (
                str(self.result.get("resume_id") or self.result.get("task_id") or "").strip()
                or (Path(storage_state_path).stem if storage_state_path else f"resume_{uuid.uuid4().hex[:12]}")
            )
            profile_dir = str(Path("data") / "browser_profiles" / "patchright" / profile_key)
            session_config["browser_profile_dir"] = profile_dir
        return session_config

    def _select_resume_oauth_proxy_for_session(self) -> None:
        verified_proxy = str(self.result.get("subscription_check_proxy") or "").strip()
        saved_proxy = str(self.result.get("registration_proxy") or "").strip()
        if verified_proxy and "127.0.0.1" not in verified_proxy and "localhost" not in verified_proxy:
            ok, exit_ip = self._check_lajiao_proxy(verified_proxy)
            if ok:
                self.config["proxy"] = verified_proxy
                if exit_ip:
                    self.config["_camoufox_geoip_ip"] = exit_ip
                self.log(f"  优先使用刚通过实时订阅检查且 OpenAI 探针通过的代理执行 resume-oauth: {verified_proxy}")
                if saved_proxy and saved_proxy != verified_proxy:
                    self.log(f"  交接 registration_proxy 与当前可用代理不同，跳过旧代理: {saved_proxy}")
                return
            self.log(f"  实时订阅代理 OpenAI 探针失败，不用于 resume-oauth: {verified_proxy}")
        if saved_proxy:
            if "127.0.0.1" in saved_proxy or "localhost" in saved_proxy:
                self.log(f"  交接代理是本地环境代理，丢弃并切换辣椒 HTTP 新 IP: {saved_proxy}")
                self._select_fresh_proxy_for_attempt()
            else:
                ok, exit_ip = self._check_lajiao_proxy(saved_proxy)
                if not ok:
                    self.log(f"  交接 registration_proxy OpenAI 探针失败，跳过旧代理: {saved_proxy}")
                    if self.config.get("rotate_proxy_each_attempt"):
                        self._select_fresh_proxy_for_attempt()
                    else:
                        raise RuntimeError(f"resume-oauth 代理 OpenAI 预检失败，请更换干净代理: {saved_proxy}")
                    return
                self.config["proxy"] = saved_proxy
                self.log(f"  优先复用注册成功辣椒 HTTP 代理: {saved_proxy}")
                if exit_ip:
                    self.result["registration_proxy_exit_ip"] = exit_ip
                    self.config["_camoufox_geoip_ip"] = exit_ip
                else:
                    saved_exit_ip = str(self.result.get("registration_proxy_exit_ip") or "").strip()
                    if saved_exit_ip:
                        self.config["_camoufox_geoip_ip"] = saved_exit_ip
                self.log("  复用注册成功代理端口和保存的浏览器 session；已通过 OpenAI 探针")
        else:
            self.log("  交接文件没有 registration_proxy，使用当前 config proxy。")
            if self.config.get("rotate_proxy_each_attempt"):
                self._select_fresh_proxy_for_attempt()

    def step_oauth_from_saved_session(self, *, headed: bool = False) -> dict:
        """复用 register-token 保存的浏览器 session 执行 Codex OAuth。"""
        self.log("=" * 60)
        self.log("Step 4: 复用手机号账号 session 获取 refresh_token")
        self.log("=" * 60)

        login_identity = str(self.result.get("email") or self.result.get("phone_number") or "").strip()
        password = str(self.result.get("password") or "").strip()
        storage_state_path = str(self.result.get("browser_storage_state_path") or "").strip()
        if storage_state_path:
            if not Path(storage_state_path).exists():
                raise RuntimeError(f"浏览器 session 文件不存在: {storage_state_path}")
            self.result["browser_storage_state_path"] = storage_state_path
            self.config["_browser_storage_state"] = storage_state_path
        else:
            self.result.pop("browser_storage_state_path", None)
            self.config.pop("_browser_storage_state", None)
            if not (login_identity and password):
                raise RuntimeError("交接文件缺少 browser_storage_state_path，且没有登录身份/密码，无法恢复 OAuth")
            self.log("  交接文件没有 browser_storage_state_path，回退为手机号/密码登录 OAuth；不重新注册、不接码")
        self._select_resume_oauth_proxy_for_session()

        session_config = self._resume_oauth_browser_config(
            headed=headed,
            storage_state_path=storage_state_path,
        )
        profile_dir = str(session_config.get("browser_profile_dir") or "").strip()
        if profile_dir:
            self.config["browser_profile_dir"] = profile_dir
        self.log(
            f"  resume-oauth 浏览器入口切换为 BrowserSession: engine={session_config.get('browser_engine') or ('camoufox' if session_config.get('use_camoufox', True) else 'playwright')}"
        )

        from platforms.chatgpt.browser_register import _get_cookies
        from registration.patch_resume_bind import ResumeOAuthProxyChallenge, run_patch_resume_bind
        from registration.phone_bind import create_binding_phone_callback

        callback_mode = self._oauth_callback_mode()
        if callback_mode not in {"local", "cpa"}:
            raise RuntimeError(f"未知 oauth_callback_mode: {callback_mode}")
        if callback_mode == "cpa":
            self.log("  OAuth callback 模式: CPA；本地只提交 callback URL，不换 refresh_token")

        phone_callback, phone_cleanup = create_binding_phone_callback(self.config, log_fn=self.log)
        if phone_callback:
            self.log(f"  resume-oauth 已启用 add-phone 接码: provider={self.config.get('bind_sms_provider') or self.config.get('sms_provider')}")

        token_candidates = [] if callback_mode == "cpa" and not str(self.config.get("oauth_bind_email") or "").strip() else self._load_mailbox_binding_candidates()
        if not token_candidates and callback_mode != "cpa":
            token_candidates = self._load_outlook_token_candidates(str(self.config.get("outlook_email") or ""))
        if not token_candidates and callback_mode != "cpa":
            fallback_email, fallback_password = self._load_outlook_credentials(str(self.config.get("outlook_email") or ""))
            if fallback_email:
                token_candidates = [(fallback_email, fallback_password, "", "")]
        if not token_candidates and callback_mode == "cpa":
            token_candidates = [("", "", "", "")]
        if not token_candidates:
            raise RuntimeError("没有可用邮箱候选；请检查邮箱 Provider、订单文件或 used_outlook_emails 标记")
        if not password:
            password = str(self.result.get("password") or "").strip()

        resume_proxy_attempts = self._config_int("resume_oauth_proxy_attempts", 3, minimum=1)
        last_proxy_challenge = ""
        for resume_proxy_attempt in range(resume_proxy_attempts):
            if resume_proxy_attempt:
                if not self.config.get("rotate_proxy_each_attempt"):
                    break
                bad_exit_ip = str(self.config.get("_camoufox_geoip_ip") or self.result.get("registration_proxy_exit_ip") or "").strip()
                if bad_exit_ip:
                    self._used_proxy_ips.add(bad_exit_ip)
                self.log(f"  resume-oauth 命中 OpenAI/Cloudflare 验证，切换新代理重试: attempt={resume_proxy_attempt + 1}/{resume_proxy_attempts}")
                self._select_fresh_proxy_for_attempt()
                session_config = self._resume_oauth_browser_config(
                    headed=headed,
                    storage_state_path=storage_state_path,
                )
                profile_dir = str(session_config.get("browser_profile_dir") or "").strip()
                if profile_dir:
                    self.config["browser_profile_dir"] = profile_dir

            try:
                with BrowserSession(session_config) as browser_session:
                    self.browser = browser_session.browser
                    self.browser_context = browser_session.browser_context
                    self.page = browser_session.page
                    try:
                        page = browser_session.page
                        if page is None:
                            raise RuntimeError("浏览器未启动")

                        cookies = _get_cookies(page)
                        last_error = ""
                        attempted_this_run = 0
                        for bind_email, bind_password, token_client_id, token_refresh_token in token_candidates:
                            attempted_this_run += 1
                            max_attempts = self._config_int("outlook_max_attempts_per_run", 3, minimum=1)
                            if attempted_this_run > max_attempts:
                                last_error = f"本轮 Outlook 候选已达上限: {max_attempts}"
                                break
                            self.config["outlook_email"] = bind_email
                            if bind_password and not self.config.get("outlook_password"):
                                self.config["outlook_password"] = bind_password
                            self._oauth_bind_email = bind_email
                            self._oauth_bind_password = bind_password
                            self._oauth_bind_client_id = token_client_id
                            self._oauth_bind_refresh_token = token_refresh_token
                            self._record_outlook_pool_state(bind_email, "reserved", reason="oauth bind attempt")
                            self.log(f"  OAuth 绑定邮箱候选: {bind_email or '-'}")
                            self.log(f"  OAuth 登录身份使用已注册账号: {login_identity}")
                            cpa_authorize_url = self._request_cpa_codex_auth_url() if callback_mode == "cpa" else ""
                            callback_handler = self._submit_cpa_oauth_callback if callback_mode == "cpa" else None
                            resume_file = str(self.result.get("resume_file") or self.config.get("resume_file") or "").strip()
                            if not resume_file:
                                raise RuntimeError("resume-oauth 缺少 resume_file，无法执行 Patch resume-bind")
                            result = run_patch_resume_bind(
                                browser_session,
                                config=self.config,
                                resume_file=resume_file,
                                log_fn=self.log,
                                login_identity=login_identity,
                                password=password,
                                otp_callback=self._manual_email_otp_callback,
                                phone_callback=phone_callback,
                                proxy=self.config.get("proxy", "") or None,
                                bind_email=bind_email or "",
                                redirect_uri=str(self.config.get("oauth_redirect_uri") or "") or None,
                                client_id=str(self.config.get("oauth_client_id") or "") or None,
                                authorize_url=cpa_authorize_url or None,
                                callback_handler=callback_handler,
                            )

                            if result and result.get("error") == "EMAIL_ALREADY_USED":
                                self.log(f"  Outlook 邮箱已被使用，标记并换下一个: {bind_email}")
                                self._record_outlook_pool_state(bind_email, "dirty_email_already_used", reason="openai email already used")
                                self._mark_outlook_email_used(bind_email, "openai email already used")
                                last_error = str(result.get("text") or "EMAIL_ALREADY_USED")
                                cookies = _get_cookies(page)
                                continue

                            activation = getattr(phone_callback, "activation", None)
                            if activation:
                                binding_phone_number = str(getattr(activation, "phone_number", "") or "").strip()
                                binding_activation_id = str(getattr(activation, "activation_id", "") or "").strip()
                                if binding_phone_number:
                                    self.result["binding_phone_number"] = binding_phone_number
                                    self.result["phone_number"] = self.result.get("phone_number") or binding_phone_number
                                if binding_activation_id:
                                    self.result["binding_activation_id"] = binding_activation_id

                            if result and result.get("cpa_submitted"):
                                self.result["email"] = bind_email or self.result.get("email", "")
                                self.result["outlook_email"] = bind_email
                                self.result["steps"].append("resume_oauth_cpa")
                                self._record_outlook_pool_state(bind_email, "completed", reason="cpa callback submitted")
                                return result
                            if result and result.get("access_token"):
                                self.result["refresh_token"] = result.get("refresh_token", "")
                                self.result["access_token"] = result.get("access_token", "")
                                self.result["id_token"] = result.get("id_token", "")
                                self.result["account_id"] = result.get("account_id") or self.result.get("account_id", "")
                                self.result["email"] = result.get("email") or bind_email or self.result.get("email", "")
                                self.result["outlook_email"] = bind_email
                                plan_type = self._refresh_plan_type_from_subscription(prefer_live=True)
                                if not plan_type:
                                    self.log("  未能从 OAuth token 或实时订阅接口确认套餐")
                                self.result["steps"].append("resume_oauth_session")
                                if self.config.get("save_tokens", True):
                                    self._save_tokens_json(result)
                                return result
                            reason = "oauth did not return access_token"
                            self._record_outlook_pool_state(bind_email, "failed_retryable", reason=reason)
                            if self._outlook_retryable_failures(bind_email) >= self._config_int("outlook_failed_retryable_limit", 2, minimum=1):
                                self._record_outlook_pool_state(bind_email, "cooldown", reason=reason)
                            last_error = "复用 session 执行 OAuth 失败，未获取到 access_token"

                        if self.result.get("status") == "email_otp_required":
                            raise RuntimeError("OAuth 需要邮箱验证码；请用 --email-otp <code> 重新运行 resume-oauth")
                        raise RuntimeError(last_error or "复用 session 执行 OAuth 失败，未获取到 access_token")
                    finally:
                        self.page = None
                        self.browser_context = None
                        self.browser = None
            except ResumeOAuthProxyChallenge as exc:
                last_proxy_challenge = str(exc)
                self.log(f"  {last_proxy_challenge}")
                if self.config.get("rotate_proxy_each_attempt") and resume_proxy_attempt < resume_proxy_attempts - 1:
                    continue
                raise RuntimeError(last_proxy_challenge)
            finally:
                activation = getattr(phone_callback, "activation", None)
                if activation:
                    binding_phone_number = str(getattr(activation, "phone_number", "") or "").strip()
                    binding_activation_id = str(getattr(activation, "activation_id", "") or "").strip()
                    if binding_phone_number:
                        self.result["binding_phone_number"] = binding_phone_number
                    if binding_activation_id:
                        self.result["binding_activation_id"] = binding_activation_id
                    if bool(getattr(phone_callback, "completed", False)):
                        self.result["binding_phone_verified"] = True
                        if binding_phone_number:
                            self.result["phone_number"] = self.result.get("phone_number") or binding_phone_number
                try:
                    if callable(phone_cleanup):
                        phone_cleanup()
                finally:
                    self.page = None
                    self.browser_context = None
                    self.browser = None
        raise RuntimeError(last_proxy_challenge or "resume-oauth 代理重试耗尽")

    def _outlook_pool_state_path(self) -> Path:
        data_dir = Path("data")
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir / "outlook_pool_state.jsonl"

    def _record_outlook_pool_state(self, email_value: str, status: str, *, reason: str = "") -> None:
        normalized = str(email_value or "").strip().lower()
        if not normalized:
            return
        row = {
            "email": normalized,
            "status": status,
            "job_id": str(self.result.get("iceaix_job_id") or self.result.get("resume_file") or ""),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "last_error": reason,
        }
        with open(self._outlook_pool_state_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _used_outlook_emails_path(self) -> Path:
        data_dir = Path("data")
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir / "used_outlook_emails.txt"

    def _load_used_outlook_emails(self) -> set[str]:
        used: set[str] = set()
        path = self._used_outlook_emails_path()
        if path.exists():
            for row in path.read_text(encoding="utf-8").splitlines():
                email_value = row.strip().split("#", 1)[0].strip().lower()
                if email_value:
                    used.add(email_value)
        products_dir = self._products_dir()
        for product_file in products_dir.glob("*.json"):
            try:
                data = json.loads(product_file.read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            for key in ("email", "outlook_email"):
                email_value = str(data.get(key) or "").strip().lower()
                if email_value:
                    used.add(email_value)
        return used

    def _mark_outlook_email_used(self, email_value: str, reason: str) -> None:
        normalized = str(email_value or "").strip().lower()
        if not normalized:
            return
        used = self._load_used_outlook_emails()
        if normalized in used:
            return
        path = self._used_outlook_emails_path()
        timestamp = datetime.now().isoformat(timespec="seconds")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{normalized} # {timestamp} {reason}\n")
        self._record_outlook_pool_state(normalized, "consumed", reason=reason)


    def _load_outlook_pool_events(self) -> list[dict]:
        path = self._outlook_pool_state_path()
        if not path.exists():
            return []
        events: list[dict] = []
        for row in path.read_text(encoding="utf-8").splitlines():
            row = row.strip()
            if not row:
                continue
            try:
                event = json.loads(row)
            except Exception:
                continue
            if isinstance(event, dict):
                events.append(event)
        return events

    def _outlook_retryable_failures(self, email_value: str) -> int:
        normalized = str(email_value or "").strip().lower()
        if not normalized:
            return 0
        return sum(
            1
            for event in self._load_outlook_pool_events()
            if str(event.get("email") or "").strip().lower() == normalized and event.get("status") == "failed_retryable"
        )

    def _is_outlook_email_cooled_down(self, email_value: str) -> bool:
        normalized = str(email_value or "").strip().lower()
        if not normalized:
            return False
        cooldown_hours = self._config_int("outlook_cooldown_hours", 24, minimum=0)
        failure_limit = self._config_int("outlook_failed_retryable_limit", 2, minimum=1)
        if cooldown_hours <= 0:
            return False
        cutoff = datetime.now() - timedelta(hours=cooldown_hours)
        recent_retryable = 0
        for event in self._load_outlook_pool_events():
            if str(event.get("email") or "").strip().lower() != normalized:
                continue
            if event.get("status") in {"consumed", "dirty_email_already_used"}:
                return True
            if event.get("status") not in {"failed_retryable", "cooldown"}:
                continue
            raw_updated_at = str(event.get("updated_at") or "").strip()
            try:
                updated_at = datetime.fromisoformat(raw_updated_at)
            except Exception:
                updated_at = datetime.now()
            if updated_at >= cutoff:
                recent_retryable += 1
        return recent_retryable >= failure_limit

    def _mailbox_provider_key(self) -> str:
        return str(self.config.get("mailbox_provider") or "outlook_token").strip().lower()

    def _load_mailbox_binding_candidates(self) -> list[tuple[str, str, str, str]]:
        provider_key = self._mailbox_provider_key()
        if provider_key in {"forwarded_domain", "domain_forward", "imap_forward", "163_forward"}:
            from core.mailbox_providers import ForwardedDomainMailbox

            mailbox = ForwardedDomainMailbox.from_config(self.config)
            provider_name = "forwarded_domain"
        elif provider_key in {"icloud_api", "email_link_api", "link_api_mailbox"}:
            from core.mailbox_providers import LinkApiMailbox

            mailbox = LinkApiMailbox.from_config(self.config)
            provider_name = "icloud_api"
        elif provider_key in {"icloud_privacy", "icloud_hide_my_email", "hide_my_email"}:
            from core.mailbox_providers import ICloudPrivacyMailbox

            mailbox = ICloudPrivacyMailbox.from_config(self.config)
            provider_name = "icloud_privacy"
        elif provider_key in {"cfworker", "cfworker_admin_api", "cloud_mail"}:
            from core.mailbox_providers import CFWorkerMailbox

            mailbox = CFWorkerMailbox.from_config(self.config)
            provider_name = "cfworker_admin_api"
        else:
            return []
        account = mailbox.create_account()
        self._mailbox_provider = mailbox
        self._mailbox_account = account
        self._mailbox_before_ids = mailbox.get_current_ids(account)
        self.result["outlook_email"] = account.email
        self.result["email_provider"] = provider_name
        self.log(f"  邮箱 Provider 候选: {provider_name} {account.email}")
        return [(account.email, "", "", "")]

    def _mailbox_provider_otp_callback(self) -> str:
        mailbox = getattr(self, "_mailbox_provider", None)
        account = getattr(self, "_mailbox_account", None)
        if not mailbox or not account:
            return ""
        try:
            return mailbox.wait_for_code(account, timeout=self._config_int("email_otp_timeout", 300, minimum=60, maximum=1800), before_ids=getattr(self, "_mailbox_before_ids", set()))
        except Exception as exc:
            self.log(f"  邮箱 Provider 获取验证码失败: {exc}")
            return ""

    def _load_outlook_token_candidates(self, email: str = "") -> list[tuple[str, str, str, str]]:
        target = str(email or self.config.get("outlook_email") or "").strip().lower()
        configured_email = str(self.config.get("outlook_email") or "").strip()
        configured_password = str(self.config.get("outlook_password") or "").strip()
        configured_client_id = str(self.config.get("outlook_client_id") or "").strip()
        configured_refresh_token = str(self.config.get("outlook_refresh_token") or "").strip()
        used = self._load_used_outlook_emails()
        candidates: list[tuple[str, str, str, str]] = []
        if configured_email and configured_client_id and configured_refresh_token:
            configured_key = configured_email.lower()
            if (not target or configured_key == target) and configured_key not in used and not self._is_outlook_email_cooled_down(configured_key):
                candidates.append((configured_email, configured_password, configured_client_id, configured_refresh_token))

        order_paths = []
        configured_order = str(self.config.get("outlook_token_order_file") or "").strip()
        if configured_order:
            order_paths.append(Path(configured_order))
        order_paths.append(Path("outlook_accounts_token.txt"))
        seen: set[str] = set()
        for order_file in order_paths:
            if not order_file.exists():
                continue
            for raw_row in order_file.read_text(encoding="utf-8-sig").splitlines():
                row = raw_row.strip()
                if not row:
                    continue
                parts = [part.strip() for part in row.split("----")]
                if len(parts) != 4:
                    continue
                candidate_email, candidate_password, client_id, refresh_token = parts
                candidate_key = candidate_email.lower()
                if "@" not in candidate_email or not client_id or not refresh_token:
                    continue
                if candidate_key in seen or candidate_key in used or self._is_outlook_email_cooled_down(candidate_key):
                    continue
                if not target or candidate_key == target:
                    candidates.append((candidate_email, candidate_password, client_id, refresh_token))
                    seen.add(candidate_key)
        return candidates

    def _load_outlook_token_account(self, email: str = "") -> tuple[str, str, str, str]:
        candidates = self._load_outlook_token_candidates(email)
        return candidates[0] if candidates else ("", "", "", "")

    def _load_outlook_credentials(self, email: str = "") -> tuple[str, str]:
        token_email, token_password, _, _ = self._load_outlook_token_account(email)
        if token_email and token_password:
            return token_email, token_password

        target = str(email or self.config.get("outlook_email") or "").strip().lower()
        configured_email = str(self.config.get("outlook_email") or "").strip()
        configured_password = str(self.config.get("outlook_password") or "").strip()
        if configured_email and configured_password and (not target or configured_email.lower() == target):
            return configured_email, configured_password

        account_file = Path(self.config.get("outlook_accounts_file", "outlook_accounts.csv"))
        if account_file.exists():
            for row in account_file.read_text(encoding="utf-8").splitlines():
                if not row.strip() or "," not in row:
                    continue
                candidate_email, candidate_password = row.split(",", 1)
                candidate_email = candidate_email.strip()
                candidate_password = candidate_password.strip()
                if not candidate_email or not candidate_password:
                    continue
                if not target or candidate_email.lower() == target:
                    return candidate_email, candidate_password
        return configured_email, configured_password

    def _outlook_graph_otp_callback(self, email: str, client_id: str, refresh_token: str, *, timeout: int = 180) -> str:
        from core.mailbox.outlook_token import OutlookTokenAccount, OutlookTokenMailbox

        mailbox = OutlookTokenMailbox(self.config, log_fn=self.log)
        return mailbox.wait_for_openai_code(
            OutlookTokenAccount(email, "", client_id, refresh_token),
            timeout=timeout,
        )

    def _extract_openai_code_from_text(self, text: str) -> str:
        return extract_verification_code(text, expected_lengths=(6,))


    def _outlook_web_otp_callback(self, email: str, password: str, *, timeout: int = 180) -> str:
        if not self.browser_context:
            return ""
        seen_codes = set(str(code or "").strip() for code in getattr(self, "_seen_outlook_otp_codes", set()) if str(code or "").strip())
        page = self.browser_context.new_page()
        try:
            self.log(f"  打开 Outlook Web 自动读取验证码: {email}")
            page.goto("https://login.live.com/", wait_until="domcontentloaded", timeout=45000)
            for selector in ('input[type="email"]', 'input[name="loginfmt"]'):
                try:
                    page.locator(selector).first.fill(email, timeout=5000)
                    break
                except Exception:
                    continue
            for selector in ('input[type="submit"]', 'button[type="submit"]', 'button:has-text("Next")', 'button:has-text("下一步")'):
                try:
                    page.locator(selector).first.click(timeout=5000)
                    break
                except Exception:
                    continue
            time.sleep(2)
            for selector in ('input[type="password"]', 'input[name="passwd"]'):
                try:
                    page.locator(selector).first.fill(password, timeout=10000)
                    break
                except Exception:
                    continue
            for selector in ('input[type="submit"]', 'button[type="submit"]', 'button:has-text("Sign in")', 'button:has-text("登录")'):
                try:
                    page.locator(selector).first.click(timeout=5000)
                    break
                except Exception:
                    continue
            time.sleep(3)
            for selector in ('input[value="No"]', 'button:has-text("No")', 'button:has-text("否")', 'button:has-text("Não")'):
                try:
                    page.locator(selector).first.click(timeout=2500)
                    break
                except Exception:
                    continue
            try:
                page.goto("https://outlook.live.com/mail/0/", wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass

            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    body = page.locator("body").inner_text(timeout=5000)
                    code = self._extract_openai_code_from_text(body)
                    if code and code not in seen_codes:
                        seen_codes.add(code)
                        self._seen_outlook_otp_codes = seen_codes
                        self.log(f"  Outlook Web 获取验证码: {code}")
                        return code
                except Exception:
                    pass
                for selector in ('text=OpenAI', 'text=ChatGPT', 'text=Codex', 'text=verification', 'text=código'):
                    try:
                        page.locator(selector).first.click(timeout=1500)
                        break
                    except Exception:
                        continue
                time.sleep(5)
                try:
                    page.reload(wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    pass
            self.log("  Outlook Web 未读取到验证码")
            return ""
        finally:
            try:
                page.close()
            except Exception:
                pass

    def _manual_email_otp_callback(self) -> str:
        """OAuth 需要邮箱验证码时读取 CLI/config、Provider、Outlook Graph、Outlook Web 或由操作者手动输入。"""
        configured = str(self.config.get("email_otp") or "").strip()
        if configured:
            return configured
        email = str(getattr(self, "_oauth_bind_email", "") or self.config.get("outlook_email") or "").strip()
        password = str(getattr(self, "_oauth_bind_password", "") or self.config.get("outlook_password") or "").strip()
        client_id = str(getattr(self, "_oauth_bind_client_id", "") or self.config.get("outlook_client_id") or "").strip()
        refresh_token = str(getattr(self, "_oauth_bind_refresh_token", "") or self.config.get("outlook_refresh_token") or "").strip()
        if self._mailbox_provider_key() in {"icloud_api", "email_link_api", "link_api_mailbox"} and not getattr(self, "_mailbox_provider", None):
            login_email = str(self.result.get("email") or self.config.get("email") or "").strip()
            if login_email:
                try:
                    from core.mailbox_providers import LinkApiMailbox
                    self._mailbox_provider = LinkApiMailbox.from_config(self.config)
                    self._mailbox_account = self._mailbox_provider.account_for_email(login_email)
                    self._mailbox_before_ids = self._mailbox_provider.get_current_ids(self._mailbox_account)
                except Exception as exc:
                    self.log(f"  iCloud 登录邮箱 OTP 准备失败: {exc}")
        if self._mailbox_provider_key() in {"cfworker", "cfworker_admin_api", "cloud_mail", "forwarded_domain", "domain_forward", "imap_forward", "163_forward", "icloud_api", "email_link_api", "link_api_mailbox", "icloud_privacy", "icloud_hide_my_email", "hide_my_email"}:
            code = self._mailbox_provider_otp_callback()
            if code:
                return code
        if email and client_id and refresh_token:
            code = self._outlook_graph_otp_callback(email, client_id, refresh_token)
            if code:
                self._seen_outlook_otp_codes = set(getattr(self, "_seen_outlook_otp_codes", set())) | {code}
                return code
        if email and password and self.config.get("outlook_web_otp", True):
            code = self._outlook_web_otp_callback(email, password)
            if code:
                self._seen_outlook_otp_codes = set(getattr(self, "_seen_outlook_otp_codes", set())) | {code}
                return code
        if not sys.stdin or not sys.stdin.isatty():
            self.result["status"] = "email_otp_required"
            self.log("  OAuth 需要邮箱验证码，但当前运行环境没有交互 stdin。")
            return ""
        try:
            return input("请输入邮箱验证码: ").strip()
        except EOFError:
            self.result["status"] = "email_otp_required"
            return ""

    # ------------------------------------------------------------------
    # Step 5: 上传 Sub2API
    # ------------------------------------------------------------------

    def step_upload_sub2api(self) -> bool:
        """格式化输出并上传到 Sub2API。"""
        self.log("=" * 60)
        self.log("Step 5: 上传 Sub2API")
        self.log("=" * 60)

        sub2api_url = self.config.get("sub2api_url", "")
        sub2api_key = self.config.get("sub2api_admin_key", "")

        if not sub2api_url or not sub2api_key:
            self.log("  未配置 sub2api_url/sub2api_admin_key，仅本地输出")
            self.result["steps"].append("upload_skipped")
            return False

        try:
            from platforms.chatgpt.sub2api_upload import upload_to_sub2api, verify_sub2api_upload

            account_data = {
                "access_token": self.result.get("access_token", ""),
                "refresh_token": self.result.get("refresh_token", ""),
                "id_token": self.result.get("id_token", ""),
                "account_id": self.result.get("account_id", ""),
                "email": self.result.get("email", ""),
                "plan_type": self.result.get("plan_type", "plus"),
            }

            success, msg = upload_to_sub2api(account_data, sub2api_url, sub2api_key)
            if not success:
                self.result["upload_ok"] = False
                self.result["upload_verified"] = False
                self.log(f"  上传失败: {msg}")
                return False
            verified, verify_msg = verify_sub2api_upload(account_data, sub2api_url, sub2api_key)
            self.result["upload_ok"] = True
            self.result["upload_verified"] = bool(verified)
            self.result["upload_verify_source"] = "sub2api/accounts?email_or_account_id"
            if verified:
                self.log(f"  上传成功并回查通过: {verify_msg}")
                self.result["steps"].append("upload_sub2api")
                return True
            self.log(f"  上传成功但回查失败: {verify_msg}")
            return False
        except Exception as e:
            self.result["upload_ok"] = False
            self.result["upload_verified"] = False
            self.log(f"  上传异常: {e}")
            return False

    # ------------------------------------------------------------------
    # 全链路
    # ------------------------------------------------------------------
    def step_email_register_phone_bind(self, *, headed: bool = False) -> dict:
        """邮箱注册 ChatGPT，并在 add-phone 阶段用当前 SMS provider 完成手机号绑定。"""
        self.log("=" * 60)
        self.log("Step 1: 邮箱注册 + 手机号绑定")
        self.log("=" * 60)

        from core.base_sms import create_phone_callbacks
        from platforms.chatgpt.browser_register import _browser_registration_flow, _do_codex_oauth, _get_cookies
        from core.browser.session import extract_chatgpt_access_token
        from platforms.chatgpt.utils import generate_random_password

        if self.config.get("rotate_proxy_each_attempt"):
            self._select_fresh_proxy_for_attempt()

        password = str(self.config.get("chatgpt_password") or "").strip() or generate_random_password(16)
        provider_key = self._mailbox_provider_key()
        email = str(self.config.get("outlook_email") or self.config.get("email") or "").strip()

        if provider_key == "outlook_token":
            candidates = self._load_outlook_token_candidates(email)
            if not candidates:
                raise RuntimeError("邮箱注册缺少可用 Outlook token 邮箱；请在服务商页导入 Outlook token 池")
            email, outlook_password, outlook_client_id, outlook_refresh_token = candidates[0]
            self.config["outlook_email"] = email
            self.config["outlook_password"] = outlook_password
            self.config["outlook_client_id"] = outlook_client_id
            self.config["outlook_refresh_token"] = outlook_refresh_token
            self._oauth_bind_email = email
            self._oauth_bind_password = outlook_password
            self._oauth_bind_client_id = outlook_client_id
            self._oauth_bind_refresh_token = outlook_refresh_token
            self.result["outlook_email"] = email
        elif provider_key in {"forwarded_domain", "domain_forward", "imap_forward", "163_forward"}:
            from core.mailbox_providers import ForwardedDomainMailbox
            self._mailbox_provider = ForwardedDomainMailbox.from_config(self.config)
            self._mailbox_account = self._mailbox_provider.create_account()
            email = self._mailbox_account.email
            try:
                self._mailbox_before_ids = self._mailbox_provider.get_current_ids(self._mailbox_account)
            except Exception:
                self._mailbox_before_ids = set()
        elif provider_key in {"icloud_api", "email_link_api", "link_api_mailbox"}:
            from core.mailbox_providers import LinkApiMailbox
            self._mailbox_provider = LinkApiMailbox.from_config(self.config)
            self._mailbox_account = self._mailbox_provider.create_account()
            email = self._mailbox_account.email
            self.result["outlook_email"] = email
            self.result["email_provider"] = "icloud_api"
            try:
                self._mailbox_before_ids = self._mailbox_provider.get_current_ids(self._mailbox_account)
            except Exception:
                self._mailbox_before_ids = set()
        elif provider_key in {"icloud_privacy", "icloud_hide_my_email", "hide_my_email"}:
            from core.mailbox_providers import ICloudPrivacyMailbox
            self._mailbox_provider = ICloudPrivacyMailbox.from_config(self.config)
            self._mailbox_account = self._mailbox_provider.create_account()
            email = self._mailbox_account.email
            self.result["outlook_email"] = email
            self.result["email_provider"] = "icloud_privacy"
            try:
                self._mailbox_before_ids = self._mailbox_provider.get_current_ids(self._mailbox_account)
            except Exception:
                self._mailbox_before_ids = set()
        elif provider_key in {"cfworker", "cfworker_admin_api", "cloud_mail"}:
            from core.mailbox_providers import CFWorkerMailbox
            self._mailbox_provider = CFWorkerMailbox.from_config(self.config)
            self._mailbox_account = self._mailbox_provider.create_account()
            email = self._mailbox_account.email
            try:
                self._mailbox_before_ids = self._mailbox_provider.get_current_ids(self._mailbox_account)
            except Exception:
                self._mailbox_before_ids = set()
        else:
            raise RuntimeError(f"不支持的邮箱注册 provider: {provider_key}")

        if not email:
            raise RuntimeError("邮箱注册缺少 email")

        self.result["email"] = email
        self.result["password"] = password
        self.result["generated_chatgpt_password"] = password

        bind_phone = self._config_bool("email_register_bind_phone", False)
        phone_callback = None
        phone_cleanup = lambda: None
        if bind_phone:
            sms_provider_key = str(self.config.get("sms_provider") or "herosms_api").strip()
            service = str(self.config.get("sms_service") or "dr").strip()
            country = str(self.config.get("sms_country") or "").strip()
            phone_callback, phone_cleanup = create_phone_callbacks(
                sms_provider_key,
                self.config,
                service=service,
                country=country,
                log_fn=self.log,
            )

        try:
            if bind_phone:
                self.log("邮箱注册使用浏览器安全链: Pipeline Camoufox + 辣椒 IP 校验 + 邮箱 OTP + add-phone UI 接码")
            else:
                self.log("邮箱注册使用浏览器安全链: Pipeline Camoufox + 辣椒 IP 校验 + 邮箱 OTP；注册完成后停止，不执行 add-phone/OAuth")
            self._launch_camoufox(headed=bool(headed or self.config.get("headed", False)))
            final_state = _browser_registration_flow(
                self.page,
                email,
                password,
                self._manual_email_otp_callback,
                phone_callback,
                self.log,
            )
            self.log(f"邮箱浏览器注册状态完成: page={final_state.get('page_type') or '-'}")
            if not bind_phone:
                self.log("提取 ChatGPT session access_token...")
                try:
                    if "chatgpt.com" not in str(self.page.url or ""):
                        self.page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=60000)
                except Exception as exc:
                    self.log(f"  ChatGPT 首页等待失败，继续尝试 /api/auth/session: {str(exc).splitlines()[0][:160]}")
                fetch_result = extract_chatgpt_access_token(self.page, attempts=30, delay=2, log_fn=self.log)
                if not fetch_result.success or not fetch_result.access_token:
                    raise RuntimeError(f"邮箱注册已完成但 access_token 提取失败: {fetch_result.failure_reason or fetch_result.status}")
                access_token = fetch_result.access_token
                self.result["access_token"] = access_token
                self.result["chatgpt_access_token_initial"] = access_token
                cookies = _get_cookies(self.page)
                self.result["session_token"] = str(cookies.get("__Secure-next-auth.session-token") or cookies.get("__Secure-authjs.session-token") or "")
                try:
                    segment = access_token.split(".")[1]
                    segment += "=" * ((4 - len(segment) % 4) % 4)
                    claims = json.loads(base64.urlsafe_b64decode(segment.encode("ascii")).decode("utf-8"))
                    auth_claims = claims.get("https://api.openai.com/auth") or {}
                    profile_claims = claims.get("https://api.openai.com/profile") or {}
                    self.result["account_id"] = str(auth_claims.get("chatgpt_account_id") or claims.get("sub") or "")
                    self.result["email"] = str(profile_claims.get("email") or claims.get("email") or email)
                    self.result["plan_type"] = str(auth_claims.get("chatgpt_plan_type") or "free")
                except Exception:
                    self.result["email"] = email
                    self.result["plan_type"] = "free"
                self.log(f"  Access Token: {access_token[:50]}...")
                self.result["plan_type"] = "free"
                self.result["status"] = "email_registered"
                self.result["stage"] = "manual_plus_required"
                self.result["success"] = True
                self.result["steps"].append("email_browser_register")
                registered_file = self._save_registered_account_json()
                resume_file = self._save_manual_plus_handoff_json()
                self.result["registered_file"] = str(registered_file)
                self.result["resume_file"] = str(resume_file)
                if self._mailbox_provider_key() in {"icloud_api", "email_link_api", "link_api_mailbox"}:
                    self._mark_outlook_email_used(email, "email registration completed")
                self.log("邮箱注册已完成并停止；未执行 add-phone、未租用手机号、未执行 OAuth 绑定")
                return self.result
            cookies = _get_cookies(self.page)
            tokens = _do_codex_oauth(
                self.page,
                cookies,
                email,
                password,
                self._manual_email_otp_callback,
                phone_callback,
                self.config.get("proxy", "") or None,
                self.log,
            )
        finally:
            try:
                phone_cleanup()
            except Exception:
                pass

        if not tokens or not tokens.get("access_token"):
            raise RuntimeError("邮箱浏览器注册/手机号绑定失败")

        activation = getattr(phone_callback, "activation", None)
        self.result["phone_number"] = str(getattr(activation, "phone_number", "") or self.result.get("phone_number") or "")
        self.result["activation_id"] = str(getattr(activation, "activation_id", "") or self.result.get("activation_id") or "")
        self.result["access_token"] = tokens.get("access_token", "")
        self.result["chatgpt_access_token_initial"] = tokens.get("access_token", "")
        self.result["refresh_token"] = tokens.get("refresh_token", "")
        self.result["id_token"] = tokens.get("id_token", "")
        self.result["account_id"] = tokens.get("account_id") or self.result.get("account_id", "")
        self.result["email"] = tokens.get("email") or email
        self.result["plan_type"] = "free"
        self.result["status"] = "email_phone_registered"
        self.result["stage"] = "manual_plus_required"
        self.result["success"] = True
        self.result["steps"].append("email_browser_register_phone_bind")
        self._save_registered_account_json()
        self._save_manual_plus_handoff_json()
        return self.result


    def run(self, *, start_step: str = "register", headed: bool = False) -> dict:
        """
        运行流水线。

        start_step 可选:
          - "register": 从头开始并尝试完整链路
          - "register-token": 注册并保存手动 Plus 交接文件后停止
          - "email-register-token": 邮箱注册到账号创建完成后停止，保存手动 Plus 交接文件；默认不执行 add-phone/OAuth
          - "phone": 跳过获取手机号 (使用已有的)
          - "activate": 跳过注册，从激活开始 (需已有 access_token)
          - "oauth": 跳过激活，从 OAuth 开始
          - "resume-oauth": 读取 register-token 交接文件，复用浏览器 session 获取 refresh_token
          - "upload": 只做上传
        """
        if headed:
            self.config["headed"] = True

        if start_step == "register" and not str(self.config.get("iceaix_api_key") or "").strip():
            self._set_failure(
                "manual_plus_flow_required",
                step="activate_plus",
                reason="当前没有 iceaix_api_key，默认 register 不会一口气跑手动 Plus",
                retryable=False,
                next_action="先运行: python full_pipeline.py --config config.yaml --step register-token",
            )
            self.log("当前是手动 Plus 模式，请先运行 --step register-token")
            return self.result

        try:
            if start_step == "upload":
                self._load_resume_file_if_configured(required=False)
                upload_ok = self.step_upload_sub2api()
                self.result["success"] = bool(upload_ok)
                self.result["status"] = "upload_complete" if upload_ok else "upload_incomplete"
                if upload_ok:
                    self.log("上传完成")
                else:
                    self.log("上传未完成")
                return self.result

            if start_step == "email-register-token":
                return self.step_email_register_phone_bind(headed=headed)

            if start_step == "register-token":
                phone_number = self.step_get_phone_number()
                self.step_browser_register(phone_number)
                if self.config.get("auto_bind_billing_email_after_register"):
                    self.step_bind_billing_email_current_browser()
                registered_file = self._save_registered_account_json()
                resume_file = self._save_manual_plus_handoff_json()
                self.result["registered_file"] = str(registered_file)
                self.result["resume_file"] = str(resume_file)
                self.result["status"] = "manual_plus_required"
                self.result["success"] = True
                self.log("=" * 60)
                self.log("已完成注册并保存手动 Plus 交接文件")
                self.log(f"交接文件: {resume_file}")
                self.log("下一步: 打开 https://plus.iceaix.com/，使用交接文件中的完整 access_token 手动 CDK 开通 Plus。")
                self.log(f"完成后运行: python full_pipeline.py --config <config> --step resume-oauth --resume-file {resume_file} --manual-plus-confirmed")
                self.log("=" * 60)
                return self.result

            if start_step == "resume-oauth":
                self._load_resume_file_if_configured(required=True)
                if not self.config.get("manual_plus_confirmed"):
                    self._set_failure(
                        "manual_plus_confirmation_required",
                        step="resume_oauth",
                        reason="resume-oauth 需要显式 --manual-plus-confirmed",
                        retryable=False,
                        next_action="加上 --manual-plus-confirmed，确认已在 plus.iceaix.com 完成 Plus 开通",
                    )
                    return self.result
                if self._config_bool("skip_plus_check_for_binding", False):
                    self.log("绑定任务已跳过 Plus 实时校验，直接执行 OAuth/CPA 绑定")
                    self.result["subscription_check_source"] = self.result.get("subscription_check_source") or "binding_skip_plus_check"
                else:
                    plan_type = self._wait_for_paid_plan()
                    if not self._is_paid_plan(plan_type):
                        if self._config_bool("manual_plus_trust_confirmation", False):
                            self.log(f"手动 Plus 实时接口仍显示 {plan_type or 'unknown'}；已按 manual_plus_trust_confirmation 继续 OAuth 绑定")
                            self.result["plan_type"] = "plus"
                            self.result["subscription_check_source"] = self.result.get("subscription_check_source") or "manual_confirmation"
                        else:
                            self.log(f"手动 Plus 未验证: plan_type={plan_type or 'unknown'}，跳过 Outlook/OAuth 和 complete 产物")
                            self.result["success"] = False
                            self.result["status"] = "manual_plus_unverified"
                            return self.result
                oauth_result = self.step_oauth_from_saved_session(headed=headed)
                cpa_ok = bool(oauth_result and oauth_result.get("cpa_submitted"))
                tokens_ok = bool(oauth_result and oauth_result.get("access_token") and oauth_result.get("refresh_token") and oauth_result.get("id_token"))
                plan_ok = True
                upload_ok = False
                if tokens_ok:
                    upload_ok = self.step_upload_sub2api()
                required_ok = cpa_ok or (tokens_ok and plan_ok)
                if not cpa_ok and (self.config.get("sub2api_url") or self.config.get("sub2api_admin_key")):
                    required_ok = required_ok and bool(upload_ok)
                if tokens_ok and required_ok:
                    final_file = self._save_final_tokens_json(oauth_result)
                    self.result["final_file"] = str(final_file)
                    self._mark_outlook_email_used(str(self.result.get("outlook_email") or self.result.get("email") or ""), "completed product")
                self.result["success"] = bool(required_ok)
                self.result["status"] = "cpa_bound" if cpa_ok else ("complete" if required_ok else "manual_plus_unverified")
                self.log("=" * 60)
                self.log("手动 Plus 后续跑完成")
                self.log(f"最终文件: {self.result.get('final_file', '') or '(未生成 complete 产物)'}")
                self.log("=" * 60)
                return self.result

            if start_step in ("activate", "oauth") and self.config.get("resume_file"):
                self._load_resume_file_if_configured(required=True)
            activation_ok = None
            oauth_result = None
            upload_ok = None
            # Step 1: 获取手机号
            if start_step in ("register",):
                phone_number = self.step_get_phone_number()
            elif start_step == "phone":
                phone_number = self.result.get("phone_number", "")
                if not phone_number:
                    phone_number = input("请输入手机号: ").strip()
                self.result["phone_number"] = phone_number
            else:
                phone_number = self.result.get("phone_number", "")

            # Step 2: 浏览器注册
            if start_step in ("register", "phone"):
                reg_result = self.step_browser_register(phone_number)
                access_token = reg_result.get("access_token", "")
                if access_token:
                    self._save_registered_account_json()
            else:
                access_token = self.result.get("access_token", "")
                if not access_token:
                    access_token = input("请输入 access_token: ").strip()
                self.result["access_token"] = access_token

            # Step 3: iceaix Plus 激活
            if start_step in ("register", "phone", "activate"):
                activation_ok = self.step_activate_plus(access_token)
                if activation_ok:
                    self._save_manual_plus_handoff_json()
                    self.result["manual_plus_status"] = "iceaix_success_pending_live_verification"
                    self.log("\n[!] 激活完成，开始等待 OpenAI live Plus 同步")
                    plan_type = self._wait_for_paid_plan()
                    if not self._is_paid_plan(plan_type):
                        self._set_failure(
                            "plus_not_verified",
                            step="verify_plus",
                            reason=f"iceaix 任务完成但 live plan 未验证为 paid: {plan_type or 'unknown'}",
                            retryable=True,
                            next_action="稍后重新运行 OAuth/验证流程，或检查 iceaix job/OpenAI 订阅状态",
                        )
                        activation_ok = False

                    else:
                        self.result["manual_plus_status"] = "iceaix_success_live_verified"
                        self._save_manual_plus_handoff_json()
            if start_step in ("register", "phone", "activate") and not activation_ok:
                self.log("  Plus 未完成或未验证，跳过 Outlook/OAuth，避免消耗邮箱池")
                oauth_result = None
            if start_step == "oauth" or (start_step in ("register", "phone", "activate") and activation_ok):
                if start_step in ("register", "phone", "activate") and self.config.get("outlook_token_order_file"):
                    resume_id = f"auto_{self._timestamp()}_{uuid.uuid4().hex[:8]}"
                    storage_path = self._save_browser_storage_state(resume_id)
                    if storage_path:
                        self.result["browser_storage_state_path"] = storage_path
                        self.config["_browser_storage_state"] = storage_path
                    self._cleanup()
                    if not storage_path:
                        self.log("  当前补 Plus 流程没有可复用浏览器 session，已保存 Plus 交接文件，跳过 Outlook/OAuth 避免浪费邮箱池")
                        oauth_result = None
                    else:
                        oauth_result = self.step_oauth_from_saved_session(headed=headed)
                elif self.config.get("outlook_email"):
                    if self.page is None:
                        self._launch_camoufox(headed=headed)
                    oauth_result = self.step_oauth_bind_email()
                else:
                    oauth_result = self.step_oauth_bind_email()

            tokens_ok = bool(oauth_result and oauth_result.get("access_token") and oauth_result.get("refresh_token") and oauth_result.get("id_token"))
            if tokens_ok:
                refreshed_plan = self._refresh_plan_type_from_subscription(prefer_live=True, allow_cached=True)
                if refreshed_plan:
                    self.result["plan_type"] = refreshed_plan
            plan_ok = self._is_paid_plan(str(self.result.get("plan_type") or ""))
            if tokens_ok and plan_ok:
                upload_ok = self.step_upload_sub2api()
            if start_step == "activate" and activation_ok and plan_ok and not tokens_ok and self.config.get("outlook_token_order_file"):
                self.result["success"] = True
                self.result["status"] = "plus_verified_needs_oauth"
                self.log("  Plus 已 live 验证；未获取 OAuth token，未写入 products。后续用保存的 resume 文件跑 resume-oauth。")
            else:
                required_ok = bool(self.result.get("access_token"))
                if start_step in ("register", "phone"):
                    required_ok = required_ok and bool(activation_ok) and tokens_ok and plan_ok
                if start_step == "activate":
                    required_ok = required_ok and bool(activation_ok) and tokens_ok and plan_ok
                if start_step == "oauth":
                    required_ok = required_ok and tokens_ok and plan_ok
                if self.config.get("sub2api_url") or self.config.get("sub2api_admin_key"):
                    required_ok = required_ok and bool(upload_ok)
                if required_ok and oauth_result:
                    final_file = self._save_final_tokens_json(oauth_result)
                    self.result["final_file"] = str(final_file)
                    self._mark_outlook_email_used(str(self.result.get("outlook_email") or self.result.get("email") or ""), "completed product")
                elif not self.result.get("status"):
                    reason = "完整链路条件未满足"
                    if tokens_ok and not plan_ok:
                        reason = f"OAuth token 已获取，但套餐未验证为 paid: {self.result.get('plan_type') or 'unknown'}"
                    self._set_failure("incomplete", step="run", reason=reason, retryable=True)
                self.result["success"] = bool(required_ok)
                self.result["status"] = "complete" if required_ok else (self.result.get("status") or "incomplete")
            self.log("=" * 60)
            self.log("全链路完成!")
            self.log("=" * 60)

        except KeyboardInterrupt:
            self.log("\n用户中断")
            self._set_failure("interrupted", step="run", reason="用户中断", retryable=True)
        except Exception as e:
            self.result["success"] = False
            if self.result.get("status") != "email_otp_required":
                self.result["status"] = "error"
            if not self.result.get("failed_step"):
                self.result["failed_step"] = "run"
            if not self.result.get("failure_reason"):
                self.result["failure_reason"] = str(e)
            failed_email = str(self.result.get("outlook_email") or self.result.get("email") or "").strip()
            if failed_email and self._mailbox_provider_key() in {"icloud_api", "email_link_api", "link_api_mailbox", "icloud_privacy", "icloud_hide_my_email", "hide_my_email"}:
                self._record_outlook_pool_state(failed_email, "failed_retryable", reason=str(e)[:300])
            self.log(f"流水线异常: {e}")
            import traceback
            traceback.print_exc()
        finally:
            if not self.result.get("success"):
                self._save_failed_run_json()
            self._cleanup()

        return self.result

    # ------------------------------------------------------------------
    # 浏览器管理
    # ------------------------------------------------------------------

    def _start_local_socks_bridge(self, upstream_proxy: str) -> str:
        from core.proxy.credential_runtime import CredentialProxyRuntime

        if self._local_socks_bridge and self._local_socks_bridge_target == upstream_proxy:
            return self._local_socks_bridge.server_url
        runtime = CredentialProxyRuntime(self.config, log_fn=self.log)
        bridge_url = runtime.start_browser_bridge(upstream_proxy)
        if runtime._bridges:
            self._local_socks_bridge = runtime._bridges[-1]
            self._local_socks_bridge_target = upstream_proxy
        return bridge_url

    def _browser_locale_for_proxy_region(self) -> tuple[str, str]:
        region = str(self.config.get("lajiao_proxy_expected_country") or self.config.get("lajiao_proxy_regions") or "").split(",", 1)[0].strip().upper()
        if region == "JP":
            return "ja-JP", "Asia/Tokyo"
        if region == "BR":
            return "pt-BR", "America/Sao_Paulo"
        if region == "US":
            return "en-US", "America/New_York"
        return str(self.config.get("browser_locale") or "en-US"), str(self.config.get("browser_timezone") or "")

    def _launch_camoufox(self, headed: bool = False):
        """启动 Camoufox 反指纹浏览器。"""
        self.log("启动 Camoufox 浏览器...")
        if self.config.get("rotate_proxy_each_browser_launch"):
            if self.config.pop("_proxy_selected_for_next_browser_launch", False):
                self.log(f"  新窗口复用刚校验代理: {self.config.get('proxy')} exit_ip={self.result.get('registration_proxy_exit_ip', '')}")
            else:
                self.config["proxy"] = self._select_fresh_proxy_for_attempt()
                self.log(f"  新窗口已切换代理: {self.config.get('proxy')} exit_ip={self.result.get('registration_proxy_exit_ip', '')}")
        try:
            from camoufox.sync_api import Camoufox

            proxy_config = None
            if self.config.get("proxy"):
                from core.proxy_utils import build_playwright_proxy_config
                proxy_config = build_playwright_proxy_config(self.config["proxy"])
                if self._use_lajiao_credentials_mode() and proxy_config and proxy_config.get("username"):
                    runtime_proxy = self._start_local_socks_bridge(self.config["proxy"])
                    proxy_config = build_playwright_proxy_config(runtime_proxy)

            launch_kwargs = {
                "headless": not headed,
                "os": ["windows", "macos", "linux"],
                "enable_cache": bool(self.config.get("camoufox_enable_cache", False)),
                "humanize": self.config.get("camoufox_humanize", True),
            }
            if proxy_config:
                launch_kwargs["proxy"] = proxy_config
                if self.config.get("camoufox_geoip", True):
                    launch_kwargs["geoip"] = self.config.get("_camoufox_geoip_ip") or True
            self._camoufox_ctx = Camoufox(**launch_kwargs)
            self.browser = self._camoufox_ctx.__enter__()
            storage_state = None if self.config.get("_force_fresh_browser_context") else (self.config.get("_browser_storage_state") or None)
            locale, timezone_id = self._browser_locale_for_proxy_region()
            context_kwargs = {
                "no_viewport": True,
                "storage_state": storage_state,
                "locale": locale,
            }
            if timezone_id:
                context_kwargs["timezone_id"] = timezone_id
            context = self.browser.new_context(**context_kwargs)
            self.browser_context = context
            self.page = context.new_page()
            self._attach_page_debug_events(self.page)
            self.log("  Camoufox 已启动 (fresh incognito)")

        except ImportError:
            self.log("  Camoufox 未安装，回退到 Playwright")
            self._launch_playwright(headed=headed)


    def _launch_playwright(self, headed: bool = False):
        """启动 Chromium 系浏览器；browser_engine=patchright 时使用 Patchright。"""
        engine = str(self.config.get("browser_engine") or "playwright").strip().lower()
        self.log(f"启动 {'Patchright' if engine == 'patchright' else 'Playwright'} 浏览器...")
        try:
            if engine == "patchright":
                from patchright.sync_api import sync_playwright
            else:
                from playwright.sync_api import sync_playwright

            self.playwright_instance = sync_playwright().start()
            channel = str(self.config.get("browser_channel") or "chrome").strip().lower()
            launch_kwargs = {"headless": not headed}
            if engine == "patchright" and channel and channel not in {"chromium", "default"}:
                launch_kwargs["channel"] = channel

            if self.config.get("proxy"):
                from core.proxy_utils import build_playwright_proxy_config
                proxy_url = str(self.config.get("proxy") or "").strip()
                mode = str(self.config.get("lajiao_proxy_mode") or "").strip().lower()
                if mode in {"credential", "credentials", "account", "auth"} and "@" in proxy_url:
                    from core.proxy.credential_runtime import CredentialProxyRuntime

                    runtime = CredentialProxyRuntime(self.config, log_fn=self.log)
                    runtime_proxy_url = runtime.runtime_url(proxy_url)
                    proxy_url = runtime.start_browser_bridge(runtime_proxy_url)
                    self._proxy_runtime = runtime
                proxy_config = build_playwright_proxy_config(proxy_url)
                if proxy_config:
                    launch_kwargs["proxy"] = proxy_config

            storage_state = None if self.config.get("_force_fresh_browser_context") else (self.config.get("_browser_storage_state") or None)
            locale, timezone_id = self._browser_locale_for_proxy_region()
            locale = str(self.config.get("browser_locale") or self.config.get("locale") or locale or "").strip() or None
            timezone_id = str(self.config.get("browser_timezone") or self.config.get("timezone_id") or timezone_id or "").strip() or None
            context_kwargs = {}
            if storage_state:
                context_kwargs["storage_state"] = storage_state
            if locale:
                context_kwargs["locale"] = locale
            if timezone_id:
                context_kwargs["timezone_id"] = timezone_id
            accept_language = str(self.config.get("accept_language") or "").strip()
            if accept_language:
                context_kwargs["extra_http_headers"] = {"Accept-Language": accept_language}
            if engine == "patchright":
                context_kwargs["no_viewport"] = bool(self.config.get("browser_no_viewport", True))

            profile_mode = str(self.config.get("browser_profile_mode") or "").strip().lower()
            profile_dir = str(self.config.get("browser_profile_dir") or "").strip()
            if engine == "patchright" and profile_mode == "per_task" and not profile_dir:
                profile_dir = str(Path("data") / "browser_profiles" / "patchright" / (self.result.get("task_id") or uuid.uuid4().hex[:12]))
            if engine == "patchright" and profile_dir:
                Path(profile_dir).mkdir(parents=True, exist_ok=True)
                context_kwargs.pop("storage_state", None)
                self.browser_context = self.playwright_instance.chromium.launch_persistent_context(
                    user_data_dir=profile_dir,
                    **launch_kwargs,
                    **context_kwargs,
                )
                self.browser = self.browser_context.browser
                self.config["browser_profile_dir"] = profile_dir
            else:
                self.browser = self.playwright_instance.chromium.launch(**launch_kwargs)
                self.browser_context = self.browser.new_context(**context_kwargs)
            self.page = self.browser_context.pages[0] if self.browser_context.pages else self.browser_context.new_page()
            self._attach_page_debug_events(self.page)
            self.log(f"  {'Patchright' if engine == 'patchright' else 'Playwright'} 已启动")

        except ImportError as exc:
            if engine == "patchright":
                raise RuntimeError("请安装 Patchright: pip install patchright && patchright install chrome") from exc
            raise RuntimeError("请安装 Playwright: pip install playwright && playwright install chromium") from exc

    def _wait_for_paid_plan(self) -> str:
        retries = self._config_int("plus_verify_retries", 6, minimum=1)
        interval = self._config_int("plus_verify_interval", 10, minimum=0)
        last_plan = ""
        for attempt in range(1, retries + 1):
            plan = self._refresh_plan_type_from_subscription(prefer_live=True, allow_cached=False)
            last_plan = plan or last_plan
            if self._is_paid_plan(plan):
                return plan
            self.log(f"  Plus 未同步或未验证: {plan or 'unknown'} ({attempt}/{retries})")
            if attempt < retries and interval:
                time.sleep(interval)
        return last_plan

    def _timestamp(self) -> str:
        return datetime.now().strftime("%Y%m%d_%H%M%S")

    def _output_dir(self) -> Path:
        output_dir = Path(self.config.get("output_dir", "output"))
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir

    def _products_dir(self) -> Path:
        products_dir = self._output_dir() / "products"
        products_dir.mkdir(parents=True, exist_ok=True)
        return products_dir

    def _registered_accounts_dir(self) -> Path:
        registered_dir = self._output_dir() / "registered_accounts"
        registered_dir.mkdir(parents=True, exist_ok=True)
        return registered_dir

    def _failed_runs_dir(self) -> Path:
        failed_dir = self._output_dir() / "failed_runs"
        failed_dir.mkdir(parents=True, exist_ok=True)
        return failed_dir

    def _save_failed_run_json(self) -> Path:
        if self.result.get("failed_file"):
            return Path(str(self.result["failed_file"]))
        status = str(self.result.get("status") or "failed").strip() or "failed"
        label = self._product_label(str(self.result.get("email") or self.result.get("outlook_email") or self.result.get("phone_number") or ""))
        filename = self._failed_runs_dir() / f"{label}_{re.sub(r'[^A-Za-z0-9_.-]+', '_', status)}.json"
        data = {
            "schema_version": 1,
            "stage": "failed",
            "created_at": datetime.now().isoformat(),
            "status": status,
            "failed_step": self.result.get("failed_step", ""),
            "failure_reason": self.result.get("failure_reason", ""),
            "retryable": bool(self.result.get("retryable")),
            "next_action": self.result.get("next_action", ""),
            "phone_number": self.result.get("phone_number", ""),
            "activation_id": self.result.get("activation_id", ""),
            "registered_file": self.result.get("registered_file", ""),
            "resume_file": self.result.get("resume_file", ""),
            "iceaix_job_id": self.result.get("iceaix_job_id", ""),
            "iceaix_status": self.result.get("iceaix_status", ""),
            "iceaix_result_code": self.result.get("iceaix_result_code", ""),
            "iceaix_billing_status": self.result.get("iceaix_billing_status", ""),
            "outlook_email": self.result.get("outlook_email") or self.config.get("outlook_email", ""),
            "plan_type": self.result.get("plan_type", ""),
            "password": self.result.get("password", ""),
            "generated_chatgpt_password": self.result.get("generated_chatgpt_password") or self.result.get("password", ""),
            "registration_proxy": self.result.get("registration_proxy", ""),
            "subscription_check_source": self.result.get("subscription_check_source", ""),
            "debug_log_file": str(self.debug_log_file),
            "steps": self.result.get("steps", []),
        }
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        self.result["failed_file"] = str(filename)
        try:
            account_store.upsert_account(data, source_file=str(filename), copy_artifacts=False)
        except Exception as exc:
            self.log(f"  失败账号规范化存储写入失败: {exc}")
        self.log(f"  失败运行已保存: {filename}")
        return filename


    def _product_label(self, email: str = "") -> str:
        account = str(
            email
            or self.result.get("email")
            or self.config.get("outlook_email")
            or self.result.get("account_id")
            or self.result.get("phone_number")
            or "account"
        ).strip()
        account = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", account).strip(" ._") or "account"
        ts = str(self.result.get("product_date") or "").strip()
        if not ts:
            ts = datetime.now().strftime("%Y%m%d")
            self.result["product_date"] = ts
        return f"{account}_{ts}"

    def _extract_plan_from_tokens(self) -> str:
        for token_key in ("id_token", "access_token"):
            token = str(self.result.get(token_key) or "")
            if token.count(".") < 2:
                continue
            segment = token.split(".")[1]
            segment += "=" * ((4 - len(segment) % 4) % 4)
            try:
                claims = json.loads(base64.urlsafe_b64decode(segment.encode("ascii")).decode("utf-8"))
            except Exception:
                continue
            auth = claims.get("https://api.openai.com/auth") or {}
            plan = str(auth.get("chatgpt_plan_type") or "").strip().lower()
            if plan:
                return plan
        return ""

    def _is_paid_plan(self, plan_type: str) -> bool:
        normalized = str(plan_type or "").strip().lower()
        return normalized in {"plus", "pro", "premium", "paid", "team", "business", "enterprise"}

    def _fetch_live_subscription_plan(self) -> str:
        access_token = str(self.result.get("access_token") or "")
        if not access_token:
            return ""
        try:
            from platforms.chatgpt.payment import fetch_subscription_status_details
        except Exception as exc:
            self.log(f"  实时订阅状态检查跳过: payment 依赖不可用: {exc}")
            return ""

        proxy_candidates = []
        fresh_proxy = self._select_fresh_proxy_for_subscription_check()
        if fresh_proxy:
            proxy_candidates.append(fresh_proxy)
        if not proxy_candidates:
            self.log("  实时订阅状态检查跳过: 未配置辣椒 HTTP 代理")
            return ""

        errors = []
        observed_plan = ""
        for proxy_value in proxy_candidates:
            try:
                account = SimpleNamespace(
                    access_token=access_token,
                    chatgpt_account_id=str(self.result.get("account_id") or ""),
                    cookies="",
                    extra={"id_token": str(self.result.get("id_token") or "")},
                )
                bridge_runtime = None
                request_proxy = proxy_value
                try:
                    from core.proxy.credential_runtime import CredentialProxyRuntime

                    bridge_runtime = CredentialProxyRuntime({"lajiao_proxy_credential_protocol": "socks5"}, log_fn=self.log)
                    request_proxy = bridge_runtime.start_browser_bridge(bridge_runtime.runtime_url(proxy_value))
                    details = fetch_subscription_status_details(account, proxy=request_proxy)
                finally:
                    if bridge_runtime is not None:
                        bridge_runtime.cleanup()
                if isinstance(details, dict):
                    plan = str(details.get("status") or "").strip().lower()
                    if plan:
                        observed_plan = plan
                        source = str(details.get("source") or "unknown")
                        route = proxy_value or ""
                        self.result["subscription_check_proxy"] = "" if "127.0.0.1" in route or "localhost" in route else route
                        self.result["subscription_check_source"] = source
                        self.log(f"  实时订阅状态: {plan} ({source}, {route})")
                        if self._is_paid_plan(plan):
                            return plan
            except Exception as exc:
                route = request_proxy or "direct"
                errors.append(f"{route}: {exc}")
        if observed_plan:
            return observed_plan
        self.log("  实时订阅状态检查失败: " + " | ".join(errors))
        return ""

    def _refresh_plan_type_from_subscription(self, *, prefer_live: bool = False, allow_cached: bool = True) -> str:
        token_plan = self._extract_plan_from_tokens()
        live_plan = self._fetch_live_subscription_plan() if prefer_live or not token_plan else ""
        plan = live_plan or token_plan
        if plan:
            self.result["plan_type"] = plan
            return plan
        if allow_cached:
            return str(self.result.get("plan_type") or "").strip().lower()
        return ""

    def _append_unique_line(self, path: Path, line: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = set(path.read_text(encoding="utf-8").splitlines()) if path.exists() else set()
        if line and line not in existing:
            with open(path, "a", encoding="utf-8", newline="\n") as f:
                f.write(line + "\n")

    def _save_token_pool_entry(self, resume_file: Path) -> None:
        if not self.config.get("save_token_pool"):
            return
        pools_dir = self._output_dir() / "pools"
        access_token = str(self.result.get("access_token") or "")
        if not access_token:
            return
        phone = str(self.result.get("phone_number") or "")
        password = str(self.result.get("password") or "")
        self._append_unique_line(pools_dir / "pool_tokens.txt", access_token)
        self._append_unique_line(pools_dir / "pool_phones.txt", f"{phone}----{password}----{access_token}----{resume_file}")

    def _account_text(self, *, stage: str, include_tokens: bool = True) -> str:
        email = self.result.get("email") or self.config.get("outlook_email", "")
        account_id = self.result.get("account_id", "")
        account = email or account_id or self.result.get("phone_number", "")
        lines = [
            f"阶段: {stage}",
            f"手机号: {self.result.get('phone_number', '')}",
            f"邮箱: {email}",
            f"账号: {account}",
            f"账号ID: {account_id}",
            f"密码: {self.result.get('password', '')}",
            f"套餐: {self.result.get('plan_type', '')}",
            f"Activation ID: {self.result.get('activation_id', '')}",
            f"注册代理: {self.result.get('registration_proxy', '')}",
            f"注册出口IP: {self.result.get('registration_proxy_exit_ip', '')}",
        ]
        if include_tokens:
            lines.extend(
                [
                    f"ChatGPT access_token: {self.result.get('access_token', '')}",
                    f"OAuth refresh_token: {self.result.get('refresh_token', '')}",
                    f"OAuth id_token: {self.result.get('id_token', '')}",
                ]
            )
        return "\n".join(lines) + "\n"

    def _write_account_text(self, filename: Path, *, stage: str, include_tokens: bool = True) -> Path:
        text_path = filename.with_suffix(".txt")
        text_path.parent.mkdir(parents=True, exist_ok=True)
        text_path.write_text(self._account_text(stage=stage, include_tokens=include_tokens), encoding="utf-8")
        self.result["text_file"] = str(text_path)
        return text_path


    def _save_resume_scripts(self, resume_file: Path, resume_id: str) -> None:
        output_dir = self._output_dir()
        project_dir = _PROJECT_ROOT
        resume_path = resume_file.resolve()
        bat_path = output_dir / f"resume_{resume_id}.bat"
        ps1_path = output_dir / f"resume_{resume_id}.ps1"
        manual_path = output_dir / f"manual_plus_{resume_id}.txt"
        cmd = f'python full_pipeline.py --config config.yaml --step resume-oauth --resume-file "{resume_path}" --manual-plus-confirmed --headed'
        bat_path.write_text(
            "@echo off\r\n"
            f'cd /d "{project_dir}"\r\n'
            f"{cmd}\r\n"
            "pause\r\n",
            encoding="utf-8",
        )
        ps1_path.write_text(
            f'Set-Location "{project_dir}"\r\n'
            f'{cmd}\r\n'
            'Read-Host "按回车退出"\r\n',
            encoding="utf-8",
        )
        manual_path.write_text(
            "手动 Plus 步骤\n"
            "1. 打开 https://plus.iceaix.com/\n"
            "2. 使用 resume JSON 中的 chatgpt_access_token_initial 开通 Plus。\n"
            "3. 开通后等待 30-60 秒。\n"
            f"4. 双击 {bat_path.name} 或运行 {ps1_path.name}。\n"
            "注意: resume JSON 和本说明包含敏感 token，请勿外传。\n",
            encoding="utf-8",
        )
        self.result["resume_bat"] = str(bat_path)
        self.result["resume_ps1"] = str(ps1_path)
        self.result["manual_plus_file"] = str(manual_path)

    def step_bind_billing_email_current_browser(self) -> None:
        if not self.page:
            raise RuntimeError("当前浏览器页面不存在，无法同窗口绑定账单邮箱")
        self.log("=" * 60)
        self.log("Step: 同一指纹浏览器窗口绑定账单邮箱")
        self.log("=" * 60)
        from services.browser_billing_email_binder import (
            CHATGPT,
            PRICING_URL,
            _click_ready_continue_if_visible,
            _email_already_linked,
            _fetch_json,
            _fill_billing_email_ui,
            _fill_billing_otp_ui,
            _mailbox_from_config,
            _mark_email_used,
            _page_text,
            _screenshot,
            _token_from_session,
            _wait_click_free_trial,
            _wait_email_bound,
        )
        self.config["dashboard_task_id"] = self.config.get("dashboard_task_id") or self.config.get("task_id") or "inline"
        mailbox_provider = str(self.config.get("billing_email_provider") or "icloud_privacy")
        self.config["mailbox_provider"] = mailbox_provider
        mailbox = _mailbox_from_config(self.config)
        account = mailbox.create_account()
        before_ids = mailbox.get_current_ids(account)
        email = account.email
        self.log(f"  账单邮箱 provider={mailbox_provider} email={email}")
        self.page.goto(PRICING_URL, wait_until="domcontentloaded", timeout=90000)
        time.sleep(4)
        _screenshot(self.page, self.config, "inline_pricing_loaded")
        clicked = _wait_click_free_trial(self.page, timeout=90)
        self.log(f"  免费试用点击: {clicked or 'not_found'} url={self.page.url}")
        time.sleep(3)
        _screenshot(self.page, self.config, "inline_after_free_trial_click")
        ready_clicked = _click_ready_continue_if_visible(self.page)
        if ready_clicked:
            self.log(f"  首次使用弹窗已继续: {ready_clicked}")
            _screenshot(self.page, self.config, "inline_after_ready_continue")
            clicked = _wait_click_free_trial(self.page, timeout=90)
            self.log(f"  免费试用二次点击: {clicked or 'not_found'} url={self.page.url}")
            time.sleep(3)
            _screenshot(self.page, self.config, "inline_after_free_trial_click_ready")
        token = _token_from_session(self.page)
        me_before = _fetch_json(self.page, f"{CHATGPT}/backend-api/me", token=token)
        existing_email = str((me_before.get("data") or {}).get("email") or "")
        self.log(f"  /me before email={existing_email} phone={(me_before.get('data') or {}).get('phone_number') or ''}")
        if existing_email:
            email = existing_email
        else:
            filled = _fill_billing_email_ui(self.page, email)
            self.log(f"  UI 已填账单邮箱: {filled}")
            time.sleep(3)
            _screenshot(self.page, self.config, "inline_after_email_submit")
            if _email_already_linked(_page_text(self.page)):
                _mark_email_used(email, "Email already linked to another account")
                raise RuntimeError(f"账单邮箱已被其他账号绑定: {email}")
            code = mailbox.wait_for_code(account, timeout=int(self.config.get("email_otp_timeout") or 600), before_ids=before_ids)
            self.log(f"  邮箱 OTP 已读取 length={len(code or '')}")
            otp_filled = _fill_billing_otp_ui(self.page, code)
            self.log(f"  UI 已填邮箱 OTP: {otp_filled}")
            _screenshot(self.page, self.config, "inline_after_otp_submit")
            self.page.evaluate("async () => await fetch('/api/auth/session?refresh=true&reason=verify_otp', {credentials: 'include'})")
        bound_ok, me = _wait_email_bound(self.page, token, email, self.config, timeout=180)
        me_email = str((me.get("data") or {}).get("email") or "")
        _screenshot(self.page, self.config, "inline_after_me_check")
        if not bound_ok:
            raise RuntimeError(f"账单邮箱绑定后 /me 未回写: expected={email} actual={me_email}")
        self.page.evaluate("async () => await fetch('/api/auth/session?refresh=true&reason=billing_email_bound', {credentials: 'include'})")
        refreshed_token = _token_from_session(self.page)
        self.result["access_token"] = refreshed_token
        self.result["chatgpt_access_token_initial"] = refreshed_token
        self.result["email"] = me_email
        self.result["billing_email"] = me_email
        self.result["codex_email"] = me_email
        self.result["binding_status"] = "email_bound"
        self.result["binding_provider"] = mailbox_provider
        self.log(f"  账单邮箱绑定成功: {me_email}; 新 access_token length={len(refreshed_token)}")

    def _save_browser_storage_state(self, resume_id: str) -> str:
        if not self.browser_context:
            return ""
        storage_path = self._output_dir() / f"storage_{resume_id}.json"
        try:
            self.browser_context.storage_state(path=str(storage_path))
            return str(storage_path)
        except Exception as exc:
            self.log(f"  保存浏览器 session 失败: {exc}")
            return ""

    def _ensure_browser_storage_state(self, resume_id: str) -> str:
        existing = str(self.result.get("browser_storage_state_path") or self.config.get("_browser_storage_state") or "").strip()
        if existing:
            return existing
        storage_path = self._save_browser_storage_state(resume_id)
        if storage_path:
            self.result["browser_storage_state_path"] = storage_path
            self.config["_browser_storage_state"] = storage_path
        return storage_path

    def _save_registered_account_json(self) -> Path:
        registered_dir = self._registered_accounts_dir()
        filename = registered_dir / f"{self._product_label()}.json"
        storage_state_path = self._ensure_browser_storage_state(f"registered_{self._timestamp()}_{uuid.uuid4().hex[:8]}")
        data = {
            "schema_version": 1,
            "stage": "registered",
            "created_at": datetime.now().isoformat(),
            "account_key": self.result.get("email") or self.result.get("account_id") or self.result.get("phone_number") or "",
            "phone_number": self.result.get("phone_number", ""),
            "activation_id": self.result.get("activation_id", ""),
            "account_id": self.result.get("account_id", ""),
            "email": self.result.get("email", ""),
            "password": self.result.get("password", ""),
            "plan_type": self.result.get("plan_type", ""),
            "registration_proxy": self.result.get("registration_proxy", ""),
            "registration_proxy_exit_ip": self.result.get("registration_proxy_exit_ip", ""),
            "browser_storage_state_path": storage_state_path,
            "chatgpt_access_token_initial": self.result.get("chatgpt_access_token_initial") or self.result.get("access_token", ""),
            "access_token": self.result.get("access_token", ""),
            "session_token": self.result.get("session_token", ""),
            "resume_file": self.result.get("resume_file", ""),
        }
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        self.result["registered_file"] = str(filename)
        try:
            account_store.upsert_account(data, source_file=str(filename), copy_artifacts=False)
        except Exception as exc:
            self.log(f"  规范化账号存储写入失败: {exc}")
        self.log(f"  注册成功账号已保存: {filename}")
        return filename

    def _save_manual_plus_handoff_json(self) -> Path:
        """保存 register-token 阶段交接文件，供手动 Plus 后 resume-oauth 使用。"""
        output_dir = self._output_dir()
        resume_id = self.config.get("resume_id") or f"{self._timestamp()}_{uuid.uuid4().hex[:8]}"
        storage_state_path = self._ensure_browser_storage_state(resume_id)
        filename = Path(self.config.get("resume_out") or output_dir / f"resume_{resume_id}.json")

        data = {
            "schema_version": 1,
            "resume_id": resume_id,
            "stage": "manual_plus_required",
            "created_at": datetime.now().isoformat(),
            "account_key": self.result.get("email") or self.result.get("account_id") or self.result.get("phone_number") or "",
            "phone_number": self.result.get("phone_number", ""),
            "activation_id": self.result.get("activation_id", ""),
            "chatgpt_access_token_initial": self.result.get("access_token", ""),
            "access_token": self.result.get("access_token", ""),
            "session_token": self.result.get("session_token", ""),
            "chatgpt_account_id": self.result.get("account_id", ""),
            "account_id": self.result.get("account_id", ""),
            "email": self.result.get("email", ""),
            "outlook_email": self.config.get("outlook_email", ""),
            "generated_chatgpt_password": self.result.get("password", ""),
            "password": self.result.get("password", ""),
            "plan_type_before_activation": self.result.get("plan_type", ""),
            "plan_type": self.result.get("plan_type", ""),
            "browser_storage_state_path": storage_state_path,
            "registration_proxy": self.result.get("registration_proxy", ""),
            "registration_proxy_exit_ip": self.result.get("registration_proxy_exit_ip", ""),
            "manual_plus_status": self.result.get("manual_plus_status") or "pending",
            "iceaix_job_id": self.result.get("iceaix_job_id", ""),
            "iceaix_status": self.result.get("iceaix_status", ""),
            "iceaix_result_code": self.result.get("iceaix_result_code", ""),
            "iceaix_billing_status": self.result.get("iceaix_billing_status", ""),
            "iceaix_resource_mode": self.result.get("iceaix_resource_mode", ""),
            "manual_plus_url": "https://plus.iceaix.com/",
            "manual_next_step": "Open https://plus.iceaix.com/, activate this ChatGPT access_token with CDK, then run resume-oauth with --manual-plus-confirmed.",
        }

        filename.parent.mkdir(parents=True, exist_ok=True)
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        self._write_account_text(filename, stage="manual_plus_required", include_tokens=True)
        self._save_token_pool_entry(filename)
        try:
            data["resume_file"] = str(filename)
            account_store.upsert_account(data, source_file=str(filename), copy_artifacts=True)
        except Exception as exc:
            self.log(f"  规范化账号存储写入失败: {exc}")
        self._save_resume_scripts(filename, resume_id)
        return filename

    def _load_resume_file_if_configured(self, *, required: bool) -> dict:
        resume_file = str(self.config.get("resume_file") or "").strip()
        if not resume_file:
            if required:
                raise RuntimeError("缺少 --resume-file")
            return {}
        path = Path(resume_file)
        if not path.exists():
            raise RuntimeError(f"resume 文件不存在: {resume_file}")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        if not isinstance(data, dict):
            raise RuntimeError(f"resume 文件格式错误: {resume_file}")

        initial_token = data.get("chatgpt_access_token_initial") or data.get("access_token", "")
        self.result.update(
            {
                "phone_number": data.get("phone_number", ""),
                "activation_id": data.get("activation_id", ""),
                "chatgpt_access_token_initial": initial_token,
                "access_token": data.get("access_token") or initial_token,
                "account_id": data.get("account_id") or data.get("chatgpt_account_id", ""),
                "email": data.get("email", ""),
                "plan_type": data.get("plan_type", ""),
                "password": data.get("password") or data.get("generated_chatgpt_password", ""),
                "resume_file": str(path),
                "registration_proxy": data.get("registration_proxy", ""),
                "registration_proxy_exit_ip": data.get("registration_proxy_exit_ip", ""),
                "iceaix_job_id": data.get("iceaix_job_id", ""),
                "iceaix_status": data.get("iceaix_status", ""),
                "iceaix_result_code": data.get("iceaix_result_code", ""),
                "iceaix_billing_status": data.get("iceaix_billing_status", ""),
                "iceaix_resource_mode": data.get("iceaix_resource_mode", ""),
            }
        )
        saved_proxy = str(self.result.get("registration_proxy") or "").strip()
        if saved_proxy and "127.0.0.1" not in saved_proxy and "localhost" not in saved_proxy:
            self.config["proxy"] = saved_proxy
            saved_exit_ip = str(self.result.get("registration_proxy_exit_ip") or "").strip()
            if saved_exit_ip:
                self.config["_camoufox_geoip_ip"] = saved_exit_ip
        if data.get("browser_storage_state_path"):
            self.result["browser_storage_state_path"] = data.get("browser_storage_state_path")
            self.config["_browser_storage_state"] = data.get("browser_storage_state_path")
        if data.get("outlook_email") and not self.config.get("outlook_email"):
            self.config["outlook_email"] = data.get("outlook_email")
        return data

    def _save_final_tokens_json(self, oauth_tokens: dict) -> Path:
        current_plan = str(self.result.get("plan_type") or "").strip().lower()
        token_plan = self._extract_plan_from_tokens()
        plan_type = current_plan if self._is_paid_plan(current_plan) else token_plan
        self.result["plan_type"] = plan_type
        email_value = str(self.result.get("email") or self.config.get("outlook_email", "")).strip()
        required_fields = {
            "access_token": str(self.result.get("access_token") or "").strip(),
            "refresh_token": str(self.result.get("refresh_token") or "").strip(),
            "id_token": str(self.result.get("id_token") or "").strip(),
            "account_id": str(self.result.get("account_id") or "").strip(),
            "email": email_value,
        }
        missing = [name for name, value in required_fields.items() if not value]
        if missing:
            raise RuntimeError(f"拒绝写入成品目录，缺少字段: {', '.join(missing)}")
        if not self._is_paid_plan(plan_type):
            raise RuntimeError(f"拒绝写入成品目录，套餐未验证为 paid: {plan_type or 'unknown'}")

        products_dir = self._products_dir()
        filename = products_dir / f"{self._product_label(email_value)}.json"
        registration_proxy = str(self.result.get("registration_proxy") or "")
        subscription_proxy = str(self.result.get("subscription_check_proxy") or "")
        proxy_record = {
            "provider": "lajiao_http",
            "registration_proxy": "" if "127.0.0.1" in registration_proxy or "localhost" in registration_proxy else registration_proxy,
            "registration_exit_ip": self.result.get("registration_proxy_exit_ip", ""),
            "subscription_check_proxy": "" if "127.0.0.1" in subscription_proxy or "localhost" in subscription_proxy else subscription_proxy,
            "subscription_check_source": self.result.get("subscription_check_source", ""),
        }
        data = {
            "schema_version": 1,
            "stage": "complete",
            "status": "complete",
            "completed_at": datetime.now().isoformat(),
            "phone_number": self.result.get("phone_number", ""),
            "email": email_value,
            "password": self.result.get("password", ""),
            "account_id": self.result.get("account_id", ""),
            "plan_type": plan_type,
            "proxy": proxy_record,
            "subscription_check_source": self.result.get("subscription_check_source", ""),
            "subscription_check_proxy": proxy_record.get("subscription_check_proxy", ""),
            "iceaix": {
                "job_id": self.result.get("iceaix_job_id", ""),
                "status": self.result.get("iceaix_status", ""),
                "result_code": self.result.get("iceaix_result_code", ""),
                "billing_status": self.result.get("iceaix_billing_status", ""),
                "resource_mode": self.result.get("iceaix_resource_mode", ""),
            },
            "chatgpt_access_token_initial": self.result.get("chatgpt_access_token_initial", ""),
            "access_token": self.result.get("access_token", ""),
            "refresh_token": self.result.get("refresh_token", ""),
            "id_token": self.result.get("id_token", ""),
            "oauth_result": oauth_tokens or {},
            "resume_file": self.result.get("resume_file", ""),
        }
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        try:
            account_store.upsert_account(data, source_file=str(filename), copy_artifacts=True)
        except Exception as exc:
            self.log(f"  规范化账号存储写入失败: {exc}")
        return filename

    def _cleanup(self):
        """清理浏览器资源。"""
        if self.browser_context:
            try:
                self.browser_context.close()
            except Exception:
                pass
            self.browser_context = None
        if self.browser:
            try:
                self.browser.close()
            except Exception:
                pass
            self.browser = None
        if self._camoufox_ctx:
            try:
                self._camoufox_ctx.__exit__(None, None, None)
            except Exception:
                pass
            self._camoufox_ctx = None
        if self._local_socks_bridge:
            try:
                self._local_socks_bridge.close()
            except Exception:
                pass
            self._local_socks_bridge = None
        proxy_runtime = getattr(self, "_proxy_runtime", None)
        if proxy_runtime:
            try:
                proxy_runtime.cleanup()
            except Exception:
                pass
            self._proxy_runtime = None
        if self.playwright_instance:
            try:
                self.playwright_instance.stop()
            except Exception:
                pass
            self.playwright_instance = None
        self.page = None
        self._clear_registration_auth_state()

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _save_session_json(self, result):
        """保存 session JSON 到 output 目录。"""
        output_dir = Path(self.config.get("output_dir", "output"))
        output_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = output_dir / f"session_{ts}.json"

        data = {
            "phone_number": result.phone_number,
            "access_token": result.access_token,
            "account_id": result.account_id,
            "email": result.email,
            "plan_type": result.plan_type,
            "exported_at": datetime.now().isoformat(),
        }

        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        self.log(f"  Session 已保存: {filename}")

    def _save_tokens_json(self, tokens: dict):
        """保存完整 token JSON。"""
        output_dir = self._output_dir() / "tokens"
        output_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = output_dir / f"tokens_{ts}.json"

        safe_tokens = {}
        for key in ("access_token", "refresh_token", "id_token", "chatgpt_account_id", "email"):
            if key in tokens:
                safe_tokens[key] = tokens[key]
        safe_tokens["exported_at"] = datetime.now().isoformat()

        with open(filename, "w", encoding="utf-8") as f:
            json.dump(safe_tokens, f, indent=2, ensure_ascii=False)
        self.log(f"  Tokens 已保存: {filename}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="GPT Register — ChatGPT 手机号注册 + Plus 激活",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python full_pipeline.py --config config.yaml                                      # 全链路
  python full_pipeline.py --config config.yaml --headed                             # 显示浏览器
  python full_pipeline.py --config config.yaml --step register-token                 # 注册并保存手动 Plus 交接文件
  python full_pipeline.py --config config.yaml --step resume-oauth --resume-file output/resume_xxx.json --manual-plus-confirmed
  python full_pipeline.py --config config.yaml --step resume-oauth --resume-file output/resume_xxx.json --manual-plus-confirmed --oauth-callback-mode cpa --cpa-base <url> --cpa-key <key>
  python full_pipeline.py --config config.yaml --step upload --resume-file output/final_xxx.json
        """
    )
    parser.add_argument("--config", "-c", default="config.yaml", help="配置文件路径")
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    parser.add_argument(
        "--step",
        default="register",
        choices=["register", "register-token", "email-register-token", "phone", "activate", "oauth", "resume-oauth", "upload"],
        help="起始步骤",
    )
    parser.add_argument("--resume-file", default="", help="register-token 生成的交接 JSON，供 resume-oauth/upload 使用")
    parser.add_argument("--resume-out", default="", help="register-token 输出交接 JSON 路径")
    parser.add_argument("--manual-plus-confirmed", action="store_true", help="确认已在 plus.iceaix.com 手动完成 Plus 开通")
    parser.add_argument("--email-otp", default="", help="OAuth 绑定邮箱验证码；非交互环境用它提交验证码")
    parser.add_argument("--oauth-callback-mode", choices=["local", "cpa"], default="", help="resume-oauth callback 处理方式：local 本地换 token，cpa 提交 CPA 入库")
    parser.add_argument("--cpa-base", default="", help="CPA base URL，用于 /v0/management/codex-auth-url 与 /oauth-callback")
    parser.add_argument("--cpa-key", default="", help="CPA management key")
    args = parser.parse_args()

    # 加载配置
    config = load_config(args.config)
    if args.headed:
        config["headed"] = True
    if args.resume_file:
        config["resume_file"] = args.resume_file
    if args.resume_out:
        config["resume_out"] = args.resume_out
    if args.manual_plus_confirmed:
        config["manual_plus_confirmed"] = True
    if args.email_otp:
        config["email_otp"] = args.email_otp
    if args.oauth_callback_mode:
        config["oauth_callback_mode"] = args.oauth_callback_mode
    if args.cpa_base:
        config["cpa_base_url"] = args.cpa_base
    if args.cpa_key:
        config["cpa_management_key"] = args.cpa_key
    # 验证最小配置
    if str(config.get("sms_provider") or "herosms_api").strip().lower() in {"herosms", "herosms_api"} and not config.get("sms_api_key"):
        print("[!] 警告: 未配置 sms_api_key (HeroSMS)，HeroSMS 取号会失败")

    # 运行
    pipeline = RegisterPipeline(config)
    result = pipeline.run(start_step=args.step, headed=args.headed)

    # 输出摘要
    print("\n" + "=" * 60)
    print("执行摘要")
    print("=" * 60)
    print(f"  成功: {result['success']}")
    print(f"  手机号: {result.get('phone_number', 'N/A')}")
    print(f"  Account ID: {result.get('account_id', 'N/A')}")
    print(f"  Email: {result.get('email', 'N/A')}")
    print(f"  Plan: {result.get('plan_type', 'N/A')}")
    print(f"  状态: {result.get('status', '')}")
    print(f"  已执行步骤: {', '.join(result.get('steps', []))}")
    if result.get("resume_file"):
        print(f"  交接文件: {result.get('resume_file')}")
    if result.get("final_file"):
        print(f"  最终文件: {result.get('final_file')}")
    if result.get("text_file"):
        print(f"  文本文件: {result.get('text_file')}")
    print(f"  输出目录: {config.get('output_dir', 'output')}/")
    print("=" * 60)

    if not result["success"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
