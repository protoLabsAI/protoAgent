"""Plugin-router mounting (``server.agent_init._mount_plugin_routers``).

* Hot-MOUNT (ADR 0018 + #797) — enabling a route-bearing plugin on a config reload
  mounts its routes WITHOUT a restart.
* Hot-REMOUNT (ADR 0096 live QA, the #942 class) — the loop's first live demo hit
  this within an hour: the agent scaffolded a view, rewrote it, called
  reload_plugins — and the iframe kept serving the stale scaffold page, because the
  first mount won forever ("FastAPI has no route-removal API" is only true of the
  public API; ``app.router.routes`` is a plain list Starlette iterates per request).
  Now a reload REPLACES a mounted plugin's routes with the current code's, and a
  roster-absent plugin (disabled/uninstalled) has its routes removed outright.
"""

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


def _view_router(reply: str) -> APIRouter:
    r = APIRouter()

    @r.get("/view")
    async def _view():
        return {"msg": reply}

    return r


@pytest.fixture
def app(monkeypatch):
    a = FastAPI()
    monkeypatch.setattr(STATE, "fastapi_app", a, raising=False)
    monkeypatch.setattr(STATE, "plugin_router_keys", set(), raising=False)
    # Reset the remount bookkeeping too, so no test inherits another's mounted routes.
    monkeypatch.setattr(STATE, "plugin_router_routes", {}, raising=False)
    return a


# ── hot-mount ────────────────────────────────────────────────────────────────


def test_mounts_and_serves(app):
    _mount_plugin_routers([{"plugin_id": "delegates", "router": _router("delegates"), "prefix": "/api/delegates"}])
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


def test_remount_is_silent_no_op(app, caplog):
    # #1878: re-passing the SAME router on a reload (the routine case — every
    # reload rebuilds the full router list from scratch) must NOT warn. Only a
    # genuine intra-batch duplicate (two distinct routers, one prefix) should —
    # and the first router in the batch keeps the prefix.
    first = {"plugin_id": "a", "router": _router("a"), "prefix": "/api/a"}
    _mount_plugin_routers([first])

    with caplog.at_level("WARNING", logger="server.agent_init"):
        _mount_plugin_routers([first])
    assert "registered a second router" not in caplog.text

    with caplog.at_level("WARNING", logger="server.agent_init"):
        _mount_plugin_routers([first, {"plugin_id": "a", "router": _router("a2"), "prefix": "/api/a"}])
    assert "registered a second router" in caplog.text
    assert TestClient(app).get("/api/a/ping").json() == {"from": "a"}


def test_noop_without_app(monkeypatch):
    monkeypatch.setattr(STATE, "fastapi_app", None)
    monkeypatch.setattr(STATE, "plugin_router_keys", set())
    _mount_plugin_routers([{"plugin_id": "x", "router": _router("x"), "prefix": "/x"}])
    assert STATE.plugin_router_keys == set()  # nothing mounted, nothing tracked


def test_bad_router_does_not_break_the_batch(app):
    _mount_plugin_routers(
        [
            {"plugin_id": "bad", "router": object(), "prefix": "/api/bad"},  # include_router raises
            {"plugin_id": "good", "router": _router("good"), "prefix": "/api/good"},
        ]
    )
    c = TestClient(app)
    assert c.get("/api/good/ping").json() == {"from": "good"}
    assert ("bad", "/api/bad") not in STATE.plugin_router_keys


# ── hot-remount ──────────────────────────────────────────────────────────────


def test_reload_serves_the_current_router_code(app):
    # The production prefix for a plugin view router (#1732: /api/plugins/<id>).
    key = {"plugin_id": "weather", "prefix": "/api/plugins/weather"}
    _mount_plugin_routers([{**key, "router": _view_router("scaffold hello")}])
    c = TestClient(app)
    assert c.get("/api/plugins/weather/view").json()["msg"] == "scaffold hello"

    n_after_first_mount = len(app.router.routes)

    # The reload passes a FRESH router built from the CURRENT code — it must serve
    # (previously the first mount won forever and the edit was invisible).
    _mount_plugin_routers([{**key, "router": _view_router("real weather page")}])
    assert c.get("/api/plugins/weather/view").json()["msg"] == "real weather page"
    # ...with no leak: the stale entry left when the fresh one landed, so the route
    # table is the same size after any number of remounts.
    _mount_plugin_routers([{**key, "router": _view_router("third revision")}])
    assert c.get("/api/plugins/weather/view").json()["msg"] == "third revision"
    assert len(app.router.routes) == n_after_first_mount


def test_disable_unmounts_the_routes(app):
    _mount_plugin_routers([{"plugin_id": "weather", "prefix": "/plugins/weather", "router": _view_router("x")}])
    c = TestClient(app)
    assert c.get("/plugins/weather/view").status_code == 200

    _mount_plugin_routers([])  # full roster without the plugin = disabled/uninstalled
    assert c.get("/plugins/weather/view").status_code == 404
    assert ("weather", "/plugins/weather") not in STATE.plugin_router_keys
    assert ("weather", "/plugins/weather") not in STATE.plugin_router_routes


def test_other_plugins_survive_a_remount(app):
    _mount_plugin_routers(
        [
            {"plugin_id": "a", "prefix": "/plugins/a", "router": _view_router("a1")},
            {"plugin_id": "b", "prefix": "/plugins/b", "router": _view_router("b1")},
        ]
    )
    c = TestClient(app)
    _mount_plugin_routers(
        [
            {"plugin_id": "a", "prefix": "/plugins/a", "router": _view_router("a2")},
            {"plugin_id": "b", "prefix": "/plugins/b", "router": _view_router("b1-again")},
        ]
    )
    assert c.get("/plugins/a/view").json()["msg"] == "a2"
    assert c.get("/plugins/b/view").json()["msg"] == "b1-again"
