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

# Re-export the supervised background-task helper as part of the consumption surface, so a
# plugin writes `from graph.sdk import supervise` for a self-perpetuating, watchdog-backed
# engine instead of hand-rolling task/restart machinery (graph/supervisor.py is host-free).
from graph.supervisor import Supervisor, supervise  # noqa: F401


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


async def complete(
    prompt: str, *, system: str | None = None, model_name: str | None = None
) -> str:
    """Run a single **bare** LLM completion and return the text — no tools, no agent
    loop, no persona, no memory. The clean primitive for a plugin that just needs the
    model to answer a prompt (e.g. an interactive artifact calling back to the agent,
    a one-shot classifier/summarizer). Distinct from :func:`run_subagent`, which runs a
    full tool-using subagent. Uses the live config's model through the gateway; pass
    ``model_name`` to target a different model on the same gateway, ``system`` for a
    system instruction.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    from graph.llm import create_llm

    llm = create_llm(STATE.graph_config, model_name=model_name)
    messages: list[Any] = []
    if system:
        messages.append(SystemMessage(system))
    messages.append(HumanMessage(prompt))
    resp = await llm.ainvoke(messages)
    content = getattr(resp, "content", resp)
    return content if isinstance(content, str) else str(content)
