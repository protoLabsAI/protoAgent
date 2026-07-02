"""SubagentFenceMiddleware — per-turn tool fence for detached subagent runs (#1639).

A detached background job runs the FULL lead graph (ADR 0050's self-POST substrate),
so the per-subagent tool scoping the in-graph ``task`` path enforces never applied —
the subagent's ``tools`` allowlist was role guidance only. The fire path now stamps
the resolved allowlist on the turn's state (``subagent_fence`` — carried A2A message
metadata → request metadata → state, the same per-turn channel ``model``/``incognito``
ride), and this gate blocks any tool call outside it with the enforcement-style
``ToolMessage`` block, so the model reads the denial and adapts. A turn without the
state key is untouched — ordinary chat turns pay one dict lookup.
"""

from __future__ import annotations

import logging

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

logger = logging.getLogger(__name__)


class SubagentFenceMiddleware(AgentMiddleware):
    """Block tool calls outside the turn's stamped subagent allowlist."""

    def _deny_reason(self, request) -> str | None:
        state = getattr(request, "state", None) or {}
        fence = state.get("subagent_fence")
        if not fence:
            return None
        name = request.tool_call.get("name", "")
        if name in fence:
            return None
        return (
            f"tool '{name}' is outside this background subagent's allowlist "
            f"({', '.join(sorted(fence))}) — work within the allowed tools."
        )

    def _blocked(self, request, reason: str) -> ToolMessage:
        logger.info("[subagent-fence] blocked %s: %s", request.tool_call.get("name", "?"), reason)
        return ToolMessage(
            content=f"Blocked by policy: {reason}",
            tool_call_id=request.tool_call.get("id", ""),
            status="error",  # render as a failure card, matching the enforcement gate
        )

    def wrap_tool_call(self, request, handler):
        reason = self._deny_reason(request)
        if reason:
            return self._blocked(request, reason)
        return handler(request)

    async def awrap_tool_call(self, request, handler):
        reason = self._deny_reason(request)
        if reason:
            return self._blocked(request, reason)
        return await handler(request)
