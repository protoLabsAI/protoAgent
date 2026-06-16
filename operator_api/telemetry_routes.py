"""Telemetry read-only routes for the operator console (ADR 0006).

Per-turn cost/latency rollups + the advise-only flywheel insight signal, read
from the local telemetry store. Extracted from ``server._main`` (ADR 0023 phase
3) into a ``register_telemetry_routes(app)`` registrar matching
``register_operator_routes``. Every route degrades to ``{"enabled": False}`` when
the store is off, so the surface is always safe to call.
"""

from __future__ import annotations

from runtime.state import STATE


def register_telemetry_routes(app) -> None:
    """Register the ``/api/telemetry/*`` read-only routes on ``app``."""

    # Per-turn cost/latency rollups from the local store. Powers the operator
    # console's cost/latency surface (Slice 3) and ad-hoc "what's expensive"
    # queries. Read-only; returns {enabled:false} when the store is off.
    @app.get("/api/telemetry/summary")
    async def _api_telemetry_summary(since: str | None = None):
        if STATE.telemetry_store is None:
            return {"enabled": False, "summary": None}
        return {"enabled": True, "summary": STATE.telemetry_store.summary(since_iso=since)}

    @app.get("/api/telemetry/recent")
    async def _api_telemetry_recent(limit: int = 50):
        if STATE.telemetry_store is None:
            return {"enabled": False, "turns": []}
        return {"enabled": True, "turns": STATE.telemetry_store.recent(limit=min(max(1, limit), 500))}

    @app.get("/api/telemetry/export")
    async def _api_telemetry_export(since: str | None = None):
        """Download every recorded turn as CSV, streamed in chunks from a
        background thread (off the event loop).  Read-only; empty CSV (header
        only) when the store is off.

        Starlette wraps sync generators with ``iterate_in_threadpool``, so the
        DB cursor iteration + CSV serialization run in a thread automatically —
        no ``run_in_executor`` boilerplate needed, and backpressure is natural
        (the thread blocks until the next chunk is consumed by the client).
        """
        import csv
        import io
        import re

        from fastapi.responses import StreamingResponse

        from observability.telemetry_store import _COLUMNS

        store = STATE.telemetry_store
        _header_line = ",".join(_COLUMNS) + "\n"

        # Empty-CSV fast path when the store is off.
        if store is None:
            return StreamingResponse(
                iter([_header_line]),
                media_type="text/csv",
                headers={"Content-Disposition": 'attachment; filename="telemetry.csv"'},
            )

        # Normalise ``since``: URL decoders turn ``+`` in ``+00:00`` into a
        # space.  Restore before passing to SQL.
        since_iso = since
        if since_iso:
            since_iso = re.sub(r" (\d{2}:\d{2})$", r"+\1", since_iso)

        CHUNK_ROWS = 500

        def _csv_chunks():
            """Sync generator — runs in a thread via Starlette's threadpool."""
            yield _header_line
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=list(_COLUMNS), extrasaction="ignore")
            count = 0
            for row in store.stream_rows(since_iso=since_iso):
                writer.writerow(row)
                count += 1
                if count >= CHUNK_ROWS:
                    yield buf.getvalue()
                    buf = io.StringIO()
                    writer = csv.DictWriter(buf, fieldnames=list(_COLUMNS), extrasaction="ignore")
                    count = 0
            if count > 0:
                yield buf.getvalue()

        return StreamingResponse(
            _csv_chunks(),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="telemetry.csv"'},
        )

    @app.get("/api/telemetry/insights")
    async def _api_telemetry_insights():
        # Advise-only flywheel signal (ADR 0006 Slice 4): flag outlier turns +
        # prove the levers we can measure from the per-turn store. Read-only.
        if STATE.telemetry_store is None:
            return {"enabled": False, "insights": None}
        from observability import pricing

        s = STATE.telemetry_store.summary()
        flagged = STATE.telemetry_store.outliers()
        # Cache lever (proven): estimated $ saved by prompt-cache reads, billed at
        # the dominant model's input rate (the per-turn store keeps no per-call
        # model breakdown of cache reads).
        by_model = s.get("by_model") or []
        dom_model = (
            by_model[0]["model"] if by_model else ((STATE.graph_config.model_name if STATE.graph_config else "") or "")
        )
        cache_saved = pricing.cache_read_savings_usd(dom_model, s.get("cache_read_input_tokens", 0))
        return {
            "enabled": True,
            "insights": {
                "turns": s.get("turns", 0),
                "flagged": flagged,
                "flagged_count": len(flagged),
                "levers": {
                    "cache": {
                        "hit_ratio": s.get("cache_hit_ratio", 0.0),
                        "read_tokens": s.get("cache_read_input_tokens", 0),
                        "est_savings_usd": cache_saved,
                    },
                    "routing": {"by_model": by_model},
                    "success_rate": s.get("success_rate", 0.0),
                },
                # Every optimization lever is now measured: routing per-turn
                # (actual models on each row); tool deferral + compaction live via
                # Prometheus (*_llm_tools_deferred_total, *_compactions_total).
                "unproven_levers": [],
            },
        }
