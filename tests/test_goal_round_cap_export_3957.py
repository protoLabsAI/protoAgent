"""#3957 — the "⏸ goal paused — round cap reached" note reaches the transcript.

The note rode only the turn's final TEXT (the stream's terminal frame, ``/v1``'s
content). The checkpoint — what ``/export``, a rebuilt chat and the next turn read — kept
the round governor's hand-back alone, so the export lost the line saying the goal is
paused and why. The drive now writes the note onto the thread: the hand-back is rewritten
in place (same id) to the text the stream showed, or — when the capped pass ran on a
fresh-context goal's own scoped thread — the note is appended to the turn's thread.

Drives the real drivers + ``GoalDrive`` + the real round governor (``test_goal_round_cap``'s
harness); the graph is the only fake, given a checkpoint whose tail is the hand-back.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

import server.goal_loop as goal_loop
from tests._turn_driver_fakes import ScriptedGraph
from tests.test_goal_round_cap import _capping_hook, _NeverMet, state  # noqa: F401 — fixture

_HANDBACK = "I'm pausing here: this goal-driven turn has run 8 model rounds…"


class _CheckpointedGraph(ScriptedGraph):
    """A ScriptedGraph whose checkpoint ends on the round governor's hand-back."""

    async def aget_state(self, config):
        snap = await super().aget_state(config)
        msgs = [HumanMessage(content="write x", id="h0"), AIMessage(content=_HANDBACK, id="handback-1")]
        return SimpleNamespace(tasks=snap.tasks, interrupts=snap.interrupts, values={"messages": msgs})


def _note_updates(graph):
    out = []
    for _config, values in graph.updates:
        for m in (values or {}).get("messages", []) if isinstance(values, dict) else []:
            if (getattr(m, "additional_kwargs", None) or {}).get("protoagent_goal_note"):
                out.append(m)
    return out


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["stream", "sync"])
async def test_a_capped_goal_turn_writes_the_pause_note_onto_the_handback(state, monkeypatch, surface):  # noqa: F811
    import importlib

    from tests._turn_driver_fakes import text, turn_result

    monkeypatch.setattr(state, "goal_controller", _NeverMet(), raising=False)
    if surface == "stream":
        g = _CheckpointedGraph(streams=[[text("r0", "pass0")]])
    else:
        g = _CheckpointedGraph(invokes=[turn_result(AIMessage(content="pass0"))])
    g.on_call = _capping_hook({1})
    monkeypatch.setattr(state, "graph", g, raising=False)
    chat = importlib.import_module("server.chat")
    if surface == "stream":
        frames = [f async for f in chat._chat_langgraph_stream("go", "s1", request_metadata={})]
        final = frames[-1][1]
    else:
        final = (await chat.chat("go", "s1"))[0]["content"]

    assert "round cap reached" in final  # the stream already had it
    (written,) = _note_updates(g)  # ...and now the checkpoint does too
    assert written.id == "handback-1"  # replaces the hand-back, no second message
    assert written.content.startswith(_HANDBACK)
    assert written.content.endswith(written.additional_kwargs["protoagent_goal_note"])
    assert "⏸ goal paused — round cap reached" in written.content


@pytest.mark.asyncio
async def test_a_pass_on_a_fresh_context_thread_appends_the_note_to_the_turns_thread(state, monkeypatch):  # noqa: F811
    g = _CheckpointedGraph()
    monkeypatch.setattr(state, "graph", g, raising=False)
    turn_cfg = {"configurable": {"thread_id": "a2a:s1"}}
    pass_cfg = {"configurable": {"thread_id": "a2a:s1:goal-iter-3"}}

    assert await goal_loop.record_goal_note(turn_cfg, "⏸ goal paused — round cap reached", pass_config=pass_cfg)

    (cfg, values) = g.updates[-1]
    assert cfg == turn_cfg
    (msg,) = values["messages"]
    assert msg.id != "handback-1" and msg.content == "⏸ goal paused — round cap reached"


@pytest.mark.asyncio
async def test_recording_the_note_never_raises(state, monkeypatch):  # noqa: F811
    class _Broken(ScriptedGraph):
        async def aget_state(self, config):
            raise RuntimeError("checkpointer down")

    monkeypatch.setattr(state, "graph", _Broken(), raising=False)
    cfg = {"configurable": {"thread_id": "a2a:s1"}}
    assert await goal_loop.record_goal_note(cfg, "note", pass_config=cfg) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["stream", "sync"])
async def test_the_pause_note_is_written_under_the_thread_lock(state, monkeypatch, surface):  # noqa: F811
    """The non-streaming driver locks the thread per pass, not across the drive; the
    note's checkpoint write takes the lock itself (the streaming driver already holds it)."""
    import importlib

    from server import turn_control
    from tests._turn_driver_fakes import text, turn_result

    held: list[bool] = []

    class _LockSpy(_CheckpointedGraph):
        async def aupdate_state(self, config, values):
            if values and any(
                (getattr(m, "additional_kwargs", None) or {}).get("protoagent_goal_note")
                for m in values.get("messages", [])
            ):
                held.append(turn_control._thread_lock(config["configurable"]["thread_id"]).locked())
            return await super().aupdate_state(config, values)

    monkeypatch.setattr(state, "goal_controller", _NeverMet(), raising=False)
    if surface == "stream":
        g = _LockSpy(streams=[[text("r0", "pass0")]])
    else:
        g = _LockSpy(invokes=[turn_result(AIMessage(content="pass0"))])
    g.on_call = _capping_hook({1})
    monkeypatch.setattr(state, "graph", g, raising=False)
    chat = importlib.import_module("server.chat")
    if surface == "stream":
        [f async for f in chat._chat_langgraph_stream("go", "s1", request_metadata={})]
    else:
        await chat.chat("go", "s1")

    assert held == [True]
