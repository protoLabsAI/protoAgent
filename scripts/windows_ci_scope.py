"""Classify a change set for the path-scoped CI gates (Windows-native + web E2E).

The Windows classifier is deliberately fail-safe: unknown paths run the Windows
Python suite. Only well-understood documentation, web, marketing, and
desktop-native paths may skip it.

The web gate (vitest + Playwright against the mock backend) is the inverse: it
runs only when a path it actually consumes changed — ``apps/web/``, the npm
manifests, and the few files outside ``apps/web`` that the console build or its
tests read (``_WEB_EXTERNAL_PATHS``; a drift test fails when a new cross-tree
reference appears without being listed there). Pushes to ``main`` bypass
classification and run every gate.
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


# Everything outside apps/web that the web job reads. The e2e mock server serves the
# in-tree artifact plugin; two vitest files import Python sources with `?raw`.
# tests/test_windows_ci_scope.py scans apps/web for relative references that leave the
# workspace and fails if one isn't covered here.
_WEB_EXTERNAL_PATHS = (
    "plugins/artifact/",
    "runtime/flags.py",
    "graph/snapshot_op.py",
)
_WEB_ROOT_FILES = {".npmrc", ".nvmrc", "package.json", "package-lock.json"}


def _needs_web(path: str) -> bool:
    """Return whether *path* can change the result of the web unit/E2E job."""

    # vite.config.ts loads PROTOAGENT_* from repo-root .env files (envDir = repo root).
    if path in _CONTROL_PATHS or path in _WEB_ROOT_FILES or path.startswith(".env"):
        return True
    return path.startswith("apps/web/") or any(
        path == ext or (ext.endswith("/") and path.startswith(ext)) for ext in _WEB_EXTERNAL_PATHS
    )


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


def classify_web(paths: list[str]) -> bool:
    """Return whether the web job must run, running it when the input is unavailable."""

    normalized = [path for raw in paths if (path := _normalize(raw))]
    return not normalized or any(_needs_web(path) for path in normalized)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the small CLI used by checks.yml and local diagnostics."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", help="Repository-relative changed paths")
    parser.add_argument("--stdin0", action="store_true", help="Read NUL-delimited paths from stdin")
    parser.add_argument("--all", action="store_true", help="Require every scoped gate")
    parser.add_argument("--github-output", action="store_true", help="Emit key=value lines for GITHUB_OUTPUT")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Print the required Windows gates as JSON or GitHub Actions outputs."""

    args = _parse_args(argv)
    if args.all:
        scope = WindowsScope(python_tests=True, rust_tests=True)
        web = True
    else:
        paths = list(args.paths)
        if args.stdin0:
            paths.extend(part.decode("utf-8") for part in sys.stdin.buffer.read().split(b"\0") if part)
        scope = classify_paths(paths)
        web = classify_web(paths)

    values = {
        "python_tests": scope.python_tests,
        "rust_tests": scope.rust_tests,
        "web_tests": web,
    }
    if args.github_output:
        for key, value in values.items():
            print(f"{key}={str(value).lower()}")
    else:
        print(json.dumps(values, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
