"""Memory-injection record routes (ADR 0069 D6).

The read surface over ``observability/injection_log.py`` — one route the
operator console (memory inspector, D7) and ad-hoc forensics use to answer
"which memory entered which turn?". Registrar-style
(``register_injection_routes(app)``), matching ``register_telemetry_routes``.

Deliberately its OWN file: a sibling lane owns ``operator_api/memory_routes.py``
(session-summary CRUD), so the two memory surfaces land without touching each
other's files.
"""

from __future__ import annotations

import logging

log = logging.getLogger("protoagent.server")

# Snippet cap for a resolved chunk's body in the detail dialog — enough to
# recognize the item, short enough to keep the grouped lists scannable.
_SNIPPET_MAX = 200


def _clip(text: str, limit: int = _SNIPPET_MAX) -> str:
    """Collapse whitespace and clip to ``limit`` chars (ellipsis when cut)."""
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _resolve_chunk(store, chunk_id) -> dict | None:
    """One knowledge-store chunk row by id, best-effort. Returns None when the
    store is off, the chunk was pruned/deleted, or the backend errors — the
    caller renders that as an 'unavailable' item, never a 500."""
    if store is None:
        return None
    try:
        return store.get_chunk(int(chunk_id))
    except Exception:  # noqa: BLE001 — a broken/backend-less store must not 500 the dialog
        log.exception("[injection-detail] get_chunk(%s) failed", chunk_id)
        return None


def _memory_item(store, chunk_id) -> dict:
    """A hot-memory chunk → ``{id, heading, snippet, unavailable}``."""
    row = _resolve_chunk(store, chunk_id)
    if row is None:
        return {"id": chunk_id, "heading": None, "snippet": None, "unavailable": True}
    return {
        "id": chunk_id,
        "heading": row.get("heading") or None,
        "snippet": _clip(row.get("content") or ""),
        "unavailable": False,
    }


def _doc_item(store, chunk_id) -> dict:
    """A RAG (knowledge) chunk → ``{id, source, snippet, unavailable}``."""
    row = _resolve_chunk(store, chunk_id)
    if row is None:
        return {"id": chunk_id, "source": None, "snippet": None, "unavailable": True}
    return {
        "id": chunk_id,
        "source": row.get("source") or row.get("source_type") or None,
        "snippet": _clip(row.get("content") or ""),
        "unavailable": False,
    }


def _session_item(session_id: str) -> dict:
    """A digest session id → ``{id, title}`` where title is the session's topic
    (first user message, via the SAME ``digest_entry`` derivation the Sessions
    tab shows). Best-effort: an invalid/missing/corrupt summary falls back to a
    null title, and the console renders the id."""
    from graph.middleware.memory import digest_entry
    from operator_api.memory_routes import _read_summary, _session_paths

    try:
        paths = _session_paths(session_id)
        if paths:
            entry = digest_entry(_read_summary(paths))
            return {"id": session_id, "title": entry.get("topic") or None}
    except Exception:  # noqa: BLE001 — an unresolvable summary falls back to the id
        log.exception("[injection-detail] session summary %s unresolvable", session_id)
    return {"id": session_id, "title": None}


def _resolve_injection(row: dict) -> dict:
    """Turn one injection record's id arrays into the grouped, resolved content
    the detail dialog renders. Every group is best-effort — a missing knowledge
    store or a pruned chunk yields an 'unavailable' item, never an error."""
    from runtime.state import STATE

    store = STATE.knowledge_store
    return {
        "ts": row.get("ts"),
        "session_id": row.get("session_id") or "",
        "past_sessions": [_session_item(str(s)) for s in row.get("digest_session_ids") or []],
        "memories": [_memory_item(store, c) for c in row.get("hot_chunk_ids") or []],
        "docs": [_doc_item(store, c) for c in row.get("rag_chunk_ids") or []],
        "approx_tokens": int(row.get("approx_tokens") or 0),
    }


def register_injection_routes(app) -> None:
    """Register the ``/api/memory/injections`` read-only route on ``app``."""

    @app.get("/api/memory/injections")
    async def _api_memory_injections(session_id: str = "", limit: int = 50):
        """Per-model-call injection rows, newest first.

        ``session_id`` filters to one session; empty/omitted = all sessions.
        Each row: ``ts``, ``session_id``, ``digest_session_ids`` (prior-session
        digest entries injected), ``hot_chunk_ids`` / ``rag_chunk_ids``
        (knowledge-store chunk ids), ``approx_tokens``.
        """
        import asyncio

        from observability.injection_log import injection_log

        rows = await asyncio.to_thread(
            injection_log().recent,
            session_id=session_id.strip() or None,
            limit=min(max(1, limit), 500),
        )
        return {"injections": rows}

    @app.get("/api/memory/injections/{record_id}")
    async def _api_memory_injection_detail(record_id: int):
        """One injection record's ids resolved to their content, grouped for the
        console's detail dialog: ``past_sessions`` (digest-session titles),
        ``memories`` (hot chunks), ``docs`` (RAG chunks), plus ``ts`` /
        ``session_id`` / ``approx_tokens``. 404 when no record has that id;
        individual items that no longer resolve are marked ``unavailable`` rather
        than failing the request."""
        import asyncio

        from fastapi.responses import JSONResponse

        from observability.injection_log import injection_log

        row = await asyncio.to_thread(injection_log().get, record_id)
        if row is None:
            return JSONResponse({"detail": "no injection record with that id"}, status_code=404)
        return await asyncio.to_thread(_resolve_injection, row)
