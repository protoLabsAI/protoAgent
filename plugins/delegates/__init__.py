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
from typing import Annotated, Any

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from .adapters import DelegateError
from .registry import DelegateRegistry

log = logging.getLogger("protoagent.plugins.delegates")


def _build_delegate_to(registry: DelegateRegistry):
    listing = registry.listing()

    @tool
    async def delegate_to(
        target: str,
        query: str,
        background: bool = False,
        state: Annotated[Any, InjectedState] = None,
    ) -> str:
        """Hand a question or task to one of your configured delegates and return its reply.

        Use this to reach beyond your own context: ask a fleet **agent**, consult
        another **model endpoint**, or hand a repo-scoped coding job to a **coding
        agent**. Pick the delegate whose description best fits the task.

        By default this WAITS for the delegate's reply (fine for a quick consult).
        For a LONG delegation — a coding agent building a PR, a deep-research run —
        set ``background=True``: the delegation runs detached, you get a job handle
        back immediately, and the delegate's reply is delivered to you when it
        finishes (you don't hold your turn open waiting). In-flight background jobs
        are tracked in the background panel (``GET /api/background``). Prefer
        ``background=True`` whenever the delegate might take minutes, or when you
        want to fan out several delegations without blocking on each.

        Args:
            target: the delegate name (see the available list in this tool's
                description).
            query: the full, self-contained question or instruction — the delegate
                does not see this conversation, so restate what it needs.
            background: run the delegation detached and get the reply back on
                completion, instead of waiting inline (default False).
        """
        if not str(query).strip():
            return "Error: `query` is empty — give the delegate something to do."
        if background:
            return await _spawn_background_delegation(registry, target, query, state)
        try:
            return await registry.dispatch(target, query)
        except DelegateError as exc:
            return f"Error: {exc}"
        except Exception as exc:  # noqa: BLE001 — surface as a tool error string
            log.warning("[delegates] dispatch to %r failed: %s", target, exc)
            return f"Error: delegate {target!r} failed: {type(exc).__name__}: {exc}"

    delegate_to.description = f"{delegate_to.description}\n\nAvailable delegates: {listing or '(none configured)'}."
    return delegate_to


async def _spawn_background_delegation(registry: DelegateRegistry, target: str, query: str, state: Any) -> str:
    """Run a delegation as a detached background job (ADR 0050): return a handle now and
    drain the delegate's reply back into the spawning session on completion — the same
    durable store + concurrency cap + drain-on-next-turn notification that
    ``task(run_in_background=True)`` uses, so a slow delegate (a coding agent building a
    PR) never holds the caller's turn open.

    Degrades gracefully: an unknown target fails fast (no orphan job), and if no
    ``BackgroundManager`` is wired (a lean/CLI/test context) it falls back to a plain
    inline dispatch so ``background=True`` is never worse than the synchronous path.
    """
    if registry.get(target) is None:
        return f"Error: unknown delegate {target!r}. Available: {registry.listing() or '(none)'}."

    try:
        from runtime.state import STATE

        mgr = getattr(STATE, "background_mgr", None)
    except Exception:  # noqa: BLE001 — no runtime state (e.g. a unit test) → inline
        mgr = None
    if mgr is None:
        return await registry.dispatch(target, query)

    try:
        from tools.lg_tools import _session_id_from

        # Injected graph state, not the tracing contextvar (empty in a tool body) — the
        # session id is what the completion drains back to (ADR 0050).
        session = _session_id_from(state) or ""
    except Exception:  # noqa: BLE001 — best-effort; job still runs, drain is degraded
        session = ""

    async def _work() -> str:
        return await registry.dispatch(target, query)

    snippet = " ".join(query.split())[:80]
    job_id = await mgr.spawn_work(
        origin_session=session,
        kind="delegate",
        description=f"delegate → {target}: {snippet}",
        detail=query,
        work=_work,
    )
    return (
        f"Started a background delegation to {target!r} (job `{job_id}`). It runs detached — "
        f"its reply comes back to me when it finishes, so I don't need to wait. In-flight "
        f"background jobs are listed in the background panel (GET /api/background)."
    )


def _build_list_agents(registry: DelegateRegistry):
    @tool
    def list_agents() -> str:
        """List the agents/delegates you can reach with `delegate_to`, with each one's
        type, description, and current reachability (🟢 reachable · 🔴 down · ⚪ unknown).

        Read this before assuming who's available — the roster is configuration, not a
        fixed set, and it changes as delegates are added or removed."""
        try:
            from .health import health_snapshot

            health = health_snapshot() or {}
        except Exception:  # noqa: BLE001 — prober not running; reachability stays unknown
            health = {}
        roster = registry.roster()
        if not roster:
            return "No delegates configured."
        lines = []
        for r in roster:
            ok = (health.get(r["name"]) or {}).get("ok")
            badge = "🟢" if ok is True else "🔴" if ok is False else "⚪"
            typ = f" ({r['type']})" if r["type"] else ""
            desc = f" — {r['description']}" if r["description"] else ""
            lines.append(f"{badge} {r['name']}{typ}{desc}")
        return "\n".join(lines)

    return list_agents


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
    registry.register_tool(_build_list_agents(reg))
    log.info("[delegates] registered delegate_to + list_agents for %d delegate(s): %s",
             len(reg.names()), ", ".join(reg.names()))
