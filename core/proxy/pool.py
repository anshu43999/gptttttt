"""
代理池 — 保留现有出口IP验证 + GeoIP对齐 + 单号单IP。
新增: DB持久化 + 健康状况追踪 + 自动禁用 (3次连续失败)。
"""
from __future__ import annotations

import threading
import time

import requests

from infrastructure.repositories.proxy_repository import ProxyRepository, ProxyEntry


class ProxyPool:
    def __init__(self, repo: ProxyRepository | None = None):
        self._repo = repo or ProxyRepository()
        self._index = 0
        self._lock = threading.Lock()
        self._used_ips: set[str] = set()

    # ── proxy supply ──────────────────────────────────────

    def load_from_lajiao_api(self, api_url: str, region: str = "JP",
                              count: int = 3, timeout: int = 30) -> int:
        """Fetch proxies from Lajiao HTTP API. Returns count loaded.

        Preserved from full_pipeline.py _fetch_lajiao_proxy_candidates.
        """
        from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

        parsed = urlparse(api_url)
        has_query = bool(parsed.query)

        if has_query:
            query = dict(parse_qsl(parsed.query, keep_blank_values=True))
            if query.get("regions") != region:
                query["regions"] = region
                api_url = urlunparse(parsed._replace(query=urlencode(query)))
            params = None
        else:
            params = {
                "regions": region,
                "num": str(count),
                "protocol": "socks5",
                "type": "txt",
                "cate": "2",
                "t": "10",
                "lb": "1",
            }

        resp = requests.get(api_url, params=params, timeout=timeout)
        resp.raise_for_status()
        text = (resp.text or "").strip()

        ct = str(resp.headers.get("content-type") or "").lower()
        wants_json = (params or {}).get("type") == "json" or "json" in ct or text.startswith("{")

        loaded = 0
        if wants_json:
            data = resp.json()
            if not data.get("success", data.get("code") == 0):
                raise RuntimeError(f"Lajiao API returned error: {data}")
            for item in data.get("data") or []:
                raw = item.get("ip") + ":" + item.get("port") if isinstance(item, dict) else str(item)
                url = self._normalize_url(raw.strip())
                if url and "None" not in url:
                    self.add_from_lajiao_api(url, region)
                    loaded += 1
        else:
            for line in text.replace("\r", "\n").split("\n"):
                url = self._normalize_url(line.strip())
                if url and ":" in url and "None" not in url:
                    self.add_from_lajiao_api(url, region)
                    loaded += 1

        if not loaded:
            raise RuntimeError(f"Lajiao API returned no proxies: {text[:200]}")
        return loaded

    def load_from_credentials(self, raw: str, region: str = "JP",
                               file_path: str = "", protocol: str = "http") -> int:
        """Parse credential proxy lines. Returns count loaded.

        Preserved from full_pipeline.py _credential_proxy_candidates.
        """
        rows: list[str] = []
        if isinstance(raw, (list, tuple)):
            rows.extend(str(r) for r in raw)
        else:
            rows.extend(str(raw).replace("\r", "\n").split("\n"))

        if file_path:
            from pathlib import Path
            p = Path(file_path)
            if not p.exists():
                raise RuntimeError(f"Credential proxy file not found: {file_path}")
            rows.extend(p.read_text(encoding="utf-8").splitlines())

        loaded = 0
        for row in rows:
            value = row.strip().strip('"').strip("'")
            if not value or value.startswith("#"):
                continue
            if "://" not in value:
                proto = str(protocol or "http").strip().lower() or "http"
                value = proto + "://" + value
            self.add_from_credentials(value, region)
            loaded += 1

        if not loaded:
            raise RuntimeError("No credential proxy candidates found")
        return loaded

    @staticmethod
    def _normalize_url(raw: str) -> str:
        """Normalize: socks5:// → socks5h:// (prevent DNS leaks).
        
        Preserved from full_pipeline.py _proxy_check_url / _proxy_runtime_url.
        """
        value = raw.strip()
        if not value:
            return ""
        if "://" not in value:
            return "socks5://" + value
        if value.startswith("socks5://"):
            return "socks5h://" + value[len("socks5://"):]
        return value

    def add_from_lajiao_api(self, url: str, region: str = "") -> None:
        url = url.strip()
        if not url: return
        entry = ProxyEntry(url=url, region=region, source="lajiao_api")
        self._repo.save(entry)

    def add_from_credentials(self, url: str, region: str = "") -> None:
        url = url.strip()
        if not url: return
        entry = ProxyEntry(url=url, region=region, source="lajiao_credentials")
        self._repo.save(entry)

    def add_manual(self, url: str, region: str = "") -> None:
        url = url.strip()
        if not url: return
        entry = ProxyEntry(url=url, region=region, source="manual")
        self._repo.save(entry)
    # ── proxy selection ───────────────────────────────────

    def next(self, region: str = "", exclude_ips: set[str] | None = None,
             max_candidates: int = 10) -> str:
        candidates = [e for e in self._repo.list_active(region=region)
                      if e.exit_ip not in (exclude_ips or set())]
        if not candidates:
            raise RuntimeError(f"No active proxy in region '{region}'")

        # sort by success_rate descending
        candidates.sort(key=lambda e: e.success_rate, reverse=True)

        for entry in candidates[:max_candidates]:
            if self._verify_and_update(entry):
                self._used_ips.add(entry.exit_ip)
                return entry.url

        raise RuntimeError(f"No verified proxy available in region '{region}'")

    def _verify_and_update(self, entry: ProxyEntry) -> bool:
        """三重验证: ipify → ipinfo → chatgpt CSRF"""
        proxies = {"http": entry.url, "https": entry.url}
        try:
            # 1. 获取出口IP
            ip = requests.get("https://api.ipify.org", proxies=proxies, timeout=10).text.strip()

            # 2. 国家匹配
            if entry.region:
                info = requests.get(f"https://ipinfo.io/{ip}/json", timeout=10).json()
                if info.get("country") != entry.region:
                    self.report_fail(entry.url)
                    return False

            # 3. ChatGPT CSRF 可达性
            r = requests.get("https://chatgpt.com/api/auth/csrf",
                             proxies=proxies, timeout=15,
                             headers={"accept": "*/*"})
            if r.status_code != 200:
                self.report_fail(entry.url)
                return False

            entry.exit_ip = ip
            self._repo.save(entry)
            return True

        except Exception:
            self.report_fail(entry.url)
            return False

    # ── health feedback ───────────────────────────────────

    def report_success(self, url: str) -> None:
        self._repo.increment_success(url)

    def report_fail(self, url: str) -> None:
        self._repo.increment_fail(url)

    # ── stats ─────────────────────────────────────────────

    @property
    def stats(self):
        return self._repo.stats()

    def reset_used_ips(self) -> None:
        self._used_ips.clear()
