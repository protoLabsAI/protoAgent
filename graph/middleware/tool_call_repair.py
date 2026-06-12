"""Repair a chat thread left with a dangling tool_call, before the model runs.

A turn can persist an assistant message whose ``tool_calls`` were never answered:
a tool that hung while the user sent the next message, an interrupted/crashed
turn, a stream that dropped the tool result. The provider then rejects EVERY
later turn in that thread:

    An assistant message with 'tool_calls' must be followed by tool messages
    responding to each 'tool_call_id'. (insufficient tool messages …)  -> HTTP 400

so the chat is permanently bricked until it's deleted. This middleware makes the
agent self-heal: before each model call it scans the history and, for any
tool_call that has no matching ``ToolMessage``, drops that dangling call from its
assistant message (replacing the message in place by id) so the request is valid
again. Already-answered calls and message text are preserved.

It is a **no-op on a healthy history** — ``before_model`` returns ``None`` unless
there is an actual orphan — so it can never alter a normal turn; it only ever
touches a thread that would otherwise 400.
"""

from __future__ import annotations

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage


def _tc_id(tc) -> str | None:
    return tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)


def repair_messages(messages: list) -> list:
    """Return replacement messages (same ids) for any assistant message that
    carries an unanswered tool_call. Empty list ⇒ nothing to repair."""
    answered = {
        m.tool_call_id
        for m in messages
        if isinstance(m, ToolMessage) and getattr(m, "tool_call_id", None)
    }
    repairs: list = []
    for m in messages:
        tool_calls = getattr(m, "tool_calls", None) or []
        if not tool_calls:
            continue
        kept = [tc for tc in tool_calls if _tc_id(tc) in answered]
        if len(kept) == len(tool_calls):
            continue  # every call answered — fine
        # Drop the dangling call(s); keep the answered ones + the message text.
        content = m.content if isinstance(m.content, str) else ""
        if not kept and not content:
            content = "[tool call abandoned — no result was produced]"
        repairs.append(m.model_copy(update={"tool_calls": kept, "content": content}))
    return repairs


class ToolCallRepairMiddleware(AgentMiddleware):
    """Drop unanswered tool_calls from history before the model call (self-heal a
    thread that would otherwise 400 forever). No-op on a healthy history."""

    def _repair(self, state):
        messages = state.get("messages") or []
        repairs = repair_messages(messages)
        return {"messages": repairs} if repairs else None

    def before_model(self, state, runtime):  # type: ignore[override]
        return self._repair(state)

    async def abefore_model(self, state, runtime):  # type: ignore[override]
        return self._repair(state)
