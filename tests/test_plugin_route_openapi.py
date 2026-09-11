"""One plugin route must not take down the whole API schema.

Found in QA of v0.164.0: ``/openapi.json`` (and ``/docs``) answered 500 on every agent
that enabled orgChart, the portfolio plugin or the learning-wiki plugin. Each has a page
route written as::

    from __future__ import annotations
    def build_view_router():
        from fastapi.responses import HTMLResponse      # imported INSIDE the function
        @router.get("/view")
        async def _view() -> HTMLResponse: ...

With postponed annotations the return type is the string ``"HTMLResponse"``, FastAPI
resolves it against the MODULE's globals, can't, and infers a response model from an
unresolved forward reference — which pydantic refuses when the schema is generated.
One such route anywhere on the app, and the schema for every route is gone.

The host now probes each plugin route's schema as it mounts it: a route whose schema
can't be built is left out of ``/openapi.json`` (it still serves) with a warning that
names the plugin, the route and the fix.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from runtime.state import STATE
from server.agent_init import _mount_plugin_routers


def _fresh_app(monkeypatch) -> FastAPI:
    app = FastAPI()
    monkeypatch.setattr(STATE, "fastapi_app", app, raising=False)
    monkeypatch.setattr(STATE, "plugin_router_keys", set(), raising=False)
    monkeypatch.setattr(STATE, "plugin_router_routes", {}, raising=False)
    return app


def _page_router_with_a_local_import() -> APIRouter:
    """The orgChart / portfolio / learning-wiki shape, verbatim in spirit."""
    from fastapi.responses import HTMLResponse  # deliberately NOT at module level

    router = APIRouter()

    @router.get("/view")
    async def _view() -> HTMLResponse:
        return HTMLResponse("<p>page</p>")

    return router


def _data_router() -> APIRouter:
    router = APIRouter()

    @router.get("/items")
    async def _items() -> dict:
        return {"items": []}

    return router


def test_a_route_whose_schema_cannot_be_built_does_not_break_the_schema(monkeypatch, caplog):
    app = _fresh_app(monkeypatch)
    with caplog.at_level(logging.WARNING):
        _mount_plugin_routers(
            [
                {"plugin_id": "pagey", "prefix": "/plugins/pagey", "router": _page_router_with_a_local_import()},
                {"plugin_id": "datay", "prefix": "/api/plugins/datay", "router": _data_router()},
            ]
        )

    schema = app.openapi()  # raised PydanticUserError before the host probed routes
    assert "/api/plugins/datay/items" in schema["paths"]  # the healthy route is documented
    assert "/plugins/pagey/view" not in schema["paths"]  # the broken one is left out...
    assert TestClient(app).get("/plugins/pagey/view").text == "<p>page</p>"  # ...but still serves
    warned = [r.getMessage() for r in caplog.records if "left out of /openapi.json" in r.getMessage()]
    assert len(warned) == 1  # only the broken route, and only once
    assert "pagey" in warned[0] and "/view" in warned[0] and "response_class" in warned[0]


def test_the_orgchart_page_is_in_the_schema(monkeypatch):
    """orgChart itself is fixed, not just contained: its page declares its response class."""
    from plugins.orgchart.view import build_view_router

    app = _fresh_app(monkeypatch)
    _mount_plugin_routers([{"plugin_id": "orgchart", "prefix": "/plugins/orgchart", "router": build_view_router()}])
    assert "/plugins/orgchart/view" in app.openapi()["paths"]


def test_a_disabled_plugin_leaves_the_cached_schema(monkeypatch):
    """FastAPI caches the schema and only rebuilds it when its route list's version moves.
    The host retires a disabled plugin's routes by removing them from the list directly,
    so the cached schema must be dropped too — or /openapi.json keeps documenting routes
    that no longer exist until a restart."""
    app = _fresh_app(monkeypatch)
    _mount_plugin_routers(
        [
            {"plugin_id": "datay", "prefix": "/api/plugins/datay", "router": _data_router()},
            {"plugin_id": "gone", "prefix": "/api/plugins/gone", "router": _data_router()},
        ]
    )
    assert "/api/plugins/gone/items" in app.openapi()["paths"]

    _mount_plugin_routers([{"plugin_id": "datay", "prefix": "/api/plugins/datay", "router": _data_router()}])
    assert TestClient(app).get("/api/plugins/gone/items").status_code == 404  # unmounted...
    assert "/api/plugins/gone/items" not in app.openapi()["paths"]  # ...and undocumented
