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

# ── the middleware (standalone, unconditional) ────────────────────────────────
def _mw():
    from graph.middleware.tool_delta import ToolDeltaMiddleware

    return ToolDeltaMiddleware()


def _note(update) -> str | None:
    from graph.middleware.tool_delta import TOOL_DELTA_NOTE_KEY

    return (update or {}).get(TOOL_DELTA_NOTE_KEY)


def test_no_change_injects_nothing():
    tool_delta.record_toolset(["a"])
    tool_delta.record_toolset(["a"])
    mw = _mw()
    assert _note(mw.before_agent({}, None)) == ""  # the marker only — no notice


def test_a_change_is_carried_in_run_state_for_wrap_model_call():
    """ADR 0108 D2: the delta is never a ``messages`` update — it rides the run's
    private, never-checkpointed channel to wrap_model_call (turn-scoped: the
    middleware instance is shared by concurrent turns)."""
    tool_delta.record_toolset(["a"])
    tool_delta.record_toolset(["a", "board_register_project"])
    mw = _mw()
    out = mw.before_agent({}, None)
    assert "messages" not in out  # nothing enters the message log
    assert "board_register_project" in _note(out)
    assert not hasattr(mw, "_pending_note")  # never held on the shared instance


def test_the_note_channel_is_untracked_and_private():
    """Never checkpointed (ADR 0108 D2) and absent from the input/output schemas."""
    import typing

    from langchain.agents.middleware.types import PrivateStateAttr
    from langgraph.channels.untracked_value import UntrackedValue

    from graph.middleware.tool_delta import TOOL_DELTA_NOTE_KEY, ToolDeltaState

    hint = typing.get_type_hints(ToolDeltaState, include_extras=True)[TOOL_DELTA_NOTE_KEY]
    meta = typing.get_args(typing.get_args(hint)[0])[1:]  # NotRequired[Annotated[...]]
    assert UntrackedValue in meta and PrivateStateAttr in meta


def test_wrap_model_call_delivers_the_tagged_frame():
    """The frame reaches the model via request.override(messages=...), never the
    checkpointer."""
    from dataclasses import dataclass, field

    tool_delta.record_toolset(["a"])
    tool_delta.record_toolset(["a", "board_register_project"])
    mw = _mw()
    update = mw.before_agent({}, None)

    @dataclass
    class _Req:
        messages: list
        state: dict = field(default_factory=dict)
        def override(self, **kw):
            return _Req(**{**{"messages": self.messages, "state": self.state}, **kw})

    captured = []
    def handler(req):
        captured.append(req)
        return "ok"

    mw.wrap_model_call(_Req(messages=[], state=dict(update)), handler)
    frame = captured[0].messages[-1]
    assert "board_register_project" in frame.content
    assert frame.additional_kwargs["protoagent_injected_context"] is True

    # A request from a run that took no note (another turn) gets nothing.
    captured.clear()
    mw.wrap_model_call(_Req(messages=[], state={}), handler)
    assert captured[0].messages == []


def test_the_injection_is_one_shot_across_turns():
    tool_delta.record_toolset(["a"])
    tool_delta.record_toolset(["a", "b"])
    mw = _mw()
    assert _note(mw.before_agent({}, None))
    assert _note(mw.before_agent({}, None)) == ""


async def test_the_async_hook_behaves_identically():
    tool_delta.record_toolset(["a"])
    tool_delta.record_toolset(["a", "b"])
    mw = _mw()
    out = await mw.abefore_agent({}, None)
    assert "b" in _note(out)
    assert _note(await mw.abefore_agent({}, None)) == ""
