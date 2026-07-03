"""Memory-inspector routes for the operator console (ADR 0069 D7).

The audit surface for the memory *delivery* layer: which session summaries
exist on disk (the ``memory_path()`` files — named via the shared
``session_filename`` mapper — behind the ``<prior_sessions>`` digest) and
which hot-memory chunks ride every turn — view/delete for summaries,
view/edit/delete for hot chunks. A security control first (SpAIware-class
memory poisoning gets *detected* here), UX second. List rows carry the
injection truth: ``in_digest`` (session is in the current digest window) and
``injecting`` (hot chunk is in the current per-turn window).

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


def _session_paths(session_id: str) -> list[str] | None:
    """Candidate summary files for *session_id* — the '%3A'-encoded name first,
    then the legacy raw-':' name (``session_file_candidates``, the shared
    mapper) — or None when the id fails the ``[A-Za-z0-9._:-]`` guard
    (path-traversal safe: no separators survive)."""
    from graph.middleware.memory import is_safe_session_id, session_file_candidates

    if not is_safe_session_id(session_id):
        return None
    return session_file_candidates(session_id)


def _read_summary(paths: list[str]) -> dict:
    """Load the first candidate file that exists (encoded name, then legacy).
    Raises FileNotFoundError only when NO candidate exists; a corrupt encoded
    file propagates its decode error (the caller 422s — it must not fall
    through to a stale legacy copy)."""
    for fpath in paths[:-1]:
        try:
            with open(fpath, encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            continue
    with open(paths[-1], encoding="utf-8") as fh:
        return json.load(fh)


def _list_sessions() -> list[dict]:
    """The sessions listing, newest first — sync (dir walk + a ``json.load``
    per summary + the digest re-derivation); callers run it off the loop."""
    from graph.middleware.memory import digest_entry, load_prior_sessions_digest, memory_path

    base = memory_path()
    # The ids the CURRENT digest injection carries — default args match the
    # middleware's (10 newest, 2000-token cap, background:* excluded), so the
    # console's "in digest" badge reflects what the model actually sees.
    digest_ids = set(load_prior_sessions_digest()[1])
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
            entry["in_digest"] = entry["session_id"] in digest_ids
            sessions.append((mtime, entry))
    sessions.sort(key=lambda t: t[0], reverse=True)  # newest first, like the digest
    return [e for _, e in sessions]


def _hot_chunks(store) -> list[dict]:
    """Hot-memory rows in the console's chunk shape. list_chunks yields Chunk
    objects (plain store) or tier-tagged dicts (LayeredKnowledgeStore).

    Commons-tier rows are EXCLUDED: on a layered store ``get_hot_memory`` (the
    actual per-turn injection) and ``add_chunk``/``delete_by_id`` all delegate
    to the PRIVATE store, and ids are per-backend — a commons hot chunk never
    injects, and letting its id pass the mutation gates would make
    ``delete_by_id`` hit whatever PRIVATE row shares that numeric id (an
    arbitrary KB chunk). Commons curation stays with promote/forget on the
    knowledge surface."""
    rows = store.list_chunks(domain="hot", limit=_HOT_LIST_LIMIT)
    return [
        _knowledge_row(c)
        for c in (c if isinstance(c, dict) else c.as_dict() for c in rows)
        if c.get("tier") != "commons"
    ]


def register_memory_routes(app) -> None:
    """Register the ``/api/memory/*`` memory-inspector routes."""

    # --- Session summaries ---------------------------------------------------
    # The files SessionSummaryMiddleware persists — the source the digest is
    # built from. List rows reuse the digest derivation (graph.middleware.memory
    # digest_entry) so the inspector shows exactly what the agent is told.

    @app.get("/api/memory/sessions")
    async def _api_memory_sessions():
        return {"sessions": await asyncio.to_thread(_list_sessions)}

    @app.get("/api/memory/sessions/{session_id}")
    async def _api_memory_session_get(session_id: str):
        from graph.middleware.memory import digest_entry, format_session_summary

        paths = _session_paths(session_id)
        if paths is None:
            return JSONResponse({"detail": "invalid session_id"}, status_code=400)
        try:
            summary = await asyncio.to_thread(_read_summary, paths)
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
        paths = _session_paths(session_id)
        if paths is None:
            return JSONResponse({"detail": "invalid session_id"}, status_code=400)

        def _remove() -> bool:
            removed = False
            # Remove EVERY name the summary may live under (encoded + legacy) —
            # a delete must not leave a legacy copy resurfacing in the digest.
            for fpath in paths:
                try:
                    os.remove(fpath)
                    removed = True
                except FileNotFoundError:
                    continue
            return removed

        try:
            removed = await asyncio.to_thread(_remove)
        except OSError as exc:
            log.warning("[memory] delete of summary %s failed: %s", session_id, exc)
            return JSONResponse({"detail": f"delete failed: {exc}"}, status_code=500)
        if not removed:
            return JSONResponse({"detail": "no session summary with that id"}, status_code=404)
        return {"deleted": True, "session_id": session_id}

    # --- Hot memory ----------------------------------------------------------
    # The domain="hot" chunks get_hot_memory injects every turn.

    @app.get("/api/memory/hot")
    async def _api_memory_hot():
        store = STATE.knowledge_store
        if store is None:
            return {"enabled": False, "chunks": []}
        try:
            chunks = await asyncio.to_thread(_hot_chunks, store)
        except Exception:  # noqa: BLE001 — never 500 the console
            log.exception("[memory] hot-memory list failed")
            return {"enabled": True, "chunks": []}
        # "injecting" = the chunk is in the CURRENT per-turn window: the ids
        # get_hot_memory_entries returns (newest 100 domain="hot" chunks under
        # the 6000-char budget) — the same reader the middleware injects from.
        # On a LayeredKnowledgeStore, __getattr__ delegates it to the PRIVATE
        # store, whose ids are consistent with the listed rows (commons rows
        # are already excluded by _hot_chunks). A custom backend without the
        # reader gets NO field — the console renders a missing flag as unknown.
        if hasattr(store, "get_hot_memory_entries"):
            try:
                window = await asyncio.to_thread(store.get_hot_memory_entries)
                window_ids = {cid for cid, _ in window}
            except Exception:  # noqa: BLE001 — the flag is best-effort; the list still serves
                log.exception("[memory] hot-memory injection window failed")
            else:
                for row in chunks:
                    row["injecting"] = row.get("id") in window_ids
        return {"enabled": True, "chunks": chunks}

    @app.put("/api/memory/hot/{chunk_id}")
    async def _api_memory_hot_update(chunk_id: int, body: dict | None = None):
        store = STATE.knowledge_store
        if store is None:
            return {"enabled": False, "id": None, "replaced": False}
        body = body or {}
        content = str(body.get("content", "")).strip()
        if not content:
            return JSONResponse({"detail": "content is required"}, status_code=400)
        rows = await asyncio.to_thread(_hot_chunks, store)
        cur = next((c for c in rows if c.get("id") == chunk_id), None)
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
        # Deliberately a HARD delete (ADR 0069 D9): automatic supersession keeps
        # invalidated rows for audit, but an operator delete is explicit intent
        # and removes the row outright.
        store = STATE.knowledge_store
        if store is None:
            return {"enabled": False, "deleted": False}
        rows = await asyncio.to_thread(_hot_chunks, store)
        if not any(c.get("id") == chunk_id for c in rows):
            return JSONResponse({"detail": "no hot-memory chunk with that id"}, status_code=404)
        deleted = await asyncio.to_thread(store.delete_by_id, chunk_id)
        return {"enabled": True, "deleted": bool(deleted)}
