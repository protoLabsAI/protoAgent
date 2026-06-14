"""Knowledge-store + playbooks routes for the operator console.

The console's "Knowledge" surface is a searchable Store + Playbooks (ADR 0020):
the FTS5 knowledge base (findings, daily-log, harvested sessions, operator notes)
and the procedural-memory skill index. Extracted from ``server._main`` (ADR 0023
phase 3) into a ``register_knowledge_routes(app)`` registrar. Browse/search is
best-effort and degrades to ``{"enabled": False}`` when its store is off; none of
the routes ever 500s the console. The chunk CRUD routes let the operator curate
the store directly (add a fact, fix a stale one, drop a wrong one) — the same
``KnowledgeBackend`` protocol surface every backend implements (ADR 0031).
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import File, Form, UploadFile
from fastapi.responses import JSONResponse

from runtime.state import STATE

log = logging.getLogger("protoagent.server")


def _knowledge_row(d: dict) -> dict:
    """Normalize a search()/list_chunks() row to the console's shape."""
    heading = d.get("heading") or ""
    content = d.get("content") or ""
    preview = d.get("preview") or ((heading + ": " if heading else "") + content)[:240]
    return {
        "id": d.get("id"),
        "heading": heading,
        "content": content,
        "preview": preview,
        "domain": d.get("domain") or "general",
        "source": d.get("source"),
        "source_type": d.get("source_type"),
        "finding_type": d.get("finding_type"),
        "created_at": d.get("created_at"),
    }


def register_knowledge_routes(app) -> None:
    """Register the ``/api/playbooks*`` + ``/api/knowledge/search`` routes."""

    # --- Playbooks (skills surface, ADR 0009) ------------------------------
    # Browse + manage the procedural-memory skill index (skills.db) the operator
    # was otherwise blind to. "Playbooks" is the operator-facing name for the
    # skill-v1 artifacts (disk = pinned SKILL.md, emitted = agent-learned).
    @app.get("/api/playbooks")
    async def _api_playbooks():
        if STATE.skills_index is None:
            return {"enabled": False, "playbooks": []}
        try:
            skills = STATE.skills_index.all_skills()
        except Exception:  # noqa: BLE001 — never 500 the console
            log.exception("[playbooks] all_skills failed")
            return {"enabled": True, "playbooks": []}
        # Drop the (potentially large) prompt_template from the list payload;
        # the table only needs metadata. Sort pinned-first, then by confidence.
        out = [
            {k: v for k, v in s.items() if k != "prompt_template"}
            for s in skills
        ]
        out.sort(key=lambda s: (s.get("source") != "disk", -(s.get("confidence") or 0)))
        return {"enabled": True, "playbooks": out}

    @app.delete("/api/playbooks/{skill_id}")
    async def _api_playbook_delete(skill_id: int):
        if STATE.skills_index is None:
            return {"enabled": False, "deleted": False}
        try:
            STATE.skills_index.delete_skill(skill_id)
            return {"enabled": True, "deleted": True}
        except Exception as exc:  # noqa: BLE001
            log.exception("[playbooks] delete failed")
            return {"enabled": True, "deleted": False, "error": str(exc)}

    # Promote a private skill into the shared commons (ADR 0041 "shared brain,
    # private hands") — the one curated lift that makes the layered tier useful.
    # Only available when the index is layered (it has a ``promote`` method); in
    # scoped/shared mode there's a single library and nothing to promote into.
    # The id is the private-DB rowid, so resolve the name within the ``private``
    # tier (commons rows carry their own rowids — never promote those).
    @app.post("/api/playbooks/{skill_id}/promote")
    async def _api_playbook_promote(skill_id: int):
        idx = STATE.skills_index
        if idx is None:
            return {"enabled": False, "promoted": False}
        promote = getattr(idx, "promote", None)
        if promote is None:
            return {
                "enabled": True,
                "promoted": False,
                "error": "skills aren't in layered mode — set skills.scope: layered to share a commons.",
            }
        try:
            match = next(
                (
                    s
                    for s in idx.all_skills()
                    if s.get("id") == skill_id and s.get("tier", "private") == "private"
                ),
                None,
            )
            if match is None:
                return {"enabled": True, "promoted": False, "error": "no private skill with that id"}
            name = match.get("name", "")
            ok = bool(promote(name))
            return {"enabled": True, "promoted": ok, "name": name}
        except Exception as exc:  # noqa: BLE001
            log.exception("[playbooks] promote failed")
            return {"enabled": True, "promoted": False, "error": str(exc)}

    # --- Knowledge store (ADR 0020) ----------------------------------------
    # Searchable view of the agent's knowledge base (knowledge/store.py, FTS5):
    # findings, daily-log entries, harvested sessions, operator notes — the same
    # store KnowledgeMiddleware queries before each turn. An empty ``q`` returns
    # the most-recent chunks (a browsable default); a non-empty ``q`` runs FTS5
    # search. Read-only; never 500s the console.
    @app.get("/api/knowledge/search")
    async def _api_knowledge_search(q: str = "", k: int = 30, domain: str | None = None):
        if STATE.knowledge_store is None:
            return {"enabled": False, "query": q, "results": [], "stats": {}}
        results: list[dict] = []
        try:
            if q and q.strip():
                # search() embeds the query over HTTP on hybrid stores — run it
                # off the event loop (same pattern as graph/checkpointer.py).
                rows = await asyncio.to_thread(
                    STATE.knowledge_store.search, q, k=k, domain=domain or None
                )
                results = [_knowledge_row(r) for r in rows]
            else:
                results = [_knowledge_row(c.as_dict()) for c in STATE.knowledge_store.list_chunks(domain=domain or None, limit=k)]
        except Exception:  # noqa: BLE001 — never 500 the console
            log.exception("[knowledge] search failed")
        try:
            stats = STATE.knowledge_store.stats()
        except Exception:  # noqa: BLE001
            stats = {}
        return {"enabled": True, "query": q, "results": results, "stats": stats}

    # --- Knowledge chunk CRUD (operator curation) ---------------------------
    # The store fills up with harvested sessions / findings the operator could
    # only read; these let them curate it: add a fact, fix a stale one, drop a
    # wrong one. add/delete are the KnowledgeBackend protocol (ADR 0031); edit
    # composes them (add the new revision FIRST, then delete the old — a failed
    # add must never lose the original) so it works on every backend, and a
    # hybrid store re-embeds the new content on the way in.

    @app.post("/api/knowledge/chunks")
    async def _api_knowledge_add(body: dict | None = None):
        if STATE.knowledge_store is None:
            return {"enabled": False, "id": None}
        body = body or {}
        content = str(body.get("content", "")).strip()
        if not content:
            return JSONResponse({"detail": "content is required"}, status_code=400)
        # add_document chunks a large paste into per-passage embeddings (ADR 0021)
        # and is a no-op split for a short fact; degrades to one add_chunk on a
        # plugin backend that only implements the ADR 0031 surface.
        from knowledge import add_document

        ids = await asyncio.to_thread(
            lambda: add_document(
                STATE.knowledge_store,
                content,
                domain=str(body.get("domain", "") or "general"),
                heading=(str(body.get("heading", "")).strip() or None),
                source="console",
                source_type="operator",
            )
        )
        if not ids:
            return JSONResponse({"detail": "the store rejected the chunk"}, status_code=400)
        return {"enabled": True, "id": ids[0], "ids": ids}

    @app.post("/api/knowledge/ingest")
    async def _api_knowledge_ingest(
        file: UploadFile | None = File(default=None),
        url: str = Form(default=""),
        text: str = Form(default=""),
        title: str = Form(default=""),
        domain: str = Form(default="general"),
    ):
        """Ingest a document (file / URL / pasted text) into the knowledge base.

        The ingestion engine turns the source into text (txt/md/html/pdf, audio +
        video via gateway STT, web + YouTube URLs), then ``add_document`` chunks +
        contextually enriches + embeds it (ADR 0021) — so a whole PDF, article, or
        recording becomes per-passage recall, not one diluted chunk. Multipart so
        a file upload and the URL/text fields share one endpoint. Extraction +
        embedding run off the event loop. Returns the created chunk ids."""
        if STATE.knowledge_store is None:
            return {"enabled": False, "ids": []}
        from ingestion import (
            ExtractResult,
            MissingDependency,
            UnsupportedSource,
            extract_bytes,
            extract_url,
        )
        from knowledge import add_document

        # Gateway STT for audio/video (None if no transcribe_model configured →
        # audio/video raise a clean "not configured" error, text/pdf unaffected).
        transcribe = None
        try:
            from graph.llm import create_transcribe_fn

            transcribe = create_transcribe_fn(STATE.graph_config) if STATE.graph_config else None
        except Exception as exc:  # noqa: BLE001 — transcription stays optional
            log.warning("[knowledge] transcribe fn unavailable: %s", exc)

        url, text, title = url.strip(), text.strip(), title.strip()
        source = "console"
        try:
            if file is not None:
                data = await file.read()
                result = await asyncio.to_thread(
                    extract_bytes, file.filename or "upload", data, file.content_type,
                    transcribe=transcribe)
                source = file.filename or "upload"
            elif url:
                result = await asyncio.to_thread(extract_url, url, transcribe=transcribe)
                source = url
            elif text:
                result = ExtractResult(text=text, title=title or None, source_type="text")
            else:
                return JSONResponse(
                    {"detail": "provide a file, url, or text"}, status_code=400)
        except MissingDependency as exc:
            return JSONResponse({"detail": str(exc)}, status_code=501)
        except UnsupportedSource as exc:
            return JSONResponse({"detail": str(exc)}, status_code=415)
        except Exception as exc:  # noqa: BLE001 — surface extraction failure, never 500
            log.warning("[knowledge] ingest extraction failed: %s", exc)
            return JSONResponse({"detail": f"extraction failed: {exc}"}, status_code=400)

        heading = title or result.title or None
        ids = await asyncio.to_thread(
            lambda: add_document(
                STATE.knowledge_store,
                result.text,
                domain=(domain.strip() or "general"),
                heading=heading,
                source=source,
                source_type=result.source_type,
            )
        )
        if not ids:
            return JSONResponse(
                {"detail": "nothing ingested (no text after extraction)"}, status_code=400)
        return {
            "enabled": True,
            "ids": ids,
            "chunks": len(ids),
            "title": heading,
            "source_type": result.source_type,
            "chars": len(result.text),
        }

    @app.put("/api/knowledge/chunks/{chunk_id}")
    async def _api_knowledge_update(chunk_id: int, body: dict | None = None):
        if STATE.knowledge_store is None:
            return {"enabled": False, "id": None}
        body = body or {}
        content = str(body.get("content", "")).strip()
        if not content:
            return JSONResponse({"detail": "content is required"}, status_code=400)
        new_id = await asyncio.to_thread(
            lambda: STATE.knowledge_store.add_chunk(
                content,
                str(body.get("domain", "") or "general"),
                heading=(str(body.get("heading", "")).strip() or None),
                source=(body.get("source") or "console"),
                source_type="operator",
            )
        )
        if new_id is None:
            return JSONResponse({"detail": "the store rejected the new revision"}, status_code=400)
        deleted = await asyncio.to_thread(STATE.knowledge_store.delete_by_id, chunk_id)
        if not deleted:
            log.warning("[knowledge] edit of chunk %s left the old row (delete failed)", chunk_id)
        return {"enabled": True, "id": new_id, "replaced": deleted}

    @app.delete("/api/knowledge/chunks/{chunk_id}")
    async def _api_knowledge_delete(chunk_id: int):
        if STATE.knowledge_store is None:
            return {"enabled": False, "deleted": False}
        deleted = await asyncio.to_thread(STATE.knowledge_store.delete_by_id, chunk_id)
        return {"enabled": True, "deleted": bool(deleted)}
