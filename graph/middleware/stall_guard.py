"""Break a no-progress tool loop.

An agent that re-issues the *same* tool call with the *same* arguments and gets
the *same* result, over and over, is stuck. It is NOT a dropped-stream bug — the
ToolMessages are right there in the history and the model received them (see the
`checkpoints.db` decode in #1446); the model simply isn't changing strategy. Left
alone it spins until the recursion limit, burning the whole turn budget.

This middleware watches the trailing run of identical ``(tool, args, result)``
round-trips at ``before_model``:

- at ``nudge_at`` it injects ONE guidance note — change approach or stop — and
  returns to the model (the recovery path);
- if the loop continues to ``stop_at`` it ends the turn with a short explanation
  instead of looping to the recursion limit.

It is a **no-op on any healthy / varied history**: the run only extends while the
*exact same* calls keep getting the *exact same* answers, contiguously, right at
the tail. A real user message (mid-turn steering) breaks the run, as it should;
the middleware's own nudge does not (it carries a marker the scan skips).

Distinct from :class:`ToolCallRepairMiddleware`, which heals the *opposite* case —
an orphaned, *unanswered* tool_call. Here every call IS answered, identically.
"""

from __future__ import annotations

import json

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

# Consecutive identical round-trips before we act. Generous on purpose: identical
# tool + args + result N times running is a strong "stuck" signal, but we leave
# headroom so a legitimate short repeat is never touched.
NUDGE_AT = 3
STOP_AT = 6

# Leading tag on our injected note — visible to the model (informative) and the
# sentinel the scan uses so the note doesn't break the round-trip run it measures.
NUDGE_MARK = "[stall-guard]"


def _tc_id(tc):
    return tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)


def _tc_name(tc):
    return tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)


def _tc_args(tc):
    return tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", None)


def _content(m) -> str:
    return m.content if isinstance(m.content, str) else str(m.content)


def _is_nudge(m) -> bool:
    return isinstance(m, HumanMessage) and isinstance(m.content, str) and m.content.startswith(NUDGE_MARK)


def _signature(msg, results: dict):
    """A hashable fingerprint of one assistant tool-call message + its answers:
    the sorted ``(tool, args_json)`` calls and the sorted result contents. Two
    units with equal signatures made the same calls and got the same results."""
    calls = tuple(
        sorted(
            (str(_tc_name(tc)), json.dumps(_tc_args(tc), sort_keys=True, default=str))
            for tc in (msg.tool_calls or [])
        )
    )
    answers = tuple(sorted((results.get(_tc_id(tc)) or "") for tc in (msg.tool_calls or [])))
    return calls, answers


def trailing_repeat(messages) -> tuple[int, str, str]:
    """Length of the trailing run of identical tool round-trips, plus the tool
    name and a result snippet for the message. ``0`` when the tail is not an
    active repeated-tool loop (e.g. a fresh user turn, a varied tail, or no tools).
    """
    msgs = messages or []
    results = {
        m.tool_call_id: _content(m)
        for m in msgs
        if isinstance(m, ToolMessage) and getattr(m, "tool_call_id", None)
    }

    i = len(msgs) - 1
    while i >= 0 and _is_nudge(msgs[i]):  # tolerate a trailing nudge (shouldn't happen)
        i -= 1
    # An active loop means we just ran tools — the tail is a ToolMessage block.
    if i < 0 or not isinstance(msgs[i], ToolMessage):
        return 0, "", ""

    units: list = []
    while i >= 0:
        if _is_nudge(msgs[i]):  # our own note never breaks the run it measures
            i -= 1
            continue
        if not isinstance(msgs[i], ToolMessage):
            break  # a real boundary (user message / plain answer) ends the loop
        j = i
        while j >= 0 and isinstance(msgs[j], ToolMessage):
            j -= 1  # peel the contiguous ToolMessage block
        if j < 0 or not getattr(msgs[j], "tool_calls", None):
            break  # results with no answering assistant tool_calls — give up scanning
        units.append(_signature(msgs[j], results))
        i = j - 1

    if not units or not units[0][0]:
        return 0, "", ""
    last = units[0]
    n = 0
    for sig in units:
        if sig == last:
            n += 1
        else:
            break
    tool = last[0][0][0]
    snippet = (last[1][0] if last[1] else "")[:200]
    return n, tool, snippet


class StallGuardMiddleware(AgentMiddleware):
    """Interrupt a no-progress loop — same tool + args + result, repeated. Nudges
    once at ``nudge_at``, ends the turn at ``stop_at``. No-op on healthy histories.
    """

    def __init__(self, *, nudge_at: int = NUDGE_AT, stop_at: int = STOP_AT):
        super().__init__()
        self.nudge_at = nudge_at
        self.stop_at = stop_at

    def _intervene(self, state):
        n, tool, snippet = trailing_repeat(state.get("messages") or [])
        if n >= self.stop_at:
            seen = f" (it keeps returning: {snippet!r})" if snippet else ""
            text = (
                f"I'm stopping because I'm stuck in a loop: I called `{tool}` {n} times in a "
                f"row with the same arguments and got the same result each time{seen}, so I'm "
                "not making progress. Rather than keep spinning, I'll pause here — could you "
                "point me at the right target or approach, or run it somewhere it works?"
            )
            return {"jump_to": "end", "messages": [AIMessage(content=text)]}
        if n == self.nudge_at:
            seen = f" ({snippet!r})" if snippet else ""
            note = (
                f"{NUDGE_MARK} You have called `{tool}` {n} times in a row with identical "
                f"arguments and received the same result each time{seen}. Repeating it will "
                "not help. Change your approach — different arguments, a different tool, or "
                "fix the underlying problem — or, if you cannot make progress, stop and tell "
                "the user what is blocking you and what you need. Do not issue that same call again."
            )
            return {"messages": [HumanMessage(content=note)]}
        return None

    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime):  # type: ignore[override]
        return self._intervene(state)

    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state, runtime):  # type: ignore[override]
        return self._intervene(state)
