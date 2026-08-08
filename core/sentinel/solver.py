"""Unified Sentinel anti-bot token solver.

Merges three strategies from the archived sentinel modules:
  1. Pure Python FNV-1a PoW  (sentinel_token.py)
  2. QuickJS VM via Node.js    (sentinel_browser.py)
  3. Turnstile Solver service  (localhost:8889)
"""

from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Sentinel SDK version string from sentinel_browser.py
SDK_VERSION = "20260219f9f6"


class SentinelSolver:
    """Multi-strategy Sentinel anti-bot token solver.

    Usage::

        solver = SentinelSolver()
        token = solver.solve(device_id, flow, user_agent, proxy=…)
    """

    SDK_VERSION = SDK_VERSION

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def solve(
        self,
        device_id: str,
        flow: str,
        user_agent: str,
        proxy: Optional[str] = None,
    ) -> Optional[str]:
        """Return a Sentinel token JSON string, or *None* if all strategies fail.

        Strategies are attempted in descending order of completeness:
        1. Turnstile Solver service (localhost:8889)
        2. QuickJS VM via Node.js
        3. Pure Python FNV-1a PoW (may return *None* when PoW unsolved)
        """
        result = self._solve_via_service(device_id, flow, user_agent, proxy)
        if result:
            return result

        result = self._solve_quickjs(device_id, flow, user_agent, proxy)
        if result:
            return result

        result = self._solve_pow(device_id, flow, user_agent)
        return result

    # ------------------------------------------------------------------
    # Strategy 1 – Pure Python FNV-1a PoW
    # ------------------------------------------------------------------

    def _solve_pow(
        self,
        device_id: str,
        flow: str,
        user_agent: str,
    ) -> Optional[str]:
        """PoW strategy: fetch challenge from Sentinel API, brute-force nonce."""
        from platforms.chatgpt.sentinel_token import (
            SentinelTokenGenerator,
            fetch_sentinel_challenge,
        )

        try:
            import requests as _requests
        except ImportError:
            logger.warning("requests library not available for PoW strategy")
            return None

        session = _requests.Session()
        try:
            challenge = fetch_sentinel_challenge(
                session,
                device_id,
                flow=flow,
                user_agent=user_agent,
            )
            if not challenge:
                logger.debug("PoW: no challenge returned")
                return None

            c_value = str(challenge.get("token") or "").strip()
            if not c_value:
                logger.debug("PoW: empty challenge token")
                return None

            gen = SentinelTokenGenerator(
                device_id=device_id,
                user_agent=user_agent,
            )
            pow_data = challenge.get("proofofwork") or {}
            if pow_data.get("required") and pow_data.get("seed"):
                p_value = gen.generate_token(
                    seed=pow_data.get("seed"),
                    difficulty=pow_data.get("difficulty", "0"),
                )
            else:
                p_value = gen.generate_requirements_token()

            return json.dumps(
                {
                    "p": p_value,
                    "t": "",
                    "c": c_value,
                    "id": device_id,
                    "flow": flow,
                },
                separators=(",", ":"),
            )
        except Exception:
            logger.exception("PoW strategy failed")
            return None
        finally:
            session.close()

    # ------------------------------------------------------------------
    # Strategy 2 – QuickJS VM via Node.js
    # ------------------------------------------------------------------

    def _solve_quickjs(
        self,
        device_id: str,
        flow: str,
        user_agent: str,
        proxy: Optional[str],
    ) -> Optional[str]:
        """QuickJS strategy: run Sentinel SDK in Node.js subprocess."""
        try:
            from platforms.chatgpt import sentinel_browser
        except ImportError:
            logger.warning("sentinel_browser archive not available")
            return None

        def _log(msg: str) -> None:
            logger.debug("QuickJS: %s", msg)

        return sentinel_browser._get_sentinel_token_via_quickjs(
            flow=flow,
            proxy=proxy,
            timeout_ms=30000,
            device_id=device_id,
            logger=_log,
        )

    # ------------------------------------------------------------------
    # Strategy 3 – Turnstile Solver service (localhost:8889)
    # ------------------------------------------------------------------

    def _solve_via_service(
        self,
        device_id: str,
        flow: str,
        user_agent: str,
        proxy: Optional[str],
    ) -> Optional[str]:
        """Service strategy: delegate to local Turnstile Solver on port 8889."""
        try:
            import requests as _requests
        except ImportError:
            logger.warning("requests library not available for service strategy")
            return None

        try:
            resp = _requests.post(
                "http://127.0.0.1:8889/solve",
                json={
                    "device_id": device_id,
                    "flow": flow,
                    "user_agent": user_agent,
                    "proxy": proxy,
                },
                timeout=30,
            )
            if resp.status_code == 200:
                data = resp.json()
                token = data.get("token") or data.get("result")
                if token:
                    return str(token)
        except Exception:
            logger.debug("Turnstile solver service unavailable", exc_info=True)

        return None
