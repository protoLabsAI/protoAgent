"""Telemetry read-only routes for the operator console (ADR 0006).

Per-turn cost/latency rollups + the advise-only flywheel insight signal, read
from the local telemetry store. Extracted from ``server._main`` (ADR 0023 phase
3) into a ``register_telemetry_routes(app)`` registrar matching
``register_operator_routes``. Every route degrades to ``{"enabled": False}`` when
the store is off, so the surface is always safe to call.

``/api/telemetry/fleet`` is the hub-side FLEET rollup (ADR 0006 fleet
extension): the same read, fanned out per fleet member over the existing
ADR 0042 slug-proxy path and merged into one response keyed per member. Pure
GET, read-only — an unreachable member is reported, never restarted.
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


def _local_summary_payload(since: str | None = None) -> dict:
    """This instance's ``/api/telemetry/summary`` body. Shared with the fleet
    rollup so a single-box fleet read carries EXACTLY the single-instance
    payload (the degradation contract is by construction, not by copy)."""
    if STATE.telemetry_store is None:
        return {"enabled": False, "summary": None}
    return {"enabled": True, "summary": STATE.telemetry_store.summary(since_iso=since)}


def _local_insights_payload() -> dict:
    """This instance's ``/api/telemetry/insights`` body (ADR 0006 Slice 4) —
    shared with the fleet rollup for the same byte-identical degradation."""
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


async def _fetch_member_json(slug: str, path: str) -> dict | None:
    """Pure-GET read of ``<member>/<path>`` via the ADR 0042 slug-proxy resolution.

    Reuses the fleet proxy's per-slug target resolution: a LOCAL member gets the
    fleet service token in place of the hub credential (ADR 0089 D3 — the same
    swap ``/agents/{slug}/*`` does for an operator caller); a REMOTE member
    carries its stored bearer. Any failure — not running, connect/timeout,
    non-200, non-JSON — returns None: the caller REPORTS the member unreachable,
    it never retries into it or restarts it.
    """
    import httpx

    from graph.fleet import proxy

    target = proxy._target_for_slug(slug)
    if target is None:
        return None
    base, extra = target
    headers = dict(extra)
    if not any(k.lower() == "authorization" for k in headers):
        from graph.fleet.service_token import resolve_service_token

        headers["authorization"] = f"Bearer {resolve_service_token()}"
    try:
        resp = await proxy._get_client().get(
            f"{base}/{path}",
            headers=headers,
            # The shared proxy client reads forever (SSE-safe); a rollup read
            # must not — a stalled member is just "unreachable" after the bound.
            timeout=httpx.Timeout(10.0, connect=5.0),
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 — every failure mode is "unreachable", never a 500
        return None


def _rollup_of(summary: dict | None) -> dict | None:
    """The fleet-grid numbers from one member's summary; None = telemetry off."""
    if not isinstance(summary, dict):
        return None
    return {
        "turns": summary.get("turns", 0),
        "cost_usd": summary.get("cost_usd", 0.0),
        "success_rate": summary.get("success_rate", 0.0),
        "cache_hit_ratio": summary.get("cache_hit_ratio", 0.0),
    }


def _flag_entry(slug: str, row: dict, template: str | None) -> dict:
    """One flagged problem (the member's advise-only outlier signal, ADR 0006
    Slice 4) with its evidence links: member, the full per-turn row, trace_id,
    the resolved Langfuse link, and the turn's timestamp."""
    row = row if isinstance(row, dict) else {}
    trace_id = str(row.get("trace_id") or "") or None
    return {
        "member": slug,
        "reasons": list(row.get("reasons") or []),
        "evidence": {
            "member": slug,
            "turn": {k: v for k, v in row.items() if k != "reasons"},
            "trace_id": trace_id,
            "trace_url": template.replace("{trace_id}", trace_id) if template and trace_id else None,
            "timestamp": row.get("ended_at"),
        },
    }


def _member_entry(
    rec: dict,
    slug: str,
    summary_payload: dict | None,
    insights_payload: dict | None,
    template: str | None,
) -> dict:
    """One member's merged fleet read: roster identity + live telemetry. A
    member whose reads both failed is a ``reachable: False`` row — reported,
    never raised."""
    reachable = summary_payload is not None or insights_payload is not None
    enabled = bool((summary_payload or {}).get("enabled") or (insights_payload or {}).get("enabled"))
    flagged = ((insights_payload or {}).get("insights") or {}).get("flagged") or []
    return {
        "name": rec.get("name", slug),
        "label": rec.get("label") or rec.get("name") or slug,
        "host": bool(rec.get("host")),
        "remote": bool(rec.get("remote")),
        "running": bool(rec.get("running")),
        "reachable": reachable,
        "telemetry_enabled": enabled,
        "rollup": _rollup_of((summary_payload or {}).get("summary")),
        "flags": [_flag_entry(slug, row, template) for row in flagged],
    }


def register_telemetry_routes(app) -> None:
    """Register the ``/api/telemetry/*`` read-only routes on ``app``."""

    # Per-turn cost/latency rollups from the local store. Powers the operator
    # console's cost/latency surface (Slice 3) and ad-hoc "what's expensive"
    # queries. Read-only; returns {enabled:false} when the store is off.
    @app.get("/api/telemetry/summary")
    async def _api_telemetry_summary(since: str | None = None):
        return _local_summary_payload(since)

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
        return _local_insights_payload()

    @app.get("/api/telemetry/fleet")
    async def _api_telemetry_fleet():
        """Hub-side READ-ONLY fleet telemetry rollup (ADR 0006 fleet extension).

        Reads the roster from the fleet supervisor (the same seam ``/api/fleet``
        serves, ADR 0042) and fans out pure GETs over each member's EXISTING
        ``/api/telemetry/{summary,insights}`` read path via the slug-proxy
        resolution (ADR 0042; this route sits behind the hub's operator-tier
        auth like the rest of ``/api/*`` per ADR 0089 — no new trust surface).
        Merged into one response keyed per member slug: reachability/running,
        a turns/cost/success/cache rollup, and the member's advise-only flagged
        problems with per-flag evidence (member, per-turn row, trace_id,
        resolved trace link, timestamp). An unreachable member is a
        ``reachable: false`` entry — never a failure of the rollup, and never
        restarted/mutated: there is no write path here. The top level is this
        instance's own read from the SAME helpers as ``/api/telemetry/summary``
        + ``/insights``, so a single-box install (no members) degrades to
        today's single-instance telemetry unchanged, ``members`` holding just
        the host.
        """
        from graph.fleet import supervisor

        roster = await asyncio.to_thread(supervisor.status)
        template = await _trace_url_template()
        summary_payload = _local_summary_payload()
        insights_payload = _local_insights_payload()

        # The host is read locally (it IS this process) — same code path as the
        # single-instance routes, keyed under the proxy's reserved "host" slug.
        host_rec = next((a for a in roster if a.get("host")), None) or {}
        members: dict[str, dict] = {
            "host": {
                "name": host_rec.get("name", "host"),
                "label": host_rec.get("label") or host_rec.get("name") or "host",
                "host": True,
                "remote": False,
                "running": True,
                "reachable": True,
                "telemetry_enabled": bool(summary_payload.get("enabled")),
                "rollup": _rollup_of(summary_payload.get("summary")),
                "flags": [
                    _flag_entry("host", row, template)
                    for row in ((insights_payload.get("insights") or {}).get("flagged") or [])
                ],
            }
        }

        peers = [a for a in roster if not a.get("host")]

        async def _read(rec: dict) -> tuple[str, dict]:
            # The member's slug is its immutable id (ADR 0042 slug routing).
            slug = str(rec.get("id") or rec.get("name") or "")
            s, i = await asyncio.gather(
                _fetch_member_json(slug, "api/telemetry/summary"),
                _fetch_member_json(slug, "api/telemetry/insights"),
            )
            return slug, _member_entry(rec, slug, s, i, template)

        for slug, entry in await asyncio.gather(*(_read(r) for r in peers)):
            members[slug] = entry

        return {
            "enabled": summary_payload.get("enabled", False),
            "summary": summary_payload.get("summary"),
            "insights": insights_payload.get("insights"),
            "fleet": bool(peers),
            "langfuse_trace_url_template": template,
            "members": members,
        }
