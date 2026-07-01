"""Memory-inspector routes for the operator console (ADR 0069 D7).

The audit surface for the memory *delivery* layer: which session summaries
exist on disk (the ``{memory_path()}/{session_id}.json`` files behind the
``<prior_sessions>`` digest) and which hot-memory chunks ride every turn —
view/delete for summaries, view/edit/delete for hot chunks. A security control
first (SpAIware-class memory poisoning gets *detected* here), UX second.

Generic chunk CRUD already lives in ``knowledge_routes``; these routes add the
domain-scoped view the inspector needs: a hot edit is pinned to
``domain="hot"`` (can't silently demote the chunk out of always-on injection)
and a hot delete only resolves ids that ARE hot chunks (can't reach arbitrary
KB rows). Session-summary ids share the ``recall_session`` filename guard, so
a crafted id can't path-traverse out of the memory dir.

Auth: gated by the server-level ``/api/*`` bearer middleware
(``a2a_impl.auth``) like every other operator route — nothing here is public.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from fastapi.responses import JSONResponse

from operator_api.knowledge_routes import _knowledge_row
from runtime.state import STATE

log = logging.getLogger("protoagent.server")

# get_hot_memory injects the 100 newest hot chunks; the inspector lists a wider
# window so the operator can also curate the backlog that no longer injects.
_HOT_LIST_LIMIT = 500


def _session_path(session_id: str) -> str | None:
    """Map *session_id* to its summary file, or None when the id fails the
    ``[A-Za-z0-9._:-]`` guard (path-traversal safe: no separators survive)."""
    from graph.middleware.memory import is_safe_session_id, memory_path

    if not is_safe_session_id(session_id):
        return None
    return os.path.join(memory_path(), f"{session_id}.json")


def _hot_chunks(store) -> list[dict]:
    """Hot-memory rows in the console's chunk shape. list_chunks yields Chunk
    objects (plain store) or tier-tagged dicts (LayeredKnowledgeStore)."""
    rows = store.list_chunks(domain="hot", limit=_HOT_LIST_LIMIT)
    return [_knowledge_row(c if isinstance(c, dict) else c.as_dict()) for c in rows]


def register_memory_routes(app) -> None:
    """Register the ``/api/memory/*`` memory-inspector routes."""

    # --- Session summaries ---------------------------------------------------
    # The files SessionSummaryMiddleware persists — the source the digest is
    # built from. List rows reuse the digest derivation (graph.middleware.memory
    # digest_entry) so the inspector shows exactly what the agent is told.

    @app.get("/api/memory/sessions")
    async def _api_memory_sessions():
        from graph.middleware.memory import digest_entry, memory_path

        base = memory_path()
        sessions: list[tuple[float, dict]] = []
        if os.path.isdir(base):
            for fname in os.listdir(base):
                if not fname.endswith(".json"):
                    continue
                fpath = os.path.join(base, fname)
                try:
                    size = os.path.getsize(fpath)
                    mtime = os.path.getmtime(fpath)
                    with open(fpath, encoding="utf-8") as fh:
                        summary = json.load(fh)
                except (OSError, json.JSONDecodeError, ValueError) as exc:
                    log.warning("[memory] skipping unreadable summary %s: %s", fpath, exc)
                    continue
                entry = digest_entry(summary)
                entry["size_bytes"] = size
                sessions.append((mtime, entry))
        sessions.sort(key=lambda t: t[0], reverse=True)  # newest first, like the digest
        return {"sessions": [e for _, e in sessions]}

    @app.get("/api/memory/sessions/{session_id}")
    async def _api_memory_session_get(session_id: str):
        from graph.middleware.memory import digest_entry, format_session_summary

        fpath = _session_path(session_id)
        if fpath is None:
            return JSONResponse({"detail": "invalid session_id"}, status_code=400)
        try:
            with open(fpath, encoding="utf-8") as fh:
                summary = json.load(fh)
        except FileNotFoundError:
            return JSONResponse({"detail": "no session summary with that id"}, status_code=404)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            return JSONResponse({"detail": f"session summary unreadable: {exc}"}, status_code=422)
        entry = digest_entry(summary)
        entry["trace_id"] = summary.get("trace_id")  # links the summary to its trace
        entry["rendered"] = format_session_summary(summary)  # same render recall_session returns
        return {"session": entry}

    @app.delete("/api/memory/sessions/{session_id}")
    async def _api_memory_session_delete(session_id: str):
        fpath = _session_path(session_id)
        if fpath is None:
            return JSONResponse({"detail": "invalid session_id"}, status_code=400)
        try:
            os.remove(fpath)
        except FileNotFoundError:
            return JSONResponse({"detail": "no session summary with that id"}, status_code=404)
        except OSError as exc:
            log.warning("[memory] delete of summary %s failed: %s", fpath, exc)
            return JSONResponse({"detail": f"delete failed: {exc}"}, status_code=500)
        return {"deleted": True, "session_id": session_id}

    # --- Hot memory ----------------------------------------------------------
    # The domain="hot" chunks get_hot_memory injects every turn.

    @app.get("/api/memory/hot")
    async def _api_memory_hot():
        if STATE.knowledge_store is None:
            return {"enabled": False, "chunks": []}
        try:
            chunks = _hot_chunks(STATE.knowledge_store)
        except Exception:  # noqa: BLE001 — never 500 the console
            log.exception("[memory] hot-memory list failed")
            chunks = []
        return {"enabled": True, "chunks": chunks}

    @app.put("/api/memory/hot/{chunk_id}")
    async def _api_memory_hot_update(chunk_id: int, body: dict | None = None):
        store = STATE.knowledge_store
        if store is None:
            return {"enabled": False, "id": None}
        body = body or {}
        content = str(body.get("content", "")).strip()
        if not content:
            return JSONResponse({"detail": "content is required"}, status_code=400)
        cur = next((c for c in _hot_chunks(store) if c.get("id") == chunk_id), None)
        if cur is None:
            return JSONResponse({"detail": "no hot-memory chunk with that id"}, status_code=404)
        # Same composition as the generic chunk edit (knowledge_routes): add the
        # new revision FIRST, then delete the old — a failed add must never lose
        # the original. domain is pinned to "hot" so an inspector edit can't
        # move the chunk out of always-on injection.
        new_id = await asyncio.to_thread(
            lambda: store.add_chunk(
                content,
                "hot",
                heading=(str(body.get("heading", "")).strip() or cur.get("heading") or None),
                source=cur.get("source") or "console",
                source_type="operator",
            )
        )
        if new_id is None:
            return JSONResponse({"detail": "the store rejected the new revision"}, status_code=400)
        deleted = await asyncio.to_thread(store.delete_by_id, chunk_id)
        if not deleted:
            log.warning("[memory] edit of hot chunk %s left the old row (delete failed)", chunk_id)
        return {"enabled": True, "id": new_id, "replaced": bool(deleted)}

    @app.delete("/api/memory/hot/{chunk_id}")
    async def _api_memory_hot_delete(chunk_id: int):
        store = STATE.knowledge_store
        if store is None:
            return {"enabled": False, "deleted": False}
        if not any(c.get("id") == chunk_id for c in _hot_chunks(store)):
            return JSONResponse({"detail": "no hot-memory chunk with that id"}, status_code=404)
        deleted = await asyncio.to_thread(store.delete_by_id, chunk_id)
        return {"enabled": True, "deleted": bool(deleted)}
