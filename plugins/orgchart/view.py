"""orgChart routers — the PAGE (public) and the DATA (gated), per the view contract.

The page is a self-contained ``view.html`` beside this module (one file on purpose:
a page split into ``.js``/``.css`` sub-resources would need each one added to the
manifest's ``public_paths`` or 401 behind the bearer gate — the artifact plugin's
documented fleet-wide incident). Topology assembly lives in :mod:`.topology`.
"""

from __future__ import annotations

from pathlib import Path

_VIEW_PAGE = Path(__file__).parent / "view.html"


def build_view_router():
    """The PUBLIC page router (mounted at ``/plugins/orgchart``)."""
    from fastapi import APIRouter
    from fastapi.responses import HTMLResponse

    router = APIRouter()

    @router.get("/view")
    async def _view() -> HTMLResponse:  # served at /plugins/orgchart/view
        return HTMLResponse(_VIEW_PAGE.read_text(encoding="utf-8"))

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
