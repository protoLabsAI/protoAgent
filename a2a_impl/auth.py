"""Request-time auth + origin enforcement — default-deny posture.

``a2a-sdk`` advertises security schemes on the agent card but does NOT enforce
them on the wire — enforcement is the host's job. This module is a small
Starlette/FastAPI middleware that guards **every** path except an explicit public
allowlist:

  - **Bearer** — ``Authorization: Bearer <token>`` validated against the
    configured token (``auth.token`` in YAML or ``A2A_AUTH_TOKEN`` env). No-op
    when unset (open mode, logged at WARNING).
  - **X-API-Key** — legacy ``<AGENT>_API_KEY`` header, validated when set.
  - **Origin** — ``A2A_ALLOWED_ORIGINS`` allowlist for browser callers. No-op
    when unset or ``*``.

Default-deny: anything NOT on the public allowlist requires auth. The SSE
endpoint ``/api/events`` accepts a short-lived HMAC query-string token so
browser ``EventSource`` clients (which cannot send ``Authorization`` headers)
can authenticate.

The active bearer token lives in a module-level holder so a wizard/drawer reload
can update it live via ``set_bearer_token`` without re-registering routes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time

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

# ---------------------------------------------------------------------------
# Public allowlist — paths/prefixes that pass WITHOUT auth.
# Everything else requires bearer / X-API-Key (default-deny).
# ---------------------------------------------------------------------------
_PUBLIC_PREFIXES = (
    "/healthz",
    "/metrics",
    "/.well-known/",
    "/app",
    "/manifest.json",
    "/sw.js",
    "/favicon.svg",
    "/favicon.ico",
    "/static/",
    "/_ds/",
)

# SSE token lifetime (seconds).
_SSE_TOKEN_LIFETIME = 30


def _is_public(path: str) -> bool:
    """Return True when ``path`` is on the public allowlist (no auth needed)."""
    return any(path.startswith(p) for p in _PUBLIC_PREFIXES)


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
            enable bearer auth the card never advertises. Empty/whitespace ->
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


# ---------------------------------------------------------------------------
# Short-lived SSE query-string token (Part 3)
# ---------------------------------------------------------------------------


def generate_sse_token(session_id: str = "") -> str:
    """Return a base64url-encoded, HMAC-signed token valid for ``_SSE_TOKEN_LIFETIME`` seconds.

    The token is a JSON payload ``{session_id, exp}`` concatenated with an
    HMAC-SHA256 signature. The signing key is the active bearer token — when
    no bearer is configured (open mode) the function returns an empty string
    (SSE is already unrestricted).
    """
    key = _BEARER[0]
    if not key:
        return ""
    payload = json.dumps({"sid": session_id, "exp": int(time.time()) + _SSE_TOKEN_LIFETIME}, separators=(",", ":"))
    sig = hmac.new(key.encode(), payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(payload.encode() + b"." + sig).decode()


def _validate_sse_token(token: str) -> bool:
    """Validate a query-string SSE token in constant time. Returns True when valid."""
    key = _BEARER[0]
    if not key:
        return True  # open mode — no bearer ⇒ no token needed
    if not token:
        return False
    try:
        raw = base64.urlsafe_b64decode(token.encode())
    except Exception:
        return False
    # HMAC-SHA256 is always 32 bytes; the delimiter "." is the byte before it.
    # Split by known offset instead of rsplit to avoid ambiguity when the
    # signature bytes happen to contain 0x2e (".").
    _SIG_LEN = 32
    if len(raw) < _SIG_LEN + 2 or raw[-_SIG_LEN - 1 : -_SIG_LEN] != b".":
        return False
    payload_bytes = raw[: -_SIG_LEN - 1]
    sig = raw[-_SIG_LEN:]
    expected = hmac.new(key.encode(), payload_bytes, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        data = json.loads(payload_bytes)
    except Exception:
        return False
    exp = data.get("exp", 0)
    if time.time() > exp:
        return False
    return True


def _unauthorized(detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=401)


class A2AAuthMiddleware(BaseHTTPMiddleware):
    """Default-deny auth: everything except the public allowlist requires auth."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Public allowlist — pass without auth.
        if _is_public(path):
            return await call_next(request)

        # SSE endpoint: accept either a valid query-string token OR a bearer header.
        # The query token is for browser EventSource clients that cannot send headers.
        if path == "/api/events" or path.endswith("/api/events"):
            sse_token = request.query_params.get("token", "")
            if _validate_sse_token(sse_token):
                return await call_next(request)
            # Fall through to the normal bearer/X-API-Key check below — a
            # server-to-server caller with an Authorization header still passes.

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
            if not hmac.compare_digest(header[len("Bearer ") :], active):
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


def evaluate_open_bind(host: str, *, bearer_configured: bool, allow_open: bool) -> tuple[bool, str | None]:
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
