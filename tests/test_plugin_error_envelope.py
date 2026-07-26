"""Plugin endpoint errors answer with a structured envelope, not a bare 500 (#2259)."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient

from server.agent_init import _install_error_envelope


def _client(router, plugin_id="pr-reviewer") -> TestClient:
    _install_error_envelope(router, plugin_id)
    app = FastAPI()
    app.include_router(router)
    # raise_server_exceptions=False so we observe the response a real caller gets
    # rather than the re-raised exception TestClient surfaces by default.
    return TestClient(app, raise_server_exceptions=False)


def test_unhandled_exception_becomes_a_json_envelope():
    router = APIRouter()

    @router.post("/api/plugins/pr-reviewer/replay")
    async def _replay(body: dict):
        return {"first": body["manifest"][0]}  # IndexError on an empty manifest

    r = _client(router).post("/api/plugins/pr-reviewer/replay", json={"manifest": []})

    assert r.status_code == 500
    assert r.headers["content-type"].startswith("application/json")
    detail = r.json()["detail"]
    assert detail["type"] == "IndexError"
    assert detail["plugin"] == "pr-reviewer"
    assert "IndexError" in detail["error"]


def test_explicit_http_exception_passes_through_untouched():
    """A plugin that validates its own input keeps its status and detail — the host
    must not reinterpret a deliberate 400 as a 500."""
    router = APIRouter()

    @router.post("/api/plugins/x/replay")
    async def _replay(body: dict):
        if not body.get("manifest"):
            raise HTTPException(400, "manifest must not be empty")
        return {"ok": True}

    r = _client(router, "x").post("/api/plugins/x/replay", json={"manifest": []})

    assert r.status_code == 400 and r.json()["detail"] == "manifest must not be empty"


def test_success_path_is_unchanged():
    router = APIRouter()

    @router.get("/api/plugins/x/ping")
    async def _ping():
        return {"pong": True}

    assert _client(router, "x").get("/api/plugins/x/ping").json() == {"pong": True}


def test_sync_handlers_are_wrapped_too():
    router = APIRouter()

    @router.get("/api/plugins/x/boom")
    def _boom():  # deliberately not async
        raise RuntimeError("kaboom")

    r = _client(router, "x").get("/api/plugins/x/boom")

    assert r.status_code == 500 and r.json()["detail"]["type"] == "RuntimeError"


def test_request_params_still_bind_after_wrapping():
    """The wrapper must preserve the signature FastAPI introspects to build the
    dependant — otherwise every wrapped route loses its parameters."""
    router = APIRouter()

    @router.get("/api/plugins/x/echo/{name}")
    async def _echo(name: str, times: int = 1):
        return {"said": name * times}

    r = _client(router, "x").get("/api/plugins/x/echo/ab", params={"times": 3})

    assert r.json() == {"said": "ababab"}


def test_wrapping_is_idempotent_across_reloads():
    router = APIRouter()

    @router.get("/api/plugins/x/ping")
    async def _ping():
        return {"pong": True}

    _install_error_envelope(router, "x")
    first = router.routes[0].endpoint
    _install_error_envelope(router, "x")  # hot-reload re-mounts the same router object

    assert router.routes[0].endpoint is first  # not stacked


def test_websocket_routes_are_left_alone():
    router = APIRouter()

    @router.websocket("/api/plugins/x/ws")
    async def _ws(websocket):  # pragma: no cover - never invoked
        await websocket.accept()

    before = router.routes[0].endpoint
    _install_error_envelope(router, "x")

    assert router.routes[0].endpoint is before
