"""Plugin-route hot-mount (ADR 0018 + #797) — enabling a route-bearing plugin on a
config reload mounts its routes WITHOUT a restart; already-mounted routers are skipped."""

from __future__ import annotations

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from runtime.state import STATE
from server.agent_init import _mount_plugin_routers


def _router(marker: str) -> APIRouter:
    r = APIRouter()

    @r.get("/ping")
    def _ping():
        return {"from": marker}

    return r


@pytest.fixture
def app(monkeypatch):
    a = FastAPI()
    monkeypatch.setattr(STATE, "fastapi_app", a)
    monkeypatch.setattr(STATE, "plugin_router_keys", set())
    return a


def test_mounts_and_serves(app):
    _mount_plugin_routers([{"plugin_id": "delegates", "router": _router("delegates"),
                            "prefix": "/api/delegates"}])
    c = TestClient(app)
    assert c.get("/api/delegates/ping").json() == {"from": "delegates"}


def test_remount_skipped_new_added(app):
    first = {"plugin_id": "a", "router": _router("a"), "prefix": "/api/a"}
    _mount_plugin_routers([first])
    n_routes = len(app.routes)

    # Reload with the same plugin again + a newly-enabled one: the existing router
    # is NOT re-mounted (no duplicate routes), the new one comes up live.
    _mount_plugin_routers([first, {"plugin_id": "b", "router": _router("b"), "prefix": "/api/b"}])
    assert len(app.routes) == n_routes + 1
    c = TestClient(app)
    assert c.get("/api/a/ping").json() == {"from": "a"}
    assert c.get("/api/b/ping").json() == {"from": "b"}
    assert STATE.plugin_router_keys == {("a", "/api/a"), ("b", "/api/b")}


def test_noop_without_app(monkeypatch):
    monkeypatch.setattr(STATE, "fastapi_app", None)
    monkeypatch.setattr(STATE, "plugin_router_keys", set())
    _mount_plugin_routers([{"plugin_id": "x", "router": _router("x"), "prefix": "/x"}])
    assert STATE.plugin_router_keys == set()  # nothing mounted, nothing tracked


def test_bad_router_does_not_break_the_batch(app):
    _mount_plugin_routers([
        {"plugin_id": "bad", "router": object(), "prefix": "/api/bad"},  # include_router raises
        {"plugin_id": "good", "router": _router("good"), "prefix": "/api/good"},
    ])
    c = TestClient(app)
    assert c.get("/api/good/ping").json() == {"from": "good"}
    assert ("bad", "/api/bad") not in STATE.plugin_router_keys
