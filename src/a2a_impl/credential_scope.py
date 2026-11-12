"""The credential-plane ceiling for ``device``-scope credentials (ADR 0121 D4).

A paired phone or tablet authenticates as the OPERATOR trust tier (ADR 0087 D1) — it runs
the whole console, and every ``trust_tier == "operator"`` consumer (chat, goal trust,
knowledge trust, the fleet proxy) keeps its meaning. What narrows is a separate, orthogonal
attribute, the **credential scope** (``request.state.credential_scope``):

* ``full`` — the shared bearer, ``X-API-Key``, the fleet service token, ``kind: agent``
  devices (a hub must proxy the whole console, ADR 0113) and legacy device rows.
* ``device`` — every new phone/tablet pairing, from any path.

The rule for ``device`` scope: it can use the whole product but cannot change **who can get
in**. The routes that do are listed ONCE, in ``CEILING`` below, and enforced in
``A2AAuthMiddleware`` (next to the ADR 0066 federation ceiling) and again in the fleet proxy
before the ADR 0089 D3 credential swap. Config writes are the other half: they are checked by
KEY at the config write path (``graph.settings_schema.credential_plane_keys``), not here.

"Who can get in" includes host code that would outlive a revoke: plugin lifecycle and MCP
servers (commands the server runs) are on the ceiling for that reason.

Matching is by SUFFIX — the bare path and every proxied form (``/agents/<slug>/…``,
``/active/<slug>/…``). Over-matching (some unrelated path that happens to END in a ceiling
path) only ever refuses a device-scope caller, which is the fail-safe direction. (The SSE
query-token paths in ``auth`` are matched the opposite way, anchored: there an over-match
would ADMIT a request, the unsafe direction.)

Both ``a2a_impl.auth`` (the middleware) and ``graph.fleet.proxy`` (the WS proxy, which runs
outside HTTP middleware) import this module; it lives here because ``graph`` may not import
``server`` or ``operator_api`` (import-linter).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SCOPE_FULL = "full"
SCOPE_DEVICE = "device"
SCOPES = frozenset({SCOPE_FULL, SCOPE_DEVICE})

#: The machine-readable refusal code. The console maps it to a readable message.
DEVICE_SCOPE_CODE = "device_scope"
DEVICE_SCOPE_MESSAGE = "This device can't change pairing or credentials — use a full-access device."
#: For the routes the hub refuses outright when a limited device aims them at a REMOTE agent
#: (``KEY_CHECKED_CONFIG_WRITES``): the refusal covers every setting there, so it says so.
DEVICE_REMOTE_SETTINGS_MESSAGE = "This device can't change settings on a remote agent — use a full-access device."

#: Carries a ``device`` scope across a proxy hop. The hub's fleet proxy authenticates the
#: caller, then presents the member with a credential that is FULL scope there (the fleet
#: service token, or a remote's stored bearer) — so the member would otherwise see a paired
#: phone as a full-access caller. The hub sets this header on such a hop and the receiving
#: middleware lowers the request to ``device``. It can only NARROW: a full credential plus
#: this header is ``device``; nothing a caller sends here ever widens anything.
SCOPE_HEADER = "x-protoagent-credential-scope"

#: Carries "this turn's caller may not approve" across a delegation hop. A hub delegating a
#: federation-tier turn (``@member`` / ``delegate_to``) presents the member with the fleet
#: service token or the delegate's own bearer — OPERATOR there — so the member would let that
#: turn answer or skip approvals (ADR 0123 / 0124). The delegating side sets this header and the
#: receiving middleware binds "may not approve" for the request. Narrow-only, like
#: ``SCOPE_HEADER``: no value of it ever grants approval.
APPROVAL_HEADER = "x-protoagent-caller-may-approve"
APPROVAL_DENIED = "no"

#: Config-write routes whose credential-plane check is per KEY, inside the route (see
#: ``operator_api.scope_guard``), so the URL ceiling below can't refuse them. The fleet proxy
#: refuses these for a ``device``-scope caller aimed at a REMOTE member: a remote on an older
#: build ignores ``SCOPE_HEADER`` and would apply every key with its own full credential.
KEY_CHECKED_CONFIG_WRITES = (
    "/api/config",
    "/api/config/setup",
    "/api/settings",
    "/api/settings/reset",
)

_ANY = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True)
class CeilingRule:
    """One credential-plane route family. ``path`` uses ``{id}`` for one segment and a
    trailing ``/**`` for "this path and everything under it"."""

    methods: frozenset[str]
    path: str
    why: str
    #: The bare ``{id}`` may be the CALLER's own device id (self rename / unpair, S3).
    self_ok: bool = False


def _rule(methods, path: str, why: str, *, self_ok: bool = False) -> CeilingRule:
    return CeilingRule(frozenset(methods), path, why, self_ok)


# ── The ceiling ─────────────────────────────────────────────────────────────────────────
# Every route a ``device``-scope credential is refused, in one table. Grep-derived from the
# route decorators in src/operator_api/ (pairing_routes, fleet_routes, runtime_routes,
# plugin_routes, secrets_routes, config_routes); tests/test_device_scope.py walks this table
# × {bare, /agents/<slug>/ proxied} and pins it against the live route table.
CEILING: tuple[CeilingRule, ...] = (
    # Pairing — a device can't pair devices or agents.
    _rule({"POST"}, "/api/pairing/start", "mint a pairing code"),
    _rule({"POST"}, "/api/devices", "mint an access token"),
    _rule({"POST"}, "/api/pairing/cancel", "cancel pending pairing codes"),
    _rule({"POST"}, "/api/fleet/remotes/pair", "pair another agent"),
    # Reachability + lifecycle (#4336 S4): serving over tailnet HTTPS changes who can reach
    # this instance; a restart applies a new bind. Not in ADR 0121's original list — added
    # with S4's routes, same rule ("can't change who can get in").
    _rule({"POST", "DELETE"}, "/api/pairing/tailscale/serve", "change how this agent is reached"),
    _rule({"POST"}, "/api/restart", "restart this agent"),
    # The device registry — except the caller's OWN id on the bare path (rename / unpair
    # yourself). Promotion (``{"scope": …}``) is refused at the route for non-full callers.
    _rule({"PATCH", "DELETE"}, "/api/devices/{id}", "change another paired device", self_ok=True),
    # Plugin lifecycle — runs host code that would outlive a revoke.
    _rule({"POST"}, "/api/plugins/install", "install a plugin"),
    _rule({"POST"}, "/api/plugins/sync", "sync plugins"),
    _rule({"POST"}, "/api/plugins/install-deps", "install plugin dependencies"),
    _rule({"POST"}, "/api/plugins/install-python", "provision a plugin's Python runtime"),
    _rule({"POST"}, "/api/plugins/ack", "acknowledge a plugin"),
    _rule({"POST"}, "/api/plugins/{id}/update", "update a plugin"),
    _rule({"POST"}, "/api/plugins/{id}/enabled", "enable or disable a plugin"),
    _rule({"DELETE"}, "/api/plugins/{id}", "remove a plugin"),
    _rule(_ANY - {"GET", "HEAD"}, "/api/plugins/bundles/**", "change a plugin bundle"),
    _rule(_ANY, "/api/plugin-setup/**", "run a plugin setup step"),
    # MCP servers — each one is a COMMAND the server spawns (and respawns on every reload), so
    # adding, importing or changing one leaves host code behind that outlives a revoke, the
    # same reason plugin lifecycle is here. Their config twins (``mcp.*``, ``delegates``,
    # ``lifecycle_hooks``, ``acp.*``, ``filesystem.editor_command``) are credential-plane keys.
    _rule({"POST"}, "/api/mcp/servers", "add an MCP server"),
    _rule({"POST"}, "/api/mcp/servers/import", "import MCP servers"),
    _rule(_ANY - {"GET", "HEAD"}, "/api/mcp/servers/**", "change an MCP server"),
    # New agents — creating one (from a bundle URL, with config the caller picks) or importing
    # a snapshot (which installs the plugins it names and applies its config verbatim) leaves
    # host code and a running process behind, exactly like a plugin install. Starting,
    # stopping and switching between agents that already exist stays open.
    _rule({"POST"}, "/api/fleet", "create an agent"),
    _rule({"POST"}, "/api/agent/import", "import an agent"),
    # Secrets.
    _rule(_ANY, "/api/secrets/**", "use the secrets manager"),
    _rule(_ANY, "/api/config/oauth/**", "change a provider sign-in"),
)


def narrowed_by_header(scope: str, header_value: str | None) -> str:
    """``scope`` after applying a ``SCOPE_HEADER`` value — ``device`` if the header asks for
    it, else unchanged. Never widens."""
    return SCOPE_DEVICE if (header_value or "").strip().lower() == SCOPE_DEVICE else scope


def approval_narrowed(header_value: str | None) -> bool:
    """Whether an ``APPROVAL_HEADER`` value says the caller may not approve."""
    return (header_value or "").strip().lower() == APPROVAL_DENIED


def is_key_checked_config_write(method: str | None, path: str) -> bool:
    """Whether ``method path`` is one of ``KEY_CHECKED_CONFIG_WRITES`` (bare or proxied)."""
    if (method or "").upper() != "POST":
        return False
    p = (path or "").split("?", 1)[0].split("#", 1)[0].rstrip("/")
    return any(p == w or p.endswith(w) for w in KEY_CHECKED_CONFIG_WRITES)


def _compile(path: str) -> re.Pattern[str]:
    tail = ""
    if path.endswith("/**"):
        path, tail = path[:-3], r"(?:/.*)?"
    body = re.escape(path).replace(re.escape("{id}"), r"(?P<id>[^/]+)")
    # Searched, not matched: any prefix (a proxied ``/agents/<slug>``) may precede it.
    return re.compile(body + tail + r"/?$")


_COMPILED: tuple[tuple[CeilingRule, re.Pattern[str]], ...] = tuple((r, _compile(r.path)) for r in CEILING)


def ceiling_rule(method: str | None, path: str, device_id: str | None = None) -> CeilingRule | None:
    """The ceiling rule a ``device``-scope request ``method path`` hits, or None if allowed.

    ``method=None`` means "any method" — for a WebSocket upgrade, whose method says nothing
    about what the socket does. ``device_id`` is the caller's own device id: a ``self_ok`` rule
    lets it through only on the BARE path (a proxied ``/agents/<slug>/api/devices/<id>`` names a
    device in the MEMBER's registry, where the hub's device id means nothing).
    """
    m_up = method.upper() if method else None
    for rule, pat in _COMPILED:
        if m_up is not None and m_up not in rule.methods:
            continue
        hit = pat.search(path or "")
        if hit is None:
            continue
        if rule.self_ok and m_up is not None and device_id and hit.start() == 0 and hit.group("id") == device_id:
            continue
        return rule
    return None


def refuses(scope: str | None, method: str | None, path: str, device_id: str | None = None) -> bool:
    """True when a credential of ``scope`` must be refused ``method path``.

    Fail-safe on the scope: anything other than exactly ``full`` is treated as ``device``.
    Callers pass ``SCOPE_FULL`` explicitly for credentials that earn it."""
    if scope == SCOPE_FULL:
        return False
    return ceiling_rule(method, path, device_id) is not None


def refusal_body(rule: CeilingRule | None = None, *, message: str = DEVICE_SCOPE_MESSAGE) -> dict:
    """The 403 body. ``detail`` is structured ``{code, reason}`` so the console's
    ``parseErrorBody`` shows the reason and keeps the code; ``error`` is the flat code."""
    body: dict = {"error": DEVICE_SCOPE_CODE, "detail": {"code": DEVICE_SCOPE_CODE, "reason": message}}
    if rule is not None:
        body["detail"]["action"] = rule.why
    return body
