"""Tests for scripts/gate.py — specifically that a KILLED check is not reported as a
FAILED one.

`subprocess` reports a signal death as a negative return code. Flattening that to `1`
tells the caller the repo failed its own gate when the check never reached a verdict at
all, and automation cannot tell the two apart afterwards. That cost projectBoard #386 a
~40-minute terminal block on a card whose merged state was fully green (7362 passed):
pytest took a SIGTERM at 65%, gate.py printed `exit -15` and returned `1`, and the board
— whose own signal guard checks for a negative code and was therefore blind — rendered it
as "the PR merges clean but the RESULT is broken".

The contract this pins: killed → `128 + signal` (the universal shell convention), so the
distinction survives the process boundary; genuinely failed → `1`, exactly as before.
"""

from __future__ import annotations

import importlib.util
import signal
import sys
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "gate.py"


def _load_gate():
    # Registered in sys.modules BEFORE exec: @dataclass resolves its own module by name
    # while the class body runs, and a module absent from sys.modules makes that fail.
    spec = importlib.util.spec_from_file_location("_gate_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    return mod


@pytest.fixture
def gate():
    return _load_gate()


def _one_check(gate, name="unit tests (pytest)"):
    return [gate.Check(name, ["true"])]


def _fake_run(returncode):
    def _run(argv, cwd=None, **kw):
        return subprocess.CompletedProcess(argv, returncode)

    return _run


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL, signal.SIGINT])
def test_a_check_killed_by_a_signal_exits_128_plus_signal(gate, monkeypatch, capsys, sig):
    """Not 1 — a caller must be able to see that no verdict was reached."""
    monkeypatch.setattr(gate, "build_checks", lambda lint_only=False: _one_check(gate))
    monkeypatch.setattr(gate.subprocess, "run", _fake_run(-int(sig)))

    rc = gate.run_gate()

    assert rc == 128 + int(sig)
    out = capsys.readouterr().out
    assert "KILLED" in out and f"signal {int(sig)}" in out
    # It must NOT claim the check failed — that is the false statement #386 acted on.
    assert "gate: FAIL" not in out


def test_a_genuinely_failing_check_still_exits_1(gate, monkeypatch, capsys):
    """The ordinary red path is untouched: a real non-zero exit is still a failure."""
    monkeypatch.setattr(gate, "build_checks", lambda lint_only=False: _one_check(gate))
    monkeypatch.setattr(gate.subprocess, "run", _fake_run(1))

    rc = gate.run_gate()

    assert rc == 1
    out = capsys.readouterr().out
    assert "gate: FAIL" in out and "KILLED" not in out


def test_a_passing_gate_still_exits_0(gate, monkeypatch, capsys):
    monkeypatch.setattr(gate, "build_checks", lambda lint_only=False: _one_check(gate))
    monkeypatch.setattr(gate.subprocess, "run", _fake_run(0))

    rc = gate.run_gate()

    assert rc == 0
    assert "gate: OK" in capsys.readouterr().out


def test_the_killed_exit_code_is_outside_the_ordinary_failure_range(gate, monkeypatch):
    """128+N must not collide with the codes a normal failing tool returns, or the
    board's re-run heuristic would swallow real failures."""
    monkeypatch.setattr(gate, "build_checks", lambda lint_only=False: _one_check(gate))
    monkeypatch.setattr(gate.subprocess, "run", _fake_run(-int(signal.SIGTERM)))
    killed = gate.run_gate()

    monkeypatch.setattr(gate.subprocess, "run", _fake_run(2))
    failed = gate.run_gate()

    assert killed > 128 >= failed
