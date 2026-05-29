"""Example protoAgent plugin.

A plugin is a directory with a ``protoagent.plugin.yaml`` manifest and a
module exposing ``register(registry)``. The registry collects what the plugin
contributes — here: one tool and one bundled SKILL.md skill directory.

Enable it with ``plugins: { enabled: [hello] }`` in config.
"""

from __future__ import annotations

from langchain_core.tools import tool


@tool
async def hello(name: str = "world") -> str:
    """Return a friendly greeting — proof the plugin loaded and its tool is live."""
    return f"Hello, {name}! (from the example protoAgent plugin)"


def register(registry) -> None:
    """Entry point — called once at load with a PluginRegistry."""
    registry.register_tool(hello)
    # Bundle a SKILL.md directory shipped alongside this plugin.
    registry.register_skill_dir("skills")
