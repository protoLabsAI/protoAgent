"""Hot REMOUNT of plugin routers (ADR 0096 live QA, the #942 class).

The loop's first live demo hit this within an hour: the agent scaffolded a view,
rewrote it, called reload_plugins — and the iframe kept serving the stale scaffold
page, because the first mount won forever ("FastAPI has no route-removal API" is
only true of the public API; ``app.router.routes`` is a plain list Starlette
iterates per request). The agent correctly told the operator "needs a restart"
mid-demo. Now: a reload REPLACES a mounted plugin's routes with the current
code's, and a roster-absent plugin (disabled/uninstalled) has its routes removed
outright.
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from runtime.state import STATE
from server.agent_init import _mount_plugin_routers


def _router(reply: str) -> APIRouter:
    r = APIRouter()

    @r.get("/view")
    async def _view():
        return {"msg": reply}

    return r


def _fresh_app(monkeypatch) -> FastAPI:
    app = FastAPI()
    monkeypatch.setattr(STATE, "fastapi_app", app, raising=False)
    monkeypatch.setattr(STATE, "plugin_router_keys", set(), raising=False)
    monkeypatch.setattr(STATE, "plugin_router_routes", {}, raising=False)
    return app


def test_reload_serves_the_current_router_code(monkeypatch):
    app = _fresh_app(monkeypatch)
    key = {"plugin_id": "weather", "prefix": "/plugins/weather"}
    _mount_plugin_routers([{**key, "router": _router("scaffold hello")}])
    c = TestClient(app)
    assert c.get("/plugins/weather/view").json()["msg"] == "scaffold hello"

    n_after_first_mount = len(app.router.routes)

    # The reload passes a FRESH router built from the CURRENT code — it must serve
    # (previously the first mount won forever and the edit was invisible).
    _mount_plugin_routers([{**key, "router": _router("real weather page")}])
    assert c.get("/plugins/weather/view").json()["msg"] == "real weather page"
    # ...with no leak: the stale entry left when the fresh one landed, so the route
    # table is the same size after any number of remounts.
    _mount_plugin_routers([{**key, "router": _router("third revision")}])
    assert c.get("/plugins/weather/view").json()["msg"] == "third revision"
    assert len(app.router.routes) == n_after_first_mount


def test_disable_unmounts_the_routes(monkeypatch):
    app = _fresh_app(monkeypatch)
    _mount_plugin_routers([{"plugin_id": "weather", "prefix": "/plugins/weather", "router": _router("x")}])
    c = TestClient(app)
    assert c.get("/plugins/weather/view").status_code == 200

    _mount_plugin_routers([])  # full roster without the plugin = disabled/uninstalled
    assert c.get("/plugins/weather/view").status_code == 404
    assert ("weather", "/plugins/weather") not in STATE.plugin_router_keys
    assert ("weather", "/plugins/weather") not in STATE.plugin_router_routes


def test_other_plugins_survive_a_remount(monkeypatch):
    app = _fresh_app(monkeypatch)
    _mount_plugin_routers(
        [
            {"plugin_id": "a", "prefix": "/plugins/a", "router": _router("a1")},
            {"plugin_id": "b", "prefix": "/plugins/b", "router": _router("b1")},
        ]
    )
    c = TestClient(app)
    _mount_plugin_routers(
        [
            {"plugin_id": "a", "prefix": "/plugins/a", "router": _router("a2")},
            {"plugin_id": "b", "prefix": "/plugins/b", "router": _router("b1-again")},
        ]
    )
    assert c.get("/plugins/a/view").json()["msg"] == "a2"
    assert c.get("/plugins/b/view").json()["msg"] == "b1-again"


def test_second_router_at_same_prefix_still_dropped(monkeypatch):
    app = _fresh_app(monkeypatch)
    _mount_plugin_routers(
        [
            {"plugin_id": "p", "prefix": "/plugins/p", "router": _router("first")},
            {"plugin_id": "p", "prefix": "/plugins/p", "router": _router("second")},
        ]
    )
    c = TestClient(app)
    assert c.get("/plugins/p/view").json()["msg"] == "first"
