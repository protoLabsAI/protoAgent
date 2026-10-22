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
    async def _api_usage(account: str | None = None, refresh: bool = False):
        """Best-effort usage for the native-OAuth providers.

        ``account`` narrows to one provider; ``refresh=true`` bypasses the ordinary poll
        min-interval (the 429 back-off still binds). The fetchers block on the network, so
        the op runs off the event loop.
        """
        from ops import OpContext
        from ops.usage import snapshot

        try:
            return await asyncio.to_thread(snapshot, OpContext(), account=account, force=refresh)
        except Exception:  # noqa: BLE001 — belt-and-braces; the op already degrades to unknown
            return {"accounts": [], "fetched_at": datetime.now(UTC).isoformat()}
