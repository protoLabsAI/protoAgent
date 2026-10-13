"""Console ↔ editor chat hand-off routes — ``/api/editor/handoff`` (+ ``/claim``).

The console's "Continue in Zed" action (and ``open_in_editor``, from inside a turn)
OFFERS a chat session for the next editor-side thread started under a project root;
the Zed ACP shim CLAIMS it on ``session/new(cwd)`` and continues that A2A context
instead of minting a new one. The store and its match rules live in
``runtime/editor_handoff.py``; these are the thin HTTP half.

The project is resolved through the fs fence (``tools.fs_tools.live_project_registry``,
the ``read_file`` chokepoint) — an offer can only name a registered project's root, so a
hand-off can never point the editor side at a directory the agent itself can't reach.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("protoagent.server")


def register_editor_routes(app) -> None:
    from fastapi import HTTPException
    from fastapi.responses import Response

    from runtime import editor_handoff

    def _err(status: int, code: str, reason: str):
        return HTTPException(status_code=status, detail={"code": code, "reason": reason})

    def _resolve(project: str, path: str | None) -> tuple[str, str | None]:
        """(project root, canonical relative path) through the live fs fence, or raise."""
        from runtime.state import STATE
        from tools.fs_tools import live_project_registry

        registry = live_project_registry(getattr(STATE, "graph_config", None))
        proj = registry.get(project)
        if proj is None:
            raise _err(400, "unknown_project", f"not a registered project: {project!r}")
        rel = None
        if path:
            try:
                target = registry.resolve(project, path)
            except ValueError as exc:
                raise _err(400, "bad_path", str(exc)) from exc
            rel = target.relative_to(proj.root).as_posix()
        return str(proj.root), rel

    @app.post("/api/editor/handoff")
    async def _api_editor_handoff(body: dict | None = None):
        """Offer ``session_id`` to the next editor thread started under ``project``'s root.

        Body ``{session_id, project?, path?, line?, title?}`` → ``{id, expires_at, root}``.
        No ``project`` → ``root: null``, which matches ANY cwd. Latest offer per root wins;
        it expires after 120 s and is one-shot. Unknown session → 404 ``not_found``; a
        project outside the fs fence → 400 ``unknown_project``; a ``path`` that escapes it →
        400 ``bad_path``."""
        from operator_api.chat_routes import session_summary

        b = body or {}
        session_id = str(b.get("session_id") or "").strip()
        if not session_id:
            raise _err(400, "bad_request", "session_id is required")
        project = str(b.get("project") or "").strip() or None
        path = str(b.get("path") or "").strip() or None
        if path and not project:
            raise _err(400, "bad_request", "path needs a project")
        line = b.get("line")
        if line is not None and (isinstance(line, bool) or not isinstance(line, int) or line < 1):
            raise _err(400, "bad_request", "line must be a positive integer")
        title = b.get("title")
        title = str(title).strip()[:200] if isinstance(title, str) else None

        try:
            known = await session_summary(session_id)
        except Exception as exc:  # noqa: BLE001 — a store hiccup must not block the hand-off
            log.warning("[editor] hand-off session check failed for %s: %s", session_id, exc)
            known = {"session_id": session_id}
        if known is None:
            raise _err(404, "not_found", f"unknown session: {session_id}")

        root = None
        if project:
            # is_dir()/resolve per project root — off the event loop like every fs route.
            root, path = await asyncio.to_thread(_resolve, project, path)
        h = editor_handoff.offer(session_id, root=root, project=project, path=path, line=line, title=title)
        log.info("[editor] hand-off %s offered: session %s → %s", h.id, session_id, h.root or "any cwd")
        return h.to_offer()

    @app.post("/api/editor/handoff/claim")
    async def _api_editor_handoff_claim(body: dict | None = None):
        """Claim the newest unexpired hand-off matching ``cwd`` (the cwd is the root, inside
        it, or a parent of it). Body ``{cwd}`` → 200 ``{session_id, project, path, line,
        title}`` and the offer is removed, or 204 when nothing matches."""
        cwd = str((body or {}).get("cwd") or "").strip()
        h = await asyncio.to_thread(editor_handoff.claim, cwd)
        if h is None:
            return Response(status_code=204)
        log.info("[editor] hand-off %s claimed from %s: session %s", h.id, cwd or "(no cwd)", h.session_id)
        return h.to_claim()
