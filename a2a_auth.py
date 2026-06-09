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
# /active/* is the fleet hub's reverse proxy to the focused agent (ADR 0042). It must
# be guarded too — it drives the agent's full API (chat, config, subagents/run, …), and
# spawned peers carry no token of their own, so the hub is the only gate.
_GUARDED_PREFIXES = ("/a2a", "/api/", "/v1/", "/active/")

# Exempt from the guard: the read-only Server-Sent-Events stream (direct + proxied).
# Browsers' EventSource cannot set an Authorization header, so a bearer can't be presented
# here — and it only exposes activity/inbox events, not any action. The agent-driving
# endpoints (/api/subagents/run, /api/config, /api/chat, /a2a, /active/* …) stay guarded.
_GUARD_EXEMPT = ("/api/events", "/active/api/events")


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
        if any(path.startswith(p) for p in _GUARD_EXEMPT):
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
