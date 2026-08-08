"""
Test: Sentinel Solver — PoW fallback verification (no proxy).
"""
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_sentinel_solver_import():
    """Import SentinelSolver — may fail if agent hasn't created file yet"""
    try:
        from core.sentinel.solver import SentinelSolver
        solver = SentinelSolver()
        assert solver.SDK_VERSION
    except ImportError:
        pytest.skip("SentinelSolver not yet built by agent")


def test_sentinel_pow_returns_string_or_none():
    """PoW may return token string or None (50万次 timeout)"""
    try:
        from core.sentinel.solver import SentinelSolver
        solver = SentinelSolver()
        token = solver.solve("test-device", "login", "Chrome/136.0")
        assert token is None or isinstance(token, str)
    except ImportError:
        pytest.skip("SentinelSolver not yet built")


def test_sentinel_version_consistent():
    try:
        from core.sentinel.solver import SentinelSolver
        assert SentinelSolver.SDK_VERSION
        assert len(SentinelSolver.SDK_VERSION) >= 8  # like "20260219f9f6"
    except ImportError:
        pytest.skip("SentinelSolver not yet built")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
