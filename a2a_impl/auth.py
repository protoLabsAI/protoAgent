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
import re
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# Live-updatable bearer token (None = open mode for bearer).
_BEARER: list[str | None] = [None]
# Optional federation token (ADR 0066) — a second credential confined to the /a2a + /v1
# consumer surfaces and DENIED the /api operator surface. None = no federation tier.
_FEDERATION: list[str | None] = [None]
# Fleet service token (ADR 0089) — the instance's internal, loopback-only credential. The
# hub authenticates the external caller, then presents a member with THIS token (never the
# caller's) so a per-device token (whose registry is the hub's) still reaches sister agents.
# Resolves to the operator tier. None = not a member of a fleet / no service token.
_FLEET: list[str | None] = [None]
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
    "/.well-known/",
    "/app",
    "/manifest.json",
    "/sw.js",
    "/favicon.svg",
    "/favicon.ico",
    "/static/",
    "/_ds/",
)

# Public paths matched EXACTLY, not by prefix. Kept separate from _PUBLIC_PREFIXES because
# prefix-matching a credential-minting route would also exempt any sibling that happens to
# share the string (``/api/pairing/claim-something``) — fine for static asset trees, not for
# this.
_PUBLIC_EXACT = frozenset(
    {
        # Device pairing claim (ADR 0087 D4) — UNAUTHENTICATED BY NECESSITY: obtaining auth
        # is its entire purpose, so it cannot require it. The only credential-minting path
        # on the allowlist, guarded in `security.devices` instead: ~190-bit codes, a 120s
        # TTL, single-use consumption, an immediate reject when nothing is pending, and a
        # failed-attempt counter that drops all pending codes rather than allowing
        # indefinite probing. The operator-only pairing/device routes beside it stay authed.
        "/api/pairing/claim",
    }
)

# /metrics is CONDITIONALLY public — see ``_metrics_public``. It carries
# operational data (model/tool inventory, cost, traffic) so it is exposed without
# auth only in open mode (no token configured). Once a bearer / X-API-Key gates
# the surface, the Prometheus scraper must authenticate too — set
# ``PROTOAGENT_PUBLIC_METRICS=1`` to keep it anonymous behind a network boundary.

# Plugin-declared auth-exempt prefixes. Set once at startup (and on reload) from
# enabled plugins' manifest ``public_paths`` — each already validated to the
# plugin's own ``/plugins/<id>/`` namespace by the manifest parser. This lets a
# plugin serve an inbound webhook (no bearer — verified by its own HMAC) or a
# public view page even when a bearer gates everything else. A plugin can ONLY
# exempt its own routes; ``set_public_prefixes`` rejects anything else as
# defence-in-depth.
_PLUGIN_PUBLIC: list[str] = []

# SSE token lifetime (seconds).
_SSE_TOKEN_LIFETIME = 30

# A plugin public-prefix must be a real SUBTREE of its own namespace —
# ``/plugins/<id>/…`` or ``/api/plugins/<id>/…`` with a trailing slash after the
# id segment — so a bare core route like ``/api/plugins/install`` can never be
# prefix-matched into the exempt set (defence-in-depth behind the manifest
# parser, which applies the same boundary).
_PLUGIN_NS_RE = re.compile(r"^/(?:api/)?plugins/[^/]+/")


def set_public_prefixes(prefixes) -> None:
    """Replace the plugin-declared public-prefix set (idempotent + reload-safe).

    Each prefix must live under a ``/plugins/<id>/`` namespace — a plugin can
    exempt its own routes, never a core path. Non-conforming entries are dropped
    with a warning."""
    cleaned: list[str] = []
    for p in prefixes or []:
        s = str(p).strip()
        if not s:
            continue
        if _PLUGIN_NS_RE.match(s):
            cleaned.append(s)
        else:
            logger.warning(
                "[a2a] ignoring plugin public prefix %r — must be under /plugins/<id>/ or /api/plugins/<id>/", s
            )
    _PLUGIN_PUBLIC[:] = cleaned
    if cleaned:
        logger.info("[a2a] %d plugin-declared auth-exempt path(s): %s", len(cleaned), ", ".join(cleaned))


def public_prefixes() -> list[str]:
    """The live plugin-declared auth-exempt prefixes (post-validation) — exactly what
    ``_is_public`` enforces. Served on the public-paths well-known endpoint so a fleet
    hub can defer its public decision to this member (#1890)."""
    return list(_PLUGIN_PUBLIC)


# Fleet-proxied member view pages (#1890). A member's plugin view page is public
# *chrome* on the member itself (see ``set_public_prefixes``) — but the console
# iframes it through the hub as ``/agents/<slug>/plugins/<id>/…``, a plain
# navigation that cannot carry the operator bearer, and the hub's own public list
# is built from the HUB's manifests (the member may run plugins the hub doesn't).
# The member is the authority on what it serves anonymously, so the hub defers:
# for a slug-prefixed path inside a plugin namespace, an injected async resolver
# ``(slug, rest) -> bool`` answers "would the member serve this anonymously?"
# (graph/fleet/member_public.py — fetched from the member's public-paths endpoint,
# TTL-cached, fail-closed). Injected from the server bootstrap so this module
# stays host-free.
_MEMBER_PUBLIC: list = [None]

_AGENTS_RE = re.compile(r"^/agents/([^/]+)(/.+)$")


def set_member_public_resolver(fn) -> None:
    """Install the async ``(slug, rest) -> bool`` member-public resolver (None = off)."""
    _MEMBER_PUBLIC[0] = fn


def _metrics_public() -> bool:
    """Whether ``/metrics`` is reachable without auth.

    Default: only in open mode (no bearer AND no X-API-Key configured), where the
    whole surface is already unauthenticated. When any token gates the surface,
    ``/metrics`` is gated too — unless ``PROTOAGENT_PUBLIC_METRICS=1`` keeps it
    open for an anonymous Prometheus scraper fenced by a network boundary.
    """
    if os.environ.get("PROTOAGENT_PUBLIC_METRICS", "").strip().lower() in ("1", "true", "yes"):
        return True
    return _BEARER[0] is None and not _API_KEY[0]


def _is_public(path: str) -> bool:
    """Return True when ``path`` is on the public allowlist (no auth needed)."""
    if path in _PUBLIC_EXACT:
        return True
    if any(path.startswith(p) for p in _PUBLIC_PREFIXES):
        return True
    # A SISTER agent's public STATIC assets ride the hub proxy as ``/agents/<slug>/<public-path>``
    # — a plugin view's ``import()``/``<link>`` of ``/_ds/*``, the SPA, favicons: plain browser
    # loads that CANNOT carry a bearer. The member already serves them anonymously; a token-gated
    # hub must too, or the DS plugin-kit 401s and every sister plugin view falls back to
    # unauthenticated ``fetch`` (→ its data 401s: the "Could not load" class). Only the static
    # ``_PUBLIC_PREFIXES`` are lifted this way (never ``/agents/<slug>/api/…`` — those stay gated,
    # then get the fleet-token swap); the #1890 member_public resolver covers plugin NAMESPACES.
    m = _AGENTS_RE.match(path)
    if m and any(m.group(2).startswith(p) for p in _PUBLIC_PREFIXES):
        return True
    if any(path.startswith(p) for p in _PLUGIN_PUBLIC):
        return True
    if path.startswith("/metrics") and _metrics_public():
        return True
    return False


def _requires_operator(path: str) -> bool:
    """Paths that require the OPERATOR credential (ADR 0066 R1 ceiling).

    The ``/api`` operator/console surface — plugin install+enable (host code-exec),
    config/SOUL rewrite, subagent runs, the operator goal set-path — is operator-only; a
    configured federation token is denied it (403). ``/a2a`` + ``/v1`` are the
    federation/consumer surfaces and are NOT operator-only. Public + SSE-token paths never
    reach the ceiling (handled earlier in dispatch). The substring form also catches the
    fleet-proxy variants (``/active/<slug>/api/…``, ``/agents/<slug>/api/…``)."""
    return "/api/" in path or path == "/api" or path.endswith("/api")


def _device_token_ok(token: str) -> bool:
    """True when ``token`` belongs to a paired, non-revoked device (ADR 0087).

    Imported lazily and failed-closed: the device registry is an optional, additive tier, and
    a broken/absent registry must never take down auth for the shared bearer — which is
    already handled above by the time we get here.
    """
    try:
        from security.devices import verify_token

        return verify_token(token) is not None
    except Exception:  # noqa: BLE001 — registry problems must not become auth outages
        logger.exception("[auth] device-token check failed; denying this credential")
        return False


def bearer_tier(token: str) -> str | None:
    """Classify a raw bearer token to its trust tier, or None if it doesn't authenticate.

    The non-middleware entry point for scopes the HTTP ``A2AAuthMiddleware`` skips — notably
    the fleet WS proxy (``@app.websocket`` runs outside HTTP middleware). Mirrors the dispatch
    classification exactly: open mode (no bearer AND no X-API-Key) ⇒ ``operator``; otherwise the
    operator bearer, a configured federation token, the fleet service token (ADR 0089), or a
    paired device token ⇒ their tier, else ``None``.
    """
    active, fed, fleet = _BEARER[0], _FEDERATION[0], _FLEET[0]
    if active is None and not _API_KEY[0]:
        return "operator"  # open mode — the surface is unauthenticated
    if not token:
        return None
    if active is not None and hmac.compare_digest(token, active):
        return "operator"
    if fed is not None and hmac.compare_digest(token, fed):
        return "federation"
    if fleet is not None and hmac.compare_digest(token, fleet):
        return "operator"
    if _device_token_ok(token):
        return "operator"
    return None


def bearer_configured() -> bool:
    """True when a bearer token is active on THIS server.

    The single source of truth for "does this instance require a token", exposed because a
    client cannot infer it: a browser holding a token in localStorage says nothing about what
    the server accepts, and the two diverge the moment a token is rotated or removed. The
    pairing flow reads this before it will write a non-loopback bind — writing one without a
    token configured makes the server refuse to start (`evaluate_open_bind`), which bricks
    the app until someone hand-edits YAML.
    """
    return _BEARER[0] is not None


def set_bearer_token(token: str | None) -> None:
    """Update the active bearer token at runtime (wizard/drawer reload)."""
    _BEARER[0] = (token or "").strip() or None


def set_federation_token(token: str | None) -> None:
    """Update the federation token at runtime (wizard/drawer reload). None = no federation tier."""
    _FEDERATION[0] = (token or "").strip() or None


def set_fleet_token(token: str | None) -> None:
    """Set the fleet service token (ADR 0089). None = not behind a fleet hub."""
    _FLEET[0] = (token or "").strip() or None


def configure(
    *,
    bearer_token: str | None,
    api_key: str,
    allowed_origins_raw: str,
    federation_token: str | None = None,
    fleet_token: str | None = None,
) -> None:
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

    # Federation token (ADR 0066) — same authoritative-vs-env-fallback rule as the bearer.
    raw_fed = federation_token if federation_token is not None else os.environ.get("A2A_FEDERATION_TOKEN", "")
    _FEDERATION[0] = (raw_fed or "").strip() or None
    if _FEDERATION[0] is not None and _BEARER[0] is None:
        logger.warning("[a2a] federation_token set but no operator bearer — federation tier is inert (open mode)")
    if _FEDERATION[0] is not None and _FEDERATION[0] == _BEARER[0]:
        logger.warning("[a2a] federation_token equals the operator token — federation tier collapses to operator")

    # Fleet service token (ADR 0089) — the caller (server bootstrap) resolves it from env (a
    # member) or the persisted file (a hub); passing None leaves this agent outside a fleet.
    _FLEET[0] = (fleet_token or "").strip() or None

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

        # CORS preflight is never authenticated — by SPEC. A browser sends `OPTIONS` with no
        # `Authorization` header (the actual credentialed request follows only if this
        # succeeds), so demanding a bearer here 401s EVERY cross-origin call before it starts:
        # any token-gated instance whose console is a different origin — the desktop webview
        # (tauri://localhost) is the common one — can't talk to its own server. This
        # middleware is added AFTER CORSMiddleware and so wraps outside it (Starlette runs
        # later-added middleware first), meaning without this branch the preflight never
        # reaches CORS to be answered. Preflight carries no credentials and triggers no side
        # effects; the real request still passes through the checks below.
        if request.method == "OPTIONS":
            return await call_next(request)

        # Public allowlist — pass without auth.
        if _is_public(path):
            return await call_next(request)

        # Fleet-proxied member-public paths (#1890): ``/agents/<slug>/<rest>`` where
        # ``<rest>`` sits inside a plugin namespace defers to the MEMBER's own public
        # list — its view pages are public chrome THERE, and the iframe navigation
        # cannot carry a bearer. Consulted only when a credential actually gates this
        # hub (open mode passes below anyway) and only for the plugin namespace —
        # never ``/agents/<slug>/api/…``. ``request.state.member_public`` tells the
        # fleet proxy NOT to lend a stored remote bearer to this request.
        resolver = _MEMBER_PUBLIC[0]
        if resolver is not None and (_BEARER[0] is not None or _API_KEY[0]):
            m = _AGENTS_RE.match(path)
            if m and _PLUGIN_NS_RE.match(m.group(2)):
                try:
                    member_public = await resolver(m.group(1), m.group(2))
                except Exception:  # noqa: BLE001 — resolver trouble = fail closed to normal auth
                    logger.exception("[a2a] member-public resolver failed for %s", path)
                    member_public = False
                if member_public:
                    request.state.member_public = True
                    return await call_next(request)

        # Core media store (#1929): ``/media/<file>`` serves tool-generated artifacts
        # that the console chat renders as inline ``<img>`` tags — a plain browser
        # fetch that cannot carry an Authorization header. Each saved URL carries a
        # per-file HMAC signature minted at save time (``infra/media.py``); a request
        # passes iff the signature verifies (or the store is explicitly opted public
        # via ``media.public``). Anything else falls through to the normal bearer /
        # X-API-Key checks below — default-deny holds.
        if path.startswith("/media/"):
            try:
                from infra.media import request_allowed as _media_allowed

                if _media_allowed(path, request.query_params.get("sig", "")):
                    return await call_next(request)
            except Exception:  # noqa: BLE001 — a check failure fails CLOSED to normal auth
                logger.exception("[a2a] media access check failed for %s", path)

        # SSE endpoint: accept either a valid query-string token OR a bearer header.
        # The query token is for browser EventSource clients that cannot send headers.
        if path == "/api/events" or path.endswith("/api/events"):
            sse_token = request.query_params.get("token", "")
            if _validate_sse_token(sse_token):
                # A valid SSE token proves the caller held the operator bearer — it's HMAC-signed
                # with it and mintable only via the operator-gated /api/sse-token. Mark the tier so
                # the fleet proxy swaps in the MEMBER's credential (ADR 0089) for a proxied
                # /agents/<slug>/api/events: this branch returns BEFORE the bearer classifier that
                # normally sets trust_tier, so without this the swap never fires — and a closed
                # member, which validates the token against ITS bearer (the fleet token, not the
                # hub's that signed it), rejects the hub-signed token → 401 on every live stream.
                request.state.trust_tier = "operator"
                return await call_next(request)
            # Fall through to the normal bearer/X-API-Key check below — a
            # server-to-server caller with an Authorization header still passes.

        # X-API-Key (legacy) — enforced only when configured.
        api_key = _API_KEY[0]
        if api_key and not hmac.compare_digest(request.headers.get("x-api-key", "") or "", api_key):
            return _unauthorized("Unauthorized")

        # Bearer — enforced only when configured. Classify which credential matched
        # (ADR 0066): the operator token → full access; a configured federation token →
        # the /a2a + /v1 consumer surfaces only (the /api ceiling below denies it the
        # operator surface). Open mode + single-token mode resolve to operator (R3
        # backward-compat: unset federation_token ⇒ this is the old single-token check).
        active = _BEARER[0]
        fed = _FEDERATION[0]
        tier = "operator"
        if active:
            header = request.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                return _unauthorized("Unauthorized: expected 'Authorization: Bearer <token>'")
            token = header[len("Bearer ") :]
            # Constant-time compare against each configured secret; classify by which
            # matched. Trust = the matched secret, never the path/Origin/loopback (R5).
            fleet = _FLEET[0]
            is_operator = hmac.compare_digest(token, active)
            is_federation = fed is not None and hmac.compare_digest(token, fed)
            is_fleet = fleet is not None and hmac.compare_digest(token, fleet)
            if is_operator:
                tier = "operator"
            elif is_federation:
                tier = "federation"
            elif is_fleet:
                # Fleet service token (ADR 0089): the hub already authenticated the external
                # caller and presents members with this internal, loopback-only credential.
                # Operator tier — a paired device reaches sister agents without the member
                # ever seeing (or being able to verify) the device token. Checked before the
                # device registry so the intra-fleet hot path is a single constant-time
                # compare and never touches disk.
                tier = "operator"
            elif _device_token_ok(token):
                # A paired device (ADR 0087 D1) is the OPERATOR — it runs the full console
                # and needs the same surface the desktop does. The point of per-device
                # tokens is identity + revocation, not a reduced tier. Checked last so the
                # shared-bearer path stays a single compare and never touches disk.
                tier = "operator"
            else:
                return _unauthorized("Unauthorized: invalid bearer token")

        # R1 path ceiling (ADR 0066): a federation credential is denied the /api operator
        # surface — otherwise the token split is cosmetic (it has RCE via
        # /api/plugins/install anyway). /a2a + /v1 stay open to either tier.
        if tier == "federation" and _requires_operator(path):
            return JSONResponse({"detail": "Forbidden: operator credential required"}, status_code=403)
        request.state.trust_tier = tier

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


def install(
    app,
    *,
    bearer_token: str | None,
    api_key: str,
    allowed_origins_raw: str,
    federation_token: str | None = None,
    fleet_token: str | None = None,
) -> None:
    """Configure the guard and add the middleware to ``app``."""
    configure(
        bearer_token=bearer_token,
        api_key=api_key,
        allowed_origins_raw=allowed_origins_raw,
        federation_token=federation_token,
        fleet_token=fleet_token,
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
