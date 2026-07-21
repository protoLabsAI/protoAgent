"""Telemetry read-only routes for the operator console (ADR 0006).

Per-turn cost/latency rollups + the advise-only flywheel insight signal, read
from the local telemetry store. Extracted from ``server._main`` (ADR 0023 phase
3) into a ``register_telemetry_routes(app)`` registrar matching
``register_operator_routes``. Every route degrades to ``{"enabled": False}`` when
the store is off, so the surface is always safe to call.
"""

from __future__ import annotations

import asyncio

from runtime.state import STATE

# A Langfuse trace URL is ``<host>/project/<project_id>/traces/<trace_id>`` — the
# console knows neither the host nor the project id (it holds no Langfuse keys),
# and the project id isn't an env var: only the Langfuse API knows it. So resolve
# it here once, hand the console a template, and let it fill in the per-row
# trace_id. ``_UNRESOLVED`` distinguishes "not looked up yet" from "looked up,
# not available" so the lookup happens at most once per process.
_UNRESOLVED: object = object()
_TRACE_URL_TEMPLATE_CACHE: object | str | None = _UNRESOLVED
_TRACE_ID_PLACEHOLDER = "__PROTOAGENT_TRACE_ID__"


def _resolve_trace_url_template() -> str | None:
    """``<host>/project/<id>/traces/{trace_id}``, or None when Langfuse is off.

    Blocking — ``get_trace_url`` hits the Langfuse API for the project id (the
    SDK memoizes it on the client), so call this off the event loop. Raises if
    that call fails, letting the caller retry rather than cache a blip.
    """
    from observability import tracing

    if not tracing.is_enabled():
        return None
    client = getattr(tracing, "_langfuse", None)
    if client is None:
        return None
    # get_trace_url() is the SDK's own formatter for this URL shape; feeding it a
    # placeholder id turns it into a template instead of reimplementing the path.
    url = client.get_trace_url(trace_id=_TRACE_ID_PLACEHOLDER)
    if not url or _TRACE_ID_PLACEHOLDER not in url:
        return None
    return url.replace(_TRACE_ID_PLACEHOLDER, "{trace_id}")


async def _trace_url_template() -> str | None:
    """Cached ``langfuse_trace_url_template`` for the recent-turns response."""
    global _TRACE_URL_TEMPLATE_CACHE
    if _TRACE_URL_TEMPLATE_CACHE is _UNRESOLVED:
        try:
            _TRACE_URL_TEMPLATE_CACHE = await asyncio.to_thread(_resolve_trace_url_template)
        except Exception:  # noqa: BLE001 — a Langfuse blip must not fail the surface
            return None  # stays unresolved; the next poll retries
    return _TRACE_URL_TEMPLATE_CACHE  # type: ignore[return-value]


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
        # Rows come through wholesale (SELECT *), so ``trace_id`` is already on
        # each turn; the template is what lets the console turn it into a link.
        if STATE.telemetry_store is None:
            return {"enabled": False, "turns": []}
        return {
            "enabled": True,
            "turns": STATE.telemetry_store.recent(limit=min(max(1, limit), 500)),
            "langfuse_trace_url_template": await _trace_url_template(),
        }

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
