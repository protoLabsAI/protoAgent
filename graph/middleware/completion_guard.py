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
from langchain_core.messages import AIMessage, HumanMessage

from graph.middleware.guard_notes import guard_note, is_guard_note

log = logging.getLogger(__name__)

# Leading tag on the injected note — informative to the model only. The nudge counter
# reads the message's guard tag (`guard_notes`), not this text.
NUDGE_MARK = "[completion-guard]"
GUARD = "completion"
# The wrap-up warning sent once as the turn budget runs out (#3559).
BUDGET_MARK = "[turn-budget]"
BUDGET_GUARD = "turn-budget"
WRAP_UP_RESERVE_PCT = 15  # warn with this share of the tool rounds left …
WRAP_UP_MIN_RESERVE = 3  # … and never fewer than this many
WRAP_UP_MIN_TURNS = 6  # a smaller budget has no meaningful "nearly spent"


def _text(message) -> str:
    content = getattr(message, "content", "")
    return content if isinstance(content, str) else (getattr(message, "text", "") or str(content))


def nudges_sent(messages) -> int:
    # By tag, never by text: the task prompt is a HumanMessage too (#3556).
    return sum(1 for m in messages or [] if is_guard_note(m, GUARD))


def task_prompt(messages) -> str:
    """The delegation's task — the first thing a person (not a guard) said in the run."""
    for m in messages or []:
        if isinstance(m, HumanMessage) and not is_guard_note(m):
            return _text(m)
    return ""


def missing_markers(markers, prompt: str, answer: str) -> list[str]:
    """The ``markers`` the task ASKED for that the answer does not carry.

    A subagent type is shared by many callers, and only some ask for a given closing line:
    pr-reviewer's structural recipe requires ``FINDER_STATUS: …`` of `review-finder`, the
    core `code-review` recipe does not. So a marker is owed only when the prompt names it.
    """
    return [m for m in markers or () if m and m in (prompt or "") and m not in (answer or "")]


def tool_rounds(messages) -> int:
    return sum(1 for m in messages or [] if isinstance(m, AIMessage) and getattr(m, "tool_calls", None))


def wrap_up_at(max_turns: int) -> int:
    """The tool round at which to warn that the budget is nearly spent, or 0 for never.

    The last 15% of the budget, at least three rounds — room to write the deliverable. A
    budget too small to have a meaningful "nearly" (under six rounds) gets no warning.
    """
    if max_turns < WRAP_UP_MIN_TURNS:
        return 0
    return max_turns - max(WRAP_UP_MIN_RESERVE, -(-max_turns * WRAP_UP_RESERVE_PCT // 100))


class CompletionGuardMiddleware(AgentMiddleware):
    """Get a subagent to FINISH: warn it before its turn budget is gone, and send a run that
    ended without its deliverable back to the model, at most ``max_nudges`` times."""

    def __init__(
        self,
        *,
        delivered: Callable[[str], bool],
        contract: str = "",
        max_nudges: int = 2,
        prompt_markers: tuple[str, ...] = (),
        max_turns: int = 0,
    ):
        super().__init__()
        self._delivered = delivered
        self._contract = contract or "the deliverable your instructions require"
        self._max_nudges = max(0, int(max_nudges))
        self._prompt_markers = tuple(prompt_markers or ())
        self._max_turns = max(0, int(max_turns or 0))
        self._wrap_up_at = wrap_up_at(self._max_turns)

    def _wrap_up(self, state) -> dict | None:
        """Once, when the budget is nearly spent: stop reading, write it up (#3559).

        Every subagent prompt says "hard stop at max_turns: return what you have", but the
        model cannot see the counter. A lane whose failure mode is open-ended exploration
        ran all 60 rounds on a four-file diff and hard-stopped on "Let me find the
        `SpyDispatcher` definition…" — every round of reading lost.
        """
        if not self._wrap_up_at:
            return None
        messages = state.get("messages") or []
        if any(is_guard_note(m, BUDGET_GUARD) for m in messages):
            return None
        used = tool_rounds(messages)
        if used < self._wrap_up_at:
            return None
        note = (
            f"{BUDGET_MARK} You have used {used} of your {self._max_turns} tool rounds. Stop reading "
            f"now. Write {self._contract} from what you have already read, and state anything you "
            "did not get to as a `Gap:` line rather than opening another file."
        )
        log.info("[completion-guard] turn budget nearly spent (%d/%d); wrap-up note sent", used, self._max_turns)
        return {"messages": [guard_note(BUDGET_GUARD, note)]}

    def _intervene(self, state) -> dict | None:
        messages = state.get("messages") or []
        last = messages[-1] if messages else None
        # Only a turn that would END the loop is in question: a turn with tool calls
        # is still working, and anything but an AIMessage is not the model's turn.
        if not isinstance(last, AIMessage) or getattr(last, "tool_calls", None):
            return None
        answer = _text(last)
        has_deliverable = self._delivered(answer)
        owed = missing_markers(self._prompt_markers, task_prompt(messages), answer)
        if has_deliverable and not owed:
            return None
        sent = nudges_sent(messages)
        if sent >= self._max_nudges:
            log.warning("[completion-guard] still no deliverable after %d nudge(s); letting the run end", sent)
            return None
        if has_deliverable:
            # The work is done and one required line is missing (pr-reviewer-plugin#145: a
            # finder wrote a full review and dropped `FINDER_STATUS`, voiding the round). The
            # delegation's answer is the LAST message, so it must be repeated whole — a reply
            # holding only the missing line would replace the review with that line.
            wanted = ", ".join(f"`{m}`" for m in owed)
            note = (
                f"{NUDGE_MARK} Your answer is complete except for a closing line your task requires, "
                f"starting {wanted}. Reply once more with your COMPLETE answer exactly as before — the "
                "prose and the fenced block, unchanged — and end with that line."
            )
            log.info("[completion-guard] answer lacks required %s; nudge %d/%d", wanted, sent + 1, self._max_nudges)
        else:
            note = (
                f"{NUDGE_MARK} Your last turn ended the run without your deliverable: it made no tool "
                f"call and did not contain {self._contract}. If you were about to read something, make "
                "that tool call now. Otherwise finish now, from what you have already read, with the "
                "deliverable — a partial answer that says what it did not cover beats none."
            )
            log.info("[completion-guard] run ended without its deliverable; nudge %d/%d", sent + 1, self._max_nudges)
        return {"jump_to": "model", "messages": [guard_note(GUARD, note)]}

    def before_model(self, state, runtime):  # type: ignore[override]
        return self._wrap_up(state)

    async def abefore_model(self, state, runtime):  # type: ignore[override]
        return self._wrap_up(state)

    @hook_config(can_jump_to=["model"])
    def after_model(self, state, runtime):  # type: ignore[override]
        return self._intervene(state)

    @hook_config(can_jump_to=["model"])
    async def aafter_model(self, state, runtime):  # type: ignore[override]
        return self._intervene(state)
