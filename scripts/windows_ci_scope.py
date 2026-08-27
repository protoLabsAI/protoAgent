"""Classify a change set for the Windows-native CI gates.

The classifier is deliberately fail-safe: unknown paths run the Windows Python
suite. Only well-understood documentation, web, marketing, and desktop-native
paths may skip it. Pushes to ``main`` bypass classification and run every gate.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass


_CONTROL_PATHS = {
    ".github/workflows/checks.yml",
    "scripts/windows_ci_scope.py",
    "tests/test_windows_ci_scope.py",
}
_PYTHON_SAFE_PREFIXES = (
    "apps/web/",
    "changelog.d/",
    "docs/",
    "sites/marketing/",
)
_PYTHON_SAFE_ROOT_FILES = {
    ".editorconfig",
    ".gitattributes",
    ".gitignore",
    ".npmrc",
    "AGENTS.md",
    "CLAUDE.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "PROTO.md",
    "README.md",
    "THIRD_PARTY_LICENSES.md",
    "package-lock.json",
    "package.json",
}


@dataclass(frozen=True)
class WindowsScope:
    """Windows gates required by a set of changed repository paths."""

    python_tests: bool
    rust_tests: bool


def _normalize(path: str) -> str:
    """Return a repository-relative path with stable POSIX separators."""

    normalized = path.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _needs_windows_python(path: str) -> bool:
    """Return whether *path* can affect Python behavior on Windows."""

    if path in _CONTROL_PATHS:
        return True
    if path in _PYTHON_SAFE_ROOT_FILES or any(path.startswith(prefix) for prefix in _PYTHON_SAFE_PREFIXES):
        return False
    if path.startswith("apps/desktop/"):
        return path.startswith("apps/desktop/sidecar/") or path.endswith(".py")
    # Unknown paths are intentionally expensive: a false positive costs CI time;
    # a false negative silently removes cross-platform coverage.
    return True


def _needs_windows_rust(path: str) -> bool:
    """Return whether *path* can affect the native Tauri crate on Windows."""

    if path in _CONTROL_PATHS:
        return True
    return path.startswith("apps/desktop/src-tauri/")


def classify_paths(paths: list[str]) -> WindowsScope:
    """Classify changed paths, running both gates when the input is unavailable."""

    normalized = [path for raw in paths if (path := _normalize(raw))]
    if not normalized:
        return WindowsScope(python_tests=True, rust_tests=True)
    return WindowsScope(
        python_tests=any(_needs_windows_python(path) for path in normalized),
        rust_tests=any(_needs_windows_rust(path) for path in normalized),
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the small CLI used by checks.yml and local diagnostics."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", help="Repository-relative changed paths")
    parser.add_argument("--stdin0", action="store_true", help="Read NUL-delimited paths from stdin")
    parser.add_argument("--all", action="store_true", help="Require every Windows gate")
    parser.add_argument("--github-output", action="store_true", help="Emit key=value lines for GITHUB_OUTPUT")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Print the required Windows gates as JSON or GitHub Actions outputs."""

    args = _parse_args(argv)
    if args.all:
        scope = WindowsScope(python_tests=True, rust_tests=True)
    else:
        paths = list(args.paths)
        if args.stdin0:
            paths.extend(part.decode("utf-8") for part in sys.stdin.buffer.read().split(b"\0") if part)
        scope = classify_paths(paths)

    values = {
        "python_tests": scope.python_tests,
        "rust_tests": scope.rust_tests,
    }
    if args.github_output:
        for key, value in values.items():
            print(f"{key}={str(value).lower()}")
    else:
        print(json.dumps(values, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
