"""POST /api/restart — the operator self-restart control (operator_api/runtime_routes)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from operator_api.runtime_routes import reexec_command, register_runtime_control_routes
from runtime.state import STATE


def test_reexec_command_from_source():
    # python -m server --port 7870 → re-run the module with the same flags.
    cmd = reexec_command("/usr/bin/python", ["/x/server/__main__.py", "--port", "7870"], frozen=False)
    assert cmd == ["/usr/bin/python", "-m", "server", "--port", "7870"]


def test_reexec_command_frozen():
    # A PyInstaller binary re-runs itself directly (no -m server).
    cmd = reexec_command("/app/protoagent", ["/app/protoagent", "--host", "0.0.0.0"], frozen=True)
    assert cmd == ["/app/protoagent", "--host", "0.0.0.0"]


def test_restart_route_sets_flag_and_returns_202(monkeypatch):
    # Stub os.kill so the route's graceful-shutdown signal can't kill the test runner.
    monkeypatch.setattr("operator_api.runtime_routes.os.kill", lambda *a, **k: None)
    STATE.restart_requested = False
    app = FastAPI()
    register_runtime_control_routes(app)
    try:
        r = TestClient(app).post("/api/restart")
        assert r.status_code == 202 and r.json()["restarting"] is True
        assert STATE.restart_requested is True  # _main re-execs once uvicorn drains
    finally:
        STATE.restart_requested = False  # don't leak the flag to other tests


# ── #2585: the drain must not depend on signalling ourselves ────────────────


def test_restart_asks_the_server_to_exit_rather_than_signalling(monkeypatch):
    """The Windows bug. `os.kill(os.getpid(), SIGINT)` is fine on POSIX and FATAL on
    Windows: os.kill there delivers only CTRL_C_EVENT/CTRL_BREAK_EVENT as signals and
    turns everything else — SIGINT included — into TerminateProcess. The frozen server
    was killed at exactly the point it should have drained, so uvicorn.run() never
    returned and the re-exec never ran."""
    import types

    from operator_api import runtime_routes
    from runtime.state import STATE

    server = types.SimpleNamespace(should_exit=False)
    monkeypatch.setattr(STATE, "uvicorn_server", server, raising=False)
    killed: list = []
    monkeypatch.setattr(runtime_routes.os, "kill", lambda *a: killed.append(a))

    assert runtime_routes.request_server_exit() is True

    assert server.should_exit is True
    assert not killed, "must not signal the process — that is the Windows kill path"


def test_falls_back_to_the_signal_when_no_server_is_registered(monkeypatch):
    """An embedding host that runs the app itself has no Server reference; the old
    behaviour is still the best available there."""
    from operator_api import runtime_routes
    from runtime.state import STATE

    monkeypatch.setattr(STATE, "uvicorn_server", None, raising=False)

    assert runtime_routes.request_server_exit() is False


def test_main_registers_the_server_so_the_route_can_reach_it():
    """The two halves have to meet: _main constructs a uvicorn.Server and publishes it, or
    the route silently takes the signal fallback on every platform."""
    import inspect

    import server as server_pkg

    src = inspect.getsource(server_pkg._main)
    # Comments legitimately mention uvicorn.run when explaining why it isn't used, so
    # judge the CODE — a naive substring check reads those and fails on the prose.
    code = "\n".join(line.split("#", 1)[0] for line in src.splitlines())
    assert "uvicorn.Server(" in code, "_main must build a Server it can hand to the route"
    assert "STATE.uvicorn_server" in code, "_main must publish it for request_server_exit()"
    assert "uvicorn.run(" not in code, "uvicorn.run() hides the Server — the route needs it"
