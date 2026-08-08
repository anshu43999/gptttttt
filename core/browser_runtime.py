"""Browser runtime configuration stub.

Full implementation exists in any-auto-register/core/browser_runtime.py.
This stub provides the minimal interface needed by sentinel_browser.py.
"""

from __future__ import annotations

from typing import Any, Optional


def get_browser_executable() -> Optional[str]:
    """Get the browser executable path. Returns None for default."""
    return None


def get_browser_user_data_dir(platform: str = "chatgpt") -> Optional[str]:
    """Get browser user data directory for persistent profiles."""
    return None


def ensure_browser_display_available(headless: bool = True) -> bool:
    """Check if a browser display is available for headed mode."""
    return True if headless else True


def resolve_browser_headless(headless: Optional[bool] = None) -> tuple[bool, str]:
    """Resolve whether to run browser in headless mode."""
    if headless is None:
        return True, "default headless"
    return bool(headless), "configured by caller"
