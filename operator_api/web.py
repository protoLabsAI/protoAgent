"""Static serving helpers for the React operator console."""

from __future__ import annotations

from pathlib import Path

from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


def mount_ds_plugin_kit(app, dist_dir: str | Path) -> bool:
    """Serve the design-system plugin-kit at ``/_ds/plugin-kit.{css,js}`` — in EVERY
    UI tier, including ``--ui none`` fleet members.

    Plugin iframe views ``<link>``/``<script>`` these same-origin paths (alongside
    their ``/plugins/<id>/…`` routes) so a view always matches the console's installed
    DS version instead of pinning a CDN copy. This is mounted **independently of the
    full console SPA** precisely because a fleet MEMBER (``--ui none``) serves its
    plugins' view pages but never mounts the console — without the kit those proxied
    views render with no design system (the "borked styling" desync; see
    ``docs/dev/version-coherence.md`` Axis 3). Emitted to ``dist/_ds/`` by the web
    build (``apps/web/scripts/copy-plugin-kit.mjs``). Returns True when at least one
    kit asset was served; False when the build output is absent (no ``npm run build``
    yet) — the ``/_ds`` route then 404s and views fall back to their own defaults.
    """
    dist = Path(dist_dir).resolve()
    mounted = False

    ds_kit_css = dist / "_ds" / "plugin-kit.css"
    if ds_kit_css.is_file():

        @app.get("/_ds/plugin-kit.css", include_in_schema=False)
        async def _ds_plugin_kit_css() -> FileResponse:
            return FileResponse(str(ds_kit_css), media_type="text/css")

        mounted = True

    ds_kit_js = dist / "_ds" / "plugin-kit.js"
    if ds_kit_js.is_file():

        @app.get("/_ds/plugin-kit.js", include_in_schema=False)
        async def _ds_plugin_kit_js() -> FileResponse:
            return FileResponse(str(ds_kit_js), media_type="application/javascript")

        mounted = True

    return mounted


def mount_react_app(app, dist_dir: str | Path, *, app_path: str = "/app") -> bool:
    """Mount a built Vite app under ``app_path`` when dist assets exist.

    The console SPA (``/app`` + its assets) — console/full tiers only. The DS
    plugin-kit (``/_ds/…``) is mounted separately by ``mount_ds_plugin_kit`` so it
    rides every tier. Returns False when the build output is absent.
    """
    dist = Path(dist_dir).resolve()
    index_path = dist / "index.html"
    if not index_path.exists():
        return False

    app_path = "/" + app_path.strip("/")
    assets_dir = dist / "assets"
    if assets_dir.exists():
        app.mount(
            f"{app_path}/assets",
            StaticFiles(directory=str(assets_dir)),
            name="operator_assets",
        )

    @app.get(app_path, include_in_schema=False)
    async def _operator_index() -> FileResponse:
        return FileResponse(str(index_path))

    @app.get(f"{app_path}/{{path:path}}", include_in_schema=False)
    async def _operator_fallback(path: str) -> FileResponse:
        candidate = (dist / path).resolve()
        try:
            candidate.relative_to(dist)
        except ValueError:
            candidate = index_path
        if candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(index_path))

    return True
