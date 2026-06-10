"""Request-time auth + origin enforcement for the A2A endpoint.

``a2a-sdk`` advertises security schemes on the agent card but does NOT enforce
them on the wire — enforcement is the host's job. This module is a small
Starlette/FastAPI middleware that guards the ``/a2a`` JSON-RPC path with the
same posture the hand-rolled handler had:

  - **Bearer** — ``Authorization: Bearer <token>`` validated against the
    configured token (``auth.token`` in YAML or ``A2A_AUTH_TOKEN`` env). No-op
    when unset (open mode, logged at WARNING).
  - **X-API-Key** — legacy ``<AGENT>_API_KEY`` header, validated when set.
  - **Origin** — ``A2A_ALLOWED_ORIGINS`` allowlist for browser callers. No-op
    when unset or ``*``.

The active bearer token lives in a module-level holder so a wizard/drawer reload
can update it live via ``set_bearer_token`` without re-registering routes.
"""

from __future__ import annotations

import hmac
import logging
import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# Live-updatable bearer token (None = open mode for bearer).
_BEARER: list[str | None] = [None]
# X-API-Key (env-seeded at install; constant for the process).
_API_KEY: list[str] = [""]
# Allowed origins: None = verification disabled; list = allowlist.
_ALLOWED_ORIGINS: list[list[str] | None] = [None]

# Path prefixes the guard applies to: the A2A JSON-RPC surface plus the operator
# console + OpenAI-compat APIs (which drive subagents, rewrite config/SOUL,
# schedule jobs, and run turns). The agent card, /healthz, /metrics, and the
# static console assets live OUTSIDE these prefixes and stay public.
# /active/* and /agents/<slug>/* are the fleet hub's reverse proxies to an agent (ADR 0042).
# They must be guarded too — they drive the agent's full API (chat, config, subagents/run, …),
# and spawned peers carry no token of their own, so the hub is the only gate.
_GUARDED_PREFIXES = ("/a2a", "/api/", "/v1/", "/active/", "/agents/")

# Exempt from the guard: the read-only Server-Sent-Events stream (direct + proxied under any
# slug). Browsers' EventSource cannot set an Authorization header, so a bearer can't be presented
# here — and it only exposes activity/inbox events, not any action. The proxied path carries a
# slug (/agents/<slug>/api/events), so match the SSE suffix rather than a fixed prefix.
_GUARD_EXEMPT = ("/api/events",)


def _is_exempt(path: str) -> bool:
    return path.endswith("/api/events") or any(path.startswith(p) for p in _GUARD_EXEMPT)


def set_bearer_token(token: str | None) -> None:
    """Update the active bearer token at runtime (wizard/drawer reload)."""
    _BEARER[0] = (token or "").strip() or None


def configure(*, bearer_token: str | None, api_key: str, allowed_origins_raw: str) -> None:
    """Seed the guard at route-registration time.

    Args:
        bearer_token: from YAML ``auth.token``. The caller is authoritative:
            ``None`` means "unspecified" and falls back to ``A2A_AUTH_TOKEN``;
            an explicit ``""`` means "bearer off" (e.g. an apiKey-only agent)
            and does NOT fall back — otherwise a stray env var would silently
            enable bearer auth the card never advertises. Empty/whitespace →
            open mode.
        api_key: the ``<AGENT>_API_KEY`` value; "" disables the X-API-Key check.
        allowed_origins_raw: ``A2A_ALLOWED_ORIGINS`` value ("" = disabled,
            "*" = disabled, else comma-separated allowlist).
    """
    raw_bearer = bearer_token if bearer_token is not None else os.environ.get("A2A_AUTH_TOKEN", "")
    seed = (raw_bearer or "").strip()
    _BEARER[0] = seed or None
    if _BEARER[0] is None:
        logger.warning("[a2a] A2A auth token not configured — endpoint is open")

    _API_KEY[0] = api_key or ""

    raw = (allowed_origins_raw or "").strip()
    if not raw:
        logger.warning("[a2a] A2A_ALLOWED_ORIGINS not set — origin verification disabled")
        _ALLOWED_ORIGINS[0] = None
    elif raw == "*":
        _ALLOWED_ORIGINS[0] = None
    else:
        _ALLOWED_ORIGINS[0] = [o.strip().lower() for o in raw.split(",") if o.strip()]


def _unauthorized(detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=401)


class A2AAuthMiddleware(BaseHTTPMiddleware):
    """Enforces bearer / X-API-Key / origin on the guarded A2A path."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not any(path.startswith(p) for p in _GUARDED_PREFIXES):
            return await call_next(request)
        if _is_exempt(path):
            return await call_next(request)

        # X-API-Key (legacy) — enforced only when configured.
        api_key = _API_KEY[0]
        if api_key and request.headers.get("x-api-key") != api_key:
            return _unauthorized("Unauthorized")

        # Bearer — enforced only when configured.
        active = _BEARER[0]
        if active:
            header = request.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                return _unauthorized("Unauthorized: expected 'Authorization: Bearer <token>'")
            if not hmac.compare_digest(header[len("Bearer "):], active):
                return _unauthorized("Unauthorized: invalid bearer token")

        # Origin — enforced only when an allowlist is set AND an Origin is
        # present. Origin is a browser-only header; server-to-server callers
        # (the hub, the LocalScheduler loopback) send none and must not be
        # rejected for it.
        allowed = _ALLOWED_ORIGINS[0]
        if allowed is not None:
            origin = request.headers.get("Origin")
            if origin is not None and origin.lower() not in allowed:
                return JSONResponse({"detail": "Forbidden: origin not allowed"}, status_code=403)

        return await call_next(request)


def install(app, *, bearer_token: str | None, api_key: str, allowed_origins_raw: str) -> None:
    """Configure the guard and add the middleware to ``app``."""
    configure(
        bearer_token=bearer_token,
        api_key=api_key,
        allowed_origins_raw=allowed_origins_raw,
    )
    app.add_middleware(A2AAuthMiddleware)


_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def evaluate_open_bind(
    host: str, *, bearer_configured: bool, allow_open: bool
) -> tuple[bool, str | None]:
    """Boot-time gate for binding a non-loopback host without an auth token.

    An unauthenticated non-loopback bind exposes the full operator API
    (plugin install+enable = code execution, config/SOUL rewrite, subagent
    runs) to anything that can reach the port — so it is refused unless the
    operator explicitly opts in with ``PROTOAGENT_ALLOW_OPEN=1`` (the posture
    for binds fenced by a published-port/network-policy boundary, e.g. a
    container publishing to 127.0.0.1 only).

    Returns ``(allowed, message)``: ``(True, None)`` silent, ``(True, msg)``
    allowed with a warning to log, ``(False, msg)`` refuse startup.
    """
    if host in _LOOPBACK_HOSTS or bearer_configured:
        return True, None
    if allow_open:
        return True, (
            f"[security] binding {host} with NO A2A auth token "
            "(PROTOAGENT_ALLOW_OPEN=1) — the agent + operator API are open to "
            "anything that can reach this port. Make sure a network boundary "
            "(localhost-published port, firewall, network policy) fences it."
        )
    return False, (
        f"[security] refusing to bind {host} with NO A2A auth token — the "
        "operator API (/api/*, /v1/*) includes plugin install/enable (code "
        "execution) and config rewrite. Set auth.token in "
        "langgraph-config.yaml or A2A_AUTH_TOKEN, bind 127.0.0.1 (the "
        "default), or set PROTOAGENT_ALLOW_OPEN=1 if a network boundary "
        "fences this port."
    )
