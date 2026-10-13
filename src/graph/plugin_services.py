"""Plugin services (ADR 0116) — a callable one plugin offers and another calls BY NAME.

The cross-plugin call seam. Before it, a plugin could reach another only through the event bus
(ADR 0039), which is fire-and-forget: no return value, and no answer to "is that plugin even
on?". A plugin that needs a RESULT from another — the data plugin creating a chart in the
Artifact panel and reporting the artifact it made — had nothing to call except the other
plugin's internals, which is exactly the coupling the plugin contract forbids.

So a plugin registers a service (``registry.register_service("show", fn)`` → ``artifact.show``)
and a consumer resolves it at CALL time (``graph.sdk.service("artifact.show")``), getting the
callable or ``None``. ``None`` is the normal "that plugin is disabled / not installed / on an
older core" answer, and every consumer must handle it — a service is an optional capability,
never a hard dependency (a hard one is a manifest ``requires_plugins`` concern, not this).

The live mapping is replaced WHOLESALE at build and on every plugin reload, like the verifier
and work-provider registries (#1752: a stale registry is worse than an empty one — a disabled
plugin's service must stop resolving the moment it's unloaded, not at the next restart). The
operator-MCP process applies the same mapping (``server/operator_mcp.py``), so a plugin tool
running there resolves services exactly as it would in the main process.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping

log = logging.getLogger(__name__)

# ``<plugin_id>.<name>`` — the provider's id is the namespace (``register_service`` prefixes it),
# so one plugin can't register a name under another's. Same id alphabet as the manifest.
SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*\.[a-z][a-z0-9_]*$")

# name -> callable. Rebound wholesale (one GIL-atomic assignment) by ``set_plugin_services``.
_SERVICES: dict[str, Callable] = {}
# name -> {"plugin_id", "description"}; parallel to _SERVICES so its values stay THE callable.
_SERVICE_META: dict[str, dict] = {}


def is_service_name(name: object) -> bool:
    """True for a well-formed ``<plugin_id>.<name>`` service name."""
    return isinstance(name, str) and len(name) <= 128 and bool(SERVICE_NAME_RE.match(name))


def set_plugin_services(mapping: Mapping[str, Callable] | None, meta: Mapping[str, dict] | None = None) -> None:
    """Replace the live service set (called at build + on every plugin reload).

    Wholesale replacement, not a merge: a reload that drops a plugin drops its services too.
    Malformed names and non-callables are skipped (the registry already refuses them; this is
    the second line for a duck-typed bundle)."""
    global _SERVICES, _SERVICE_META
    fresh = {n: fn for n, fn in (mapping or {}).items() if is_service_name(n) and callable(fn)}
    _SERVICES = fresh
    _SERVICE_META = {n: dict(m) for n, m in (meta or {}).items() if n in fresh}


def get_service(name: str) -> Callable | None:
    """The callable registered as ``name``, or ``None`` when no loaded plugin provides it."""
    return _SERVICES.get(name) if isinstance(name, str) else None


def service_names() -> list[str]:
    """Registered service names, sorted — the introspection half, for a status surface."""
    return sorted(_SERVICES)


def service_meta(name: str) -> dict | None:
    """``{"plugin_id", "description"}`` for a registered service, or ``None``."""
    m = _SERVICE_META.get(name)
    return dict(m) if m is not None else None
