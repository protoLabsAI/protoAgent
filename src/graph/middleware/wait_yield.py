"""End the turn after the agent calls the ``wait`` tool.

The ``wait`` tool (tools/scheduler_tools.py) schedules a one-shot resume and is meant to
*yield* — the agent should stop here and be re-triggered later by the scheduler,
instead of busy-polling a status tool (which burns the whole recursion budget in
one turn). LangChain's ``create_agent`` has no built-in "a tool ended the turn"
signal, so this middleware provides it: once ``wait`` has run, the next
``before_model`` jumps straight to ``end`` instead of looping back to the model.

It is also where a GOAL-DRIVEN turn ends once the goal's verifier passes mid-turn
(``graph.middleware.goal_checkpoint``): probed (debounced) after each tool round; on a pass
the goal is recorded achieved and ONE closing model call (tools unbound) writes a short
summary, so a turn that already met its goal doesn't run on re-announcing "already
complete". Async hooks only — the verifier is async.

Detection is precise — it only fires when a ``wait`` ToolMessage sits in the
trailing tool-result block (i.e. ``wait`` just ran in *this* turn). On a fresh
turn the last message is the new stimulus, so it's a no-op; it never short-
circuits a turn that didn't call ``wait``.
"""

from __future__ import annotations

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import ToolMessage

from graph.middleware.goal_checkpoint import closing_call, goal_checkpoint

WAIT_TOOL_NAME = "wait"

# The ``wait`` tool's success confirmation (tools/scheduler_tools.py) — "Wait scheduled:
# <duration>. Will resume to: <then>". It is a status line for the MODEL; the resume
# instruction in it is the agent talking to its future self.
_WAIT_CONFIRMATION_PREFIX = "Wait scheduled: "
_WAIT_RESUME_MARK = ". Will resume to: "


def wait_turn_reply(tool_output: str) -> str | None:
    """The chat reply for a turn that yielded on ``wait`` without saying anything, or None
    when ``tool_output`` isn't a wait confirmation.

    A turn that ends on ``wait`` often has no assistant text — the model calls the tool and
    the turn ends here — so the answer used to fall back to the raw confirmation. In chat
    that read as the agent prompting itself: the operator saw "Wait scheduled: 45 minutes.
    Will resume to: Check on …" as the reply. The tool card already shows the resume plan;
    the reply only needs to say when the agent is back."""
    text = (tool_output or "").strip()
    if not text.startswith(_WAIT_CONFIRMATION_PREFIX):
        return None
    duration = text[len(_WAIT_CONFIRMATION_PREFIX) :].split(_WAIT_RESUME_MARK, 1)[0].strip().rstrip(".")
    if not duration:
        return None
    return f"I'll pick this back up in {duration}."


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
        return await goal_checkpoint(state)

    async def awrap_model_call(self, request, handler):  # type: ignore[override]
        # The goal checkpoint's closing call runs with no tools bound (a no-op otherwise).
        # A wrap hook adds no graph node.
        return await closing_call(request, handler)
