"""The registry handed to a plugin's ``register(registry)`` function.

A plugin contributes capabilities by calling methods on this object; the loader
collects them and threads them into the graph. Keeping the surface small and
explicit means a plugin never imports protoAgent internals to extend it.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("protoagent.plugins")


class PluginRegistry:
    """Collects a single plugin's contributions during ``register()``.

    Current contribution types: ``tools`` (LangChain ``BaseTool``s) and
    ``skill_dirs`` (directories of ``SKILL.md`` skills to load). Subagent and
    middleware contributions are planned follow-ups.
    """

    def __init__(self, plugin_id: str, plugin_dir: Path):
        self.plugin_id = plugin_id
        self.plugin_dir = plugin_dir
        self.tools: list = []
        self.skill_dirs: list[Path] = []

    def register_tool(self, tool) -> None:
        """Expose a LangChain tool to the agent."""
        if tool is None or not hasattr(tool, "name"):
            log.warning("[plugins] %s: register_tool got a non-tool: %r", self.plugin_id, tool)
            return
        self.tools.append(tool)

    def register_tools(self, tools) -> None:
        """Convenience: register an iterable of tools."""
        for tool in tools or []:
            self.register_tool(tool)

    def register_skill_dir(self, path: str | Path) -> None:
        """Add a directory of ``SKILL.md`` skills bundled with the plugin.

        Relative paths resolve against the plugin's own directory.
        """
        p = Path(path)
        if not p.is_absolute():
            p = self.plugin_dir / p
        self.skill_dirs.append(p)
