"""Example protoAgent plugin.

A plugin is a directory with a ``protoagent.plugin.yaml`` manifest and a module
exposing ``register(registry)``. The registry collects what the plugin
contributes. This example shows **all five** contribution types (ADR 0001 + 0018):

- a **tool** (``hello``),
- a bundled **SKILL.md** directory (``skills/``),
- an HTTP **route** (``GET /plugins/hello/ping``),
- a lifecycle **surface** (logs on startup/shutdown),
- a **subagent** (``hello_helper``).

Enable it with ``plugins: { enabled: [hello] }`` in config. A fork drops a
directory like this in ``plugins/`` and never edits core ``server.py``.
"""

from __future__ import annotations

import logging

from langchain_core.tools import tool

log = logging.getLogger("protoagent.plugins.hello")


@tool
async def hello(name: str = "world") -> str:
    """Return a friendly greeting — proof the plugin loaded and its tool is live."""
    return f"Hello, {name}! (from the example protoAgent plugin)"


def _build_router(greeting: str):
    """A tiny FastAPI router — mounted at ``/plugins/hello`` (ADR 0018). Closes
    over the plugin's configured greeting (ADR 0019) to prove it reads its config."""
    from fastapi import APIRouter

    router = APIRouter()

    @router.get("/ping")
    async def _ping() -> dict:
        return {"ok": True, "plugin": "hello", "greeting": greeting}

    return router


async def _surface_start() -> None:
    """A no-op lifecycle surface — a real one would open a gateway/listener here
    (it runs on the server loop, like the Discord gateway)."""
    log.info("[hello] example surface started")


async def _surface_stop() -> None:
    log.info("[hello] example surface stopped")


def _build_subagent():
    """A minimal delegate the lead agent can call via ``task``/``task_batch``."""
    from graph.subagents.config import SubagentConfig

    return SubagentConfig(
        name="hello_helper",
        description="Example plugin subagent — echoes a friendly status. Proof a "
        "plugin can register a delegate without editing SUBAGENT_REGISTRY.",
        system_prompt="You are the hello_helper, a tiny example subagent. Reply "
        "briefly and cheerfully, then stop.",
        tools=["current_time"],
    )


def register(registry) -> None:
    """Entry point — called once at load with a PluginRegistry."""
    greeting = registry.config.get("greeting", "Hello")   # plugin config (ADR 0019)
    registry.register_tool(hello)
    registry.register_skill_dir("skills")            # bundled SKILL.md folder
    registry.register_router(_build_router(greeting))  # → /plugins/hello/ping (ADR 0018)
    registry.register_surface(_surface_start, stop=_surface_stop, name="hello-surface")
    registry.register_subagent(_build_subagent())
