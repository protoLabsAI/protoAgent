"""Completion guard — a subagent that stops mid-loop is continued, not reported done.

#3552. An agent loop ends on the first model turn that carries no tool call, and the
delegation's answer is the last AIMessage with content. Those two rules make a model
that simply STOPS — announces its next read and never makes the call, or returns an
empty turn so the walk-back lands on an earlier narration — indistinguishable from
one that finished: the step reads ``completed`` and its whole output is a sentence of
intent ("Let me check the other callers of X:"). Measured on a live review panel,
17% of ``review-finder`` steps ended that way, deep into a long tool loop (p50 509s),
which put a four-lane panel's completion rate at 0.83^4.

A subagent that owes a recognisable deliverable declares it as
``SubagentConfig.completion_marker`` (a substring) or ``completion_check`` (a predicate,
for a deliverable a substring cannot vouch for — a fence can open and never close). When a turn ends the loop without it, this
middleware appends one short note and sends the run back to the model — one extra
model call instead of a lost lane. Bounded by ``max_nudges``, and each nudge is an
ordinary model pass, so it spends the subagent's ``max_turns`` budget like any other.

A subagent that declares neither never gets this middleware; its stack is unchanged.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage

from graph.middleware.guard_notes import guard_note, is_guard_note

log = logging.getLogger(__name__)

# Leading tag on the injected note — informative to the model only. The nudge counter
# reads the message's guard tag (`guard_notes`), not this text.
NUDGE_MARK = "[completion-guard]"
GUARD = "completion"


def _text(message) -> str:
    content = getattr(message, "content", "")
    return content if isinstance(content, str) else (getattr(message, "text", "") or str(content))


def nudges_sent(messages) -> int:
    # By tag, never by text: the task prompt is a HumanMessage too (#3556).
    return sum(1 for m in messages or [] if is_guard_note(m, GUARD))


class CompletionGuardMiddleware(AgentMiddleware):
    """Send a run that ended without its deliverable back to the model, at most ``max_nudges`` times."""

    def __init__(self, *, delivered: Callable[[str], bool], contract: str = "", max_nudges: int = 2):
        super().__init__()
        self._delivered = delivered
        self._contract = contract or "the deliverable your instructions require"
        self._max_nudges = max(0, int(max_nudges))

    def _intervene(self, state) -> dict | None:
        messages = state.get("messages") or []
        last = messages[-1] if messages else None
        # Only a turn that would END the loop is in question: a turn with tool calls
        # is still working, and anything but an AIMessage is not the model's turn.
        if not isinstance(last, AIMessage) or getattr(last, "tool_calls", None):
            return None
        if self._delivered(_text(last)):
            return None
        sent = nudges_sent(messages)
        if sent >= self._max_nudges:
            log.warning("[completion-guard] still no deliverable after %d nudge(s); letting the run end", sent)
            return None
        note = (
            f"{NUDGE_MARK} Your last turn ended the run without your deliverable: it made no tool "
            f"call and did not contain {self._contract}. If you were about to read something, make "
            "that tool call now. Otherwise finish now, from what you have already read, with the "
            "deliverable — a partial answer that says what it did not cover beats none."
        )
        log.info("[completion-guard] run ended without its deliverable; nudge %d/%d", sent + 1, self._max_nudges)
        return {"jump_to": "model", "messages": [guard_note(GUARD, note)]}

    @hook_config(can_jump_to=["model"])
    def after_model(self, state, runtime):  # type: ignore[override]
        return self._intervene(state)

    @hook_config(can_jump_to=["model"])
    async def aafter_model(self, state, runtime):  # type: ignore[override]
        return self._intervene(state)
