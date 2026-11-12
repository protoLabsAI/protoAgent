#!/usr/bin/env python3
"""Test-suite policy gate — keeps the suites from growing faster than the code.

The rules live in ``docs/dev/testing.md``. ``scripts/gate.py`` runs this with the lint
checks, so CI's Lint job enforces it.

    python scripts/check_test_policy.py            # check; exit 1 on a violation
    python scripts/check_test_policy.py --tighten  # lower every burndown to today's count

Two kinds of check, both with limits in ``tests/test_policy_baseline.json``:

* **Change budget** — measured on THIS change only (the diff against its merge base):
  net test lines it adds per net source line it adds, with a free allowance so a small
  fix and its regression test always pass, and a cap on new Playwright tests. A change
  is only ever asked to fix its own diff. The first version compared repo-wide totals
  to a ceiling, which blocked whichever PR arrived after the headroom was spent — an
  8-line fix paid for another epic's tests.
* **Burndowns** — repo-wide counts of test shapes the policy bans (source-text pins,
  tombstones, issue-numbered files, real sleeps, fixed browser waits). The existing
  ones are grandfathered; the count only goes down.

A change that genuinely needs more tests than its budget (a hardening or coverage PR)
takes the ``test-budget`` label and says why in the PR description; CI then reports
the numbers without failing. Locally: ``TEST_POLICY_ALLOW=1``.

Stdlib only. The one subprocess is ``git diff`` for the change budget; with no git or
no base it is skipped with a notice, never failed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
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
# The opening of a case: `test(`, `it(`, or a modifier chain such as `test.each(` / `test.skip(`.
# Counted at the opening paren, so `.each` arguments with their own parentheses do not matter.
_WEB_CASE = re.compile(r"^\s*(?:it|test)(?:\.(?:each|only|skip|fixme|fail|concurrent))*\s*\(", re.M)
_SOURCE_PIN = re.compile(r"""\?raw["']|readFileSync\(""")
_TOMBSTONE = re.compile(r"(Removed|Retired|Deleted)\.(test|spec)\.tsx?$")
_ISSUE_NUMBERED = re.compile(r"(^test_.*_\d{3,5}\.py$)|(\d{4}.*\.(test|spec)\.tsx?$)")
_PY_SLEEP = re.compile(r"\b(?:time|asyncio)\.sleep\(\s*(\d*\.?\d+)\s*\)")
_FIXED_WAIT = re.compile(r"\bwaitForTimeout\(")

# A sleep this long or longer is a "never returns" stub that a test cancels, not a wait.
_HANG_STUB_SECONDS = 60.0
_REAL_SLEEP_SECONDS = 0.2

# Repo-wide trends: printed for the record, never a failure.
REPORTED = ("py_test_lines_per_source_line", "web_unit_test_lines_per_source_line", "web_e2e_tests")

FIXES = {
    "py": "fold cases into a table, drop a layer that repeats another, or delete tests this change made redundant",
    "web_unit": "fold cases into a table (it.each) and keep one layer per behavior",
    "e2e": "Playwright is for what needs a real browser (layout, focus, scroll, drag, touch, iframes, "
    "storage, WebKit) — put logic and render checks in vitest",
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
    """One message per burndown over its ceiling (a burndown with no ceiling is over)."""
    out = []
    for name, value in actual.items():
        if name in REPORTED:
            continue
        ceiling = ceilings.get(name)
        if ceiling is None:
            out.append(f"{name}: no ceiling in {BASELINE.name} — add one")
        elif value > ceiling:
            out.append(f"{name}: {value:g} is over the ceiling {ceiling:g} — {FIXES[name]}")
    return out


def _git(*args: str) -> str | None:
    try:
        done = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=False)
    except OSError:
        return None
    return done.stdout if done.returncode == 0 else None


def change_delta(base: str | None = None) -> dict[str, int] | None:
    """Net lines this change adds per category, and net Playwright tests — the working
    tree against its merge base with ``base`` (``$TEST_POLICY_BASE``, else origin/main).
    None when git or the base is unavailable."""
    base = base or os.environ.get("TEST_POLICY_BASE") or "origin/main"
    fork = (_git("merge-base", "HEAD", base) or "").strip()
    numstat = _git("diff", "--numstat", fork) if fork else None
    if numstat is None:
        return None
    delta = {"py_test": 0, "py_source": 0, "web_test": 0, "web_source": 0, "e2e_tests": 0}
    for line in numstat.splitlines():
        added, removed, path = (line.split("\t") + ["", ""])[:3]
        if not added.isdigit():  # binary
            continue
        net, name = int(added) - int(removed), path.rsplit("/", 1)[-1]
        if path.startswith("tests/") and path.endswith(".py"):
            delta["py_test"] += net
        elif path.startswith(tuple(r + "/" for r in PY_SOURCE_ROOTS)) and path.endswith(".py"):
            delta["py_source"] += net
        elif path.startswith(WEB_SRC + "/") and path.endswith((".ts", ".tsx")):
            delta["web_test" if _WEB_TEST.search(name) else "web_source"] += net
    patch = _git("diff", "-U0", fork, "--", WEB_E2E) or ""
    for line in patch.splitlines():
        if line[:1] in "+-" and not line.startswith(("+++", "---")) and _WEB_CASE.match(line[1:]):
            delta["e2e_tests"] += 1 if line[0] == "+" else -1
    return delta


def change_violations(delta: dict[str, int], limits: dict[str, float]) -> list[str]:
    """One message per budget this change's own diff is over."""
    out = []
    free = limits["free_test_lines"]
    for key, label, ratio_key in (
        ("py", "Python", "py_test_lines_per_source_line"),
        ("web", "console unit", "web_unit_test_lines_per_source_line"),
    ):
        tests, source = delta[f"{key}_test"], max(delta[f"{key}_source"], 0)
        allowed = max(free, int(limits[ratio_key] * source))
        if tests > allowed:
            out.append(
                f"this change adds {tests} {label} test lines for {source} source lines; its budget is "
                f"{allowed} ({limits[ratio_key]:g}x source, or {free:g} free) — cut {tests - allowed}: "
                + FIXES["py" if key == "py" else "web_unit"]
            )
    if delta["e2e_tests"] > limits["e2e_tests"]:
        out.append(
            f"this change adds {delta['e2e_tests']} Playwright tests; its budget is "
            f"{limits['e2e_tests']:g} — {FIXES['e2e']}"
        )
    return out


def tightened(actual: dict[str, float], ceilings: dict[str, float]) -> dict[str, float]:
    """Burndown ceilings lowered to today's counts — never raised."""
    return {
        name: min(value, ceilings.get(name, value)) for name, value in actual.items() if name not in REPORTED
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tighten", action="store_true", help="lower every burndown to today's count")
    args = parser.parse_args(argv)

    actual = measure()
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    if args.tighten:
        baseline["burndown"] = tightened(actual, baseline["burndown"])
        BASELINE.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")
        print(f"test-policy: burndowns written to {BASELINE.relative_to(REPO)}")
        return 0

    for name, value in actual.items():
        note = "trend, not enforced" if name in REPORTED else f"ceiling {baseline['burndown'].get(name, '—')}"
        print(f"test-policy: {name:38s} {value:>8g}  ({note})")
    failed = violations(actual, baseline["burndown"])

    delta = change_delta()
    if delta is None:
        print("test-policy: no git merge base — change budget skipped")
    else:
        print(
            "test-policy: this change: "
            f"Python tests {delta['py_test']:+d} / source {delta['py_source']:+d}, "
            f"console tests {delta['web_test']:+d} / source {delta['web_source']:+d}, "
            f"Playwright tests {delta['e2e_tests']:+d}"
        )
        over = change_violations(delta, baseline["per_change"])
        if over and os.environ.get("TEST_POLICY_ALLOW") == "1":
            for message in over:
                print(f"test-policy: ALLOWED (test-budget)  {message}")
        else:
            failed += over

    for message in failed:
        print(f"test-policy: FAIL  {message}", file=sys.stderr)
    if failed:
        print(
            "test-policy: rules, and the test-budget label for a change that needs more: docs/dev/testing.md",
            file=sys.stderr,
        )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
