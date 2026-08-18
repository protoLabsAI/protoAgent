"""The two routers: public PAGE (/plugins/artifact) + gated DATA (/api/plugins/artifact)."""

from __future__ import annotations

import logging
from pathlib import Path

from . import _config, _render_status, _shell, _store

log = logging.getLogger("protoagent.plugins.artifact")

_VENDOR_FILES = {
    # UMD (SRI-pinned in the shell's LIB map)
    "mermaid.min.js",
    "react.production.min.js",
    "react-dom.production.min.js",
    "babel.min.js",
    # ESM modules (the `react` import map): curated libs …
    "d3.mjs",
    "chartjs.mjs",
    "lucide.mjs",
    "marked.mjs",
    # … React shims + authored design-system wrappers
    "react.shim.mjs",
    "react-dom-client.shim.mjs",
    "pl-ui.mjs",
}


def _build_view_router():
    """The shell PAGE — served under the PUBLIC ``/plugins/artifact`` prefix
    (plugin-view rule 2): a browser iframe page-load can't carry an Authorization
    bearer, so a gated page 401-blanks under the token gate. The page is also where
    the slug-aware base is derived (``location.pathname.split("/plugins/")[0]``), so
    it MUST live under ``/plugins/`` — a ``/api/plugins/`` page poisons the base to
    ``/api`` and the kit's ``/_ds/`` assets 404 (the bug this split fixes). The page
    fetches its DATA from the gated data router with the handshake token."""
    from fastapi import APIRouter
    from fastapi.responses import FileResponse, HTMLResponse, Response

    router = APIRouter()

    @router.get("/view")
    async def _view():
        return HTMLResponse(_shell._SHELL_HTML)

    # Vendored JS libs (react/react-dom/babel/mermaid) served SAME-ORIGIN so the
    # react/mermaid kinds work fully OFFLINE — no cdnjs dependency, and the
    # `network: []` capability is now literally true. Allowlisted (no path
    # traversal); the sandboxed artifact iframe loads these by absolute URL.
    # Versioned bytes → cache hard; SRI in the artifact still pins them.
    @router.get("/vendor/{name}")
    async def _vendor(name: str):
        if name not in _VENDOR_FILES:
            return Response(status_code=404)
        f = Path(__file__).parent / "vendor" / name
        if not f.exists():
            return Response(status_code=404, content=f"{name} not vendored")
        return FileResponse(
            f,
            media_type="application/javascript",
            headers={
                "Cache-Control": "public, max-age=31536000, immutable",
                # The sandboxed artifact iframe is an opaque origin, so its load of
                # this lib is cross-origin → CORS + crossorigin="anonymous" are
                # needed for the SRI check to run.
                "Access-Control-Allow-Origin": "*",
            },
        )

    return router


def _build_data_router():
    """The DATA routes — mounted under ``/api/plugins/artifact`` so they inherit the
    operator bearer gate (plugin-view rule 2). The shell page reads them with the
    handshake token; DELETE is the panel's user-driven cleanup."""
    from fastapi import APIRouter, Body, Header, HTTPException, Response
    from fastapi.responses import JSONResponse

    router = APIRouter()

    @router.get("/current")
    async def _current_artifact() -> dict:
        """The focused artifact's latest version (back-compat shape + version info)."""
        _render_status._note_poll()  # a poll ⇒ a renderer is live (gates the inline render-error wait, #1458)
        store = _store._read_store()
        art = _store._find(store, store["current"])
        if art is None:
            return {
                "id": "",
                "kind": "",
                "code": "",
                "title": "",
                "ts": 0,
                "version": 0,
            }
        v = art["versions"][-1]
        return {
            "id": art["id"],
            "kind": art["kind"],
            "code": v["code"],
            "title": art["title"],
            "ts": v["ts"],
            "version": len(art["versions"]),
        }

    @router.get("/history")
    async def _history(if_none_match: str | None = Header(default=None)):
        """The full store — every artifact with its version chain — for the panel's
        artifact picker + version navigation.

        Conditional (#2256): responses carry a weak ETag derived from the store
        file's mtime+size, and a matching ``If-None-Match`` short-circuits to an
        empty 304 BEFORE the store is even read — the panel polls continuously,
        and between changes that poll must cost nothing to serve."""
        _render_status._note_poll()  # a poll ⇒ a renderer is live (gates the inline render-error wait, #1458)
        etag = _store._store_etag()
        if etag and if_none_match == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return JSONResponse(_store._read_store(), headers={"ETag": etag})

    @router.post("/render-status")
    async def _render_status_route(body: dict = Body(...)) -> dict:
        # Named *_route: a bare `_render_status` here would shadow the module import
        # for every sibling closure in this builder (the #2817 split's one collision).
        """The sandbox's render verdict for a version, relayed by the shell (#1458): the
        nested artifact frame reports ``{ok}`` once it mounts or ``{ok:false, error}`` when
        it throws / never mounts. Stamped onto the version so check_artifact + the create/edit
        tools can surface render failures back to the agent. Best-effort: unknown id/version
        is a no-op (the panel may be a version behind), never an error."""
        art_id = str(body.get("id") or "")
        try:
            version = int(body.get("version") or 0)
        except (TypeError, ValueError):
            version = 0
        store = _store._read_store()
        art = _store._find(store, art_id)
        if art is None or not (1 <= version <= len(art.get("versions") or [])):
            return {"ok": True, "recorded": False}
        art["versions"][version - 1]["render"] = {
            "ok": bool(body.get("ok")),
            "error": str(body.get("error") or "")[: _render_status._RENDER_ERR_MAX],
            "ts": _store._now(),
        }
        _store._write_store(store)
        return {"ok": True, "recorded": True}

    @router.post("/ask")
    async def _ask(body: dict = Body(...)) -> dict:
        """Interactive bridge: a sandboxed artifact's ``window.protoArtifact.ask(prompt)``
        reaches the agent here (the ``window.claude.complete`` analog). OPT-IN
        (``ARTIFACT_ASK_ENABLED``) — letting artifact code trigger LLM calls is a
        cost/abuse surface. Gated by the operator bearer like the rest. Runs a BARE
        completion (no tools/agent loop) via the consumption SDK."""
        if not _config._ask_enabled():
            raise HTTPException(
                403,
                "Artifact 'ask' is disabled — set ARTIFACT_ASK_ENABLED=1 to let artifacts call the agent.",
            )
        prompt = str(body.get("prompt", "")).strip()
        if not prompt:
            raise HTTPException(400, "prompt required")
        cap = _config._ask_max_chars()
        if len(prompt) > cap:
            raise HTTPException(413, f"prompt too long (> {cap} chars)")
        try:
            from graph.sdk import complete  # ADR 0043 consumption SDK
        except Exception:  # noqa: BLE001
            raise HTTPException(
                501,
                "This protoAgent build doesn't support artifact ask (needs graph.sdk.complete — upgrade the host).",
            ) from None
        try:
            text = await complete(prompt, system=_config._ask_system())
        except Exception as e:  # noqa: BLE001
            log.warning("[artifact] ask completion failed", exc_info=True)
            raise HTTPException(502, f"completion failed: {e}") from None
        return {"text": text}

    @router.put("/artifact/{art_id}")
    async def _save_edit(art_id: str, body: dict = Body(...)) -> dict:
        """Save a USER edit (the panel's in-panel code editor) as a new version. Like the
        agent's rewrite, but tagged ``by: user`` so the provenance is visible — and, like
        every edit, it APPENDS a version rather than overwriting (no silent clobber)."""
        code = str(body.get("code", ""))
        if err := _store._too_big(code):
            raise HTTPException(413, err)
        store = _store._read_store()
        art = _store._find(store, art_id)
        if art is None:
            raise HTTPException(404, f"unknown artifact {art_id}")
        if _store._is_file(art):  # a file artifact's preview isn't user-editable (would orphan its blob)
            raise HTTPException(409, "file artifacts are not editable — re-save the file")
        v = _store._commit_version(store, art, code, by="user")
        return {"ok": True, "id": art_id, "version": v}

    @router.get("/artifact/{art_id}/blob")
    async def _blob(art_id: str, version: int = 0):
        """Serve a `file` artifact version's stored BYTES for download (ADR 0092 D2). The
        panel's Download button hits this with the operator bearer; ``version`` is 1-based
        (0/absent = latest). Returns the sidecar blob with its stored mime + an attachment
        filename. 404 if the artifact/version/blob is missing or isn't a file artifact."""
        from fastapi.responses import FileResponse

        store = _store._read_store()
        art = _store._find(store, art_id)
        if art is None:
            raise HTTPException(404, f"unknown artifact {art_id}")
        vers = art.get("versions") or []
        if not vers:
            raise HTTPException(404, "no versions")
        if version:  # an EXPLICIT version must be in range — don't silently fall back to latest
            if not (1 <= version <= len(vers)):
                raise HTTPException(404, f"no version {version} (have 1..{len(vers)})")
            idx = version - 1
        else:  # 0/absent → latest
            idx = len(vers) - 1
        v = vers[idx]
        blob_name, meta = v.get("blob"), v.get("file") or {}
        if not blob_name:
            raise HTTPException(404, "not a file artifact / no stored blob")
        f = _store._blob_path(art_id, blob_name)
        if not f.exists():
            raise HTTPException(404, "blob missing")
        return FileResponse(
            f,
            media_type=meta.get("mime") or "application/octet-stream",
            filename=meta.get("filename") or f.name,
        )

    @router.delete("/artifact/{art_id}")
    async def _delete(art_id: str) -> dict:
        """Delete an artifact (the panel's trash button). Gated like the rest."""
        store = _store._read_store()
        if _store._find(store, art_id) is None:
            raise HTTPException(404, f"unknown artifact {art_id}")
        store["artifacts"] = [a for a in store["artifacts"] if a["id"] != art_id]
        if store["current"] == art_id:
            store["current"] = store["artifacts"][0]["id"] if store["artifacts"] else None
        _store._write_store(store)
        _store._emit("deleted", {"id": art_id})
        return {"ok": True, "deleted": art_id}

    return router
