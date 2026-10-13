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

**Out-of-order answers.** An answered call can still 400 if its ``ToolMessage`` isn't
the next thing after the assistant message. Anthropic requires the ``tool_result``
blocks to lead the very next user turn, and rejects anything in between:

    messages.N: `tool_use` ids were found without `tool_result` blocks immediately after

A foreground ``delegate_to`` used to record its room envelopes (``<room-message>``
human turns) *before* its ``ToolMessage``, which bricked every later turn of that chat
on a native Anthropic model. ``reorder_tool_results`` repairs such a history for the
model request only (``wrap_model_call``): each call's answers are pulled up to sit
directly after it and whatever was interleaved follows them, in its original order.
The checkpointed history is untouched (a reducer can't reorder by id), and a healthy
history passes through as the same list.
"""

from __future__ import annotations

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage


def _tc_id(tc) -> str | None:
    return tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)


def repair_messages(messages: list) -> list:
    """Return replacement messages (same ids) for any assistant message that
    carries an unanswered tool_call. Empty list ⇒ nothing to repair."""
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage) and getattr(m, "tool_call_id", None)}
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


def reorder_tool_results(messages: list) -> list | None:
    """``messages`` with every assistant tool call's answers moved to directly follow
    it, or ``None`` when nothing is out of order (the common case).

    For each assistant message with tool calls, the scan runs forward to the next
    assistant message: that call's ``ToolMessage``s are collected, and anything else met
    on the way (a room envelope, a steering note) is deferred to just after them. Only
    a group whose answers were actually preceded by something else is rewritten.
    """
    msgs = list(messages)
    out: list = []
    changed = False
    i, n = 0, len(msgs)
    while i < n:
        m = msgs[i]
        out.append(m)
        i += 1
        if not isinstance(m, AIMessage):
            continue
        pending = {cid for cid in (_tc_id(tc) for tc in (getattr(m, "tool_calls", None) or [])) if cid}
        if not pending:
            continue
        results: list = []
        deferred: list = []
        j = i
        while j < n and pending:
            x = msgs[j]
            if isinstance(x, AIMessage):
                break  # the next assistant turn — this call's answers aren't coming
            if isinstance(x, ToolMessage) and x.tool_call_id in pending:
                results.append(x)
                pending.discard(x.tool_call_id)
            else:
                deferred.append(x)
            j += 1
        if results and deferred:
            out.extend(results)
            out.extend(deferred)
            i = j
            changed = True
    return out if changed else None


class ToolCallRepairMiddleware(AgentMiddleware):
    """Drop unanswered tool_calls from history before the model call (self-heal a
    thread that would otherwise 400 forever). No-op on a healthy history."""

    def _repair(self, state):
        messages = state.get("messages") or []
        repairs = repair_messages(messages)
        if repairs:
            try:
                from observability.trajectory import log_surface_op

                log_surface_op(
                    str((state or {}).get("session_id") or ""),
                    "repair",
                    cause="dangling tool_call",
                    rewritten_ids=[getattr(m, "id", None) for m in repairs],
                )
            except Exception:  # noqa: BLE001 — trajectory is best-effort
                pass
        return {"messages": repairs} if repairs else None

    def before_model(self, state, runtime):  # type: ignore[override]
        return self._repair(state)

    async def abefore_model(self, state, runtime):  # type: ignore[override]
        return self._repair(state)


class ToolResultOrderMiddleware(AgentMiddleware):
    """Hand the model a history whose tool results directly follow their calls.

    View-only (``request.override``) — the checkpoint keeps its stored order. Placed
    INSIDE PromptCache and OUTSIDE PromptCapture (see ``graph.agent``): Trajectory, which
    sits outside PromptCache, must hash the STORED messages; PromptCapture must record
    what the model actually saw. A healthy history passes through as the same request.
    """

    def wrap_model_call(self, request, handler):  # type: ignore[override]
        fixed = reorder_tool_results(getattr(request, "messages", None) or [])
        return handler(request.override(messages=fixed) if fixed is not None else request)

    async def awrap_model_call(self, request, handler):  # type: ignore[override]
        fixed = reorder_tool_results(getattr(request, "messages", None) or [])
        return await handler(request.override(messages=fixed) if fixed is not None else request)
