"""Plugin SDK — the stable surface a plugin uses to TAP CORE capabilities.

The plugin contract has two halves:

  • Contribution — ``PluginRegistry.register_*`` (tools, routers, recipe dirs, goal
    verifiers, …): what a plugin ADDS to the host.
  • Consumption — THIS module: what a plugin CALLS back into the host (run a subagent,
    read the live config, …).

Plugins import ``from graph.sdk import …`` rather than reaching into ``graph.agent`` /
``runtime.state`` internals, so core can refactor underneath them without breaking
plugins. Keep this surface **small, stable, and deliberate** — it's the seam we lean on
as plugins tap core more aggressively (the workflows plugin is the first real consumer:
its engine injects ``run_subagent`` as the per-step runner).
"""

from __future__ import annotations

from typing import Any

from runtime.state import STATE


def config() -> Any:
    """The live runtime ``LangGraphConfig``."""
    return STATE.graph_config


def subagent_types() -> set[str]:
    """Ids of the configured subagents — for validating/listing recipe steps."""
    from graph.subagents.config import SUBAGENT_REGISTRY

    return set(SUBAGENT_REGISTRY)


async def run_subagent(
    subagent_type: str,
    prompt: str,
    *,
    description: str,
    extra_tools: Any = None,
    truncate: int | None = None,
) -> str:
    """Run a subagent to completion and return its text output.

    Pulls the config + knowledge store + scheduler from runtime state, so a plugin
    tool only supplies the subagent + prompt. This is the capability the workflows
    plugin's engine injects as its per-step ``run_step``.
    """
    from graph.agent import run_manual_subagent

    return await run_manual_subagent(
        STATE.graph_config,
        knowledge_store=getattr(STATE, "knowledge_store", None),
        scheduler=getattr(STATE, "scheduler", None),
        description=description,
        prompt=prompt,
        subagent_type=subagent_type,
        extra_tools=extra_tools,
        truncate=truncate,
    )
