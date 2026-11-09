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

An EMPTY-AT-CAP turn — a reasoning-only turn (#3584, then Vera on protoAgent#4108), or one
the provider CUT at the output cap with no tool call and almost no text — gets a different
retry: drop the failed turn's reasoning and make ONE call with thinking off. That retry
moved to ``EmptyAtCapMiddleware`` (ADR 0128 S3, #4875) so it covers EVERY subagent, not
only those that declare a deliverable. EmptyAtCap is registered BEFORE this guard, so its
``after_model`` runs AFTER this one (LangChain runs after_model in reverse registration
order); this guard DEFERS empty-at-cap turns to it (``empty_at_cap_reason``), so the two
never both retry one turn. The shared helpers the retry needs — ``reasoning_only``,
``without_reasoning``, ``thinking_off_settings`` and the #4108 / #2642 lessons — stay
here and EmptyAtCap imports them.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, HumanMessage

from graph.middleware.guard_notes import guard_note, is_guard_note
from graph.middleware.tool_result_pruner import _est_tokens as estimate_tokens

log = logging.getLogger(__name__)

# Leading tag on the injected note — informative to the model only. The nudge counter
# reads the message's guard tag (`guard_notes`), not this text.
NUDGE_MARK = "[completion-guard]"
GUARD = "completion"
LINE_GUARD = "completion-line"  # the single ask for a closing line the caller requires
REASONING_GUARD = "completion-reasoning"  # the thinking-off retry after a reasoning-only turn
# The wrap-up warning sent once as the turn budget runs out (#3559).
BUDGET_MARK = "[turn-budget]"
CONTEXT_MARK = "[context-budget]"
CONTEXT_WRAP_UP_FRACTION = 0.8  # of the model window — past the pruner's 0.6, before the provider refuses
BUDGET_GUARD = "turn-budget"
CONTEXT_GUARD = "context-budget"
WRAP_UP_RESERVE_PCT = 15  # warn with this share of the tool rounds left …
WRAP_UP_MIN_RESERVE = 3  # … and never fewer than this many
WRAP_UP_MIN_TURNS = 6  # a smaller budget has no meaningful "nearly spent"


def _text(message) -> str:
    content = getattr(message, "content", "")
    return content if isinstance(content, str) else (getattr(message, "text", "") or str(content))


def nudges_sent(messages) -> int:
    # By tag, never by text: the task prompt is a HumanMessage too (#3556). Both kinds of
    # nudge count toward the one budget; the closing-line ask is further capped at one.
    return sum(1 for m in messages or [] if any(is_guard_note(m, g) for g in (GUARD, LINE_GUARD, REASONING_GUARD)))


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
    if isinstance(markers, str):
        markers = (markers,)  # a bare string would otherwise be read one CHARACTER at a time
    return [m for m in markers or () if m and m in (prompt or "") and not has_marker_line(answer, m)]


def has_marker_line(answer: str, marker: str) -> bool:
    """Does the answer carry a LINE that starts with ``marker``?

    A line, not a substring: a review that says "`FINDER_STATUS:` is missing from the other
    lane" mentions the marker without giving one. Leading markdown the model may wrap the
    line in (backticks, emphasis, a quote or list mark) is allowed before it.
    """
    return any(line.lstrip(" \t`*_>-").startswith(marker) for line in (answer or "").splitlines())


def tool_rounds(messages) -> int:
    return sum(1 for m in messages or [] if isinstance(m, AIMessage) and getattr(m, "tool_calls", None))


REASONING_ONLY_MIN_CHARS = 4_000  # of reasoning — a real deliberation, not a preamble
REASONING_ONLY_MAX_TEXT = 200  # of content — nothing, or one sentence of intent


def reasoning_only(message) -> bool:
    """Did this turn spend itself thinking and deliver (almost) nothing? Reads the
    `reasoning_content` the reasoning round-trip keeps on the message (#2642)."""
    extra = getattr(message, "additional_kwargs", None) or {}
    reasoning = str(extra.get("reasoning_content") or extra.get("reasoning") or "")
    return len(reasoning) >= REASONING_ONLY_MIN_CHARS and len(_text(message).strip()) <= REASONING_ONLY_MAX_TEXT


# finish_reasons that mean the provider CUT the reply at the output cap, not that the model
# chose to stop — a closed provider set (`max_tokens` is Anthropic's spelling of `length`).
CAP_FINISH_REASONS = frozenset({"length", "max_tokens"})


def finish_reason(message) -> str:
    meta = getattr(message, "response_metadata", None) or {}
    return str(meta.get("finish_reason") or meta.get("stop_reason") or "")


def empty_at_cap_reason(message) -> str | None:
    """Why this turn ended the loop with nothing usable, or None if it did not (ADR 0128 S3).

    A turn is "empty at the cap" when it makes no tool call and carries ≤200 chars of
    visible text, AND either the provider cut it at the output cap (``finish_reason``
    length/max_tokens) or it was reasoning-only. Returns the label the retry counts
    under — ``"length"`` or ``"reasoning_only"`` — shared by ``EmptyAtCapMiddleware``
    (which retries such a turn) and this guard (which defers it).
    """
    if not isinstance(message, AIMessage) or getattr(message, "tool_calls", None):
        return None
    if len(_text(message).strip()) > REASONING_ONLY_MAX_TEXT:
        return None
    if finish_reason(message) in CAP_FINISH_REASONS:
        return "length"
    if reasoning_only(message):
        return "reasoning_only"
    return None


def without_reasoning(message: AIMessage) -> AIMessage:
    """The same turn (same id, so the state reducer REPLACES it) minus its reasoning.

    A reasoning-only turn's reasoning is the failure itself — on protoAgent#4108 a 45k-char
    loop ending in a literal ``<|im_end|>``. Round-tripped (#2642), it is the first thing the
    retry reads, and the model picks the loop back up where it stopped.
    """
    extra = {k: v for k, v in (message.additional_kwargs or {}).items() if k not in ("reasoning_content", "reasoning")}
    return message.model_copy(update={"additional_kwargs": extra})


def thinking_off_settings(model, settings: dict | None = None) -> dict | None:
    """``model_settings`` for one call to ``model`` with thinking OFF — or None when the slot
    has no switch we know to be safe to send.

    Only an OpenAI-compatible thinking slot qualifies: its ``extra_body`` already carries
    ``thinking`` enabled or ``chat_template_kwargs`` (the operator turned thinking on through
    the gateway). Anything else — Claude, a plain OpenAI model — is never sent a template
    kwarg it may reject. ``chat_template_kwargs.enable_thinking: false`` is the switch the
    vLLM lane honours (probed through the gateway, 2026-10-10: ``thinking: {type: disabled}``
    alone still produced reasoning); ``thinking`` is set too for a DeepSeek-style fallback.
    """
    base = (settings or {}).get("extra_body")
    if not isinstance(base, dict):
        base = getattr(model, "extra_body", None)
    if not isinstance(base, dict):
        return None
    thinking = base.get("thinking")
    on = isinstance(thinking, dict) and thinking.get("type") == "enabled"
    if not on and "chat_template_kwargs" not in base:
        return None
    template = base.get("chat_template_kwargs")
    template = dict(template) if isinstance(template, dict) else {}
    template["enable_thinking"] = False
    body = {**base, "thinking": {"type": "disabled"}, "chat_template_kwargs": template}
    return {**(settings or {}), "extra_body": body}


def describe_turn(message) -> str:
    """``finish_reason=… out_tokens=… in_tokens=… text_chars=…`` for the give-up log (#3582)."""
    meta = getattr(message, "response_metadata", None) or {}
    usage = getattr(message, "usage_metadata", None) or {}
    return (
        f"finish_reason={meta.get('finish_reason') or meta.get('stop_reason') or '?'} "
        f"out_tokens={usage.get('output_tokens', '?')} in_tokens={usage.get('input_tokens', '?')} "
        f"text_chars={len(_text(message))}"
    )


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
        context_window: int | None = None,
    ):
        super().__init__()
        self._delivered = delivered
        self._contract = contract or "the deliverable your instructions require"
        self._max_nudges = max(0, int(max_nudges))
        self._prompt_markers = tuple(prompt_markers or ())
        self._max_turns = max(0, int(max_turns or 0))
        self._wrap_up_at = wrap_up_at(self._max_turns)
        self._context_window = int(context_window) if context_window else 0
        self._context_wrap_up_at = int(self._context_window * CONTEXT_WRAP_UP_FRACTION)

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

    def _context_wrap_up(self, state) -> dict | None:
        """Once, when the context is nearly full: stop reading, write it up (#3576).

        The turn budget cannot see this: a lane that reads large files fills a 262k
        window in 30 rounds of a 60-round budget and the provider refuses the next call
        — a failed step, every round of reading lost. Estimated as the pruner does
        (chars//4), against the SUBAGENT model's window; no window, no note.
        """
        if not self._context_wrap_up_at:
            return None
        messages = state.get("messages") or []
        if any(is_guard_note(m, CONTEXT_GUARD) for m in messages):
            return None
        used = estimate_tokens(messages)
        if used < self._context_wrap_up_at:
            return None
        note = (
            f"{CONTEXT_MARK} Your context is nearly full (~{used // 1000}k of {self._context_window // 1000}k "
            f"tokens). Stop reading now — one more large file may end this run with no output. "
            f"Write {self._contract} from what you have already read, and state anything you "
            "did not get to as a `Gap:` line rather than opening another file."
        )
        log.info(
            "[completion-guard] context nearly full (~%dk/%dk); wrap-up note sent",
            used // 1000,
            self._context_window // 1000,
        )
        return {"messages": [guard_note(CONTEXT_GUARD, note)]}

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
        if not has_deliverable and empty_at_cap_reason(last) is not None:
            # A capped / reasoning-only turn is EmptyAtCapMiddleware's to retry (ADR 0128
            # S3): it owns the one thinking-off retry, for EVERY subagent, not only those
            # that declare a deliverable. It is registered before this guard, so its
            # after_model runs AFTER ours — deferring here is what keeps the two from both
            # retrying one turn. The guard still nudges a plain stall (a narration, or an
            # empty turn the provider did NOT cap) exactly as before.
            return None
        if sent >= self._max_nudges:
            # What the record cannot say otherwise (#3582): a lane that ends on "Let me verify
            # X" twice looks the same whether its output was cut (finish_reason=length), its
            # tool call was stripped, or the model simply stopped after a long think.
            log.warning(
                "[completion-guard] still no deliverable after %d nudge(s); letting the run end (%s)",
                sent,
                describe_turn(last),
            )
            return None
        if has_deliverable and any(is_guard_note(m, LINE_GUARD) for m in messages):
            # Asked once already for the closing line and the answer still lacks it. That ask
            # is a courtesy on finished work, not a loop: the run ends and is labelled.
            log.warning(
                "[completion-guard] required closing line still missing after one ask; letting the run end (%s)",
                describe_turn(last),
            )
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
            log.info("[completion-guard] answer lacks required %s; asking once", wanted)
            return {"jump_to": "model", "messages": [guard_note(LINE_GUARD, note)]}
        else:
            note = (
                f"{NUDGE_MARK} Your last turn ended the run without your deliverable: it made no tool "
                f"call and did not contain {self._contract}. If you were about to read something, make "
                "that tool call now. Otherwise finish now, from what you have already read, with the "
                "deliverable — a partial answer that says what it did not cover beats none."
            )
            log.info(
                "[completion-guard] run ended without its deliverable; nudge %d/%d (%s)",
                sent + 1,
                self._max_nudges,
                describe_turn(last),
            )
        return {"jump_to": "model", "messages": [guard_note(GUARD, note)]}

    def before_model(self, state, runtime):  # type: ignore[override]
        return self._wrap_up(state) or self._context_wrap_up(state)

    async def abefore_model(self, state, runtime):  # type: ignore[override]
        return self._wrap_up(state) or self._context_wrap_up(state)

    @hook_config(can_jump_to=["model"])
    def after_model(self, state, runtime):  # type: ignore[override]
        return self._intervene(state)

    @hook_config(can_jump_to=["model"])
    async def aafter_model(self, state, runtime):  # type: ignore[override]
        return self._intervene(state)
