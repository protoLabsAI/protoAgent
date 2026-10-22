"""The two routers: public PAGE (/plugins/artifact) + gated DATA (/api/plugins/artifact)."""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
from pathlib import Path
from urllib.parse import quote

from . import _config, _links, _media, _render_status, _shell, _store

log = logging.getLogger("protoagent.plugins.artifact")

_BLOB_CHUNK = 64 * 1024  # bytes per body message when streaming a blob download
_BLOB_RESOLVE_TRIES = 5  # store re-reads when the blob a read named was swept before it could be opened


def _open_linked(link: dict, meta: dict):
    """Open a reference through the live fence; refuse unavailable or changed source bytes.

    Validate the opened handle before returning it, and stream that same handle. The
    reference digest also binds media tickets without inventing a sidecar blob.
    """
    from fastapi import HTTPException
    from tools.fs_view import open_regular

    fh = None
    try:
        registry = _links._registry()
        if registry is None:
            raise ValueError("no filesystem registry")
        project, rel = link["project"], link["path"]
        target = registry.resolve(project, rel)
        fh = open_regular(target)
        before = os.fstat(fh.fileno())
        if before.st_size > _config._max_linked_bytes():
            raise ValueError("linked file exceeds cap")
        if (before.st_size, before.st_mtime_ns) != (link["size"], link["mtime_ns"]):
            raise ValueError("linked file changed")
        digest = hashlib.file_digest(fh, "sha256").hexdigest()
        # Re-check containment and identity after opening/reading, including parent swaps.
        current = registry.resolve(project, rel).stat()
        if not os.path.samestat(before, current) or digest != link["sha256"]:
            raise ValueError("linked file changed")
        if (current.st_size, current.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            raise ValueError("linked file changed")
        fh.seek(0)
        identity = "link:" + hashlib.sha256(json.dumps(link, sort_keys=True).encode()).hexdigest()
        return fh, meta, identity
    except (OSError, ValueError, KeyError, TypeError):
        if fh is not None:
            fh.close()
        raise HTTPException(404, "linked file unavailable or changed") from None


def _open_blob(art_id: str, version: int):
    """Resolve a `file` artifact version to its blob or fenced reference and OPEN it: ``(file, meta, name)``.

    The blob is opened HERE, before any byte is sent, and the download streams from that handle
    (``_open_file_response``) — never re-opened by path at send time. The store is read without
    the lock, so another process's eviction can orphan and sweep this blob at any moment. Once
    it's open, that can't break the download: on POSIX the handle keeps reading the unlinked file;
    on Windows the sweep's delete is refused while the handle is open, and it retries on its next
    run (``_store._gc_blobs``). Serving by path instead — a check, then an open at send time —
    let a sweep in between fail the download after its headers had gone out.

    A blob swept between the store read and the open is not an error by itself: the store is
    read again, so "latest" follows a newer version that replaced it; a blob the store STILL
    names but that isn't on disk is a 404, as is a version that's gone."""
    from fastapi import HTTPException

    missing: set[str] = set()
    for _ in range(_BLOB_RESOLVE_TRIES):
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
        if art.get("linked") is True and isinstance(v.get("link"), dict):
            return _open_linked(v["link"], meta)
        if not blob_name:
            raise HTTPException(404, "not a file artifact / no stored blob")
        if blob_name in missing:  # the store still names it, and it isn't on disk
            break
        f = _store._blob_path(art_id, blob_name)
        try:
            return _store._retry_denied(lambda: open(f, "rb")), meta, f.name  # closed by the response
        except FileNotFoundError:
            missing.add(blob_name)
    raise HTTPException(404, "blob missing")


def _attachment(filename: str, disposition: str = "attachment") -> str:
    """A download's Content-Disposition, built the way Starlette's FileResponse builds it."""
    quoted = quote(filename)
    if quoted != filename:
        return f"{disposition}; filename*=utf-8''{quoted}"
    return f'{disposition}; filename="{filename}"'


@functools.cache
def _open_file_response():
    """The Response class that streams an ALREADY-OPEN file (built lazily, like the routers, so
    importing the plugin doesn't import Starlette)."""
    import anyio
    from starlette.responses import Response

    class OpenFileResponse(Response):
        """Stream ``fh`` from where it is, then close it — however the send ends (a completed
        download, a client that disconnected, an error), so a Windows handle never outlives
        the send and keeps blocking the blob sweep.

        Honours one HTTP ``Range`` (#4257): a ``bytes=`` range is a 206 with ``Content-Range``
        streaming just that slice (a <video> seeks without downloading the whole file), an
        unsatisfiable one a 416, and a multi-range or no header the whole body as a 200. Every
        response advertises ``Accept-Ranges: bytes``."""

        def __init__(
            self,
            fh,
            media_type: str,
            filename: str,
            range_header: str | None = None,
            disposition: str = "attachment",
            headers: dict | None = None,
        ) -> None:
            self.fh = fh
            size = os.fstat(fh.fileno()).st_size
            self.media_type = media_type
            self.background = None
            hdrs = {"accept-ranges": "bytes", "content-disposition": _attachment(filename, disposition)}
            hdrs.update({k.lower(): v for k, v in (headers or {}).items()})
            rng = _media.parse_range(range_header, size)
            self.start = 0
            if rng is False:
                self.status_code = 416
                self.size = 0
                hdrs["content-range"] = f"bytes */{size}"
            elif rng is None:
                self.status_code = 200
                self.size = size
            else:
                self.status_code = 206
                self.start, end = rng
                self.size = end - self.start + 1
                hdrs["content-range"] = f"bytes {self.start}-{end}/{size}"
            hdrs["content-length"] = str(self.size)
            self.init_headers(hdrs)

        async def __call__(self, scope, receive, send) -> None:
            try:
                await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
                if scope.get("method", "").upper() == "HEAD" or self.status_code == 416:
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
                    return
                if self.start:
                    await anyio.to_thread.run_sync(self.fh.seek, self.start)
                left = self.size
                while True:
                    chunk = await anyio.to_thread.run_sync(self.fh.read, min(_BLOB_CHUNK, left)) if left else b""
                    left -= len(chunk)
                    more = bool(chunk) and left > 0
                    await send({"type": "http.response.body", "body": chunk, "more_body": more})
                    if not more:
                        return
            finally:
                self.fh.close()

    return OpenFileResponse

_VENDOR_FILES = {
    # UMD (SRI-pinned in the shell's LIB map)
    "mermaid.min.js",
    "react.production.min.js",
    "react-dom.production.min.js",
    "babel.min.js",
    "pptx-renderer.min.js",  # @aiden0z/pptx-renderer as an IIFE (window.PptxRenderer) — slide previews
    "vega.min.js",  # vega / vega-lite / vega-embed UMD builds — `vega-lite` chart artifacts (ADR 0116)
    "vega-lite.min.js",
    "vega-embed.min.js",
    "jszip.min.js",  # JSZip + docx-preview UMD builds — .docx page previews
    "docx-preview.min.js",
    # ESM modules loaded by <script type="module"> (SRI-pinned too): pdf.js, legacy build —
    # PDF page previews. The worker module only sets globalThis.pdfjsWorker, so pdf.js runs on
    # the frame's main thread (the sandbox CSP forbids workers).
    "pdfjs.min.mjs",
    "pdfjs-worker.min.mjs",
    # ESM modules (the `react` import map): curated libs …
    "d3.mjs",
    "chartjs.mjs",
    "lucide.mjs",
    "marked.mjs",
    "three.module.min.js",  # three.js r170 self-contained ESM build — 3D `html`/`react` artifacts
    "anime.esm.min.js",  # anime.js 4.5.0 self-contained ESM build — animation for `html`/`react` artifacts
    # … React shims + authored design-system wrappers
    "react.shim.mjs",
    "react-dom-client.shim.mjs",
    "pl-ui.mjs",
    # The DS Vega chart theme (@protolabsai/design /vega — buildVegaConfig / vegaTooltipTheme),
    # loaded same-origin by the SHELL itself (not a frame), then injected into the chart frame as
    # source. Not SRI-pinned in the LIB map: the shell load is same-origin, not an opaque-sandbox
    # subresource. Verbatim-vendored; tests/test_artifact_vega.py guards it against DS drift.
    "pl-design-vega.mjs",
    # The DS embed theme-handoff module (@protolabsai/design /embed — themeTokenSnapshot /
    # snapshotToCss). Verbatim-vendored like pl-design-vega.mjs; same entry shape, same header.
    # embed.js is NOT self-contained — it does `import … from "./tokens.js"`, so tokens.js is
    # vendored beside it and MUST stay allowlisted too: the /vendor/{name} route is flat, and the
    # browser resolves that relative import against this module. tests/test_artifact_plugin.py
    # guards the closure and the DS drift.
    "pl-design-embed.mjs",
    "tokens.js",  # the embed module's relative dependency (./tokens.js) — must resolve same-origin
    # The DS element-default stylesheet (@protolabsai/ui-css /styles/embed.css). FRAME-ONLY
    # :where() zero-specificity defaults, embedded in the sandboxed frame beside the embed
    # module's token snapshot — NEVER imported under apps/web. Served as text/css (below), not
    # JavaScript. Verbatim-vendored; tests/test_artifact_plugin.py guards the DS drift.
    "pl-embed.css",
}


def _is_int(x) -> bool:
    """A JSON integer — not a bool (``True`` is an ``int`` in Python) and not a float."""
    return isinstance(x, int) and not isinstance(x, bool)


def _build_view_router():
    """The shell PAGE — served under the PUBLIC ``/plugins/artifact`` prefix
    (plugin-view rule 2): a browser iframe page-load can't carry an Authorization
    bearer, so a gated page 401-blanks under the token gate. The page is also where
    the slug-aware base is derived (``location.pathname.split("/plugins/")[0]``), so
    it MUST live under ``/plugins/`` — a ``/api/plugins/`` page poisons the base to
    ``/api`` and the kit's ``/_ds/`` assets 404 (the bug this split fixes). The page
    fetches its DATA from the gated data router with the handshake token."""
    from fastapi import APIRouter, Header, HTTPException
    from fastapi.responses import FileResponse, HTMLResponse, Response

    router = APIRouter()
    _media.install_log_redaction()  # the media ticket rides the query; keep it out of access logs

    @router.get("/view")
    async def _view():
        return HTMLResponse(_shell._SHELL_HTML)

    # Inline media (#4257): a file artifact's image/video bytes for the panel's <img>/<video>,
    # which can't carry the bearer. PUBLIC prefix, so it gates ITSELF on a ticket minted by the
    # gated ``POST /api/plugins/artifact/artifact/{id}/media-ticket`` and bound to this exact
    # version. Range-aware (seek without downloading the whole file); only allowlisted image/
    # video types, under nosniff + a sandboxing CSP (_media.SAFE_HEADERS). Unknown/expired
    # tickets and non-media files are the same 404 — the route reveals nothing without a ticket.
    @router.api_route("/media/{art_id}", methods=["GET", "HEAD"], include_in_schema=False)
    async def _media_route(art_id: str, version: int = 0, t: str = "", req_range: str | None = Header(None, alias="range")):
        granted = _media.check(t, art_id, version) if version >= 1 else None
        if granted is None:
            return Response(status_code=404)
        try:
            fh, meta, name = _open_blob(art_id, version)
        except HTTPException:
            return Response(status_code=404)
        mt = _media.media_type(meta)
        # The version at this index must still hold the MINTED blob/reference: a version-cap trim shifts
        # indices, and a ticket for v1 must never start serving what is now v1 (an older v2).
        if mt is None or name != granted:
            fh.close()
            return Response(status_code=404)
        return _open_file_response()(
            fh, mt, meta.get("filename") or name, range_header=req_range, disposition="inline", headers=_media.SAFE_HEADERS
        )

    # The shell's module script, same-origin beside the page (its relative ``src``
    # resolves against /plugins/artifact/view). NOT immutable-cached like vendor
    # files — it changes with the plugin, so a stale cache would desync it from
    # the page and the data plane.
    @router.get("/shell.js")
    async def _shell_js():
        return Response(
            content=_shell._SHELL_JS,
            media_type="text/javascript",
            headers={"Cache-Control": "no-cache"},
        )

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
        # Media type follows the extension: a .css default sheet (pl-embed.css) must be served
        # as text/css so the frame can apply it as a stylesheet; everything else is a JS/ESM lib.
        media_type = "text/css" if name.endswith(".css") else "application/javascript"
        return FileResponse(
            f,
            media_type=media_type,
            headers={
                "Cache-Control": "public, max-age=31536000, immutable",
                # The sandboxed artifact iframe is an opaque origin, so its load of
                # this lib is cross-origin → CORS + crossorigin="anonymous" are
                # needed for the SRI check to run.
                "Access-Control-Allow-Origin": "*",
            },
        )

    return router


# Max ids one /refs call resolves — a long transcript's chips each ask for their own id, so
# this only bounds a hand-crafted request.
_REFS_MAX = 50


def _build_data_router():
    """The DATA routes — mounted under ``/api/plugins/artifact`` so they inherit the
    operator bearer gate (plugin-view rule 2). The shell page reads them with the
    handshake token; DELETE is the panel's user-driven cleanup."""
    from fastapi import APIRouter, Body, Header, HTTPException, Response
    from fastapi.responses import JSONResponse

    router = APIRouter()

    def _busy_503(fn):
        """A store that stays locked past its bound (``_store.StoreLockTimeout``, raised before the
        route changed anything) is a 503 with Retry-After, not a 500 — a wedged holder elsewhere."""

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except _store.StoreLockTimeout as e:
                raise HTTPException(503, str(e), headers={"Retry-After": "5"}) from None

        return wrapper

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

    @router.get("/refs")
    async def _refs(ids: str = "") -> dict:
        """Metadata for the console's ``artifact-ref`` chips (#3617): for each requested id
        still in the store, its title, kind, lifetime ``version_count`` and the lifetime
        number of its OLDEST kept version (``oldest`` — anything below it was trimmed at the
        ``max_versions`` cap). An evicted/deleted id is simply absent. No code: a chip needs
        "does v2 still exist, and how many are there now", never the source.

        Deliberately NOT a renderer poll (no ``_note_poll``): a chip in the transcript is not a
        live panel, so it must not make create/edit wait on a render verdict nothing will send."""
        wanted = [i for i in dict.fromkeys(x.strip() for x in (ids or "").split(",")) if i][:_REFS_MAX]
        out: dict = {}
        if wanted:
            store = _store._read_store()
            for art_id in wanted:
                art = _store._find(store, art_id)
                if art is None:
                    continue
                total = int(_store._version_key(art)[0])
                out[art_id] = {
                    "title": art.get("title") or "",
                    "kind": art.get("kind") or "",
                    "version_count": total,
                    "oldest": total - len(art.get("versions") or []) + 1,
                }
        return {"artifacts": out}

    @router.post("/render-status")
    @_busy_503
    @_store.serialized
    def _render_status_route(body: dict = Body(...)) -> dict:
        # Named *_route: a bare `_render_status` here would shadow the module import
        # for every sibling closure in this builder (the #2817 split's one collision).
        """The sandbox's render verdict for a version, relayed by the shell (#1458): the
        nested artifact frame reports ``{ok}`` once it mounts or ``{ok:false, error}`` when
        it throws / never mounts. Stamped onto the version so check_artifact + the create/edit
        tools can surface render failures back to the agent. Best-effort: unknown id/version
        is a no-op (the panel may be a version behind), never an error.

        ``version`` is the position the panel rendered — and at the max_versions cap a commit
        between the render and this POST trims the front and shifts every version down a slot,
        so position N then holds the NEXT edit, which would be stamped with this verdict. So the
        shell also sends the rendered version's identity: ``n``, its lifetime number, and its
        ``ts`` — exactly the key ``_store._locate_version`` resolves, trim or no trim (``ts``
        alone can't: two commits in one millisecond share it). The verdict lands on that version,
        or is dropped if it's gone. Older cached shells degrade: ``ts`` without ``n`` matches by
        ts (a same-millisecond pair can still be confused there), and a bare position stamps by
        position as it always did."""
        art_id = str(body.get("id") or "")
        try:
            version = int(body.get("version") or 0)
        except (TypeError, ValueError):
            version = 0
        store = _store._read_store()
        art = _store._find(store, art_id)
        vers = (art or {}).get("versions") or []
        idx = version - 1 if 1 <= version <= len(vers) else None
        ts, n = body.get("ts"), body.get("n")
        if _is_int(ts) and _is_int(n):  # the current shell: resolve by identity, never by position
            pos = _store._locate_version(art, (n, ts)) if art else None
            idx = pos - 1 if pos else None
        elif _is_int(ts) and (idx is None or vers[idx].get("ts") != ts):  # an older shell: ts only
            hits = [i for i, v in enumerate(vers) if v.get("ts") == ts]
            idx = hits[0] if len(hits) == 1 else None
        if idx is None:
            return {"ok": True, "recorded": False}
        art["versions"][idx]["render"] = {
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
    @_busy_503
    @_store.serialized
    def _save_edit(art_id: str, body: dict = Body(...)) -> dict:
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
        # A panel edit is a small change to the same diagram: its code links carry over, like
        # update_artifact's (a key the edit removed simply stops matching anything).
        links = art["versions"][-1].get("links") or None
        v = _store._commit_version(store, art, code, by="user", extra={"links": dict(links)} if links else None)
        return {"ok": True, "id": art_id, "version": v}

    @router.get("/artifact/{art_id}/blob")
    async def _blob(art_id: str, version: int = 0, req_range: str | None = Header(None, alias="range")):
        """Serve a `file` artifact version's stored BYTES for download (ADR 0092 D2). The
        panel's Download button hits this with the operator bearer; ``version`` is 1-based
        (0/absent = latest). Returns the sidecar blob with its stored mime + an attachment
        filename. 404 if the artifact/version/blob is missing or isn't a file artifact.
        A ``Range`` header gets a 206 slice (``Accept-Ranges: bytes`` on every response).

        Streamed from a handle opened before the response starts (``_open_blob``), so an
        eviction in another process can't cut the download short once it's begun."""
        fh, meta, name = _open_blob(art_id, version)
        return _open_file_response()(
            fh, meta.get("mime") or "application/octet-stream", meta.get("filename") or name, range_header=req_range
        )

    @router.post("/artifact/{art_id}/media-ticket")
    async def _media_ticket(art_id: str, version: int = 0) -> dict:
        """Mint a short-lived ticket for the PUBLIC media route (#4257) — a <video>/<img> can't
        carry the bearer, so this gated call stands in for it. Bound to one explicit version
        (0/absent resolves to the latest NOW, so a later save doesn't swap what's playing).
        404 for an unknown artifact/version, 415 for a file that isn't inline image/video."""
        store = _store._read_store()
        art = _store._find(store, art_id)
        if art is None:
            raise HTTPException(404, f"unknown artifact {art_id}")
        vers = art.get("versions") or []
        if version and not (1 <= version <= len(vers)):
            raise HTTPException(404, f"no version {version} (have 1..{len(vers)})")
        n = version or len(vers)
        if not n or not (vers[n - 1].get("blob") or art.get("linked") is True):
            raise HTTPException(404, "not a file artifact / no stored blob")
        if _media.media_kind(vers[n - 1].get("file") or {}) is None:
            raise HTTPException(415, "not an inline image/video file")
        if art.get("linked") is True:
            fh, _, identity = _open_blob(art_id, n)
            fh.close()
        else:
            identity = _store._blob_path(art_id, vers[n - 1]["blob"]).name
        tok = _media.mint(art_id, n, identity)
        return {"ticket": tok, "version": n, "path": f"/plugins/artifact/media/{quote(art_id)}?version={n}&t={tok}"}

    @router.delete("/artifact/{art_id}")
    @_busy_503
    @_store.serialized
    def _delete(art_id: str) -> dict:
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
