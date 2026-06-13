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
