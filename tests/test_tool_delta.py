"""A runtime toolset change is announced to the agent, once (#2640).

The failure this pins: an agent mid-session concluded it had no tool for a job, the tool
was then deployed and bound, and it went on refusing the work — politely, with reasoning
— until an operator said "you have a tool for this". A refusal that confident reads as a
missing feature, not a stale belief, which is what made it expensive to spot.
"""

from __future__ import annotations

import pytest

from graph import tool_delta


@pytest.fixture(autouse=True)
def _clean():
    tool_delta.reset_for_tests()
    yield
    tool_delta.reset_for_tests()


# ── silence where silence is correct ──────────────────────────────────────────
def test_the_first_build_announces_nothing():
    """Boot isn't a change. Announcing the whole toolset on every start would train the
    model to skim the block, which is exactly what breaks the one case that matters."""
    assert tool_delta.record_toolset(["a", "b"]) is None
    assert tool_delta.take_pending_delta() is None


def test_an_unchanged_rebuild_announces_nothing():
    tool_delta.record_toolset(["a", "b"])
    assert tool_delta.record_toolset(["b", "a"]) is None  # order is not change
    assert tool_delta.take_pending_delta() is None


def test_a_no_op_turn_costs_nothing():
    tool_delta.record_toolset(["a"])
    tool_delta.record_toolset(["a"])
    assert tool_delta.take_pending_delta() is None


# ── the change ────────────────────────────────────────────────────────────────
def test_an_added_tool_is_recorded_and_announced():
    tool_delta.record_toolset(["a"])
    delta = tool_delta.record_toolset(["a", "board_register_project"])
    assert delta == {"added": ["board_register_project"], "removed": []}
    note = tool_delta.format_delta(tool_delta.take_pending_delta())
    assert "board_register_project" in note
    assert "re-check" in note  # the instruction, not just a list


def test_a_removed_tool_is_announced_too():
    """The mirror failure: planning around a tool that's gone fails at call time
    instead of planning differently."""
    tool_delta.record_toolset(["a", "b"])
    tool_delta.record_toolset(["a"])
    note = tool_delta.format_delta(tool_delta.take_pending_delta())
    assert "No longer available: b" in note


def test_the_announcement_is_one_shot():
    tool_delta.record_toolset(["a"])
    tool_delta.record_toolset(["a", "b"])
    assert tool_delta.take_pending_delta() is not None
    assert tool_delta.take_pending_delta() is None  # consumed


def test_a_later_change_supersedes_an_unconsumed_one():
    """Two rebuilds before a turn: the agent should learn the CURRENT set, not replay a
    stale intermediate."""
    tool_delta.record_toolset(["a"])
    tool_delta.record_toolset(["a", "b"])
    tool_delta.record_toolset(["a", "b", "c"])
    delta = tool_delta.take_pending_delta()
    assert delta == {"added": ["c"], "removed": []}


def test_a_long_list_is_capped_but_says_how_many_more():
    tool_delta.record_toolset(["base"])
    tool_delta.record_toolset(["base", *[f"t{i:02d}" for i in range(20)]])
    note = tool_delta.format_delta(tool_delta.take_pending_delta())
    assert "+8 more" in note  # 20 added, 12 listed


def test_blank_names_are_ignored_not_counted_as_change():
    tool_delta.record_toolset(["a", ""])
    assert tool_delta.record_toolset(["a", None]) is None


def test_format_of_an_empty_delta_is_empty():
    assert tool_delta.format_delta({"added": [], "removed": []}) == ""


def test_record_accepts_a_generator():
    """create_agent_graph passes a generator expression over tool objects."""
    tool_delta.record_toolset(n for n in ["a"])
    delta = tool_delta.record_toolset(n for n in ["a", "b"])
    assert delta == {"added": ["b"], "removed": []}


# ── the injection path (KnowledgeMiddleware carries it) ───────────────────────
def _compose(record: bool = True):
    from graph.middleware.knowledge import KnowledgeMiddleware

    mw = KnowledgeMiddleware(knowledge_store=None)
    return mw.compose_context({"messages": []}, None, record=record)


def test_the_note_reaches_the_injected_context():
    tool_delta.record_toolset(["a"])
    tool_delta.record_toolset(["a", "board_register_project"])
    out = _compose()
    assert out is not None
    assert "board_register_project" in out["context"]
    assert any(s["label"] == "Toolset changed" for s in out["context_sections"])


def test_a_second_turn_does_not_repeat_it():
    tool_delta.record_toolset(["a"])
    tool_delta.record_toolset(["a", "b"])
    first = _compose()
    assert first is not None and "b" in first["context"]
    assert _compose() is None  # nothing else to inject, and the note is spent


def test_a_prompt_preview_does_not_burn_the_one_shot():
    """`record=False` is the #2388 P3 speculative preview. Consuming there would spend
    the announcement on a prompt no model ever receives."""
    tool_delta.record_toolset(["a"])
    tool_delta.record_toolset(["a", "b"])
    assert _compose(record=False) is None  # preview stays silent
    assert "b" in _compose(record=True)["context"]  # the real turn still gets it


def test_no_toolset_change_injects_nothing():
    tool_delta.record_toolset(["a"])
    tool_delta.record_toolset(["a"])
    assert _compose() is None
