"""Per-frame timing and dispatch of the decomposed streaming event loop (#3880).

``server.turn_stream._run_turn_stream`` dispatches each ``astream_events`` event to one
handler, a lazy generator the loop re-yields frame by frame. The characterization goldens
pin WHAT frames come out. These pin WHEN a handler's work runs relative to the frames it
yields, which a refactor could change while every golden still passes:

* work after a frame (the model-end latency read and ``metrics.record_llm_call`` after
  its ``tool_start`` cards) runs only once the consumer pulls the next frame, and never if
  the consumer closes the stream at that frame. A handler that returned a list, or
  recorded telemetry before yielding the cards, would pass the goldens and fail here.
* events with no handler produce no frames and leave no state behind.
"""

from __future__ import annotations

import importlib

import pytest

from tests._turn_driver_fakes import (
    Clock,
    ScriptedGraph,
    custom,
    model_end,
    model_start,
    text,
    tool_end,
    tool_msg,
    tool_start,
)

turn_stream = importlib.import_module("server.turn_stream")


@pytest.fixture
def env(monkeypatch):
    from observability import metrics, pricing

    import runtime.state as rs

    class Env:
        pass

    e = Env()
    e.clock = Clock()
    monkeypatch.setattr(turn_stream, "time", e.clock)
    e.llm_calls = []
    monkeypatch.setattr(metrics, "record_llm_call", lambda *a, **k: e.llm_calls.append((a, k)))
    monkeypatch.setattr(pricing, "cost_usd", lambda model, usage: 0.0)
    for attr, val in {"background_mgr": None, "goal_controller": None}.items():
        monkeypatch.setattr(rs.STATE, attr, val, raising=False)

    def install(*events):
        e.graph = ScriptedGraph([list(events)])
        monkeypatch.setattr(rs.STATE, "graph", e.graph, raising=False)

    e.install = install
    return e


def _stream():
    return turn_stream._run_turn_stream("hi", "s-h", {"configurable": {"thread_id": "t-h"}})


def _two_tool_calls_with_usage():
    return model_end("m1", tool_calls=[("tc1", "a", {}), ("tc2", "b", {})], usage=(10, 1, 0, 0))


@pytest.mark.asyncio
async def test_model_end_records_telemetry_only_after_its_cards_are_consumed(env):
    env.install(model_start("m1"), _two_tool_calls_with_usage())
    gen = _stream()

    assert (await gen.__anext__())[0] == "tool_start"
    assert (await gen.__anext__())[0] == "tool_start"
    # Both cards are out; the handler is suspended at the second one — no telemetry yet.
    assert env.llm_calls == []
    # The consumer takes 2s before asking for more: the latency is read AFTER that.
    env.clock.now += 2.0
    kind, payload = await gen.__anext__()
    assert kind == "usage" and payload["input_tokens"] == 10
    assert len(env.llm_calls) == 1
    assert env.llm_calls[0][0][2] == 2.0  # latency_s
    assert await gen.__anext__() == ("__raw__", "")
    await gen.aclose()


@pytest.mark.asyncio
async def test_closing_at_a_card_skips_the_rest_of_its_handler(env):
    env.install(model_start("m1"), _two_tool_calls_with_usage())
    gen = _stream()
    assert (await gen.__anext__())[0] == "tool_start"
    await gen.aclose()
    assert env.llm_calls == []


@pytest.mark.asyncio
async def test_events_without_a_handler_produce_nothing(env):
    env.install(
        # Shaped like a text chunk, but under kinds the loop does not handle.
        {**text("x1", "leak"), "event": "on_chain_stream"},
        {**text("x2", "leak"), "event": "on_llm_stream"},
        custom("not_a_lane", {"items": [{"id": "1", "text": "t"}], "input_tokens": 5}),
        text("r1", "answer"),
    )
    frames = [f async for f in _stream()]
    assert frames == [("text", "answer"), ("__raw__", "answer")]


def test_handler_map():
    """Each handled event kind maps to exactly one handler, and every other kind to none."""
    ts = turn_stream
    assert ts._handler_for("on_chat_model_start", "model") is ts._on_chat_model_start
    assert ts._handler_for("on_chat_model_stream", "model") is ts._on_chat_model_stream
    assert ts._handler_for("on_chat_model_end", "model") is ts._on_chat_model_end
    assert ts._handler_for("on_tool_start", "x") is ts._on_tool_start
    assert ts._handler_for("on_tool_end", "x") is ts._on_tool_end
    assert ts._handler_for("on_custom_event", "usage") is ts._on_custom_usage
    assert ts._handler_for("on_custom_event", "steer_consumed") is ts._on_custom_steer_consumed
    assert ts._handler_for("on_custom_event", "other") is None
    assert ts._handler_for("on_chain_end", "usage") is None
    assert ts._handler_for("", "") is None


# ── per-run bookkeeping is consumed at tool end (#3883) ──────────────────────
#
# ``_on_tool_start`` stashes per-run_id state (the execution-latency stamp, a foreground
# ``delegate_to``'s target) that ``_on_tool_end`` must REMOVE, not just read: a run_id seen
# again later in the same turn is a fresh start. Reading without removing would carry the
# first run's stamp/target into the second — a stale latency, or a plain tool card
# rendered as the earlier delegate's room bubble — and grow the maps for a long turn.


@pytest.mark.asyncio
async def test_a_reused_run_id_for_plain_tools_starts_fresh(env):
    c = env.clock
    env.install(
        tool_start("t1", "get_time"),
        c.tick(0.5),
        tool_end("t1", "get_time", tool_msg("noon", "tc1")),
        c.tick(2),
        # The same run_id ends again with no start of its own: no latency stamp is left
        # over from the first run, so it measures 0 — not the 2s since that stale stamp.
        tool_end("t1", "get_time", tool_msg("later", "tc2")),
    )
    frames = [f async for f in _stream() if f[0] == "tool_end"]
    assert [(p["id"], p["duration_ms"]) for _, p in frames] == [("tc1", 500), ("tc2", 0)]


@pytest.mark.asyncio
async def test_a_reused_run_id_for_delegate_to_starts_fresh(env):
    c = env.clock
    env.install(
        tool_start("f1", "delegate_to", {"target": "proto", "query": "Review it"}),
        tool_end("f1", "delegate_to", tool_msg("LGTM", "d1")),
        # The same run_id now carries a delegate_to with no target: nothing is stashed, so
        # it closes as an ordinary tool card — not as another bubble authored by `proto`.
        tool_start("f1", "delegate_to", {"target": "  "}),
        c.tick(0.1),
        tool_end("f1", "delegate_to", tool_msg("Error: target required", "d2")),
    )
    frames = [f async for f in _stream() if f[0] in ("room_reply", "tool_end")]
    assert [(k, p.get("author") or p.get("addressed_to") or p.get("id")) for k, p in frames] == [
        ("room_reply", "proto"),  # the lead's ask
        ("room_reply", "proto"),  # proto's reply
        ("tool_end", "d2"),
    ]
    assert frames[1][1]["text"] == "LGTM"
    assert frames[2][1]["output"] == "Error: target required"
    assert frames[2][1]["duration_ms"] == 100
