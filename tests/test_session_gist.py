"""Idle gist behavior through the session-memory middleware; no real clocks/models."""

import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from graph import session_gist
from graph.config import LangGraphConfig
from graph.goals.goal_turn import goal_turn
from graph.middleware import memory


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_PATH", str(tmp_path))
    monkeypatch.setattr(memory, "_PERSISTENCE_DISABLED", False)
    monkeypatch.setattr(session_gist, "_pending", {})
    timers = []
    calls = []

    class Timer:
        def __init__(self, delay, callback):
            self.delay, self.callback = delay, callback
            self.cancelled = False
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            self.cancelled = True

        def fire(self):
            # Also exercise a cancelled callback racing its replacement.
            self.callback()

    def invoke(messages):
        calls.append(json.loads(messages[-1].content))
        return AIMessage(content="We are designing the idle session gist. Implementation is ready for review. " * 5)

    def model(config, role):
        assert role == "memory_gist"
        return SimpleNamespace(invoke=invoke)

    monkeypatch.setattr(session_gist, "Timer", Timer)
    monkeypatch.setattr(session_gist, "create_role_llm", model)
    config = LangGraphConfig()
    mw = memory.SessionSummaryMiddleware(config=config)
    state = {"session_id": "chat-1", "messages": []}

    def turn(pairs=1):
        mw.before_agent(state, None)
        for _ in range(pairs):
            state["messages"].extend([HumanMessage(content="Design this feature"), AIMessage(content="Working on it")])
        mw.after_agent(state, None)

    def card():
        return json.loads((tmp_path / memory.session_filename(state["session_id"])).read_text())

    return SimpleNamespace(mw=mw, state=state, timers=timers, calls=calls, turn=turn, card=card, config=config)


def test_idle_debounce_incremental_input_and_preservation(harness):
    h = harness
    h.turn()
    assert not h.timers  # fewer than four new messages
    h.turn()
    first = h.timers[-1]
    assert first.delay == 120
    assert not h.calls and "gist" not in h.card()
    h.turn()
    assert first.cancelled
    first.fire()
    assert not h.calls
    h.timers[-1].fire()
    initial = h.card()
    assert 0 < len(initial["gist"]) <= 240
    assert initial["gist_at"] and initial["gist_message_count"] == 6
    assert len(h.calls[0]["messages"]) == 6
    h.timers[-1].fire()
    assert len(h.calls) == 1  # no periodic regeneration
    h.turn()
    assert h.card()["gist"] == initial["gist"]
    assert not session_gist._pending
    h.turn()
    assert len(h.calls) == 1  # never per turn
    h.timers[-1].fire()
    assert len(h.calls) == 2
    assert h.calls[-1]["previous_gist"] == initial["gist"]
    assert len(h.calls[-1]["messages"]) == 4
    assert h.card()["gist_message_count"] == 10


def test_long_chat_payload_is_bounded(harness):
    """A long first gist (since=0 sends the whole transcript) must not ship the
    unclipped, uncapped transcript — that could exceed the gist model's input
    limit and get swallowed, so the chat would never receive a gist."""
    h = harness
    h.mw.before_agent(h.state, None)
    filler = "x" * 5000
    messages = []
    for i in range(60):
        messages.append(HumanMessage(content=f"topic marker {i} {filler}"))
        messages.append(AIMessage(content=f"reply {i} {filler}"))
    h.state["messages"] = messages
    h.mw.after_agent(h.state, None)
    h.timers[-1].fire()
    payload = h.calls[0]["messages"]
    assert len(payload) <= memory._SUMMARY_MAX_MESSAGES
    assert all(len(m["content"]) <= memory._SUMMARY_MESSAGE_MAX_CHARS for m in payload)
    # The first user message (the chat's topic) survives the cap, and the chat
    # still receives a bounded gist rather than a swallowed failure.
    assert payload[0]["content"].startswith("topic marker 0")
    assert 0 < len(h.card()["gist"]) <= 240


def test_idle_seconds_zero_disables_generation(harness):
    """gist_idle_seconds: 0 means off (as _coerce_retention documents), not a
    one-second timer."""
    h = harness
    h.config.memory_gist_idle_seconds = 0
    h.turn(2)  # four new messages — would otherwise schedule
    assert not h.timers and not h.calls


@pytest.mark.parametrize(
    "sid,incognito,goal,enabled",
    [
        ("chat-1", True, False, True),
        ("background:1", False, False, True),
        ("job:1", False, False, True),
        ("palette-1", False, False, True),
        ("system:1", False, False, True),
        ("chat-1", False, True, True),
        ("chat-1", False, False, False),
    ],
)
def test_excluded_turns_never_generate(harness, sid, incognito, goal, enabled):
    h = harness
    h.state.update(session_id=sid, incognito=incognito)
    h.config.memory_session_gist = enabled
    with goal_turn() if goal else nullcontext():
        h.turn(2)
    assert not h.timers and not h.calls


@pytest.mark.parametrize("existing", [False, True])
def test_model_failure_leaves_card_unchanged(harness, monkeypatch, existing):
    h = harness
    h.turn(2)
    if existing:
        h.timers[-1].fire()
        h.turn(2)
    before = h.card()

    def fail(*args):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(session_gist, "create_role_llm", fail)
    h.timers[-1].fire()
    assert h.card() == before
    assert not session_gist._pending


@pytest.mark.parametrize("action", ["turn", "delete", "rewrite"])
def test_inflight_result_cannot_overwrite_new_activity_or_deleted_card(harness, monkeypatch, action):
    h = harness
    h.turn(2)

    def invoke(messages):
        if action == "delete":
            memory.delete_session_summary("chat-1")
        elif action == "turn":
            h.state["incognito"] = True
            h.mw.before_agent(h.state, None)
        else:
            h.turn()
        return AIMessage(content="Stale gist")

    monkeypatch.setattr(session_gist, "create_role_llm", lambda *_: SimpleNamespace(invoke=invoke))
    h.timers[-1].fire()
    if action == "delete":
        with pytest.raises(FileNotFoundError):
            h.card()
    else:
        assert "gist" not in h.card()


def test_private_source_survives_public_turn_compaction_restart_and_stale_timer(harness, tmp_path, monkeypatch):
    import asyncio
    from langchain_core.messages import ToolMessage
    from graph import public_memory
    from graph.compaction_op import compact_thread
    from tests._public_memory_source import PublicMemorySource
    from tests.test_compaction_op import _FakeGraph, _FakeHistory, _cfg

    h = harness
    # Seed an already-running legacy timer and contaminated previous gist.
    h.state["messages"] = [HumanMessage(content="PRIVATE-CANARY")] * 4
    h.turn(2)
    stale = h.timers[-1]
    path = tmp_path / "chat-1.json"
    path.write_text(json.dumps({**h.card(), "gist": "PRIVATE-OLD-GIST"}))
    source = PublicMemorySource(tmp_path / "source.record")
    public = [HumanMessage(content="rocket planning"), AIMessage(content="public answer")] * 3
    source.save(public)
    monkeypatch.setattr(public_memory, "_source_reader", source)
    stale.fire()  # Source changed before another turn had a chance to cancel it.
    assert not h.calls
    h.state["messages"].extend([
        AIMessage(content="PRIVATE-REPLY", tool_calls=[{"name": "lookup", "args": {"x": "PRIVATE-ARGS"}, "id": "p"}]),
        ToolMessage(content="PRIVATE-TOOL", tool_call_id="p"),
    ])
    h.state["incognito"] = False
    h.turn()
    stale.fire()
    h.timers[-1].fire()
    assert h.calls[-1]["previous_gist"] == ""
    assert h.card()["message_count"] == len(public)
    assert h.card()["tool_calls"] == []
    assert h.card()["final_output"] == "public answer"

    async def summarize(*args):
        return "PRIVATE-COMPACTED"

    graph = _FakeGraph(h.state["messages"])
    result = asyncio.run(compact_thread(
        graph, object(), None, _cfg(2), "chat-1", "chat-1", history_store=_FakeHistory(), summarizer=summarize,
    ))
    assert not result["refused"]
    h.state["messages"] = graph.updates[-1][1]["messages"][1:]
    # Reopen source and middleware, then run an ordinary turn over the compacted checkpoint.
    monkeypatch.setattr(public_memory, "_source_reader", PublicMemorySource(source.path))
    h.mw = memory.SessionSummaryMiddleware(config=h.config)
    h.mw.after_agent(h.state, None)
    assert "PRIVATE" not in json.dumps(h.card()) + json.dumps(h.calls)
    assert "rocket planning" in memory.load_digest(memory_dir=str(tmp_path)).block

    # Source changes while an auxiliary call is running, without a card rewrite.
    source.save(public * 2)
    h.mw.after_agent(h.state, None)
    before = h.card()

    def invoke(_):
        source.save(public * 3)
        return AIMessage(content="PRIVATE-STALE")

    monkeypatch.setattr(session_gist, "create_role_llm", lambda *_: SimpleNamespace(invoke=invoke))
    h.timers[-1].fire()
    assert h.card() == before
    assert memory.load_digest(memory_dir=str(tmp_path)).entries == []
