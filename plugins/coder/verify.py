"""The verifier — run caller-supplied tests against a candidate (ADR 0064).

This is the P1 verifier for the ``coder_solve`` tool path: no repo, just a task +
tests. It writes the candidate solution and the test file into a throwaway temp
dir and runs ``python -m pytest`` in a subprocess with a hard timeout, then parses
pass/fail and the *named* failing cases into a :class:`~plugins.coder.solve.Verdict`.

It is the same ``verify(code) -> Verdict`` contract the ladder gates on; the P2
board seam supplies a different implementation (run the repo's tests in the
feature's git worktree) behind the same contract.

Like ``execute_code``, this runs model-authored code in a subprocess with a
scrubbed env + timeout — isolation, not a true sandbox. ``coder`` ships disabled
for the same reason; enable only for a trusted model or a hardened container.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile

from .solve import Verdict

# Count tokens within pytest's summary line, e.g. "1 failed, 2 passed".
_SUMMARY = re.compile(r"(\d+)\s+(passed|failed|error|errors|skipped)", re.IGNORECASE)
# pytest prints its result counts on ONE line ending in "in <time>s" (e.g.
# "1 failed, 2 passed in 0.03s"). We scope count-parsing to that line so a
# candidate that PRINTS "1000 passed" to stdout can't pollute the verdict.
_SUMMARY_LINE = re.compile(r"\bin\s+[\d.]+\s*s\b")
# Per-test failure lines, e.g. "FAILED test_solution.py::test_adds - assert ..."
_FAILED = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.MULTILINE)


def _parse(output: str, returncode: int) -> Verdict:
    # The pytest summary is the LAST line that has both a count token and the
    # "in <time>s" suffix; later wins (pytest's own line is last).
    summary = ""
    for line in output.splitlines():
        if _SUMMARY_LINE.search(line) and _SUMMARY.search(line):
            summary = line
    counts = {"passed": 0, "failed": 0, "error": 0, "errors": 0, "skipped": 0}
    for n, kind in _SUMMARY.findall(summary):
        counts[kind.lower()] = int(n)
    failed = counts["failed"] + counts["error"] + counts["errors"]
    total = counts["passed"] + failed
    failing = [m.split(" ")[0] for m in _FAILED.findall(output)]
    # No parsed counts but a non-zero exit (collection error, import failure, no
    # tests) ⇒ treat as failed, not silently passed.
    ok = returncode == 0 and failed == 0 and total > 0
    if total == 0 and returncode != 0:
        failed, total = 1, 1
    return Verdict(passed=ok, total=total, failed=failed, failing=failing, output=output)


async def run_tests(
    code: str,
    tests: str,
    *,
    solution_name: str = "solution",
    timeout: float = 60.0,
    truncate: int = 4000,
) -> Verdict:
    """Write ``code`` to ``<solution_name>.py`` + ``tests`` to ``test_<solution_name>.py``
    in a temp dir and run pytest there. ``tests`` should import from
    ``solution_name`` (e.g. ``from solution import add``)."""
    if getattr(sys, "frozen", False):
        # In a PyInstaller build, ``sys.executable`` is the frozen server binary, not a
        # Python interpreter — ``-m pytest`` would relaunch the server. No standalone
        # Python to run the tests, so fail cleanly (same guard as execute_code).
        return Verdict(passed=False, output="coder verifier unavailable in the packaged desktop app (no standalone Python to run pytest)")
    with tempfile.TemporaryDirectory(prefix="coder_") as d:
        with open(os.path.join(d, f"{solution_name}.py"), "w") as f:
            f.write(code)
        with open(os.path.join(d, f"test_{solution_name}.py"), "w") as f:
            f.write(tests)
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                cwd=d,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
                env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"},
            )
        except FileNotFoundError as exc:  # pragma: no cover - env-dependent
            return Verdict(passed=False, output=f"could not launch pytest: {exc}")
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return Verdict(passed=False, output=f"tests timed out after {timeout:.0f}s")
        out = (stdout or b"").decode(errors="replace")
        if len(out) > truncate:
            out = out[:truncate] + f"\n…[truncated to {truncate} chars]"
        return _parse(out, proc.returncode)
