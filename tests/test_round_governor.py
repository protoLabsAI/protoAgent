"""Tests for RoundGovernorMiddleware (#2710, ADR 0101 D8).

Round count is an instruction-adherence lever: one re-grounding nudge per turn
at the soft threshold, an optional honest hand-back at the hard cap, and a count
that keys on REAL operator input — machinery never resets it.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from graph.context_frame import context_frame_message
from graph.middleware.guard_notes import guard_note
from graph.middleware.round_governor import (
    NUDGE_MARK,
    RoundGovernorMiddleware,
    rounds_since_last_input,
)


def _turn(rounds: int, lead=None):
    msgs = list(lead if lead is not None else [HumanMessage(content="do the thing")])
    for i in range(rounds):
        msgs.append(AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": f"c{i}", "type": "tool_call"}]))
        msgs.append(ToolMessage(content="ok", tool_call_id=f"c{i}"))
    return msgs


def test_counts_rounds_since_the_last_real_input():
    assert rounds_since_last_input(_turn(7)) == (7, False)


def test_machinery_never_resets_the_count():
    msgs = _turn(3)
    msgs.append(context_frame_message("injected memory"))  # #2776 frame
    msgs.append(guard_note("stall", "[stall-guard] change approach"))
    msgs.append(HumanMessage(content="summary", additional_kwargs={"lc_source": "compaction"}))
    msgs.extend(_turn(2, lead=[]))  # two more rounds, no new real input
    rounds, _ = rounds_since_last_input(msgs)
    assert rounds == 5


def test_text_that_merely_starts_with_a_guard_tag_is_a_person_not_machinery():
    # #3556: a note is recognised by its tag, never its text. An operator message that
    # happens to open with "[round-governor]" is real steering — it resets the count and
    # must not latch "already nudged", which used to suppress the turn's re-grounding.
    msgs = _turn(4)
    msgs.append(HumanMessage(content="[round-governor] is a weird name, anyway: try the other repo"))
    msgs.extend(_turn(2, lead=[]))
    assert rounds_since_last_input(msgs) == (2, False)
    # The governor's own tagged note latches, and does not reset.
    msgs = _turn(3) + [guard_note("round-governor", "[round-governor] re-ground")] + _turn(2, lead=[])
    assert rounds_since_last_input(msgs) == (5, True)


def test_real_steering_resets_the_count():
    msgs = _turn(9)
    msgs.append(HumanMessage(content="actually, focus on the tests"))  # genuine steering
    msgs.extend(_turn(2, lead=[]))
    assert rounds_since_last_input(msgs)[0] == 2


def test_soft_nudge_fires_once_per_turn():
    mw = RoundGovernorMiddleware(nudge_after=5, hard_cap=0)
    msgs = _turn(5)
    out = mw.before_model({"messages": msgs}, None)
    assert out is not None
    nudge = out["messages"][0]
    assert nudge.content.startswith(NUDGE_MARK)
    assert "re-check whether it already exists" in nudge.content
    # Apply it; more rounds follow — the marker is the latch, no second nudge.
    msgs.append(nudge)
    msgs.extend(_turn(3, lead=[]))
    assert mw.before_model({"messages": msgs}, None) is None


def test_hard_cap_ends_the_turn_honestly():
    mw = RoundGovernorMiddleware(nudge_after=0, hard_cap=8)
    out = mw.before_model({"messages": _turn(8)}, None)
    assert out is not None and out.get("jump_to") == "end"
    assert "round_hard_cap" in out["messages"][0].content


def test_disabled_is_a_noop():
    mw = RoundGovernorMiddleware(nudge_after=0, hard_cap=0)
    assert mw.before_model({"messages": _turn(50)}, None) is None


def test_under_threshold_is_a_noop():
    mw = RoundGovernorMiddleware(nudge_after=25, hard_cap=0)
    assert mw.before_model({"messages": _turn(24)}, None) is None
