"""Classify an exception from a model call by the upstream hop that produced it.

Shared by the turn drivers (``server.chat.turn_error`` → ``/v1``'s status mapping) and
the operator API (``/api/subagents/run`` / ``/batch``, #3957), which may not import
``server``. Neutral, dependency-free, and never raises.
"""

from __future__ import annotations


def upstream_status(exc: BaseException | None) -> int | None:
    """The HTTP status an upstream provider returned, if the exception carries one.

    Covers the openai SDK (``status_code``), older/alternate clients (``http_status``),
    and anything wrapping an httpx/requests response.
    """
    for attr in ("status_code", "http_status"):
        code = getattr(exc, attr, None)
        if isinstance(code, int) and 400 <= code < 600:
            return code
    code = getattr(getattr(exc, "response", None), "status_code", None)
    return code if isinstance(code, int) and 400 <= code < 600 else None


def upstream_unreachable(exc: BaseException | None) -> bool:
    """True when the turn failed because the model gateway could not be REACHED at all —
    connection refused, DNS failure, a connect/read timeout (#3946). No HTTP status comes
    back in that case, so :func:`upstream_status` is ``None`` and ``/v1`` used to call it
    an internal 500; it is a failed proxy hop and belongs with the other 502s.

    Walks the ``__cause__``/``__context__`` chain, since the openai SDK's
    ``APIConnectionError`` wraps the underlying ``httpx`` transport error (and a
    framework layer may wrap it again). Bounded, so a cyclic chain can't spin."""
    transport: tuple[type[BaseException], ...] = (ConnectionError,)
    try:
        import httpx

        transport += (httpx.TransportError,)
    except ImportError:  # pragma: no cover — httpx ships with the openai SDK
        pass
    try:
        import openai

        transport += (openai.APIConnectionError,)  # APITimeoutError subclasses it
    except ImportError:  # pragma: no cover
        pass
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen and len(seen) < 16:
        if isinstance(exc, transport):
            return True
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return False


def upstream_status_in_chain(exc: BaseException | None) -> int | None:
    """:func:`upstream_status` of ``exc`` or the first exception in its ``__cause__`` /
    ``__context__`` chain that carries one. A subagent run re-raises its provider failure
    wrapped (``SubagentError(...) from e``), so the status sits one link down. Bounded,
    so a cyclic chain can't spin."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen and len(seen) < 16:
        code = upstream_status(exc)
        if code is not None:
            return code
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return None


def upstream_http_status(exc: BaseException | None) -> int | None:
    """The HTTP status an operator endpoint should answer for an upstream failure, or
    ``None`` when ``exc`` is not one (a fault in our own code — the caller's 500).

    Same policy as ``/v1`` (``operator_api.chat_routes._v1_error_response``): a 429 is
    mirrored so a client's backoff keys on it; any other upstream HTTP failure, or a
    gateway that could not be reached, is a 502 — never the upstream's own status, since
    a 401 from US means "your protoAgent bearer is bad"."""
    code = upstream_status_in_chain(exc)
    if code == 429:
        return 429
    if code is not None or upstream_unreachable(exc):
        return 502
    return None
