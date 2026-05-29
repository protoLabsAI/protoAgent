"""Subagent contracts for discovery and manual launch."""

from __future__ import annotations

from typing import Any

from graph.subagents.config import SUBAGENT_REGISTRY


def list_subagents(config: Any) -> list[dict[str, Any]]:
    """Return UI-safe subagent metadata from registry + config overrides."""
    out: list[dict[str, Any]] = []
    for name, registry_def in SUBAGENT_REGISTRY.items():
        override = getattr(config, name, None) if config is not None else None
        tools = list(getattr(override, "tools", registry_def.tools) or [])
        out.append({
            "name": name,
            "description": registry_def.description,
            "enabled": bool(getattr(override, "enabled", True)),
            "tools": tools,
            "default_tools": list(registry_def.tools),
            "max_turns": int(getattr(override, "max_turns", registry_def.max_turns)),
            "default_max_turns": int(registry_def.max_turns),
            "allow_skill_emission": bool(registry_def.allow_skill_emission),
        })
    return out


async def run_manual_subagent(
    *,
    config: Any,
    knowledge_store: Any,
    scheduler: Any,
    description: str,
    prompt: str,
    subagent_type: str = "researcher",
    emit_skill: bool = False,
) -> str:
    """Run one manually launched subagent task."""
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
        emit_skill=emit_skill,
    )


async def run_manual_subagent_batch(
    *,
    config: Any,
    knowledge_store: Any,
    scheduler: Any,
    tasks: list[dict[str, Any]],
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
    )
