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

# Path prefix the guard applies to. The agent card + health are public.
_GUARDED_PREFIX = "/a2a"


def set_bearer_token(token: str | None) -> None:
    """Update the active bearer token at runtime (wizard/drawer reload)."""
    _BEARER[0] = (token or "").strip() or None


def configure(*, bearer_token: str | None, api_key: str, allowed_origins_raw: str) -> None:
    """Seed the guard at route-registration time.

    Args:
        bearer_token: from YAML ``auth.token`` (``A2A_AUTH_TOKEN`` env fallback
            applied by the caller). Empty/whitespace → open mode.
        api_key: the ``<AGENT>_API_KEY`` value; "" disables the X-API-Key check.
        allowed_origins_raw: ``A2A_ALLOWED_ORIGINS`` value ("" = disabled,
            "*" = disabled, else comma-separated allowlist).
    """
    seed = (bearer_token or os.environ.get("A2A_AUTH_TOKEN", "") or "").strip()
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
        if not request.url.path.startswith(_GUARDED_PREFIX):
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

        # Origin — enforced only when an allowlist is set.
        allowed = _ALLOWED_ORIGINS[0]
        if allowed is not None:
            origin = request.headers.get("Origin", "").lower()
            if origin not in allowed:
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
