#!/usr/bin/env python
"""The repo-owned fast gate: the quick deterministic pre-PR checks, one command.

This is the SAME entrypoint CI's ``lint`` job invokes (``--lint-only``), so the
local gate and the CI gate cannot drift — if it passes here it passes there,
tool versions aside (CI pins ``ruff==0.15.10`` / ``import-linter==2.11``; see
.github/workflows/checks.yml).

Usage:

    python scripts/gate.py              # full fast gate: lint checks + unit tests
    python scripts/gate.py --lint-only  # just the lint checks (quick pre-commit smoke)

What it runs, in order, stopping at the first failure:

  1. ``ruff check .``                              — lint (config in pyproject.toml)
  2. ``lint-imports``                              — import-layering contracts
  3. ``python scripts/gen_attribution.py --check`` — attribution manifest in sync
  4. ``uv lock --check``                           — lockfile in sync with pyproject
                                                     (skipped with a warning if ``uv``
                                                     isn't on PATH — secondary check)
  5. ``python -m pytest tests/ -q``                — the unit suite (unless --lint-only)

What it deliberately does NOT run (heavyweight / CI-only / network-dependent):
fleet integration (``PA_RUN_INTEGRATION=1``), the live smoke
(``scripts/live_smoke.py``), web unit/e2e (npm workspace), the Windows exclusion
matrix, Playwright, Docker/release. Those stay CI jobs — see the "Must pass
before opening a PR" table in PROTO.md for the full breakdown.

Pure stdlib and cross-platform: no ``/bin/sh``, no shell strings, no POSIX
signals — every check is a ``subprocess.run`` argv list, so it behaves the same
under ``python scripts/gate.py`` on Windows and POSIX. Tools are resolved from
the running interpreter's scripts directory first (the active venv — local
``.venv`` or CI's ``actions/setup-python``), then PATH.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import sysconfig
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def find_tool(name: str) -> str | None:
    """Resolve a console script, preferring the active venv over bare PATH.

    ``sysconfig.get_path("scripts")`` is the running interpreter's scripts
    directory (``<venv>/bin`` on POSIX, ``<venv>\\Scripts`` on Windows), so a
    tool installed into the venv wins even when PATH holds a different global
    copy. ``shutil.which`` handles the Windows ``.exe``/PATHEXT resolution.
    """
    hit = shutil.which(name, path=sysconfig.get_path("scripts"))
    return hit or shutil.which(name)


@dataclass(frozen=True)
class Check:
    """One gate step: a display name plus the argv to run (None = tool missing)."""

    name: str
    argv: list[str] | None
    missing_hint: str = ""
    # A missing OPTIONAL tool skips the check with a warning instead of failing
    # the gate — used only for `uv`, which is a standalone binary rather than a
    # pip install, so contributors without it still get the primary checks.
    optional: bool = False


def build_checks(lint_only: bool) -> list[Check]:
    ruff = find_tool("ruff")
    lint_imports = find_tool("lint-imports")
    uv = find_tool("uv")
    checks = [
        Check(
            "ruff check",
            [ruff, "check", "."] if ruff else None,
            missing_hint="pip install ruff==0.15.10",
        ),
        Check(
            "import contracts (lint-imports)",
            [lint_imports] if lint_imports else None,
            missing_hint="pip install import-linter==2.11",
        ),
        Check(
            "attribution in sync (gen_attribution --check)",
            [sys.executable, str(REPO / "scripts" / "gen_attribution.py"), "--check"],
        ),
        Check(
            "lockfile in sync (uv lock --check)",
            [uv, "lock", "--check"] if uv else None,
            missing_hint="install uv (https://docs.astral.sh/uv/) to enable this check",
            optional=True,
        ),
    ]
    if not lint_only:
        checks.append(
            Check("unit tests (pytest)", [sys.executable, "-m", "pytest", "tests/", "-q"])
        )
    return checks


def run_gate(lint_only: bool = False) -> int:
    """Run the checks sequentially, fail-fast. Returns the process exit code."""
    passed = 0
    skipped = 0
    for check in build_checks(lint_only):
        if check.argv is None:
            if check.optional:
                print(f"gate: SKIP  {check.name} — tool not found; {check.missing_hint}", flush=True)
                skipped += 1
                continue
            print(f"gate: FAIL  {check.name} — tool not found; {check.missing_hint}", flush=True)
            return 1
        print(f"gate: RUN   {check.name}", flush=True)
        result = subprocess.run(check.argv, cwd=REPO)
        if result.returncode != 0:
            print(f"gate: FAIL  {check.name} (exit {result.returncode})", flush=True)
            return 1
        passed += 1
        print(f"gate: PASS  {check.name}", flush=True)
    suffix = " (lint only)" if lint_only else ""
    note = f", {skipped} skipped" if skipped else ""
    print(f"gate: OK — {passed} checks passed{note}{suffix}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="protoAgent fast gate — the quick deterministic pre-PR checks, "
        "shared verbatim by local devs and the CI lint job."
    )
    parser.add_argument(
        "--lint-only",
        action="store_true",
        help="run only the lint checks (ruff, lint-imports, attribution, lockfile) "
        "and skip the unit test suite",
    )
    args = parser.parse_args(argv)
    return run_gate(lint_only=args.lint_only)


if __name__ == "__main__":
    raise SystemExit(main())
