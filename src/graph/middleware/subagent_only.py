"""SubagentOnlyMiddleware — keeps ``tools.subagent_only`` tools off the lead (ADR 0117).

The held tools stay in the graph's ToolNode, because a detached background subagent
(``task(run_in_background=True)``, auto-background) runs THIS graph under a
``subagent_fence`` stamped from its allowlist (#1639), so it needs them executable here.
What changes is who can reach them:

* **Unfenced pass = the lead.** ``wrap_model_call`` drops the held schemas from the
  request, so the lead model never sees them, and ``wrap_tool_call`` blocks a call to
  one (a hallucinated or replayed name) with a ToolMessage that says to delegate.
* **Fenced pass = a subagent (or a peer channel).** Untouched here: the fence already
  trims schemas to its allowlist and blocks everything outside it
  (``SubagentFenceMiddleware``), so a held tool runs exactly when the fence names it.

In-graph ``task`` runs never reach this graph: ``_run_subagent`` builds a graph of its
own from the tool map snapshotted before the lead's split.
"""

from __future__ import annotations

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from graph.middleware.subagent_fence import _bound_tool_name, _state_fence


class SubagentOnlyMiddleware(AgentMiddleware):
    """Hide and block the held tools on every unfenced (lead) pass."""

    def __init__(self, held: frozenset[str] | set[str]):
        super().__init__()
        self.held = frozenset(str(n) for n in held)

    def _hide(self, request):
        if _state_fence(request):
            return request
        tools = getattr(request, "tools", None) or []
        kept = [t for t in tools if _bound_tool_name(t) not in self.held]
        return request if len(kept) == len(tools) else request.override(tools=kept)

    def _blocked(self, request) -> ToolMessage | None:
        name = request.tool_call.get("name", "")
        if name not in self.held or _state_fence(request):
            return None
        return ToolMessage(
            content=(
                f"Blocked by policy: tool '{name}' is subagent-only on this agent — delegate "
                "with `task` to the subagent that owns it."
            ),
            tool_call_id=request.tool_call.get("id", ""),
            status="error",
        )

    def wrap_model_call(self, request, handler):
        return handler(self._hide(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._hide(request))

    def wrap_tool_call(self, request, handler):
        return self._blocked(request) or handler(request)

    async def awrap_tool_call(self, request, handler):
        return self._blocked(request) or await handler(request)
