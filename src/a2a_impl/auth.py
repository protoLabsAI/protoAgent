"""Request-time auth + origin enforcement — default-deny posture.

``a2a-sdk`` advertises security schemes on the agent card but does NOT enforce
them on the wire — enforcement is the host's job. This module is a small
Starlette/FastAPI middleware that guards **every** path except an explicit public
allowlist:

  - **Bearer** — ``Authorization: Bearer <token>`` validated against the
    configured token (``auth.token`` in YAML or ``A2A_AUTH_TOKEN`` env). No-op
    when unset (open mode, logged at WARNING).
  - **X-API-Key** — ``<AGENT>_API_KEY`` header. **DEPRECATED** (#2632): still validated
    when set, warns once at configure time, and will be removed. Use the bearer.
  - **Origin** — ``A2A_ALLOWED_ORIGINS`` allowlist for browser callers. No-op
    when unset or ``*``.

The two credentials are ALTERNATIVES: presenting either one authenticates (#2620). They
were once checked in sequence, which silently made them a conjunction — enabling the
legacy key broke every bearer client.

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
import ipaddress
import json
import logging
import os
import re
import time
from dataclasses import dataclass

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from a2a_impl.credential_scope import SCOPE_DEVICE, SCOPE_FULL, SCOPES, ceiling_rule, refusal_body

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
# One-shot latch for the X-API-Key deprecation warning (see `configure`).
_API_KEY_DEPRECATION_LOGGED: list[bool] = [False]
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

# Plugin-declared FEDERATION-tier prefixes (#2747). Same namespace rule and same
# set-on-startup/reload lifecycle as ``_PLUGIN_PUBLIC``, opposite effect: these
# paths are NOT exempt from auth — a valid credential is still required — they
# only lower the ADR 0066 ``/api`` operator ceiling so a federation credential
# reaches a plugin-owned route. The seam for a deterministic plugin RPC that a
# peer holding only the federation token must call (a second device syncing a
# plugin-owned store) without being issued the operator bearer, which would hand
# it host code-exec via ``/api/plugins/install``. Direct paths only — the fleet
# proxy variants (``/active/<slug>/api/…``) are never lifted.
_PLUGIN_FEDERATION: list[str] = []

# SSE token lifetime (seconds).
_SSE_TOKEN_LIFETIME = 30

# Read-only SSE streams a browser ``EventSource`` opens — which cannot send an
# ``Authorization`` header, so they authenticate with a short-lived query-string token
# instead (see ``_validate_sse_token``). ``/api/events`` is the event bus; ``/api/chat/attend``
# is the session-attendance presence stream (#3110) the console holds open while a chat is on
# screen, so a background-resume into that session can park for HITL. Matched by exact path OR
# trailing suffix so the fleet-proxy variants (``/agents/<slug>/api/events``) are covered too.
_SSE_QUERY_TOKEN_PATHS = ("/api/events", "/api/chat/attend")


def _is_sse_query_token_path(path: str) -> bool:
    """Whether ``path`` is one of the EventSource streams that accept a ``?token=`` in place
    of a bearer header (see ``_SSE_QUERY_TOKEN_PATHS``)."""
    return any(path == p or path.endswith(p) for p in _SSE_QUERY_TOKEN_PATHS)

# A plugin public-prefix must be a real SUBTREE of its own namespace —
# ``/plugins/<id>/…`` or ``/api/plugins/<id>/…`` with a trailing slash after the
# id segment — so a bare core route like ``/api/plugins/install`` can never be
# prefix-matched into the exempt set (defence-in-depth behind the manifest
# parser, which applies the same boundary).
_PLUGIN_NS_RE = re.compile(r"^/(?:api/)?plugins/[^/]+/")

# CORE routes that live INSIDE a plugin's ``/api/plugins/<id>/`` namespace: the per-plugin
# operator lifecycle routes (``POST /api/plugins/<id>/update`` + ``/enabled``) and the
# bundle routes (``/api/plugins/bundles/…``). A manifest ``public_paths`` /
# ``federation_paths`` prefix of ``/api/plugins/<id>/`` legitimately covers the plugin's
# own data routes — and used to cover these too, so an installed plugin could make its
# own update / enable / disable callable with no credential (or by a federation peer).
# They are never exempt and never lose the operator ceiling, whatever a plugin declares.
# Mirrored by ``graph.plugins.manifest`` (which drops such entries at parse time) and
# pinned against the live route table by tests/test_core_plugin_routes_never_exempt.py.
_CORE_PLUGIN_ROUTE_RE = re.compile(r"^/api/plugins/(?:[^/]+/(?:enabled|update)/?$|bundles(?:/|$))")


def is_core_plugin_route(path: str) -> bool:
    """True for a core-owned operator route inside the ``/api/plugins/<id>/`` namespace —
    which no plugin-declared public / federation prefix may lower."""
    return bool(_CORE_PLUGIN_ROUTE_RE.match(path))


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


def set_federation_prefixes(prefixes) -> None:
    """Replace the plugin-declared federation-tier prefix set (idempotent + reload-safe).

    Same boundary as ``set_public_prefixes`` — a plugin may lower the ceiling only on
    its own ``/plugins/<id>/`` / ``/api/plugins/<id>/`` subtree, never a core route.
    Non-conforming entries are dropped with a warning. Replacing (not merging) is what
    makes a disabled/uninstalled/reloaded plugin's contribution vanish immediately: its
    router may stay mounted, but the path reverts to operator-only and a federation
    caller gets 403 — fail-closed (#2747)."""
    cleaned: list[str] = []
    for p in prefixes or []:
        s = str(p).strip()
        if not s:
            continue
        if _PLUGIN_NS_RE.match(s):
            cleaned.append(s)
        else:
            logger.warning(
                "[a2a] ignoring plugin federation prefix %r — must be under /plugins/<id>/ or /api/plugins/<id>/", s
            )
    _PLUGIN_FEDERATION[:] = cleaned
    if cleaned:
        logger.info("[a2a] %d plugin-declared federation-tier path(s): %s", len(cleaned), ", ".join(cleaned))


def federation_prefixes() -> list[str]:
    """The live plugin-declared federation-tier prefixes (post-validation) — exactly what
    ``_requires_operator`` exempts from the operator ceiling."""
    return list(_PLUGIN_FEDERATION)


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


# A fleet member's signed media link, as the hub receives it (#4258). The slug and file name
# are single plain segments; the query is checked separately (``_is_fleet_media_link``).
_FLEET_MEDIA_RE = re.compile(r"^/agents/([A-Za-z0-9][A-Za-z0-9_-]*)/media/([^/]+)$")


def _is_fleet_media_link(request) -> bool:
    """Whether ``request`` is EXACTLY a member's signed media fetch the hub may forward
    without its own auth (#4258): a GET/HEAD of ``/agents/<slug>/media/<file>`` where

    * the path arrived with NO percent-encoding at all (raw bytes == decoded path) — so no
      ``%2e%2e``, ``%2f`` or double-encoded slug can make the path the hub matched differ
      from the one the proxy forwards (#3999's rule, applied before the auth decision);
    * ``<file>`` is a servable store name (``infra.media.is_media_name``: no dotfile, no
      nested path);
    * the query is ``exp=<unix seconds>`` and ``sig=<64 hex>`` — both required (only an
      EXPIRING link is exempt; a permanent one never is), each once, nothing else.

    Shape only: the hub never verifies the member's signature (it doesn't hold the key) —
    the member's own media gate does. Anything else takes the hub's normal auth (401)."""
    if request.method not in ("GET", "HEAD"):
        return False
    scope = getattr(request, "scope", {}) or {}
    path = scope.get("path") or ""
    m = _FLEET_MEDIA_RE.match(path)
    if not m:
        return False
    raw = scope.get("raw_path")
    if not isinstance(raw, (bytes, bytearray)) or raw.decode("latin-1") != path:
        return False
    try:
        from infra.media import is_link_query_value, is_media_name
    except Exception:  # noqa: BLE001 — no media module: never exempt
        return False
    if not is_media_name(m.group(2)):
        return False
    query = (scope.get("query_string") or b"").decode("latin-1")
    seen: dict[str, str] = {}
    for pair in query.split("&") if query else ():
        key, sep, value = pair.partition("=")
        if not sep or key not in ("sig", "exp") or key in seen or not is_link_query_value(key, value):
            return False
        seen[key] = value
    # BOTH are required: only an EXPIRING link is ever exempt. A permanent ``?sig=`` link (the
    # console's inline links — in answer text, history, exports, logs) stays behind the hub's
    # auth, or every link a member ever minted would be fetchable through the hub forever.
    return "sig" in seen and "exp" in seen


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
    if any(path.startswith(p) for p in _PLUGIN_PUBLIC) and not is_core_plugin_route(path):
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
    fleet-proxy variants (``/active/<slug>/api/…``, ``/agents/<slug>/api/…``).

    A plugin may lower the ceiling on its OWN namespaced routes via manifest
    ``federation_paths`` (#2747) — matched as a direct prefix, so the proxied variants
    stay operator-only; the plugin route itself still sees a verified credential and
    reads the tier from ``request.state.trust_tier``."""
    if any(path.startswith(p) for p in _PLUGIN_FEDERATION) and not is_core_plugin_route(path):
        return False
    return "/api/" in path or path == "/api" or path.endswith("/api")


def _device_for_token(token: str):
    """The paired, non-revoked device ``token`` belongs to (ADR 0087), else None.

    Imported lazily and failed-closed: the device registry is an optional, additive tier, and
    a broken/absent registry must never take down auth for the shared bearer — which is
    already handled above by the time we get here.
    """
    try:
        from security.devices import verify_token

        return verify_token(token)
    except Exception:  # noqa: BLE001 — registry problems must not become auth outages
        logger.exception("[auth] device-token check failed; denying this credential")
        return None


@dataclass(frozen=True)
class Credential:
    """What a matched credential earns: its trust TIER (ADR 0066 — who the caller is for goal,
    knowledge and chat trust) and its credential SCOPE (ADR 0121 D4 — whether it may change
    who can get in). Orthogonal: a paired phone is ``operator`` tier with ``device`` scope.
    ``device_id`` is set for a device token (self rename / unpair, stream re-verification)."""

    tier: str
    scope: str = SCOPE_FULL
    device_id: str | None = None


_OPEN_CREDENTIAL = Credential("operator", SCOPE_FULL)


def bearer_scope(token: str) -> str | None:
    """The credential scope a raw bearer earns (ADR 0121 D4), or None if it doesn't
    authenticate. ``bearer_tier``'s twin, for the scopes the HTTP middleware skips (the fleet
    WS proxy). Open mode ⇒ ``full``: nothing is checked, so there is nothing to narrow."""
    if open_mode():
        return SCOPE_FULL
    cred = _classify_token(token)
    return cred.scope if cred else None


def bearer_tier(token: str) -> str | None:
    """Classify a raw bearer token to its trust tier, or None if it doesn't authenticate.

    The non-middleware entry point for scopes the HTTP ``A2AAuthMiddleware`` skips — notably
    the fleet WS proxy (``@app.websocket`` runs outside HTTP middleware). Mirrors the dispatch
    classification exactly: open mode (no bearer AND no X-API-Key) ⇒ ``operator``; otherwise the
    operator bearer, a configured federation token, the fleet service token (ADR 0089), or a
    paired device token ⇒ their tier, else ``None``.
    """
    if open_mode():
        return "operator"  # open mode — the surface is unauthenticated
    return _bearer_tier_for(token)


def open_mode() -> bool:
    """True when this server enforces NO credential (no bearer AND no X-API-Key) — the
    desktop default on its loopback bind, where the middleware rates every caller operator.

    For callers that must treat "operator because nothing is checked" differently from
    "operator because a secret matched": the fleet proxy gates browser-driven requests to a
    REMOTE member on an open hub (#3662), because there an anonymous cross-site page would
    otherwise be lent the remote's stored credential; and ``a2a_impl.hosts.HostGuardMiddleware``
    applies the instance-wide Host allowlist + JSON content-type rule only here (#3668)."""
    return _BEARER[0] is None and not _API_KEY[0]


def credential_tier(token: str) -> str | None:
    """The tier a PRESENTED raw bearer earns on its own merits — ``bearer_tier`` without the
    open-mode shortcut. On an open hub nothing authenticates here, so every token is None.

    For a caller that is about to hand out something stronger than "this surface is open":
    the fleet WS proxy swaps an authenticated credential for a REMOTE member's stored bearer
    (ADR 0113 D6). ``bearer_tier`` says "operator" for ANY string on an open hub, which would
    let an anonymous ``?token=x`` — from any web page in the operator's browser, since the WS
    route has no Origin gate of its own — trade itself for the remote's credential. An open
    hub has no credential to authenticate against, so it can't vouch for one either."""
    return _bearer_tier_for(token)


def allowed_origins() -> list[str] | None:
    """The configured ``A2A_ALLOWED_ORIGINS`` allowlist (lowercased), or None when origin
    verification is disabled (unset or ``*``). For scopes the HTTP middleware skips — the
    fleet WS proxy.

    The declared public console origin (ADR 0121 D2, ``network.public_console_url``) is folded
    in whenever an allowlist IS set — otherwise every console call from the public origin would
    be a 403. With no allowlist nothing is added: verification stays off, as configured."""
    base = _ALLOWED_ORIGINS[0]
    if base is None:
        return None
    from security.public_console import public_origin

    origin = public_origin()
    return [*base, origin] if origin and origin not in base else base


def _bearer_tier_for(token: str) -> str | None:
    """The tier a raw bearer earns, or None. Open mode is the CALLER's concern."""
    cred = _classify_token(token)
    return cred.tier if cred else None


def _classify_token(token: str) -> Credential | None:
    """The tier + scope a raw bearer earns, or None. Open mode is the CALLER's concern.

    One ladder, two callers — this function and the middleware's ``_classify_request``.
    Keeping a second copy in the dispatch path is the same "two derivations of one fact"
    that produced #2620, and on a security classification it is the expensive kind: the
    copies drift, and the one that drifts is the one nobody reads.

    Order is load-bearing. The shared-bearer comparisons come first so the hot path is a
    constant-time compare; ``_device_for_token`` is last because it touches the device
    registry on disk.
    """
    active, fed, fleet = _BEARER[0], _FEDERATION[0], _FLEET[0]
    if not token:
        return None
    if active is not None and hmac.compare_digest(token, active):
        return Credential("operator", SCOPE_FULL)
    if fed is not None and hmac.compare_digest(token, fed):
        # Scope is moot here — the ADR 0066 ceiling already denies federation all of /api.
        return Credential("federation", SCOPE_FULL)
    if fleet is not None and hmac.compare_digest(token, fleet):
        # Fleet service token (ADR 0089): the hub already authenticated the external caller
        # (and applied ITS credential-plane ceiling) and presents members with this internal,
        # loopback-only credential.
        return Credential("operator", SCOPE_FULL)
    device = _device_for_token(token)
    if device is not None:
        # A paired device (ADR 0087 D1) is the OPERATOR tier — never a new tier string, which
        # would silently fail every `== "operator"` consumer. What narrows is the SCOPE (ADR
        # 0121 D4): a phone is ``device``; an agent device and a legacy row are ``full``.
        try:
            scope = device.effective_scope()
        except Exception:  # noqa: BLE001 — an odd row must not widen: fail to the narrow scope
            scope = SCOPE_DEVICE
        return Credential("operator", scope if scope in SCOPES else SCOPE_DEVICE, str(device.id))
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
    if _API_KEY[0] and not _API_KEY_DEPRECATION_LOGGED[0]:
        # Deprecated, NOT removed: it keeps authenticating exactly as before, because
        # silently dropping a credential someone deployed against would lock them out of
        # their own agent. The warning is the migration path; removal is tracked in #2632.
        # Once per process — this runs on every config reload, and a warning that repeats
        # every few minutes is one operators learn to filter out.
        _API_KEY_DEPRECATION_LOGGED[0] = True
        logger.warning(
            "[a2a] %s_API_KEY is DEPRECATED and will be removed in a future release. It still "
            "works for now. Migrate to the bearer token (auth.token in langgraph-config.yaml, "
            "or the A2A_AUTH_TOKEN env): it is settable from Settings and the wizard, rotatable "
            "at runtime, routed through secrets.yaml, and it is what every protoAgent client "
            "already sends. Tracking: "
            "https://github.com/protoLabsAI/protoAgent/issues/2632",
            os.environ.get("AGENT_NAME", "AGENT").upper(),
        )

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


def generate_sse_token(session_id: str = "", scope: str = SCOPE_DEVICE, device_id: str | None = None) -> str:
    """Return a base64url-encoded, HMAC-signed token valid for ``_SSE_TOKEN_LIFETIME`` seconds.

    The token is a JSON payload ``{sid, exp, scope}`` concatenated with an
    HMAC-SHA256 signature. The signing key is the active bearer token — when
    no bearer is configured (open mode) the function returns an empty string
    (SSE is already unrestricted).

    ``scope`` (ADR 0121 D4) is the MINTING caller's credential scope, signed into the payload
    so an EventSource stream a device opened doesn't come back as ``full``. It defaults to the
    NARROW scope: a caller that forgets to pass it gets a token that can do less, never more.

    ``device_id`` (ADR 0121 D5) is the minting caller's paired device, when it is one. The
    stream it opens is stamped with it, so the stream re-verifies that device at its
    keepalive and closes when it is revoked or expires — and a revoked device can't open a
    new stream with a token it minted in the 30 s before the revoke.
    """
    key = _BEARER[0]
    if not key:
        return ""
    scope = scope if scope in SCOPES else SCOPE_DEVICE
    claims: dict = {"sid": session_id, "exp": int(time.time()) + _SSE_TOKEN_LIFETIME, "scope": scope}
    if device_id:
        claims["did"] = str(device_id)
    payload = json.dumps(claims, separators=(",", ":"))
    sig = hmac.new(key.encode(), payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(payload.encode() + b"." + sig).decode()


def _validate_sse_token(token: str) -> bool:
    """Validate a query-string SSE token in constant time. Returns True when valid."""
    return _sse_token_scope(token) is not None


def _sse_token_scope(token: str) -> str | None:
    """The credential scope a valid query-string SSE token carries, or None when invalid."""
    claims = _sse_token_claims(token)
    return claims[0] if claims is not None else None


def _sse_token_claims(token: str) -> tuple[str, str | None] | None:
    """``(scope, device_id)`` a valid query-string SSE token carries, or None when invalid.

    A token minted by a paired device carries its id (``did``, ADR 0121 D5); one whose device
    has since been revoked or idle-expired is INVALID, so the 30 s token lifetime is no window
    to open a fresh stream after a revoke.

    A token with NO ``scope`` field (minted before ADR 0121, at most ``_SSE_TOKEN_LIFETIME``
    seconds before an upgrade) or an unknown value reads as ``device``: the old format can't say
    whether a phone minted it, and the narrow scope is the fail-safe reading. That costs a full
    caller nothing — the SSE streams are reads, none of them is behind the credential-plane
    ceiling — so no format version bump is needed. Open mode ⇒ ``full`` (nothing is checked)."""
    key = _BEARER[0]
    if not key:
        return SCOPE_FULL, None  # open mode — no bearer ⇒ no token needed
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token.encode())
    except Exception:
        return None
    # HMAC-SHA256 is always 32 bytes; the delimiter "." is the byte before it.
    # Split by known offset instead of rsplit to avoid ambiguity when the
    # signature bytes happen to contain 0x2e (".").
    _SIG_LEN = 32
    if len(raw) < _SIG_LEN + 2 or raw[-_SIG_LEN - 1 : -_SIG_LEN] != b".":
        return None
    payload_bytes = raw[: -_SIG_LEN - 1]
    sig = raw[-_SIG_LEN:]
    expected = hmac.new(key.encode(), payload_bytes, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(payload_bytes)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    exp = data.get("exp", 0)
    if not isinstance(exp, (int, float)) or time.time() > exp:
        return None
    scope = data.get("scope")
    scope = scope if isinstance(scope, str) and scope in SCOPES else SCOPE_DEVICE
    did = data.get("did")
    if did is None:
        return scope, None
    if not isinstance(did, str) or not did or not device_is_live(did):
        return None
    return scope, did


def device_is_live(device_id: str | None) -> bool:
    """Does the paired device ``device_id`` still authenticate (not revoked, not expired)?

    The re-verification a long-lived stream runs at its keepalive (ADR 0121 D5). Failed
    closed: a registry error reads as "not live", which closes a device's stream — never the
    shared bearer's, which has no device id and is never asked."""
    try:
        from security.devices import is_live

        return is_live(device_id)
    except Exception:  # noqa: BLE001 — a registry problem must not keep a revoked stream open
        logger.exception("[auth] device liveness check failed; treating %s as revoked", device_id)
        return False


def presented_device_id(token: str) -> str | None:
    """The paired device a raw credential speaks for, or None (ADR 0121 D5).

    For ``a2a_impl.socket_guard``, which classifies a WebSocket handshake's ``?token=`` or
    bearer header the way the HTTP middleware classifies a request: a device token names its
    device, and so does a stream token (``/api/sse-token``) a device minted. The shared bearer,
    the fleet token, a federation token and anything that doesn't authenticate are None, so
    nothing re-verifies them."""
    if not token:
        return None
    cred = _classify_token(token)
    if cred is not None:
        return cred.device_id
    claims = _sse_token_claims(token)
    return claims[1] if claims is not None else None


def _unauthorized(detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=401)


def _record_trust_tier_telemetry(request):
    """Surface the ALREADY-classified trust tier as bounded telemetry (#1504).

    Reads ONLY ``request.state.trust_tier`` — the operator/federation label dispatch
    derived from the matched credential — and forwards it to structured request
    telemetry. It never parses, reads, or logs credential material (no header, no token),
    and it never raises into the request path. Returns a context reset token when
    telemetry accepted the label; callers reset it after the downstream request finishes
    so no tier leaks into later unclassified work.
    """
    try:
        from observability import tracing

        return tracing.set_trust_tier(getattr(request.state, "trust_tier", None))
    except Exception:  # noqa: BLE001 — telemetry must never break auth
        return None


def _clear_trust_tier_telemetry(token) -> None:
    """Undo ``_record_trust_tier_telemetry``'s contextvar set once the request finishes.

    Its exact mirror: reset the telemetry trust-tier context so a classified tier can
    never bleed into a later, unrelated request handled on the same context. Best-effort
    and silent — telemetry cleanup, like the record side, must never surface as an
    auth-path failure. ``None`` (nothing was recorded) is a no-op.
    """
    if token is None:
        return
    try:
        from observability import tracing

        tracing.reset_trust_tier(token)
    except Exception:  # noqa: BLE001 — telemetry cleanup must not break auth
        pass


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

        # A fleet member's EXPIRING signed media link (#4258):
        # ``/agents/<slug>/media/<file>?exp=…&sig=…``.
        # The hub can't verify a member's signature (the key never leaves the member), so a
        # request of EXACTLY that shape is forwarded without hub auth and the MEMBER's own
        # ``/media/`` gate verifies it — an unsigned, forged or expired link is refused there.
        # ``member_public`` makes the proxy forward it with NO credential of the hub's (or the
        # caller's, to a remote), so the member's signature check is the only thing that can
        # admit it. Anything not of that exact shape takes the normal auth below.
        if _is_fleet_media_link(request):
            request.state.member_public = True
            request.state.fleet_media = True  # the proxy answers hub-side failures 401 (no slug probe)
            return await call_next(request)

        # Core media store (#1929): ``/media/<file>`` serves tool-generated artifacts
        # that the console chat renders as inline ``<img>`` tags — a plain browser
        # fetch that cannot carry an Authorization header. Each saved URL carries a
        # per-file HMAC signature minted at save time (``infra/media.py``); a request
        # passes iff the signature verifies (or the store is explicitly opted public
        # via ``media.public``). An expiring link (``?exp=``, #4258) past its expiry is
        # answered 403 unless a credential admits the request anyway. Anything else
        # falls through to the normal bearer / X-API-Key checks below — default-deny holds.
        media_expired = False
        if path.startswith("/media/"):
            try:
                from infra.media import check_access as _media_access

                verdict = _media_access(
                    path, request.query_params.get("sig", ""), request.query_params.get("exp", "")
                )
                if verdict == "ok":
                    return await call_next(request)
                media_expired = verdict == "expired"
            except Exception:  # noqa: BLE001 — a check failure fails CLOSED to normal auth
                logger.exception("[a2a] media access check failed for %s", path)

        # SSE endpoint: accept either a valid query-string token OR a bearer header.
        # The query token is for browser EventSource clients that cannot send headers
        # (the event bus AND the session-attendance stream — see _SSE_QUERY_TOKEN_PATHS).
        if _is_sse_query_token_path(path):
            sse_token = request.query_params.get("token", "")
            sse_claims = _sse_token_claims(sse_token)
            if sse_claims is not None:
                sse_scope, sse_device = sse_claims
                # A valid SSE token proves the caller held the operator bearer — it's HMAC-signed
                # with it and mintable only via the operator-gated /api/sse-token. Mark the tier so
                # the fleet proxy swaps in the MEMBER's credential (ADR 0089) for a proxied
                # /agents/<slug>/api/events: this branch returns BEFORE the bearer classifier that
                # normally sets trust_tier, so without this the swap never fires — and a closed
                # member, which validates the token against ITS bearer (the fleet token, not the
                # hub's that signed it), rejects the hub-signed token → 401 on every live stream.
                request.state.trust_tier = "operator"
                # The scope the MINTER held (ADR 0121 D4), restored from the signed payload.
                request.state.credential_scope = sse_scope
                # The minting device (ADR 0121 D5), so `DeviceStreamGuard` re-verifies it.
                request.state.device_id = sse_device
                denied = _credential_plane_refusal(request, path, sse_scope, None)
                if denied is not None:
                    return denied
                _trust_tier_token = _record_trust_tier_telemetry(request)
                try:
                    return await call_next(request)
                finally:
                    _clear_trust_tier_telemetry(_trust_tier_token)
            # Fall through to the normal bearer/X-API-Key check below — a
            # server-to-server caller with an Authorization header still passes.

        # Configured credentials are ALTERNATIVES — satisfy any one (#2620).
        cred = _OPEN_CREDENTIAL
        if _API_KEY[0] or _BEARER[0]:
            cred = _classify_request_credential(request)
            if cred is None:
                if media_expired:
                    return JSONResponse(
                        {"detail": "Forbidden: this media link has expired — ask the agent for a fresh link"},
                        status_code=403,
                    )
                return _unauthorized(_credential_error())
        tier = cred.tier

        # R1 path ceiling (ADR 0066): a federation credential is denied the /api operator
        # surface — otherwise the token split is cosmetic (it has RCE via
        # /api/plugins/install anyway). /a2a + /v1 stay open to either tier.
        if tier == "federation" and _requires_operator(path):
            return JSONResponse({"detail": "Forbidden: operator credential required"}, status_code=403)
        request.state.trust_tier = tier
        # Credential scope (ADR 0121 D4): orthogonal to the tier. The credential-plane ceiling
        # sits right beside the federation one, and runs before any route — including the
        # fleet proxy's ADR 0089 D3 credential swap, which is just a route handler.
        request.state.credential_scope = cred.scope
        request.state.device_id = cred.device_id
        denied = _credential_plane_refusal(request, path, cred.scope, cred.device_id)
        if denied is not None:
            return denied

        # Origin — enforced only when an allowlist is set AND an Origin is
        # present. Origin is a browser-only header; server-to-server callers
        # (the hub, the LocalScheduler loopback) send none and must not be
        # rejected for it.
        allowed = allowed_origins()
        if allowed is not None:
            origin = request.headers.get("Origin")
            if origin is not None and origin.lower() not in allowed:
                return JSONResponse({"detail": "Forbidden: origin not allowed"}, status_code=403)

        # Surface the ALREADY-decided trust tier to structured request telemetry for the
        # span of the downstream handler only — recorded here, immediately before call_next,
        # and always cleared after. The auth decision (including the Origin denial above) is
        # untouched: a rejected request returns before this scope and records nothing.
        _trust_tier_token = _record_trust_tier_telemetry(request)
        try:
            return await call_next(request)
        finally:
            _clear_trust_tier_telemetry(_trust_tier_token)


def _credential_error() -> str:
    """The 401 detail, naming every credential this agent would have accepted."""
    accepted = []
    if _BEARER[0]:
        accepted.append("'Authorization: Bearer <token>'")
    if _API_KEY[0]:
        accepted.append("'X-API-Key: <key>'")
    if not accepted:  # unreachable — the caller only asks when something is configured
        return "Unauthorized"
    return "Unauthorized: expected " + " or ".join(accepted)


def _credential_plane_refusal(request, path: str, scope: str, device_id: str | None) -> JSONResponse | None:
    """The ADR 0121 D4 credential-plane ceiling: a 403 when a non-``full`` credential asks for
    a route that changes who can get in (``a2a_impl.credential_scope.CEILING``), else None."""
    if scope == SCOPE_FULL:
        return None
    # Both spellings of the path: ``request.url.path`` (what the rest of this middleware
    # keys on) and the ASGI ``scope["path"]`` the router dispatches on. They differ when a
    # percent-decoded ``?``/``#`` lands in the path; checking both means neither can be used
    # to slip a ceiling route past the other.
    raw = (getattr(request, "scope", None) or {}).get("path") or path
    rule = ceiling_rule(request.method, path, device_id)
    if rule is None and raw != path:
        rule = ceiling_rule(request.method, raw, device_id)
    if rule is None:
        return None
    logger.info("[auth] device-scope credential refused %s %s (%s)", request.method, path, rule.why)
    return JSONResponse(refusal_body(rule), status_code=403)


def _classify_request(request) -> str | None:
    """The trust tier this request's credential earns, or None if none matched."""
    cred = _classify_request_credential(request)
    return cred.tier if cred else None


def _classify_request_credential(request) -> Credential | None:
    """The tier + scope this request's credential earns, or None if none matched.

    **Alternatives, not a conjunction (#2620).** This used to be two sequential checks —
    an X-API-Key guard, then a bearer guard — which made them an AND: with a legacy key
    AND a bearer configured, neither credential alone was accepted, so turning on the
    legacy key silently broke every bearer client.

    That was never a decision. The two checks were added years apart, nothing documents a
    conjunction (this module's own docstring lists them as parallel mechanisms), and A2A's
    ``securityRequirements`` list means alternatives. It survived because every INTERNAL
    caller — the scheduler and console self-invoke paths — sends both credentials, so only
    an external client trusting the agent card could ever hit it. One did.

    Matching stays constant-time per secret, and trust is still the matched secret alone —
    never the path, Origin or loopback-ness (R5).
    """
    # Legacy X-API-Key ⇒ operator. Checked first: it's a single compare and the internal
    # callers that still present it are the hot path.
    api_key = _API_KEY[0]
    if api_key:
        presented = request.headers.get("x-api-key", "") or ""
        if presented and hmac.compare_digest(presented, api_key):
            return Credential("operator", SCOPE_FULL)

    active = _BEARER[0]
    if not active:
        return None  # api key was the only credential configured, and it didn't match

    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    token = header[len("Bearer ") :]

    # Classify by WHICH secret matched (ADR 0066): the operator token → full access; a
    # configured federation token → the /a2a + /v1 consumer surfaces only (the R1 ceiling
    # below denies it /api). Single-token mode resolves to operator (R3 back-compat).
    # Shared with `bearer_tier` — one ladder, so the WS proxy and the HTTP middleware can
    # never disagree about what a token earns.
    return _classify_token(token)


def inbound_credentials() -> tuple[str, str]:
    """``(bearer, api_key)`` — the credentials a caller must present to THIS agent.

    The single source for anything that needs to *present* or *describe* this agent's
    inbound auth: the agent card, the scheduler's self-invocation client, the console's
    self-invoke path. Each of those used to re-read ``auth.token`` / ``A2A_AUTH_TOKEN`` /
    ``<AGENT>_API_KEY`` for itself, with subtly different precedence — five derivations of
    one fact, which is how the card came to advertise a credential the guard never
    enforced (#2620). Read it from the guard and there is nothing left to drift.
    """
    return _BEARER[0] or "", _API_KEY[0] or ""


def self_invocation_headers(fallback_bearer: str = "", fallback_api_key: str = "") -> dict[str, str]:
    """Auth headers for an agent calling its OWN endpoint, resolved at call time.

    The scheduler and background manager both self-invoke over HTTP, and both are built
    during agent init — BEFORE ``auth.install()`` seeds the guard (server/__init__.py:423
    vs :928). Reading credentials at construction therefore captures nothing and every
    scheduled turn 401s; capturing them at all also misses a live ``set_bearer_token``
    rotation, which is the #2582 failure mode one layer down. Resolving here, per request,
    is immune to both.

    ``fallback_*`` keep an embedding caller that passes credentials explicitly working —
    the guard is a source, not a requirement. Live values win when present.

    One function, two callers: the first cut of this fix put a byte-identical copy in each,
    which is precisely the duplicated-security-derivation failure this PR exists to remove.
    """
    bearer, api_key = fallback_bearer or "", fallback_api_key or ""
    try:
        live_bearer, live_api_key = inbound_credentials()
        bearer, api_key = live_bearer or bearer, live_api_key or api_key
    except Exception:  # noqa: BLE001 — credential lookup must never break an invocation
        pass
    headers: dict[str, str] = {}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    if api_key:
        headers["X-API-Key"] = api_key
    return headers


def enforced_schemes() -> tuple[bool, bool]:
    """``(bearer, api_key)`` — which credentials this guard ACTUALLY enforces (#2620).

    The public agent card has to describe the server, and it used to describe a guess:
    ``server/a2a.py`` re-derived "is a bearer configured?" from env + config, and the card
    builder hardcoded the ``apiKey`` scheme unconditionally. Two derivations of one fact
    drift, and this one did — a bearer-only agent advertised ``X-API-Key`` as a valid
    alternative, so a standards-following A2A client that picked the advertised apiKey
    scheme got 401 from a healthy, correctly-configured agent.

    Reading it from the guard's own state is what keeps the card honest: whatever
    ``configure()`` was handed is exactly what the card claims.

    Each returned credential is an ALTERNATIVE — presenting any one authenticates (see
    ``_classify_request``), so the card lists one requirement per credential.
    """
    return bool(_BEARER[0]), bool(_API_KEY[0])


def install(
    app,
    *,
    bearer_token: str | None,
    api_key: str,
    allowed_origins_raw: str,
    federation_token: str | None = None,
    fleet_token: str | None = None,
) -> None:
    """Configure the guard and add the middleware to ``app``.

    Two layers, added in this order so the second wraps the first (Starlette runs the
    last-added middleware outermost): ``A2AAuthMiddleware`` (credentials, HTTP only), then
    ``a2a_impl.hosts.HostGuardMiddleware`` — the open-mode Host allowlist + JSON content-type
    rule (#3668), pure ASGI so it also covers WebSocket routes, and outermost so a refused
    request reaches nothing. Between them, ``a2a_impl.stream_guard.DeviceStreamGuard`` closes
    a paired device's streamed responses within one keepalive of a revoke, and
    ``a2a_impl.socket_guard.DeviceSocketGuard`` does the same for its open WebSockets
    (ADR 0121 D5)."""
    from a2a_impl.hosts import HostGuardMiddleware
    from a2a_impl.socket_guard import DeviceSocketGuard
    from a2a_impl.stream_guard import DeviceStreamGuard

    configure(
        bearer_token=bearer_token,
        api_key=api_key,
        allowed_origins_raw=allowed_origins_raw,
        federation_token=federation_token,
        fleet_token=fleet_token,
    )
    app.add_middleware(A2AAuthMiddleware)
    app.add_middleware(DeviceSocketGuard)
    app.add_middleware(DeviceStreamGuard)
    app.add_middleware(HostGuardMiddleware)


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


#: 0.0.0.0 / :: mean "every interface", so loopback is served too. Mirrors
#: ``pairing_routes._WILDCARD_BINDS`` (that one answers "is this address reachable?",
#: this one answers "did we just exclude ourselves?").
_WILDCARD_BINDS = ("0.0.0.0", "::", "*", "")


def evaluate_bind_reachability(host: str) -> str | None:
    """Warn when the configured bind **excludes loopback** (#2147).

    uvicorn binds a *single* host. Pinning one interface IP — e.g. the box's Tailscale
    address, to make the operator API tailnet-reachable but invisible on other networks
    — therefore stops serving ``127.0.0.1``. The desktop app's Tauri webview reaches its
    sidecar over loopback, so this is a total UI lockout, recoverable only by reverting
    ``network.bind`` and relaunching. It is a silent footgun: nothing about the setting
    says "and this will lock you out of your own console".

    There is no bind that expresses "loopback + tailnet, nothing else". The way to get it
    is ``0.0.0.0`` (which keeps loopback) plus scoping *reach* at the network layer —
    Tailscale ACL, firewall, or simply not publishing the port. Post-ADR-0089 the wide
    bind is token-gated, so that posture is not an open hole.

    Returns a message to log/surface, or ``None`` when loopback is still served.
    """
    h = (host or "").strip()
    if h in _LOOPBACK_HOSTS or h in _WILDCARD_BINDS:
        return None
    try:
        if ipaddress.ip_address(h).is_loopback:  # 127.0.0.0/8 beyond 127.0.0.1
            return None
    except ValueError:
        pass  # a hostname, not an IP — can't classify without resolving; `localhost` is above
    return (
        f"[network] bind is pinned to {h}, which EXCLUDES loopback — uvicorn binds a single "
        "host, so 127.0.0.1 is no longer served and the local console (and the desktop app's "
        "own webview, which reaches its sidecar over loopback) cannot reach this instance. "
        "For 'reachable on my tailnet/LAN but nothing else', set network.bind to 0.0.0.0 — "
        "which keeps loopback — and scope REACH at the network layer (Tailscale ACL, "
        "firewall, or not publishing the port) rather than at the bind."
    )


def evaluate_public_url(host: str, public_url: str | None) -> str | None:
    """Warn when the server is reachable off-box but ``A2A_PUBLIC_URL`` is unset (#4252).

    With no public URL the agent card's interface and every absolute ``/media`` link an
    answer carries (A2A file parts, ``/v1`` attachments) fall back to the bound loopback
    port — fine for a same-host client, a dead link for a remote peer. A loopback bind
    stays silent (its clients ARE same-host); a wildcard or interface bind with no public
    URL gets one warning at startup. Returns the message, or ``None``.
    """
    if (public_url or "").strip():
        return None
    h = (host or "").strip()
    if not h or h in _LOOPBACK_HOSTS:
        return None
    try:
        if ipaddress.ip_address(h).is_loopback:
            return None
    except ValueError:
        pass  # a hostname — `localhost` is above; anything else is treated as non-loopback
    return (
        f"[a2a] bound to {h} but A2A_PUBLIC_URL is unset — links/agent card will advertise a "
        "loopback URL remote peers can't use — set A2A_PUBLIC_URL to the address other agents "
        "and clients reach (e.g. http://roxy:7870)."
    )
