"""A tool's ToolException is a failed CALL, never a failed TURN.

LangChain only turns a raised ``ToolException`` into a tool result (``status="error"``) when the
tool sets ``handle_tool_error``; otherwise ``BaseTool.run`` re-raises it, langgraph's ToolNode
default handler re-raises anything that isn't a ``ToolInvocationError``, and the whole agent turn
dies. ``run_command`` raised ``ToolException`` for a timeout or spawn error with no handler, so a
slow command killed the operator's turn instead of telling the model "that command timed out"
(found 2026-11-08 while fixing #4862's refusal telemetry; reproduced with ``create_agent``).

``return_tool_exceptions`` sets ``handle_tool_error=True`` on every final tool that hasn't chosen
its own handler (MCP tools keep theirs). Only ``ToolException`` is affected: any other exception
still propagates, so genuine bugs stay loud.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def return_tool_exceptions(tools: list) -> list:
    """Make every tool in ``tools`` return its ``ToolException`` text as an error result. Mutates
    the tool objects (subagents share them) and returns the same list for chaining."""
    for tool in tools or []:
        if getattr(tool, "handle_tool_error", None):
            continue  # the tool chose its own handler (e.g. MCP's)
        try:
            tool.handle_tool_error = True
        except Exception:  # noqa: BLE001 — an exotic tool object: leave it as it was
            log.debug("[tools] could not set handle_tool_error on %r", getattr(tool, "name", tool))
    return tools
