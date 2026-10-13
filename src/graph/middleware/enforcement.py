"""EnforcementMiddleware — a pre-execution gate for tool calls.

A generic safety gate (the protoAgent template otherwise has audit /
knowledge / memory / message-capture middleware but no way to *block* a
tool call). Before a tool runs, the gate checks, in order:

  1. deny list — exact tool names that are never allowed,
  2. a pluggable ``predicate(tool_name, args) -> reason | None`` for
     fork-specific policy (scope/mode/cost/etc.),
  3. a sliding-window rate limit per tool.

If any check returns a reason, the tool is **not executed** — instead a
``ToolMessage`` carrying the reason is returned, preserving the
tool_use/tool_result pairing the model expects (so the agent can read the
denial and adapt). Place it first in the chain so it gates before execution.

Mechanism backported from the protoLabs fleet (pwnDeck
``graph/middleware/enforcement.py``); the domain policy (pentest scope /
kill-chain phases) is intentionally dropped — core ships the gate + a
pluggable predicate, off by default.
"""

from __future__ import annotations

import logging
from typing import Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from enforcement.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

# predicate(tool_name, args) -> deny-reason string, or None to allow.
EnforcementPredicate = Callable[[str, dict], str | None]


class EnforcementMiddleware(AgentMiddleware):
    """Block disallowed / rate-limited tool calls before they execute."""

    def __init__(
        self,
        disallowed_tools: set[str] | list[str] | None = None,
        rate_limits: dict | None = None,
        predicate: EnforcementPredicate | None = None,
    ):
        super().__init__()
        self._denied = set(disallowed_tools or ())
        self._predicate = predicate
        self._limiter = RateLimiter(rate_limits) if rate_limits else None

    def _enforce(self, request) -> str | None:
        """Return a deny-reason for this tool call, or None to allow."""
        name = request.tool_call.get("name", "")
        args = request.tool_call.get("args", {}) or {}

        if name in self._denied:
            return f"Tool '{name}' is disabled by policy."
        if self._predicate is not None:
            reason = self._predicate(name, args)
            if reason:
                return reason
        if self._limiter is not None:
            allowed, reason = self._limiter.check(name)
            if not allowed:
                return reason
        return None

    def _blocked(self, request, reason: str) -> ToolMessage:
        logger.info("[enforcement] blocked %s: %s", request.tool_call.get("name", "?"), reason)
        return ToolMessage(
            content=f"Blocked by policy: {reason}",
            tool_call_id=request.tool_call.get("id", ""),
            status="error",  # the chat card renders a policy block as a failure (X), not green
        )

    def wrap_tool_call(self, request, handler):
        reason = self._enforce(request)
        if reason:
            return self._blocked(request, reason)
        return handler(request)

    async def awrap_tool_call(self, request, handler):
        reason = self._enforce(request)
        if reason:
            return self._blocked(request, reason)
        return await handler(request)
