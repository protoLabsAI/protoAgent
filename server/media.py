"""Core media-serving route (#1929) — the serve half of the media output channel.

ONE core route serves every artifact a plugin tool persisted via
``registry.save_media()`` (``infra/media.py``), so no media-producing plugin has
to mount its own FastAPI route. Auth is enforced UPSTREAM by the default-deny
middleware (``a2a_impl/auth.py``): a ``/media/`` request reaches this handler
only with a valid per-file ``?sig=`` signature, a bearer credential, or the
explicit ``media.public`` opt-in — so the handler itself only validates the
name and streams the file.

Lives in ``server/`` (not ``graph``/``infra``) per the import-layering contract:
the store is infra state; the HTTP surface that exposes it is the server's.
"""

from __future__ import annotations


def register_media_routes(app) -> None:
    """Mount ``GET /media/{name}`` on the FastAPI app."""
    from fastapi import HTTPException
    from fastapi.responses import FileResponse

    @app.get("/media/{name}", include_in_schema=False)
    async def _serve_media(name: str) -> FileResponse:
        from infra.media import resolve_media

        got = resolve_media(name)
        if got is None:
            # One answer for unsafe, internal and absent names — discloses nothing.
            raise HTTPException(status_code=404, detail="media not found")
        path, mime = got
        return FileResponse(str(path), media_type=mime)
