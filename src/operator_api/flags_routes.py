"""Developer flags (ADR 0068).

    GET    /api/flags        → the resolved flag list (registry metadata + each flag's state)
    PUT    /api/flags/{id}   → store this instance's override for a flag: ``{"enabled": bool}``
    DELETE /api/flags/{id}   → clear that override (back to the channel default)
    DELETE /api/flags        → clear every stored override

The writes are what the console's **Developer panel** toggle calls, so a flag that gates
server behaviour can be switched from the console and survives a restart. Gated by the
``/api/*`` operator bearer (a2a_impl/auth.py) like every operator route. The registry,
the stored overrides and the resolution live in ``runtime.flags``.
"""

from __future__ import annotations

from pydantic import BaseModel


class FlagOverride(BaseModel):
    enabled: bool


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

    def _refusal(exc: Exception):  # noqa: ANN202
        """An unknown flag is 404; a flag pinned by its environment variable is 409."""
        from fastapi import HTTPException

        message = str(exc)
        return HTTPException(status_code=404 if message.startswith("no developer flag") else 409, detail=message)

    @router.put("/api/flags/{flag_id}")
    async def _set_flag(flag_id: str, body: FlagOverride) -> dict:
        """Store this instance's override for ``flag_id``. ``404`` for an unregistered flag;
        ``409`` when the flag is pinned by its environment variable. Returns the refreshed
        list, so the panel needs no second request."""
        import asyncio

        from runtime import flags

        try:
            await asyncio.to_thread(flags.set_override, flag_id, body.enabled)
        except flags.FlagOverrideError as exc:
            raise _refusal(exc) from exc
        return flags.resolved_flags()

    @router.delete("/api/flags/{flag_id}")
    async def _clear_flag(flag_id: str) -> dict:
        """Clear the stored override for ``flag_id`` (idempotent). ``404`` for an
        unregistered flag."""
        import asyncio

        from runtime import flags

        try:
            await asyncio.to_thread(flags.set_override, flag_id, None)
        except flags.FlagOverrideError as exc:
            raise _refusal(exc) from exc
        return flags.resolved_flags()

    @router.delete("/api/flags")
    async def _clear_flags() -> dict:
        """Clear every stored override."""
        import asyncio

        from runtime import flags

        await asyncio.to_thread(flags.clear_overrides)
        return flags.resolved_flags()

    app.include_router(router)
