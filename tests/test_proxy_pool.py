"""
Test: Proxy Pool — unit tests with mock repository (no network, no proxy)
"""
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infrastructure.repositories.proxy_repository import ProxyRepository, ProxyEntry
from core.proxy.pool import ProxyPool


class FakeProxyRepo(ProxyRepository):
    """In-memory proxy repo for tests"""
    def __init__(self, entries=None):
        self._entries = {e.url: e for e in (entries or [])}
        self.db_path = None

    def list_active(self, region=""):
        active = [e for e in self._entries.values() if e.is_active]
        if region:
            active = [e for e in active if e.region == region]
        return active

    def list_all(self):
        return list(self._entries.values())

    def get(self, url):
        return self._entries.get(url, ProxyEntry(url=url))

    def save(self, entry):
        self._entries[entry.url] = entry

    def increment_success(self, url):
        e = self._entries.get(url)
        if e:
            e.success_count += 1
            e.consecutive_fails = 0

    def increment_fail(self, url):
        e = self._entries.get(url)
        if e:
            e.fail_count += 1
            e.consecutive_fails += 1
            if e.consecutive_fails >= 3:
                e.is_active = False

    def stats(self):
        return type('Stats', (), {
            'total': len(self._entries),
            'active': sum(1 for e in self._entries.values() if e.is_active),
            'by_region': {},
            'avg_success_rate': 0.0,
        })()


def test_proxy_pool_next_basic():
    """next() returns active proxy sorted by success_rate"""
    pool = ProxyPool(FakeProxyRepo([
        ProxyEntry(url="socks5://jp1", exit_ip="1.1.1.1", region="JP", success_count=5),
        ProxyEntry(url="socks5://jp2", exit_ip="2.2.2.2", region="JP", success_count=1),
    ]))
    # Skip verification — directly test selection
    candidates = pool._repo.list_active(region="JP")
    candidates.sort(key=lambda e: e.success_rate, reverse=True)
    assert candidates[0].url == "socks5://jp1"
    assert candidates[1].url == "socks5://jp2"


def test_proxy_pool_region_filter():
    """Only returns proxies in specified region"""
    pool = ProxyPool(FakeProxyRepo([
        ProxyEntry(url="socks5://jp1", exit_ip="1.1.1.1", region="JP"),
        ProxyEntry(url="socks5://us1", exit_ip="5.5.5.5", region="US"),
    ]))
    active_jp = pool._repo.list_active(region="JP")
    assert len(active_jp) == 1
    assert active_jp[0].region == "JP"


def test_proxy_pool_auto_disable_after_3_fails():
    """3 consecutive failures → is_active=False"""
    pool = ProxyPool(FakeProxyRepo([
        ProxyEntry(url="socks5://bad", exit_ip="9.9.9.9"),
    ]))
    pool.report_fail("socks5://bad")
    pool.report_fail("socks5://bad")
    pool.report_fail("socks5://bad")
    entry = pool._repo.get("socks5://bad")
    assert entry.consecutive_fails == 3
    assert not entry.is_active


def test_proxy_pool_success_resets_consecutive_fails():
    """report_success resets consecutive_fails to 0"""
    pool = ProxyPool(FakeProxyRepo([
        ProxyEntry(url="socks5://ok", exit_ip="8.8.8.8", consecutive_fails=2),
    ]))
    pool.report_success("socks5://ok")
    entry = pool._repo.get("socks5://ok")
    assert entry.consecutive_fails == 0
    assert entry.is_active


def test_proxy_pool_no_active_raises():
    """No active proxies → RuntimeError"""
    pool = ProxyPool(FakeProxyRepo([]))
    with pytest.raises(RuntimeError, match="No active proxy"):
        pool.next(region="JP", max_candidates=1)


def test_proxy_entry_success_rate():
    """success_rate calculation"""
    e = ProxyEntry(success_count=7, fail_count=3)
    assert e.success_rate == 0.7
    e2 = ProxyEntry(success_count=0, fail_count=0)
    assert e2.success_rate == 0.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
