"""Classify an exception from a model call by the upstream hop that produced it.

Shared by the turn drivers (``server.chat.turn_error`` → ``/v1``'s status mapping) and
the operator API (``/api/subagents/run`` / ``/batch``, #3957), which may not import
``server``. Neutral, dependency-free, and never raises.
"""

from __future__ import annotations

# The machine-readable error ``type`` / ``code`` for an upstream HTTP status — what
# ``/v1``'s error ``type``, ``/api/chat``'s ``detail.code`` and the subagent routes'
# ``detail.code`` carry (#3991). Anything unlisted is ``server_error``.
ERROR_TYPE_BY_STATUS: dict[int, str] = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "authentication_error",
    404: "invalid_request_error",
    422: "invalid_request_error",
    429: "rate_limit_error",
}


def upstream_error_type(status: int | None) -> str:
    """:data:`ERROR_TYPE_BY_STATUS` for ``status``, else ``"server_error"``."""
    return ERROR_TYPE_BY_STATUS.get(status, "server_error") if status is not None else "server_error"


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

    Walks the explicit ``__cause__`` chain only (``raise … from e``): the openai SDK's
    ``APIConnectionError`` wraps the underlying ``httpx`` transport error that way, and a
    framework layer may wrap it again. NOT the implicit ``__context__`` (#3957): a bug in our
    own code raised inside an ``except`` that had handled a transport error would carry it
    there and be mislabelled an upstream 502. Bounded, so a cyclic chain can't spin."""
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
        exc = exc.__cause__
    return False


def upstream_status_in_chain(exc: BaseException | None) -> int | None:
    """:func:`upstream_status` of ``exc`` or the first exception in its explicit
    ``__cause__`` chain that carries one (never the implicit ``__context__`` — see
    :func:`upstream_unreachable`). A subagent run re-raises its provider failure
    wrapped (``SubagentError(...) from e``), so the status sits one link down. Bounded,
    so a cyclic chain can't spin."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen and len(seen) < 16:
        code = upstream_status(exc)
        if code is not None:
            return code
        seen.add(id(exc))
        exc = exc.__cause__
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


def provider_retry_delay(headers, *, now=None) -> float | None:
    """Parse a positive finite cooldown, falling back from unusable milliseconds."""
    import math
    from datetime import UTC, datetime
    from email.utils import parsedate_to_datetime

    retry: float | None = None
    now = now or datetime.now(UTC)
    try:
        retry = float(headers.get("retry-after-ms", "")) / 1000
    except (ValueError, TypeError, OverflowError):
        pass
    if retry is None or not math.isfinite(retry) or retry <= 0:
        retry = None
        try:
            raw = headers.get("retry-after", "")
            try:
                retry = float(raw)
            except ValueError:
                date = parsedate_to_datetime(raw)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=UTC)
                retry = (date - now).total_seconds()
        except (ValueError, TypeError, OverflowError):
            pass
    return retry if retry is not None and math.isfinite(retry) and retry > 0 else None


def rate_limit_details(exc: BaseException | None) -> dict:
    """Preserve a provider's cooldown without waiting through it (#4205).

    Only explicit 429 responses qualify; an OAuth identity refusal or an ordinary
    stream stall must not be diagnosed as exhausted subscription quota.
    """
    import math
    from datetime import UTC, datetime, timedelta

    seen: set[int] = set()
    while exc is not None and id(exc) not in seen and len(seen) < 16:
        seen.add(id(exc))
        if upstream_status(exc) == 429:
            break
        exc = exc.__cause__
    else:
        return {}
    headers = getattr(getattr(exc, "response", None), "headers", {}) or {}
    now = datetime.now(UTC)
    retry = provider_retry_delay(headers, now=now)
    body = getattr(exc, "body", None)
    error = body.get("error", body) if isinstance(body, dict) else {}
    reason = str(error.get("message") or str(exc)) if isinstance(error, dict) else str(exc)
    details = {"provider_rate_limited": True}
    if any(marker in reason.lower() for marker in ("quota", "weekly limit", "usage limit", "insufficient_quota")):
        details["quota_exhausted"] = True
    message = f"Model provider rate limit: {reason}"
    if retry is not None and math.isfinite(retry) and 0 < retry <= 315360000:
        seconds = math.ceil(retry)
        details["retry_after"] = seconds
        details["retry_at"] = (now + timedelta(seconds=seconds)).isoformat()
        message += f" Retry after {seconds} seconds (retry at {details['retry_at']})."
    details["message"] = message + " Choose another configured model in the chat model selector, or wait and retry."
    return details
