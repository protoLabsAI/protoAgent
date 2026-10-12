"""Graceful fallback wrapper for agent tools.

Wraps a tool so an *unhandled* exception becomes a structured "partial
result" string the model can read and recover from, instead of propagating
out of the tool loop and surfacing as a 500 in the A2A handler.

Tools should still return their own ``"Error: ..."`` strings for *expected*
failures (the established protoLabs convention — the LLM reads the string and
retries). ``with_fallback`` is the safety net for the *unexpected*: a network
library raising a novel exception type, a parsing edge case, etc.

Adapted from the pattern shipped across the protoLabs agent fleet
(quinn / protoResearcher / pwnDeck ``tools/fallbacks.py``), generalised to
neutral wording and both sync and async tools.

Usage — apply between ``@tool`` and the function so LangGraph still sees the
real signature::

    @tool
    @with_fallback()
    async def my_tool(query: str) -> str:
        ...
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Callable

log = logging.getLogger(__name__)


def _format_fallback(func_name: str, fallback_msg: str, exc: Exception) -> str:
    error_type = type(exc).__name__
    error_msg = str(exc)[:200]
    log.warning("[tool:%s] fell back after %s: %s", func_name, error_type, error_msg)
    detail = fallback_msg or f"{func_name} could not complete."
    # Lead with "Error" so AuditMiddleware records success=False, matching the
    # convention tools use for their own returned error strings.
    return (
        f"Error (partial result): {detail}\n"
        f"{error_type}: {error_msg}\n"
        f"Tip: adjust the arguments and try again, or use a different tool."
    )


def with_fallback(fallback_msg: str = "") -> Callable:
    """Decorator: turn an unhandled tool exception into a partial-result string.

    Works on both ``async def`` and ``def`` tools. ``fallback_msg`` overrides
    the generic "<tool> could not complete." line.
    """

    def decorator(func: Callable) -> Callable:
        func_name = getattr(func, "__name__", "tool")

        if asyncio.iscoroutinefunction(func):

            @functools.wraps(func)
            async def awrapper(*args, **kwargs):
                try:
                    return await func(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001 - safety net by design
                    return _format_fallback(func_name, fallback_msg, exc)

            return awrapper

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - safety net by design
                return _format_fallback(func_name, fallback_msg, exc)

        return wrapper

    return decorator
