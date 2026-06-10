"""Static serving helpers for the React operator console."""

from __future__ import annotations

from pathlib import Path

from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


def mount_react_app(app, dist_dir: str | Path, *, app_path: str = "/app") -> bool:
    """Mount a built Vite app under ``app_path`` when dist assets exist.

    Returns False when the build output is absent so local Gradio-only dev
    remains unchanged until ``npm run web:build`` has produced the React app.
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

    # The design-system plugin-kit, served same-origin at /_ds/plugin-kit.{css,js}
    # (root, alongside /plugins/<id>/…). Plugin iframe views `<link>`/`<script>` these
    # paths instead of pinning a CDN copy, so every view matches the console's installed
    # DS version. Emitted to dist/_ds/ by the web build (apps/web/scripts/copy-plugin-kit.mjs).
    ds_kit_css = dist / "_ds" / "plugin-kit.css"
    if ds_kit_css.is_file():

        @app.get("/_ds/plugin-kit.css", include_in_schema=False)
        async def _ds_plugin_kit_css() -> FileResponse:
            return FileResponse(str(ds_kit_css), media_type="text/css")

    ds_kit_js = dist / "_ds" / "plugin-kit.js"
    if ds_kit_js.is_file():

        @app.get("/_ds/plugin-kit.js", include_in_schema=False)
        async def _ds_plugin_kit_js() -> FileResponse:
            return FileResponse(str(ds_kit_js), media_type="application/javascript")

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
