"""Unified delegate registry — `delegate_to` over a2a / openai / acp (ADR 0025).

One tool, ``delegate_to(target, query)``, dispatches to any configured delegate:
a fleet **A2A agent**, an OpenAI-compatible **model endpoint**, or an **ACP coding
agent**. Replaces the three split surfaces (`peer_consult`, `code_with`, and the
gateway-only model) with one hot-swappable roster.

PR1 (this slice): the registry + `delegate_to` + the three adapters, configured
via the ``delegates`` config section and hot-reloaded by Save & Reload. The CRUD
REST API (PR2) and the React panel (PR3) build on this. Enabled by default — it
contributes ``delegate_to`` only once you declare a delegate in config (a no-op
until then), so the gate is the delegate, not a plugin toggle.
"""

from __future__ import annotations

import logging

from langchain_core.tools import tool

from .adapters import DelegateError
from .registry import DelegateRegistry

log = logging.getLogger("protoagent.plugins.delegates")


def _build_delegate_to(registry: DelegateRegistry):
    listing = registry.listing()

    @tool
    async def delegate_to(target: str, query: str) -> str:
        """Hand a question or task to one of your configured delegates and return its reply.

        Use this to reach beyond your own context: ask a fleet **agent**, consult
        another **model endpoint**, or hand a repo-scoped coding job to a **coding
        agent**. Pick the delegate whose description best fits the task.

        Args:
            target: the delegate name (see the available list in this tool's
                description).
            query: the full, self-contained question or instruction — the delegate
                does not see this conversation, so restate what it needs.
        """
        if not str(query).strip():
            return "Error: `query` is empty — give the delegate something to do."
        try:
            return await registry.dispatch(target, query)
        except DelegateError as exc:
            return f"Error: {exc}"
        except Exception as exc:  # noqa: BLE001 — surface as a tool error string
            log.warning("[delegates] dispatch to %r failed: %s", target, exc)
            return f"Error: delegate {target!r} failed: {type(exc).__name__}: {exc}"

    delegate_to.description = f"{delegate_to.description}\n\nAvailable delegates: {listing or '(none configured)'}."
    return delegate_to


def _load_delegates_config() -> list:
    """Read the top-level ``delegates: [...]`` list from the live config doc.

    A top-level list (ORBIS parity) doesn't fit the plugin's dict-shaped
    config_section, so we read it from the live YAML directly. register() re-runs
    on every graph build / Save & Reload, so this reflects the current config —
    that's the hot-swap (ADR 0025). Falls back to ``registry.config['delegates']``
    if a fork nests it under the plugin section.
    """
    try:
        from .store import merged_delegates

        return merged_delegates()  # delegates + secrets overlaid from secrets.yaml
    except Exception:  # noqa: BLE001 — config read is best-effort
        log.exception("[delegates] reading delegates config failed")
    return []


def register(registry) -> None:
    """Entry point — called once per graph build with the live config."""
    # CRUD API for the console panel (PR2) + the background health prober (PR4).
    # Mounted/started once at process init; the roster they serve is config, which
    # hot-reloads — so the static routes + the loop's per-tick re-read are fine.
    try:
        from .api import build_router

        registry.register_router(build_router(), prefix="")
    except Exception:  # noqa: BLE001 — API is best-effort; the tool still works
        log.exception("[delegates] mounting CRUD API failed")
    try:
        from .health import start as _health_start, stop as _health_stop

        registry.register_surface(_health_start, stop=_health_stop, name="delegate-health")
    except Exception:  # noqa: BLE001 — health is best-effort
        log.exception("[delegates] registering health prober failed")

    delegates = _load_delegates_config()
    if not delegates:
        cfg = registry.config or {}
        nested = cfg.get("delegates")
        if isinstance(nested, list):
            delegates = nested
    reg = DelegateRegistry(delegates)
    if not reg.names():
        # The default state for a fresh install (the plugin is always-on): no
        # delegates declared ⇒ no `delegate_to` tool. Not an anomaly — debug, not warn.
        log.debug(
            "[delegates] no delegates declared — `delegate_to` not registered. Add "
            "entries under `delegates` (docs/guides/delegates.md) or use the Delegates panel."
        )
        return
    registry.register_tool(_build_delegate_to(reg))
    log.info("[delegates] registered delegate_to for %d delegate(s): %s", len(reg.names()), ", ".join(reg.names()))
