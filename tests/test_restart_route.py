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
    # Built through the factory (#3428 — the subclass that tears owned trees down on
    # the exit signal), so check what the factory actually returns rather than the
    # constructor spelling: the route needs a uvicorn.Server whatever builds it.
    assert "build_uvicorn_server(" in code, "_main must build a Server it can hand to the route"
    import uvicorn

    assert isinstance(server_pkg.build_uvicorn_server(uvicorn.Config(lambda *a: None)), uvicorn.Server)
    assert "STATE.uvicorn_server" in code, "_main must publish it for request_server_exit()"
    assert "uvicorn.run(" not in code, "uvicorn.run() hides the Server — the route needs it"


def test_readiness_uses_existing_registries(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from runtime import turn_activity

    store = Mock()
    store.list.return_value = [object(), object()]
    monkeypatch.setattr(STATE, "background_mgr", SimpleNamespace(store=store))
    monkeypatch.setattr(STATE, "uvicorn_server", object())
    app = FastAPI()
    register_runtime_control_routes(app)
    turn_activity.begin("restart-test")
    try:
        data = TestClient(app).get("/api/restart/readiness").json()
        assert data["busy"] == ["a chat turn is running", "2 background jobs"]
        assert not data["idle"] and data["supervised"]
        store.list.assert_called_once_with(status="running", limit=500, include_dismissed=True)
    finally:
        turn_activity.end("restart-test")
    store.list.return_value = []
    assert TestClient(app).get("/api/restart/readiness").json()["idle"]


def test_readiness_without_supervisor_and_guard(monkeypatch):
    monkeypatch.delenv("PROTOAGENT_FLEET_TOKEN", raising=False)
    monkeypatch.setattr(STATE, "uvicorn_server", None)
    monkeypatch.setattr(STATE, "background_mgr", None)
    monkeypatch.setattr(STATE, "restart_requested", False)
    app = FastAPI()
    register_runtime_control_routes(app)
    client = TestClient(app)
    data = client.get("/api/restart/readiness").json()
    assert not data["supervised"] and "start it again" in data["how_to"]
    assert client.post("/api/restart", json={"when_idle": True}).status_code == 409
    assert not STATE.restart_requested
    monkeypatch.setenv("PROTOAGENT_FLEET_TOKEN", "test-supervisor")
    assert client.get("/api/restart/readiness").json()["supervised"]


def test_restart_auth_audit_and_busy_guard(monkeypatch):
    from unittest.mock import Mock

    from a2a_impl import auth
    from observability.audit import audit_logger
    from runtime import turn_activity

    monkeypatch.setattr(auth, "_BEARER", ["test-operator"])
    monkeypatch.setattr(auth, "_FEDERATION", ["test-federation"])
    monkeypatch.setattr(auth, "_FLEET", [None])
    monkeypatch.setattr(STATE, "uvicorn_server", object())
    monkeypatch.setattr(STATE, "background_mgr", None)
    monkeypatch.setattr(STATE, "restart_requested", False)
    audit = Mock()
    monkeypatch.setattr(audit_logger, "log", audit)
    app = FastAPI()
    app.add_middleware(auth.A2AAuthMiddleware)
    register_runtime_control_routes(app)
    client = TestClient(app)
    for path, method in [("/api/restart/readiness", client.get), ("/api/restart", client.post)]:
        assert method(path).status_code == 401
        assert method(path, headers={"Authorization": "Bearer test-federation"}).status_code in (401, 403)
    headers = {"Authorization": "Bearer test-operator"}
    assert client.get("/api/restart/readiness", headers=headers).status_code == 200
    turn_activity.begin("busy-restart")
    try:
        response = client.post("/api/restart", headers=headers,
                               json={"when_idle": True, "reason": "settings: Self update"})
        assert response.status_code == 409
        assert not STATE.restart_requested
        assert audit.call_args.kwargs["args"] == {
            "actor": "operator", "reason": "settings: Self update", "target": "self",
        }
    finally:
        turn_activity.end("busy-restart")
    assert client.post("/api/restart", headers=headers,
                       json={"when_idle": True, "reason": "settings: Self update"}).status_code == 202
    assert audit.call_count == 2


def test_readiness_store_failure_does_not_report_idle(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    store = Mock()
    store.list.side_effect = OSError("unreadable")
    monkeypatch.setattr(STATE, "background_mgr", SimpleNamespace(store=store))
    app = FastAPI()
    register_runtime_control_routes(app)
    assert TestClient(app, raise_server_exceptions=False).get("/api/restart/readiness").status_code == 500


def test_guard_rechecks_work_that_starts_while_response_flushes(monkeypatch):
    import asyncio
    from unittest.mock import Mock

    from operator_api import runtime_routes
    from runtime import turn_activity

    pending = []
    sleep_calls = []
    exited = Mock()
    monkeypatch.setattr(STATE, "uvicorn_server", object())
    monkeypatch.setattr(STATE, "background_mgr", None)
    monkeypatch.setattr(STATE, "restart_requested", False)

    async def sleep(seconds):
        sleep_calls.append(seconds)
        if seconds == 0.3:
            turn_activity.begin("flush-race")
        else:
            exited.assert_not_called()
            turn_activity.end("flush-race")

    monkeypatch.setattr(runtime_routes.asyncio, "sleep", sleep)
    monkeypatch.setattr(runtime_routes, "request_server_exit", exited)
    from types import SimpleNamespace

    routers = []
    register_runtime_control_routes(SimpleNamespace(include_router=routers.append))
    # Exercise the drain without shutting down a server or delaying the test.
    route = next(r for r in routers[0].routes if r.path == "/api/restart")
    from starlette.requests import Request

    async def invoke():
        with monkeypatch.context() as patch:
            patch.setattr(runtime_routes.asyncio, "create_task", pending.append)
            response = await route.endpoint(Request({"type": "http"}), {"when_idle": True})
        assert response.status_code == 202
        await pending.pop()

    try:
        asyncio.run(invoke())
        assert sleep_calls == [0.3, 1]
        exited.assert_called_once()
    finally:
        turn_activity.end("flush-race")


def _drive_when_idle_drain(monkeypatch, sleep):
    """Run the when-idle restart drain with a stubbed asyncio.sleep, returning the
    captured request_server_exit mock. The route 202s at request time (idle), schedules
    the drain task, and we await exactly that task — no real server, no delay."""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import Mock

    from operator_api import runtime_routes
    from starlette.requests import Request

    exited = Mock()
    monkeypatch.setattr(runtime_routes.asyncio, "sleep", sleep)
    monkeypatch.setattr(runtime_routes, "request_server_exit", exited)
    routers = []
    register_runtime_control_routes(SimpleNamespace(include_router=routers.append))
    route = next(r for r in routers[0].routes if r.path == "/api/restart")
    pending = []

    async def invoke():
        with monkeypatch.context() as patch:
            patch.setattr(runtime_routes.asyncio, "create_task", pending.append)
            response = await route.endpoint(Request({"type": "http"}), {"when_idle": True})
        assert response.status_code == 202
        await pending.pop()

    asyncio.run(invoke())
    return exited


def test_idle_wait_abandons_restart_when_shutdown_starts(monkeypatch):
    """A Ctrl-C or a hub supervisor stop during the when-idle wait must NOT re-exec.
    The flag that arms _main's re-exec is set only after idle is reconfirmed; if the
    server is already winding down, the wait bails and leaves it unset."""
    from types import SimpleNamespace

    from runtime import turn_activity

    server = SimpleNamespace(should_exit=False)
    monkeypatch.setattr(STATE, "uvicorn_server", server)
    monkeypatch.setattr(STATE, "background_mgr", None)
    monkeypatch.setattr(STATE, "restart_requested", False)

    sleep_calls = []

    async def sleep(seconds):
        sleep_calls.append(seconds)
        if seconds == 0.3:
            turn_activity.begin("shutdown-race")  # a turn races in during the flush
        else:
            server.should_exit = True  # Ctrl-C / hub stop arrives while we wait for idle

    try:
        exited = _drive_when_idle_drain(monkeypatch, sleep)
    finally:
        turn_activity.end("shutdown-race")

    assert sleep_calls == [0.3, 1]
    assert STATE.restart_requested is False  # re-exec never armed → _main exits cleanly
    exited.assert_not_called()


def test_idle_wait_gives_up_and_never_restarts_if_agent_stays_busy(monkeypatch):
    """A turn that raced in during the flush and never finishes must not leave a
    restart queued indefinitely. After the bounded grace the drain abandons without
    arming the re-exec, so no restart can fire later once the operator moved on."""
    from operator_api import runtime_routes
    from runtime import turn_activity

    monkeypatch.setattr(STATE, "uvicorn_server", object())
    monkeypatch.setattr(STATE, "background_mgr", None)
    monkeypatch.setattr(STATE, "restart_requested", False)
    monkeypatch.setattr(runtime_routes, "_RESTART_IDLE_GRACE_S", 3)

    sleep_calls = []

    async def sleep(seconds):
        sleep_calls.append(seconds)
        if seconds == 0.3:
            turn_activity.begin("stuck")  # busy for the whole grace — never ends

    try:
        exited = _drive_when_idle_drain(monkeypatch, sleep)
    finally:
        turn_activity.end("stuck")

    assert sleep_calls == [0.3, 1, 1, 1]  # flush + exactly _RESTART_IDLE_GRACE_S polls
    assert STATE.restart_requested is False
    exited.assert_not_called()
