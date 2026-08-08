"""Fail closed when tracked files are not suitable for a public source release."""

from __future__ import annotations

import argparse
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from typing import Iterable


PRIVATE_ROOTS = frozenset(
    {
        "accounts",
        "auth",
        "browser_profiles",
        "data",
        "logs",
        "log",
        "output",
        "runtime",
        "tmp",
    }
)
BUILD_OR_DEPENDENCY_DIRECTORIES = frozenset(
    {
        ".cache",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "coverage",
        "dist",
        "htmlcov",
        "node_modules",
        "test-results",
        "venv",
    }
)
BANNED_EXTENSIONS = frozenset(
    {
        ".cer",
        ".crt",
        ".db",
        ".db3",
        ".der",
        ".egg",
        ".gz",
        ".har",
        ".jks",
        ".jsonl",
        ".key",
        ".keystore",
        ".log",
        ".npm",
        ".p12",
        ".pem",
        ".pfx",
        ".pyc",
        ".pyo",
        ".sqlite",
        ".sqlite3",
        ".tgz",
        ".tsbuildinfo",
        ".whl",
        ".zip",
    }
)
CONFIG_EXTENSIONS = frozenset({".cfg", ".conf", ".ini", ".json", ".jsonl", ".properties", ".toml", ".yaml", ".yml"})
SOURCE_EXTENSIONS = frozenset({".c", ".cc", ".cpp", ".css", ".go", ".h", ".html", ".java", ".js", ".jsx", ".md", ".py", ".rs", ".rst", ".sh", ".ts", ".tsx"})
PUBLIC_CONFIG_PATHS = frozenset({".github/issue_template/config.yml"})
CREDENTIAL_FILE_FAMILY = re.compile(
    r"(?:^|[._-])(?:api[_-]?key|auth|cookie|credential|credentials|mailbox|password|passwd|pass|proxy|proxies|secret|session|storage|token|tokens|resume|account|accounts)(?:[._-]|$)",
    re.IGNORECASE,
)
SECRET_ASSIGNMENT = re.compile(
    r"""(?imx)
    ^\s*[\"']?
    (?P<key>[a-z0-9_.-]*(?:api[_-]?key|secret|password|passwd|pass|token|credentials?))
    [\"']?\s*[:=]\s*
    (?P<value>[^#\r\n]+)
    """
)
EMPTY_VALUES = frozenset({"", "[]", "{}", "false", "none", "null", "~", "true"})


def tracked_files(root: Path) -> list[PurePosixPath]:
    """Return tracked paths, failing closed when Git metadata is unavailable."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError("unable to enumerate tracked files with git ls-files") from error

    return [
        PurePosixPath(value)
        for value in result.stdout.decode("utf-8", errors="surrogateescape").split("\0")
        if value
    ]


def path_reason(path: PurePosixPath) -> str | None:
    """Describe why a tracked path is private or generated, if applicable."""
    parts = tuple(part.lower() for part in path.parts)
    if not parts:
        return "empty tracked path"

    if parts[0] in PRIVATE_ROOTS:
        return f"private runtime root: {parts[0]}"
    if any(part in BUILD_OR_DEPENDENCY_DIRECTORIES for part in parts):
        return "build or dependency directory"
    if parts[0] == "config":
        return "private configuration root"

    filename = parts[-1]
    suffix = PurePosixPath(filename).suffix.lower()
    is_public_config = path.as_posix().lower() in PUBLIC_CONFIG_PATHS
    if (
        filename in {"config.yaml", "config.yml"} and not is_public_config
    ) or (
        filename.startswith("config.")
        and not is_public_config
        and ".example." not in filename
        and suffix not in SOURCE_EXTENSIONS
    ):
        return "private configuration file"
    if filename == ".env" or filename.startswith(".env.") or filename.endswith(".env"):
        return "environment file"
    if filename in {"id_dsa", "id_ecdsa", "id_ed25519", "id_rsa"}:
        return "private key file"
    if ".db-" in filename or ".sqlite-" in filename:
        return "database sidecar file"

    if suffix in BANNED_EXTENSIONS:
        return f"sensitive or generated file type: {suffix}"
    if CREDENTIAL_FILE_FAMILY.search(PurePosixPath(filename).stem) and suffix not in SOURCE_EXTENSIONS:
        return "credential-bearing filename family"
    return None


def nonempty_secret_value(value: str) -> bool:
    """Allow empty and boolean sample settings while rejecting populated secret fields."""
    normalized = value.strip().strip("\"'").strip().lower()
    return normalized not in EMPTY_VALUES


def content_reason(root: Path, path: PurePosixPath) -> str | None:
    """Detect populated secret-style settings in tracked configuration data."""
    suffix = path.suffix.lower()
    if suffix not in CONFIG_EXTENSIONS:
        return None

    try:
        content = (root / path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        return f"unable to safely inspect configuration content: {error.__class__.__name__}"

    for match in SECRET_ASSIGNMENT.finditer(content):
        if nonempty_secret_value(match.group("value")):
            return f"populated sensitive setting: {match.group('key')}"
    return None


def find_violations(root: Path, paths: Iterable[PurePosixPath]) -> list[tuple[PurePosixPath, str]]:
    """Inspect every tracked path and return all release-tree violations."""
    violations: list[tuple[PurePosixPath, str]] = []
    for path in paths:
        reason = path_reason(path) or content_reason(root, path)
        if reason:
            violations.append((path, reason))
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.root.resolve()

    try:
        violations = find_violations(root, tracked_files(root))
    except RuntimeError as error:
        print(f"release-tree check failed: {error}", file=sys.stderr)
        return 2

    if violations:
        print("release-tree check failed; remove these non-public tracked files:", file=sys.stderr)
        for path, reason in violations:
            print(f"  {path.as_posix()}: {reason}", file=sys.stderr)
        return 1

    print("release-tree check passed: tracked files are suitable for a public source release.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
