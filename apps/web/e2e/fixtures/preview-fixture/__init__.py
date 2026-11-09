"""Preview Fixture — the host-free view-harness fixture (#4809).

Exercises the REAL two-router pattern (ADR 0026): a PUBLIC page router under
``/plugins/preview-fixture`` that serves ``public/view.html`` (the iframe page the console
mounts), and a GATED data router under ``/api/plugins/preview-fixture`` with a ``/ping``
endpoint — so ``plugin preview`` proves a router contract, not just a static page. Loaded
host-free by the testkit; FastAPI is imported inside ``register`` so the module imports
with no web stack present (the testkit's parity check only needs ``register`` to exist).
"""

from pathlib import Path


def register(registry):
    """Wire the fixture's view page + data route (ADR 0018)."""
    from fastapi import APIRouter
    from fastapi.responses import HTMLResponse, JSONResponse

    # The page lives beside this module; capture it at register() time so the handler needs
    # no host paths. The harness mock serves the same file off disk, so the two never drift.
    page = Path(getattr(registry, "plugin_dir", Path(__file__).parent)) / "public" / "view.html"

    view = APIRouter()

    @view.get("/view")
    async def _view() -> HTMLResponse:
        return HTMLResponse(page.read_text(encoding="utf-8"))

    registry.register_router(view, prefix="/plugins/preview-fixture")

    data = APIRouter()

    @data.get("/ping")
    async def _ping() -> JSONResponse:
        # A real gated data route — the harness confirms /api/plugins/<id>/* answers for real.
        return JSONResponse({"ok": True, "plugin": "preview-fixture"})

    registry.register_router(data, prefix="/api/plugins/preview-fixture")
