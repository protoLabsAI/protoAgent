"""Home conversation routes (ADR 0135 D9, ADR 0129 D6 amendment, #5545).

    GET  /api/home       → {session_id, last_read_at}
    POST /api/home/read  → sets last_read_at = now, returns the same shape

Each instance has one Home chat with a stable id (``runtime.home_session``). The console
asks for it on start so it can pin that chat, and marks it read when the operator looks
at it. Both routes answer 404 while the ``home_conversation`` developer flag is off
(ADR 0135 D4).

No check of its own: like the other operator routes, the global ``/api/*`` auth
middleware gates these.

``GET /api/home`` also makes sure Home is in the server-held chat session list (ADR 0130
S6), so the session index lists it before its first turn. That list lives in ``server``,
which this package must not import, so ``server/__init__.py`` hands the two list
functions in. A Home the list already knows — open or closed — is left as it is.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse

log = logging.getLogger("protoagent.home")

_NO_STORE = {"Cache-Control": "no-store"}


def _require_flag() -> None:
    from runtime.flags import flag_enabled
    from runtime.home_session import FLAG_ID

    if not flag_enabled(FLAG_ID):
        raise HTTPException(status_code=404, detail="Not Found")


async def _read(fn, *args) -> dict:
    """Run a ``runtime.home_session`` call off the event loop; a storage failure is a 503
    (retryable), never a freshly minted id."""
    try:
        return await asyncio.to_thread(fn, *args)
    except OSError as exc:
        log.warning("[home] instance state unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="home state is unavailable") from exc


def register_home_routes(
    app,
    *,
    read_session_list: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    register_session: Callable[..., Awaitable[None]] | None = None,
) -> None:
    async def _ensure_listed(session_id: str) -> None:
        """Add Home to the chat session list when it is not there. Best-effort: the id is
        the answer the console needs, and a list failure must not withhold it."""
        if read_session_list is None or register_session is None:
            return
        from runtime.home_session import HOME_TITLE

        try:
            if session_id not in await read_session_list():
                await register_session(session_id, title=HOME_TITLE)
        except Exception as exc:  # noqa: BLE001 — listing is opportunistic here
            log.warning("[home] could not add Home to the session list: %s", exc)

    @app.get("/api/home")
    async def _home():  # noqa: ANN202
        """``{session_id, last_read_at}``: this instance's Home chat. ``session_id`` never
        changes; ``last_read_at`` is zoned ISO-8601 UTC, or null before the first read."""
        from runtime.home_session import home_state

        _require_flag()
        state = await _read(home_state)
        await _ensure_listed(state["session_id"])
        return JSONResponse(state, headers=_NO_STORE)

    @app.post("/api/home/read")
    async def _home_read():  # noqa: ANN202
        """Mark Home read now. Returns the new ``{session_id, last_read_at}``."""
        from runtime.home_session import mark_home_read

        _require_flag()
        return JSONResponse(await _read(mark_home_read), headers=_NO_STORE)
