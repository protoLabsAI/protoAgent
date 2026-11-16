"""Subagent contracts for discovery and manual launch."""

from __future__ import annotations

from typing import Any

from graph.subagents.config import SUBAGENT_REGISTRY


def list_subagents(config: Any) -> list[dict[str, Any]]:
    """Return UI-safe subagent metadata from registry + config overrides.

    Each row carries ``model`` — the operator's declared pin (a scalar, or an ADR
    0133 fallback chain) — so the console reads a subagent's chain here even for
    ``researcher``, whose pin lives on a typed config field and is deliberately
    excluded from the ``subagent_overrides`` map the Settings form round-trips.

    A worker DISABLED in config is POPPED from ``SUBAGENT_REGISTRY`` by
    ``server.agent_init._apply_config_subagents`` before this runs, so iterating
    the live registry alone would drop it. It is recovered below from the shipped
    static defaults (including retained disabled plugin definitions) + the config overlay and reported
    with ``enabled: false`` — the ADR 0133 M7c Model-roles line marks it "(off)"
    instead of silently losing it.
    """
    overrides = dict(getattr(config, "subagent_overrides", None) or {}) if config is not None else {}

    def _model(name: str, override: Any) -> Any:
        # researcher's pin is a typed SubagentDef field; every other worker's is a dict overlay.
        if override is not None:
            return getattr(override, "model", "")
        sub = overrides.get(name)
        return sub.get("model", "") if isinstance(sub, dict) else ""

    out: list[dict[str, Any]] = []
    for name, registry_def in SUBAGENT_REGISTRY.items():
        override = getattr(config, name, None) if config is not None else None
        sub = overrides.get(name) if isinstance(overrides.get(name), dict) else {}
        tools = list(getattr(override, "tools", registry_def.tools) or [])
        # enabled is authoritative from config — researcher's typed field, else the overlay dict —
        # so a worker still in the live registry but marked off reports enabled:false too.
        enabled = bool(getattr(override, "enabled", sub.get("enabled", True)))
        out.append(
            {
                "name": name,
                "description": registry_def.description,
                "enabled": enabled,
                "tools": tools,
                "default_tools": list(registry_def.tools),
                "max_turns": int(getattr(override, "max_turns", registry_def.max_turns)),
                "default_max_turns": int(registry_def.max_turns),
                "model": _model(name, override),
            }
        )

    # Recover workers a config `enabled: false` popped from the live registry (above). Their
    # shipped defaults and disabled registry snapshots remain available; the overlay (researcher's
    # typed field, or the `subagent_overrides` dict) carries what the operator set.
    from dataclasses import asdict

    from graph.subagents import config as _sub

    bases = {
        value.name: value
        for key, value in vars(_sub).items()
        if key.endswith("_CONFIG") and isinstance(value, _sub.SubagentConfig)
    }
    bases.update(_sub.DISABLED_SUBAGENT_DEFAULTS)
    seen = {row["name"] for row in out}
    disabled: list[tuple[str, dict[str, Any]]] = [
        (name, sub)
        for name, sub in overrides.items()
        if isinstance(sub, dict) and not sub.get("enabled", True)
    ]
    researcher = getattr(config, "researcher", None) if config is not None else None
    if researcher is not None and not getattr(researcher, "enabled", True):
        disabled.append(("researcher", asdict(researcher)))
    for name, sub in disabled:
        if name in seen:
            continue  # still live (apply did not pop it) — already reported with enabled:false
        registry_def = bases.get(name)
        if registry_def is None:
            continue  # an overlay naming a worker with no shipped default — nothing to render
        out.append(
            {
                "name": name,
                "description": registry_def.description,
                "enabled": False,
                "tools": list(sub.get("tools") or registry_def.tools),
                "default_tools": list(registry_def.tools),
                "max_turns": int(sub.get("max_turns") or registry_def.max_turns),
                "default_max_turns": int(registry_def.max_turns),
                "model": sub.get("model", ""),
            }
        )
    return out


async def run_manual_subagent(
    *,
    config: Any,
    knowledge_store: Any,
    scheduler: Any,
    description: str,
    prompt: str,
    subagent_type: str = "researcher",
    extra_tools: Any = None,
    session_id: str = "",
) -> str:
    """Run one manually launched subagent task.

    ``extra_tools`` (plugin + MCP tools) are forwarded so an out-of-graph
    subagent sees the same tool surface as the lead graph — without them a
    plugin-tool allowlist silently degrades to "not a valid tool".
    """
    if config is None:
        raise RuntimeError("agent config is not loaded")
    if not prompt or not prompt.strip():
        raise ValueError("prompt is required")
    if not description or not description.strip():
        description = prompt.strip()[:80]

    from graph.agent import run_manual_subagent as _run_manual_subagent

    return await _run_manual_subagent(
        config,
        knowledge_store=knowledge_store,
        scheduler=scheduler,
        description=description,
        prompt=prompt,
        subagent_type=subagent_type,
        extra_tools=extra_tools,
        session_id=session_id,
    )


async def run_manual_subagent_batch(
    *,
    config: Any,
    knowledge_store: Any,
    scheduler: Any,
    tasks: list[dict[str, Any]],
    extra_tools: Any = None,
) -> str:
    """Run a manually launched batch of independent subagent tasks."""
    if config is None:
        raise RuntimeError("agent config is not loaded")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("tasks must be a non-empty list")

    from graph.agent import run_manual_subagent_batch as _run_manual_subagent_batch

    return await _run_manual_subagent_batch(
        config,
        knowledge_store=knowledge_store,
        scheduler=scheduler,
        tasks=tasks,
        extra_tools=extra_tools,
    )
