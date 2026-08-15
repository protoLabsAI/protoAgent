"""Tests for scripts/gate.py — the repo-owned fast gate shared by local devs
and the CI lint/tests jobs (#2746).

What these pin down is the gate's CONTRACT, not the underlying tools: which
checks run and in what order, that ``--lint-only`` drops the test suite, that
the gate fails fast on the first red check with a summary naming it, that a
missing ``uv`` is a warn+skip (secondary check) while a missing ``ruff``/
``lint-imports`` is a hard failure, and that every check is a plain argv list
(no shell strings — the cross-platform guarantee). Subprocesses are faked, so
the suite never actually runs ruff/pytest recursively.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "gate.py"


def _load(monkeypatch):
    spec = importlib.util.spec_from_file_location("gate_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    # Registered in sys.modules (undone by monkeypatch) because the script's
    # dataclass resolves its stringified annotations via sys.modules[__module__]
    # on Python ≥3.13 — exec'ing unregistered raises AttributeError there.
    monkeypatch.setitem(sys.modules, spec.name, mod)
    spec.loader.exec_module(mod)
    return mod


class _FakeRunner:
    """Records every subprocess argv; fails any call whose argv mentions `fail_on`."""

    def __init__(self, fail_on: str | None = None):
        self.calls: list[list[str]] = []
        self.fail_on = fail_on

    def run(self, argv, cwd=None):
        self.calls.append(list(argv))
        failed = self.fail_on is not None and any(self.fail_on in str(a) for a in argv)
        return SimpleNamespace(returncode=2 if failed else 0)


@pytest.fixture()
def gate(monkeypatch):
    """Load gate.py fresh with every tool resolvable and subprocesses faked."""
    mod = _load(monkeypatch)
    monkeypatch.setattr(mod, "find_tool", lambda name: f"/fake/bin/{name}")
    runner = _FakeRunner()
    monkeypatch.setattr(mod, "subprocess", SimpleNamespace(run=runner.run))
    mod._runner = runner  # test-side handle, not part of the script's API
    return mod


def test_full_gate_runs_all_checks_in_order(gate, capsys):
    assert gate.run_gate() == 0
    calls = gate._runner.calls
    assert len(calls) == 5
    assert calls[0] == ["/fake/bin/ruff", "check", "."]
    assert calls[1] == ["/fake/bin/lint-imports"]
    assert calls[2][0] == sys.executable
    assert calls[2][1].endswith("gen_attribution.py") and calls[2][2] == "--check"
    assert calls[3] == ["/fake/bin/uv", "lock", "--check"]
    assert calls[4] == [sys.executable, "-m", "pytest", "tests/", "-q"]
    assert "gate: OK" in capsys.readouterr().out


def test_full_gate_never_touches_the_heavyweight_ci_legs(gate):
    gate.run_gate()
    flat = " ".join(" ".join(c) for c in gate._runner.calls)
    for forbidden in ("live_smoke", "npm", "playwright", "docker", "tests/integration"):
        assert forbidden not in flat


def test_all_argv_are_lists_not_shell_strings(gate):
    # The cross-platform contract: no shell=True, no command strings — every
    # check must be an argv list so Windows and POSIX behave identically.
    gate.run_gate()
    assert all(isinstance(c, list) for c in gate._runner.calls)
    assert all(isinstance(a, str) for c in gate._runner.calls for a in c)


def test_lint_only_skips_pytest(gate):
    assert gate.run_gate(lint_only=True) == 0
    calls = gate._runner.calls
    assert len(calls) == 4
    assert not any("pytest" in " ".join(c) for c in calls)


def test_fail_fast_stops_at_first_red_check(gate, capsys):
    gate._runner.fail_on = "ruff"
    assert gate.run_gate() == 1
    assert len(gate._runner.calls) == 1  # nothing after the failed check ran
    out = capsys.readouterr().out
    assert "gate: FAIL" in out and "ruff check" in out


def test_test_failure_fails_the_full_gate(gate, capsys):
    gate._runner.fail_on = "pytest"
    assert gate.run_gate() == 1
    assert len(gate._runner.calls) == 5  # lint checks all passed first
    out = capsys.readouterr().out
    assert "gate: FAIL" in out and "unit tests (pytest)" in out


def test_missing_uv_is_a_warned_skip_not_a_failure(gate, monkeypatch, capsys):
    monkeypatch.setattr(
        gate, "find_tool", lambda name: None if name == "uv" else f"/fake/bin/{name}"
    )
    assert gate.run_gate(lint_only=True) == 0
    assert not any("lock" in c for c in gate._runner.calls)  # the uv step never ran
    out = capsys.readouterr().out
    assert "gate: SKIP" in out and "uv lock --check" in out


def test_missing_ruff_is_a_hard_failure(gate, monkeypatch, capsys):
    monkeypatch.setattr(
        gate, "find_tool", lambda name: None if name == "ruff" else f"/fake/bin/{name}"
    )
    assert gate.run_gate() == 1
    assert gate._runner.calls == []  # fails before running anything
    out = capsys.readouterr().out
    assert "gate: FAIL" in out and "ruff==0.15.10" in out


def test_cli_flag_parsing_maps_to_lint_only(gate):
    assert gate.main(["--lint-only"]) == 0
    assert len(gate._runner.calls) == 4
    gate._runner.calls.clear()
    assert gate.main([]) == 0
    assert len(gate._runner.calls) == 5
