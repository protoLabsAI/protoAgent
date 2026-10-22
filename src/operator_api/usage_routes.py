"""Subscription-usage read route (ADR 0122 D6) — ``GET /api/usage``.

A thin REST projection of the ``usage.snapshot`` op (ADR 0075 D2): the op owns the cache,
the polling cadence, and the degrade-to-unknown contract; this route just adapts query
params and JSON. Behind the standard operator-tier auth the rest of ``/api/*`` sits behind
(ADR 0089) — no new trust surface — and read-only. It always answers 200: a disconnected
provider or a fetcher that raised is an ``unknown`` entry in the response, never a 500.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime


def register_usage_routes(app) -> None:
    """Register ``GET /api/usage`` on ``app``."""

    @app.get("/api/usage")
    async def _api_usage(account: str | None = None, refresh: bool = False, cache_only: bool = False):
        """Best-effort usage for the native-OAuth providers.

        ``account`` narrows to one provider; ``refresh=true`` bypasses the ordinary poll
        min-interval (the 429 back-off still binds). The fetchers block on the network, so
        the op runs off the event loop.
        """
        from ops import OpContext
        from ops.usage import snapshot

        try:
            return await asyncio.to_thread(snapshot, OpContext(), account=account, force=refresh, cache_only=cache_only)
        except Exception:  # noqa: BLE001 — belt-and-braces; the op already degrades to unknown
            return {"accounts": [], "fetched_at": datetime.now(UTC).isoformat()}

    @app.get("/api/usage/fleet")
    async def _api_usage_fleet():
        """Operator-only, read-only rollup using the fleet proxy's paired bearers.

        Host polls through the usual store; members provide last-known snapshots
        without polling. Cold member caches report unknown until locally populated.
        A per-member deadline and concurrency limit bound the fan-out.
        """
        from graph.fleet import supervisor
        from operator_api.telemetry_routes import _fetch_member_json, _slug_of
        from ops.usage import merge_fleet_usage

        roster = await asyncio.to_thread(supervisor.status)
        host = await _api_usage()
        slugs = dict.fromkeys(_slug_of(rec) for rec in roster if not rec.get("host"))
        slugs.pop("host", None)
        slugs.pop("", None)
        limit = asyncio.Semaphore(8)

        async def read(slug):
            async with limit:
                try:
                    return await asyncio.wait_for(
                        _fetch_member_json(slug, "api/usage?cache_only=true"), timeout=12.0,
                    )
                except Exception:  # noqa: BLE001 — never expose exception text or credentials
                    return None

        results = await asyncio.gather(*(read(slug) for slug in slugs))
        return merge_fleet_usage(host, dict(zip(slugs, results, strict=True)))
