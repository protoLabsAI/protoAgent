"""Developer flags (ADR 0068) — ``GET /api/flags``.

Serves the resolved flag list (registry metadata + each flag's enabled state for the
current channel) so the console **Developer panel** (slice 4) can render and toggle them.
Read-only; gated by the ``/api/*`` operator bearer (a2a_impl/auth.py) like every operator
route. The registry + resolution live in ``runtime.flags`` (slice 1).
"""

from __future__ import annotations


def register_flags_routes(app) -> None:
    from fastapi import APIRouter

    router = APIRouter()

    @router.get("/api/flags")
    async def _flags() -> dict:
        """The active channel + every registered developer flag with its resolved state
        (ADR 0068). Shape: ``{"channel": "prod|beta|dev", "flags": [{id, description, tier,
        owner, remove_by, enabled, source}]}``. See ``runtime.flags.resolved_flags``."""
        from runtime.flags import resolved_flags

        return resolved_flags()

    app.include_router(router)
