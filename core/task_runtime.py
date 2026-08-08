"""Task runtime stub.

Full implementation exists in any-auto-register/core/task_runtime.py.
This stub provides the minimal interface needed by oauth_client.py.
"""

from __future__ import annotations


class TaskInterruption(Exception):
    """Task interruption exception."""
    pass
