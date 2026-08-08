from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAILAT_PROTOCOL_RUNTIME_ROOT = (
    PROJECT_ROOT / "vendor" / "mailat-codex-register"
)


class MailatProtocolRuntime(NamedTuple):
    root: Path
    package_json: Path
    sdk: Path
    entry: Path
    tsx: Path


def validate_mailat_protocol_runtime() -> MailatProtocolRuntime:
    """Validate and return the fixed project-local Mailat runtime."""
    root = MAILAT_PROTOCOL_RUNTIME_ROOT
    runtime = MailatProtocolRuntime(
        root=root,
        package_json=root / "package.json",
        sdk=root / "sdk.js",
        entry=root / "src" / "index.ts",
        tsx=(
            root / "node_modules" / ".bin"
            / ("tsx.cmd" if os.name == "nt" else "tsx")
        ),
    )
    required = {
        "package.json": runtime.package_json,
        "sdk.js": runtime.sdk,
        "src/index.ts": runtime.entry,
        "tsx": runtime.tsx,
    }
    missing = [label for label, path in required.items() if not path.is_file()]
    if missing:
        raise RuntimeError(
            f"项目内置 Mailat 协议运行时不完整: {runtime.root}；缺少 {', '.join(missing)}"
        )
    if os.name != "nt" and not os.access(runtime.tsx, os.X_OK):
        raise RuntimeError(f"项目内置 Mailat tsx 不可执行: {runtime.tsx}")
    return runtime
