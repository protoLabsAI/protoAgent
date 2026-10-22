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
    """Mount ``GET``/``HEAD /media/{name}`` on the FastAPI app."""
    from fastapi import HTTPException
    from fastapi.responses import FileResponse

    # HEAD answers the same headers with no body, behind the same auth (#4258).
    @app.api_route("/media/{name}", methods=["GET", "HEAD"], include_in_schema=False)
    async def _serve_media(name: str) -> FileResponse:
        from infra.media import resolve_media

        got = resolve_media(name)
        if got is None:
            # One answer for unsafe, internal and absent names — discloses nothing.
            raise HTTPException(status_code=404, detail="media not found")
        path, mime = got
        return FileResponse(str(path), media_type=mime)

    # The desktop shell's "open in new tab" for a chat image (#4312): the shell hands the file
    # to the SYSTEM BROWSER, which has no bearer — so it swaps the console's permanent link for
    # a short-lived one first. Under ``/api/``, so the default-deny gate requires the operator
    # bearer; the body must already carry a valid signed link (``infra.media.open_link``).
    @app.post("/api/media/open-link", include_in_schema=False)
    async def _media_open_link(body: dict) -> dict:
        from infra.media import OPEN_LINK_TTL_S, open_link

        link = open_link(str((body or {}).get("url") or ""))
        if link is None:
            raise HTTPException(status_code=404, detail="media not found")
        return {"url": link, "expires_in": OPEN_LINK_TTL_S}

    # Re-mint an EXPIRED signed link this box minted (#4274): a member's old hub-console history
    # carries expiring links (``media.remote_link_ttl_s``, default 7 days), so once they lapse the
    # inline image 403s. This hands back a fresh link for the SAME file. Behind the operator bearer
    # like open-link — a valid signature in the body does NOT exempt it (it is not a media path).
    # The handler logs nothing: the body carries a ``sig`` (and the full signed URL), and neither
    # must reach the logs.
    @app.post("/api/media/remint", include_in_schema=False)
    async def _media_remint(body: dict) -> dict:
        from infra.media import remint_link

        link = remint_link(str((body or {}).get("url") or ""))
        if link is None:
            raise HTTPException(status_code=404, detail="media not found")
        return {"url": link}
