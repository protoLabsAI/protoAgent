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
