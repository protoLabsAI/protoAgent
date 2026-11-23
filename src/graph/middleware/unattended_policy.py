"""UnattendedPolicyMiddleware — applies the allow / ask / never tiers (ADR 0124 D5).

A no-op unless the turn driver marked the invocation unattended
(``graph.unattended_policy.unattended_turn``), so an operator's turn is untouched.

On an unattended turn, around each tool call:

* a tool the operator listed under ``unattended.never`` is refused before it runs;
* a tool that pauses for approval (``interrupt({"kind": "approval", ...})``) is ruled on by
  tier. **allow**: the pause is stamped and the turn driver approves it. **ask**: the pause
  is replaced by one ``request_decision`` payload showing the tool and its arguments, which
  the existing decision path parks and resumes. **never**, or no policy: the call returns a
  refusal and nothing is parked.

A pause that is not an approval (``ask_human``, a form) is a question, not an action. With
no policy it passes through, and the driver answers it with the no-operator notice as before;
listed under ``ask`` it becomes a decision; listed under ``never`` it is refused.
A decision pause (``kind: "decision"``, e.g. a ``request_decision`` paused before #5678
slice 10 removed that tool's pause) always passes through.

Each tool call runs as its own graph task, so refusing one never disturbs another call's
pause in the same model turn.
"""

from __future__ import annotations

import logging

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.errors import GraphInterrupt
from langgraph.types import Interrupt

from graph import unattended_policy as policy

log = logging.getLogger(__name__)


class UnattendedPolicyMiddleware(AgentMiddleware):
    """Rule on every approval pause of an unattended turn."""

    def __init__(self, config):
        super().__init__()
        self.config = config

    def _refuse(self, request, tool: str, action: str, tier: str | None) -> ToolMessage:
        log.warning("[unattended] refused %s%s (tier: %s)", tool, f":{action}" if action else "", tier or "default")
        return ToolMessage(
            content=policy.refusal_text(tool, action, tier),
            tool_call_id=request.tool_call.get("id", ""),
            name=tool,
        )

    def _before(self, request) -> ToolMessage | None:
        """Refuse a never-tier tool before it runs. Skipped when a tier names one of the
        tool's actions: the tier then depends on the action its approval card states."""
        if not policy.is_unattended_turn():
            return None
        tool = str(request.tool_call.get("name") or "")
        if policy.has_action_entries(self.config, tool):
            return None
        if policy.resolve_tier(self.config, tool) == policy.NEVER:
            return self._refuse(request, tool, "", policy.NEVER)
        return None

    def _on_interrupt(self, request, exc: GraphInterrupt):
        """Rule on a pause the tool raised: a refusal to return, or the interrupt to raise."""
        if not policy.is_unattended_turn():
            return exc
        interrupts = exc.args[0] if exc.args else ()
        if not isinstance(interrupts, (tuple, list)) or len(interrupts) != 1:
            return exc
        pending = interrupts[0]
        payload = getattr(pending, "value", None)
        if not isinstance(payload, dict):
            payload = {"question": str(payload)} if payload is not None else {}
        kind = str(payload.get("kind") or "").strip().lower()
        if kind == "decision":
            return exc  # a decision pause parks on every turn
        tool = str(request.tool_call.get("name") or "")
        args = request.tool_call.get("args") or {}
        call_id = str(request.tool_call.get("id") or "")
        action = str(payload.get("action") or "").strip()
        tier = policy.resolve_tier(self.config, tool, action)
        if tier == policy.ALLOW and kind == "approval":
            log.info("[unattended] allowed %s%s by policy", tool, f":{action}" if action else "")
            value = policy.allow_payload(payload, tool=tool, args=args, action=action, call_id=call_id)
        elif tier == policy.ASK:
            log.info("[unattended] %s%s needs a decision", tool, f":{action}" if action else "")
            value = policy.ask_payload(payload, tool=tool, args=args, action=action, call_id=call_id)
        elif tier == policy.NEVER or kind == "approval":
            return self._refuse(request, tool, action, tier)
        else:
            return exc  # an unlisted question: the driver answers it with the no-operator notice
        return GraphInterrupt((Interrupt(value=value, id=pending.id),))

    def wrap_tool_call(self, request, handler):
        refused = self._before(request)
        if refused is not None:
            return refused
        try:
            return handler(request)
        except GraphInterrupt as exc:
            ruled = self._on_interrupt(request, exc)
            if ruled is exc:
                raise
            if isinstance(ruled, ToolMessage):
                return ruled
            raise ruled from None

    async def awrap_tool_call(self, request, handler):
        refused = self._before(request)
        if refused is not None:
            return refused
        try:
            return await handler(request)
        except GraphInterrupt as exc:
            ruled = self._on_interrupt(request, exc)
            if ruled is exc:
                raise
            if isinstance(ruled, ToolMessage):
                return ruled
            raise ruled from None
