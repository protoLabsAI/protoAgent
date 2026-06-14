"""End the turn after the agent calls the ``wait`` tool.

The ``wait`` tool (tools/lg_tools.py) schedules a one-shot resume and is meant to
*yield* — the agent should stop here and be re-triggered later by the scheduler,
instead of busy-polling a status tool (which burns the whole recursion budget in
one turn). LangChain's ``create_agent`` has no built-in "a tool ended the turn"
signal, so this middleware provides it: once ``wait`` has run, the next
``before_model`` jumps straight to ``end`` instead of looping back to the model.

Detection is precise — it only fires when a ``wait`` ToolMessage sits in the
trailing tool-result block (i.e. ``wait`` just ran in *this* turn). On a fresh
turn the last message is the new stimulus, so it's a no-op; it never short-
circuits a turn that didn't call ``wait``.
"""

from __future__ import annotations

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import ToolMessage

WAIT_TOOL_NAME = "wait"


def _just_waited(messages: list) -> bool:
    """True if the trailing contiguous ToolMessage block contains a *successful*
    wait-tool result — i.e. wait ran in this model cycle and we should yield. A
    failed wait (scheduling error) does NOT yield, so the agent sees the error
    and can react instead of silently dropping the task."""
    for m in reversed(messages or []):
        if isinstance(m, ToolMessage):
            if (getattr(m, "name", None) or "") == WAIT_TOOL_NAME:
                if getattr(m, "status", None) == "error":
                    return False
                content = m.content if isinstance(m.content, str) else ""
                return not content.startswith("Error:")
            continue  # keep scanning the trailing tool block (parallel tool calls)
        break  # hit a non-ToolMessage → end of the trailing block
    return False


class WaitYieldMiddleware(AgentMiddleware):
    """Jump to ``end`` once the ``wait`` tool has run, so the turn yields instead
    of returning to the model. No-op on every turn that didn't call ``wait``."""

    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime):  # type: ignore[override]
        if _just_waited(state.get("messages") or []):
            return {"jump_to": "end"}
        return None

    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state, runtime):  # type: ignore[override]
        if _just_waited(state.get("messages") or []):
            return {"jump_to": "end"}
        return None
