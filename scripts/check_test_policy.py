#!/usr/bin/env python3
"""Test-suite policy gate — keeps the suites from growing faster than the code.

The rules live in ``docs/dev/testing.md``. This script measures the ones a machine can
count and compares each to a ceiling in ``tests/test_policy_baseline.json``. A metric
over its ceiling fails the gate (``scripts/gate.py`` runs this with the lint checks, so
CI's Lint job enforces it).

    python scripts/check_test_policy.py            # check; exit 1 when a metric is over
    python scripts/check_test_policy.py --tighten  # lower every ceiling to today's value

Two kinds of metric:

* **Budgets** — test lines per source line (Python, console unit) and the Playwright
  test count. Tests may grow with the code, not faster than it.
* **Burndowns** — counts of test shapes the policy bans (source-text pins, tombstones,
  issue-numbered files, real sleeps, fixed browser waits). The existing ones are
  grandfathered; the count only goes down.

Raising a ceiling is a one-line diff in the baseline file, which is the point: it is
visible in review and needs a reason in the PR description. Never raise a burndown.

Stdlib only, no subprocess, fixed roots — the numbers are the same on every machine.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BASELINE = REPO / "tests" / "test_policy_baseline.json"
_SKIP_DIRS = {"node_modules", "__pycache__", ".venv", "dist", "build", "screenshots"}

PY_SOURCE_ROOTS = ("src", "plugins", "scripts")
WEB_SRC = "apps/web/src"
WEB_E2E = "apps/web/e2e"

_WEB_TEST = re.compile(r"\.(test|spec)\.tsx?$")
_WEB_CASE = re.compile(r"^\s*(?:it|test)(?:\.each\([^)]*\))?\s*\(", re.M)
_SOURCE_PIN = re.compile(r"""\?raw["']|readFileSync\(""")
_TOMBSTONE = re.compile(r"(Removed|Retired|Deleted)\.(test|spec)\.tsx?$")
_ISSUE_NUMBERED = re.compile(r"(^test_.*_\d{3,5}\.py$)|(\d{4}.*\.(test|spec)\.tsx?$)")
_PY_SLEEP = re.compile(r"\b(?:time|asyncio)\.sleep\(\s*(\d*\.?\d+)\s*\)")
_FIXED_WAIT = re.compile(r"\bwaitForTimeout\(")

# A sleep this long or longer is a "never returns" stub that a test cancels, not a wait.
_HANG_STUB_SECONDS = 60.0
_REAL_SLEEP_SECONDS = 0.2

BUDGETS = ("py_test_lines_per_source_line", "web_unit_test_lines_per_source_line", "web_e2e_tests")

FIXES = {
    "py_test_lines_per_source_line": "Python tests are growing faster than Python source — fold cases into a "
    "table, drop a layer that repeats another, or delete tests the change made redundant",
    "web_unit_test_lines_per_source_line": "console unit tests are growing faster than console source — same "
    "remedy: tables, one layer per behavior",
    "web_e2e_tests": "Playwright is for what needs a real browser (layout, focus, scroll, drag, touch, "
    "iframes, storage, WebKit) — put logic and render checks in vitest, or replace a spec",
    "web_source_pin_files": "a test that reads a source file and matches text in it pins the spelling, not "
    "the behavior — assert on rendered output, or write a lint rule",
    "tombstone_test_files": "do not add a test that asserts deleted code stays deleted",
    "issue_numbered_test_files": "put the regression test in the subject's existing file, named for the "
    "behavior; the issue number goes in a comment",
    "py_real_sleeps": "no real sleeps in tests — use tests/_virtual_clock.py or wait on an event",
    "e2e_fixed_waits": "no waitForTimeout — wait on the condition (expect(...).toBeVisible, expect.poll)",
}


def _files(root: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            yield Path(dirpath) / name


def _lines(paths: list[Path]) -> int:
    return sum(p.read_bytes().count(b"\n") for p in paths)


def _count(paths: list[Path], counter: Callable[[str], int]) -> int:
    return sum(counter(p.read_text(encoding="utf-8", errors="replace")) for p in paths)


def _real_sleeps(text: str) -> int:
    return sum(1 for m in _PY_SLEEP.finditer(text) if _REAL_SLEEP_SECONDS <= float(m.group(1)) < _HANG_STUB_SECONDS)


def _ratio(tests: int, source: int) -> float:
    return round(tests / source, 4) if source else 0.0


def measure(repo: Path = REPO) -> dict[str, float]:
    """Today's value of every metric."""
    py_tests = [p for p in _files(repo / "tests") if p.suffix == ".py"]
    py_source = [
        p
        for root in PY_SOURCE_ROOTS
        for p in _files(repo / root)
        if p.suffix == ".py" and not p.name.startswith("test_")
    ]
    web = [p for p in _files(repo / WEB_SRC) if p.suffix in (".ts", ".tsx")]
    web_unit = [p for p in web if _WEB_TEST.search(p.name)]
    web_source = [p for p in web if not _WEB_TEST.search(p.name)]
    e2e = [p for p in _files(repo / WEB_E2E) if p.name.endswith(".spec.ts")]
    named = [p.name for p in py_tests + web_unit + e2e]
    return {
        "py_test_lines_per_source_line": _ratio(_lines(py_tests), _lines(py_source)),
        "web_unit_test_lines_per_source_line": _ratio(_lines(web_unit), _lines(web_source)),
        "web_e2e_tests": _count(e2e, lambda t: len(_WEB_CASE.findall(t))),
        "web_source_pin_files": _count(web_unit, lambda t: 1 if _SOURCE_PIN.search(t) else 0),
        "tombstone_test_files": sum(1 for n in named if _TOMBSTONE.search(n)),
        "issue_numbered_test_files": sum(1 for n in named if _ISSUE_NUMBERED.search(n)),
        "py_real_sleeps": _count(py_tests, _real_sleeps),
        "e2e_fixed_waits": _count(e2e, lambda t: len(_FIXED_WAIT.findall(t))),
    }


def violations(actual: dict[str, float], ceilings: dict[str, float]) -> list[str]:
    """One message per metric over its ceiling (a metric with no ceiling is over)."""
    out = []
    for name, value in actual.items():
        ceiling = ceilings.get(name)
        if ceiling is None:
            out.append(f"{name}: no ceiling in {BASELINE.name} — add one")
        elif value > ceiling:
            out.append(f"{name}: {value:g} is over the ceiling {ceiling:g} — {FIXES[name]}")
    return out


def tightened(actual: dict[str, float], ceilings: dict[str, float]) -> dict[str, float]:
    """Ceilings lowered to today's values — never raised. Ratios keep two decimals of room."""
    out = {}
    for name, value in actual.items():
        target = math.ceil(value * 100) / 100 if isinstance(value, float) else value
        out[name] = min(target, ceilings.get(name, target))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tighten", action="store_true", help="lower every ceiling to today's value")
    args = parser.parse_args(argv)

    actual = measure()
    ceilings = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else {}
    if args.tighten:
        BASELINE.write_text(json.dumps(tightened(actual, ceilings), indent=2) + "\n", encoding="utf-8")
        print(f"test-policy: ceilings written to {BASELINE.relative_to(REPO)}")
        return 0

    for name, value in actual.items():
        kind = "budget" if name in BUDGETS else "burndown"
        print(f"test-policy: {name:38s} {value:>8g}  (ceiling {ceilings.get(name, '—')}, {kind})")
    failed = violations(actual, ceilings)
    for message in failed:
        print(f"test-policy: FAIL  {message}", file=sys.stderr)
    if failed:
        print("test-policy: the rules and how to comply: docs/dev/testing.md", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
