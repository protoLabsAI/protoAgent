"""The per-plugin Python worker seam (``sdk.plugin_python`` / ``sdk.worker`` / ``sdk.aworker``,
#4807 part 2) and the testkit fixture-venv hook that lets these contract tests run in CI
WITHOUT ``uv`` or a real venv.

Every case points ``PLUGIN_ID``'s runtime at the test's own interpreter via
``testkit.use_fixture_python`` (which sets the ``PROTOAGENT_PLUGIN_PYTHON_<ID>`` override that
``infra.python_runtime`` honours), then drives a tiny script written into ``tmp_path``. No uv,
no network, no managed runtime.
"""

from __future__ import annotations

import asyncio
import os
import textwrap
from pathlib import Path

import pytest

from graph import sdk
from graph.plugins import testkit

# This module spawns real subprocesses, signals them (os.kill), and imports
# graph.plugins.testkit, which branches on os.name — so the Windows-native CI lane
# (which runs only `-m platform_sensitive`) must include it. tests/test_platform_sensitive_marks.py
# enforces this mark from the code.
pytestmark = pytest.mark.platform_sensitive

PLUGIN_ID = "worker-test-plugin"

# Reads one JSON line from stdin, echoes it back as the result object's `echo` field.
ECHO_SCRIPT = textwrap.dedent(
    """\
    import json, sys
    payload = json.loads(sys.stdin.readline())
    sys.stdout.write(json.dumps({"echo": payload, "got": True}) + "\\n")
    """
)

# Records its own PID, then sleeps far longer than any test timeout — so a timeout test can
# confirm the child was actually killed (its PID no longer resolves) rather than left running.
SLEEP_SCRIPT = textwrap.dedent(
    """\
    import os, sys, time
    with open(sys.argv[1], "w") as f:
        f.write(str(os.getpid()))
        f.flush()
    time.sleep(30)
    """
)

# Writes a recognisable line to stderr and exits non-zero (no JSON on stdout).
FAIL_SCRIPT = textwrap.dedent(
    """\
    import sys
    sys.stderr.write("boom: the worker broke\\n")
    sys.exit(3)
    """
)

# Prints a non-JSON line on stdout (violates the one-JSON-object-line protocol).
GARBAGE_SCRIPT = textwrap.dedent(
    """\
    import sys
    sys.stdout.write("this is not json at all\\n")
    """
)

# Logs progress lines before the result — worker() must still read the LAST line as the result.
CHATTY_SCRIPT = textwrap.dedent(
    """\
    import json, sys
    payload = json.loads(sys.stdin.readline())
    print("starting up")
    print("working...")
    sys.stdout.write(json.dumps({"doubled": payload["n"] * 2}) + "\\n")
    """
)


def _write(tmp_path: Path, name: str, body: str) -> Path:
    script = tmp_path / name
    script.write_text(body, encoding="utf-8")
    return script


# ── plugin_python ──────────────────────────────────────────────────────────────────────


def test_plugin_python_returns_none_when_unprovisioned(monkeypatch):
    # No override set and no provisioned venv → not provisioned → None (never raises).
    monkeypatch.delenv(testkit._venv_env_key(PLUGIN_ID), raising=False)
    assert sdk.plugin_python(PLUGIN_ID) is None


def test_plugin_python_resolves_the_fixture_interpreter(tmp_path, monkeypatch):
    base = testkit.use_fixture_python(PLUGIN_ID, root=tmp_path / "venv", monkeypatch=monkeypatch)
    exe = sdk.plugin_python(PLUGIN_ID)
    assert exe is not None
    exe = Path(exe)
    assert exe.exists()  # follows the symlink to the real interpreter
    # It lives inside the fixture venv we built, not some ambient interpreter (check the
    # returned path itself, not its symlink target, which points at the base interpreter).
    assert base in exe.parents


def test_plugin_python_never_raises_on_a_bad_id():
    # A non-bare id raises ValueError inside python_runtime; the SDK swallows it to None.
    assert sdk.plugin_python("a/b") is None


# ── worker: happy path ───────────────────────────────────────────────────────────────────


def test_worker_round_trip(tmp_path, monkeypatch):
    testkit.use_fixture_python(PLUGIN_ID, root=tmp_path / "venv", monkeypatch=monkeypatch)
    script = _write(tmp_path, "echo.py", ECHO_SCRIPT)
    result = sdk.worker(PLUGIN_ID, str(script), {"a": 1, "b": "x"})
    assert result == {"echo": {"a": 1, "b": "x"}, "got": True}


def test_worker_takes_an_argv_list_and_reads_the_last_line(tmp_path, monkeypatch):
    testkit.use_fixture_python(PLUGIN_ID, root=tmp_path / "venv", monkeypatch=monkeypatch)
    script = _write(tmp_path, "chatty.py", CHATTY_SCRIPT)
    # argv form: a list is spawned verbatim after the interpreter.
    result = sdk.worker(PLUGIN_ID, [str(script)], {"n": 21})
    assert result == {"doubled": 42}


def test_aworker_round_trip_off_the_event_loop(tmp_path, monkeypatch):
    testkit.use_fixture_python(PLUGIN_ID, root=tmp_path / "venv", monkeypatch=monkeypatch)
    script = _write(tmp_path, "echo.py", ECHO_SCRIPT)
    result = asyncio.run(sdk.aworker(PLUGIN_ID, str(script), {"k": [1, 2]}))
    assert result == {"echo": {"k": [1, 2]}, "got": True}


# ── worker: structured errors (never raises) ─────────────────────────────────────────────


def test_worker_no_runtime_returns_the_setup_hint(tmp_path, monkeypatch):
    monkeypatch.delenv(testkit._venv_env_key(PLUGIN_ID), raising=False)
    script = _write(tmp_path, "echo.py", ECHO_SCRIPT)
    result = sdk.worker(PLUGIN_ID, str(script), {"a": 1})
    assert result["ok"] is False
    # The hint names the runtime and the remedy the setup banner offers.
    assert "runtime" in result["error"].lower()
    assert "requires_runtimes.python" in result["error"]


def test_worker_timeout_returns_an_error_and_kills_the_child(tmp_path, monkeypatch):
    testkit.use_fixture_python(PLUGIN_ID, root=tmp_path / "venv", monkeypatch=monkeypatch)
    script = _write(tmp_path, "sleep.py", SLEEP_SCRIPT)
    pidfile = tmp_path / "child.pid"
    result = sdk.worker(PLUGIN_ID, [str(script), str(pidfile)], {}, timeout=1)
    assert result["ok"] is False
    assert "timed out" in result["error"].lower()
    # The child must have been killed and reaped — its PID no longer resolves.
    pid = int(pidfile.read_text())
    with pytest.raises(OSError):
        os.kill(pid, 0)


def test_worker_non_zero_exit_returns_the_stderr_tail(tmp_path, monkeypatch):
    testkit.use_fixture_python(PLUGIN_ID, root=tmp_path / "venv", monkeypatch=monkeypatch)
    script = _write(tmp_path, "fail.py", FAIL_SCRIPT)
    result = sdk.worker(PLUGIN_ID, str(script), {})
    assert result["ok"] is False
    assert result["returncode"] == 3
    assert "boom: the worker broke" in result["error"]
    assert "boom: the worker broke" in result["stderr"]


def test_worker_non_json_output_returns_an_error(tmp_path, monkeypatch):
    testkit.use_fixture_python(PLUGIN_ID, root=tmp_path / "venv", monkeypatch=monkeypatch)
    script = _write(tmp_path, "garbage.py", GARBAGE_SCRIPT)
    result = sdk.worker(PLUGIN_ID, str(script), {})
    assert result["ok"] is False
    assert "json" in result["error"].lower()


def test_worker_rejects_an_empty_spec(tmp_path, monkeypatch):
    testkit.use_fixture_python(PLUGIN_ID, root=tmp_path / "venv", monkeypatch=monkeypatch)
    result = sdk.worker(PLUGIN_ID, "", {})
    assert result["ok"] is False
    assert "script or module" in result["error"]


def test_worker_rejects_an_unserializable_payload(tmp_path, monkeypatch):
    testkit.use_fixture_python(PLUGIN_ID, root=tmp_path / "venv", monkeypatch=monkeypatch)
    script = _write(tmp_path, "echo.py", ECHO_SCRIPT)
    result = sdk.worker(PLUGIN_ID, str(script), {"bad": object()})
    assert result["ok"] is False
    assert "serializable" in result["error"]


# ── parity: the testkit's duplicated override rule must match the host's ──────────────────


def test_testkit_env_override_matches_python_runtime():
    # testkit is host-free by contract (vendored into standalone plugin CI), so it duplicates
    # the override-env rule instead of importing python_runtime — keep the copies in sync.
    from infra import python_runtime

    assert testkit._VENV_ENV_PREFIX == python_runtime._VENV_ENV_PREFIX
    assert testkit._VENV_MARKER == python_runtime._VENV_MARKER
    for pid in ("worker-test-plugin", "Cad.View 2", "a-b_c"):
        assert testkit._venv_env_key(pid) == python_runtime._venv_env_key(pid)
