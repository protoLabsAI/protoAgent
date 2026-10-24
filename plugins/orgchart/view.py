"""orgChart routers — the PAGE (public) and the DATA (gated), per the view contract.

The page is authored as three files under ``view/`` and served as ONE self-contained
document: the CSS and JS are inlined here. A page that loaded ``.js``/``.css``
sub-resources would need each one added to the manifest's ``public_paths`` or 401 behind
the bearer gate — the artifact plugin's documented fleet-wide incident. Topology assembly
lives in :mod:`.topology`.
"""

from __future__ import annotations

from pathlib import Path

_VIEW_DIR = Path(__file__).parent / "view"


def view_page() -> str:
    """The assembled page. Read per request, so an edited view shows on reload."""
    html = (_VIEW_DIR / "orgchart.html").read_text(encoding="utf-8")
    css = (_VIEW_DIR / "orgchart.css").read_text(encoding="utf-8")
    js = (_VIEW_DIR / "orgchart.js").read_text(encoding="utf-8")
    # Replace the JS last and by split/join: the script body could contain the CSS marker.
    return html.replace("__ORGCHART_CSS__", css).replace("__ORGCHART_JS__", js)


def build_view_router():
    """The PUBLIC page router (mounted at ``/plugins/orgchart``)."""
    from fastapi import APIRouter
    from fastapi.responses import HTMLResponse

    router = APIRouter()

    # `response_class=`, not a `-> HTMLResponse` annotation: with postponed annotations the
    # return type is a string FastAPI resolves against this MODULE's globals, where the
    # function-local import isn't visible — the unresolved reference broke /openapi.json
    # for every agent with orgChart enabled.
    @router.get("/view", response_class=HTMLResponse)
    async def _view():  # served at /plugins/orgchart/view
        return HTMLResponse(view_page())

    return router


def build_data_router(live_config):
    """The GATED data router (mounted at ``/api/plugins/orgchart``). ``live_config`` is
    ``registry.live_config`` — read per request so Settings edits apply without a
    restart. ``?refresh=1`` drops every cache and crawls inline (the view's ⟳ button)."""
    from fastapi import APIRouter

    from . import topology

    router = APIRouter()

    @router.get("/topology")
    async def _topology(refresh: int = 0) -> dict:
        return await topology.get_topology(live_config() or {}, force=bool(refresh))

    return router
