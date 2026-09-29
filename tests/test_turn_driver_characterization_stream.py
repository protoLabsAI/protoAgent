"""Characterization of the STREAMING turn driver (epic #3804, phase A).

Pins today's observable behaviour of ``_chat_langgraph_stream`` → ``_chat_langgraph_stream_impl``
→ ``_run_native_turn`` → ``_run_turn_stream`` before the split refactor: the exact
``(kind, payload)`` frame sequence for scripted graph event streams, and the side effects
at the boundaries (graph inputs + configs, the failed-turn record, telemetry, tracing).

These are GOLDENS of current behaviour, oddities included (each flagged ``ODDITY``) — a
refactor that changes any of them must change the golden on purpose, not by accident.
The only fake is the graph (``tests/_turn_driver_fakes.ScriptedGraph``) plus the
external services (pricing, metrics, compaction, tracing); the drivers run for real.
"""

from __future__ import annotations

import asyncio
import importlib
import json

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langgraph.types import Command

from graph.config import LangGraphConfig
from tests._turn_driver_fakes import (
    Clock,
    FakeGoals,
    Raise,
    ScriptedGraph,
    TraceSpy,
    chunk,
    custom,
    model_end,
    model_start,
    reasoning,
    set_interrupt,
    text,
    tool_chunk,
    tool_end,
    tool_msg,
    tool_start,
)

chat_mod = importlib.import_module("server.chat")
turn_control = importlib.import_module("server.turn_control")
turn_stream_mod = importlib.import_module("server.turn_stream")

# Modules whose ``time`` the deterministic clock replaces. A refactor that moves the
# event loop into a new module adds that module here.
_CLOCK_MODULES = ("server.chat", "server.turn_stream")

_OVERFLOW = "Error code: 400 - This model's maximum context length is 128000 tokens."
_EMPTY = "_(The agent ended the turn without a textual reply.)_"


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def env(monkeypatch):
    """Install a scripted graph + deterministic services; returns a namespace."""
    from observability import metrics, pricing

    import runtime.state as rs

    class Env:
        pass

    e = Env()
    e.clock = Clock()
    for mod in _CLOCK_MODULES:
        monkeypatch.setattr(importlib.import_module(mod), "time", e.clock)
    e.llm_calls = []
    monkeypatch.setattr(metrics, "record_llm_call", lambda *a, **k: e.llm_calls.append((a, k)))
    e.overflow_recoveries = []
    monkeypatch.setattr(metrics, "record_overflow_recovery", lambda: e.overflow_recoveries.append(1))
    monkeypatch.setattr(pricing, "cost_usd", lambda model, usage: round(usage["input_tokens"] / 1000, 6))
    e.trace = TraceSpy().install(monkeypatch)
    for attr, val in {
        "goal_controller": None,
        "background_mgr": None,
        "watch_controller": None,
        "scheduler": None,
        "graph_auth_error": None,
        "thread_id_resolver": None,
        "checkpointer": object(),
        "knowledge_store": None,
        "graph_config": LangGraphConfig(),
    }.items():
        monkeypatch.setattr(rs.STATE, attr, val, raising=False)

    def install(streams=(), invokes=()):
        e.graph = ScriptedGraph(streams, invokes)
        monkeypatch.setattr(rs.STATE, "graph", e.graph, raising=False)
        return e.graph

    e.install = install
    e.state = rs.STATE
    yield e
    # A driver making a graph call the test didn't script must fail the test even when the
    # driver's own error handling swallowed the fake's AssertionError into a frame/record.
    graph = getattr(e, "graph", None)
    assert graph is None or not graph.overrun, "driver made an unscripted graph call"


async def _run(message="hello", session_id="s1", **kw):
    return [f async for f in chat_mod._chat_langgraph_stream(message, session_id, **kw)]


def _usage(tin, tout, cread, ccreate, model):
    return {
        "input_tokens": tin,
        "output_tokens": tout,
        "cache_read_input_tokens": cread,
        "cache_creation_input_tokens": ccreate,
        "cost_usd": round(tin / 1000, 6),
        "model": model,
    }


# ── _run_turn_stream: event → frame goldens ───────────────────────────────────


@pytest.mark.asyncio
async def test_golden_answer_with_a_tool_round_trip(env):
    c = env.clock
    g = env.install(
        streams=[
            [
                model_start("r1"),
                reasoning("r1", "plan it"),
                text("r1", "I'll check"),
                text("r1", " the time."),
                tool_chunk("r1", "tc1", "get_time"),
                tool_chunk("r1", "tc1", "get_time"),  # already announced → no second early card
                tool_chunk("r1", "tc2", None),  # a name-less chunk announces nothing
                c.tick(2),
                model_end(
                    "r1",
                    tool_calls=[("tc1", "get_time", {"tz": "UTC"})],
                    usage=(100, 10, 40, 5),
                    ls_model_name="lead-model",
                ),
                tool_start("t1", "get_time", {"tz": "UTC"}),
                c.tick(0.25),
                tool_end("t1", "get_time", tool_msg("noon", "tc1")),
                model_start("r2"),
                text("r2", "It is noon."),
                c.tick(1),
                model_end("r2", usage=(150, 5, 0, 0)),
            ]
        ]
    )

    frames = await _run("what time is it?", "s-golden")

    assert frames == [
        ("reasoning", "plan it"),
        ("text", "I'll check"),
        ("text", " the time."),
        ("tool_start", {"id": "tc1", "name": "get_time", "input": ""}),
        # ODDITY (documented contract): on_chat_model_end RE-emits a start for an id the
        # stream already announced, now with the full args — the console upserts by id.
        ("tool_start", {"id": "tc1", "name": "get_time", "input": '{"tz": "UTC"}'}),
        ("usage", _usage(100, 10, 40, 5, "lead-model")),
        (
            "tool_end",
            {"id": "tc1", "name": "get_time", "output": "noon", "output_chars": 4, "error": False, "duration_ms": 250},
        ),
        # A different model call's text opens a new paragraph — on the DELTA itself.
        ("text", "\n\nIt is noon."),
        ("usage", _usage(150, 5, 0, 0, "resp-model")),
        ("done", "I'll check the time.\n\nIt is noon."),
    ]
    # Per-call Prometheus seam: latency from on_chat_model_start → end, per run id.
    assert [(a, k) for a, k in env.llm_calls] == [
        (
            ("lead-model", "stop", 2.0),
            {"tokens_input": 100, "tokens_output": 10, "cache_read": 40, "cache_creation": 5, "cost_usd": 0.1},
        ),
        (
            ("resp-model", "stop", 1.0),
            {"tokens_input": 150, "tokens_output": 5, "cache_read": 0, "cache_creation": 0, "cost_usd": 0.15},
        ),
    ]
    # One graph call: the fresh user message, session id, explicit incognito=False, on the
    # a2a thread with the configured recursion limit.
    ((graph_input, config),) = g.stream_calls
    assert [type(m) for m in graph_input["messages"]] == [HumanMessage]
    assert graph_input["messages"][0].content == "what time is it?"
    assert {k: v for k, v in graph_input.items() if k != "messages"} == {"session_id": "s-golden", "incognito": False}
    assert config == {
        "configurable": {"thread_id": "a2a:s-golden"},
        "recursion_limit": LangGraphConfig().max_iterations,
    }


@pytest.mark.asyncio
async def test_golden_filtered_and_degenerate_events(env):
    c = env.clock
    env.install(
        streams=[
            [
                model_start(None),  # no run id → no latency stamp, no crash
                {
                    "event": "on_chat_model_stream",
                    "name": "model",
                    "run_id": "r1",
                    "metadata": {},
                    "data": {},
                },  # chunk None
                text("s1", "SUBAGENT TOKENS", parent_task_id="task-1"),  # a subagent's answer: suppressed
                tool_chunk("s1", "sub-tc", "read_file", parent_task_id="task-1"),  # its card nests by id
                tool_start("st1", "read_file", parent_task_id="task-1"),
                tool_end("st1", "read_file", tool_msg("file body", "sub-tc"), parent_task_id="task-1"),  # nests too
                text(
                    "w1", "WORKFLOW TOKENS", langgraph_checkpoint_ns="tools:abc|model:def"
                ),  # under a tool: suppressed
                text("m1", "COMPACTION SUMMARY", lc_source="summarization"),  # middleware call: suppressed
                chunk("r1", AIMessageChunk(content=[{"type": "reasoning", "summary": []}])),  # blocks, no text
                chunk(
                    "r1", AIMessageChunk(content=[{"type": "text", "text": "Block answer."}])
                ),  # Responses-API blocks
                c.tick(3),
                model_end("s1", usage=(7, 3, 0, 0), parent_task_id="task-1"),  # billed via metrics, NOT a usage frame
                model_end("r1"),  # no usage → no frame
                model_end("r1", output=False),  # no output at all
                model_end("r9", tool_calls=[(None, "nameless_id", {})]),  # a tool call with no id: no card
                custom("usage", "not a dict"),
                custom("usage", {"input_tokens": 5, "output_tokens": 1, "subagent_type": "researcher"}),
                custom("steer_consumed", {"items": [{"id": "", "text": "x"}, "junk"]}),  # nothing clean → no frame
                custom("steer_consumed", {"items": "bad"}),
                custom("steer_consumed", {"items": [{"id": "st1", "text": "go left"}]}),
                custom("some_other_event", {"a": 1}),
                tool_start(None, "orphan"),
                tool_end(None, "orphan", {"k": "v"}),  # dict output, no run id, no tool_call_id
            ]
        ]
    )

    frames = await _run()

    assert frames == [
        ("tool_start", {"id": "sub-tc", "name": "read_file", "input": "", "parentId": "task-1"}),
        (
            "tool_end",
            {
                "id": "sub-tc",
                "name": "read_file",
                "output": "file body",
                "output_chars": 9,
                "error": False,
                "duration_ms": 0,
                "parentId": "task-1",
            },
        ),
        ("text", "Block answer."),
        ("usage", {"input_tokens": 5, "output_tokens": 1, "subagent_type": "researcher"}),
        ("steer_consumed", {"items": [{"id": "st1", "text": "go left"}]}),
        # No run id and no tool_call_id: the card id falls back to the tool NAME.
        (
            "tool_end",
            {
                "id": "orphan",
                "name": "orphan",
                "output": '{"k": "v"}',
                "output_chars": 10,
                "error": False,
                "duration_ms": 0,
            },
        ),
        ("done", "Block answer."),
    ]
    # The subagent's call still reaches the per-call metrics (latency 0: never stamped).
    assert [a for a, _ in env.llm_calls] == [("resp-model", "stop", 0.0)]


@pytest.mark.asyncio
async def test_golden_component_and_failed_tool_results(env):
    from graph.components import encode_component

    payload = encode_component("keyvalue", {"items": [{"k": "a", "v": 1}]})
    env.install(
        streams=[
            [
                tool_start("t1", "show_component"),
                tool_end("t1", "show_component", tool_msg("Here it is.\n" + payload, "tc1")),
                tool_start("t2", "run_command"),
                tool_end("t2", "run_command", tool_msg("declined by operator", "tc2", status="error")),
                tool_start("t3", "big"),
                tool_end("t3", "big", tool_msg("x" * 2000, "tc3")),
                text("r1", "ok"),
            ]
        ]
    )

    frames = await _run()

    assert frames == [
        ("component", {"component": "keyvalue", "props": {"items": [{"k": "a", "v": 1}]}}),
        # The card keeps only the human prefix; output_chars is the FULL (sentinel) size.
        (
            "tool_end",
            {
                "id": "tc1",
                "name": "show_component",
                "output": "Here it is.",
                "output_chars": len("Here it is.\n" + payload),
                "error": False,
                "duration_ms": 0,
            },
        ),
        (
            "tool_end",
            {
                "id": "tc2",
                "name": "run_command",
                "output": "declined by operator",
                "output_chars": 20,
                "error": True,
                "duration_ms": 0,
            },
        ),
        (
            "tool_end",
            {
                "id": "tc3",
                "name": "big",
                "output": "x" * chat_mod._TOOL_PREVIEW_CHARS,
                "output_chars": 2000,
                "error": False,
                "duration_ms": 0,
            },
        ),
        ("text", "ok"),
        ("done", "ok"),
    ]


@pytest.mark.asyncio
async def test_golden_delegate_to_renders_as_room_bubbles(env):
    job = "bg-0123456789ab"
    env.install(
        streams=[
            [
                # tool_call chunks / finalize for delegate_to never make a card.
                tool_chunk("r1", "d1", "delegate_to"),
                model_end("r1", tool_calls=[("d1", "delegate_to", {"target": "proto"})]),
                # Foreground, with a query: the ask, then the reply as the participant.
                tool_start(
                    "f1", "delegate_to", {"target": "proto", "query": "Review the diff. Be strict.", "summary": ""}
                ),
                tool_end("f1", "delegate_to", tool_msg("LGTM", "d1")),
                # Foreground, no query: only the reply; an error ToolMessage → ok False.
                tool_start("f2", "delegate_to", {"target": "rev"}),
                tool_end("f2", "delegate_to", tool_msg("boom", "d2", status="error")),
                # Background with a job handle: one ask row carrying the job id; no reply.
                tool_start(
                    "b1",
                    "delegate_to",
                    {"target": "proto", "query": "Long job", "summary": "the job", "background": True},
                ),
                tool_end("b1", "delegate_to", tool_msg(f"Dispatched (job `{job}`) — you'll be notified.", "d3")),
                # Background refused: the ask row, failed, carrying the error.
                tool_start("b2", "delegate_to", {"target": "ghost", "query": "Hi", "background": True}),
                tool_end("b2", "delegate_to", tool_msg("Error: unknown delegate ghost", "d4")),
                # Background with no manager wired: inline fallback → ask + reply.
                tool_start("b3", "delegate_to", {"target": "proto", "query": "Quick one", "background": True}),
                tool_end("b3", "delegate_to", tool_msg("inline answer", "d5")),
                # ODDITY: an EMPTY target is not a delegation at all — it closes as an
                # ordinary delegate_to tool card (no start card was ever announced for it).
                tool_start("e1", "delegate_to", {"target": "  "}),
                tool_end("e1", "delegate_to", tool_msg("Error: target required", "d6")),
                text("r2", "done delegating"),
            ]
        ]
    )

    frames = await _run()

    assert frames == [
        (
            "room_reply",
            {
                "id": "f1",
                "addressed_to": "proto",
                "text": "Review the diff. Be strict.",
                "summary": "Review the diff.",
                "ok": True,
            },
        ),
        (
            "room_reply",
            {"author": "proto", "from": "assistant", "text": "LGTM", "ok": True, "catchup": 0, "truncated": False},
        ),
        (
            "room_reply",
            {"author": "rev", "from": "assistant", "text": "boom", "ok": False, "catchup": 0, "truncated": False},
        ),
        (
            "room_reply",
            {
                "id": "b1",
                "addressed_to": "proto",
                "text": "Long job",
                "summary": "the job",
                "background": True,
                "ok": True,
                "job_id": job,
            },
        ),
        (
            "room_reply",
            {
                "id": "b2",
                "addressed_to": "ghost",
                "text": "Hi",
                "summary": "Hi",
                "background": True,
                "ok": False,
                "error": "Error: unknown delegate ghost",
            },
        ),
        ("room_reply", {"id": "b3", "addressed_to": "proto", "text": "Quick one", "summary": "Quick one", "ok": True}),
        (
            "room_reply",
            {
                "author": "proto",
                "from": "assistant",
                "text": "inline answer",
                "ok": True,
                "catchup": 0,
                "truncated": False,
            },
        ),
        (
            "tool_end",
            {
                "id": "d6",
                "name": "delegate_to",
                "output": "Error: target required",
                "output_chars": 22,
                "error": False,
                "duration_ms": 0,
            },
        ),
        ("text", "done delegating"),
        ("done", "done delegating"),
    ]


@pytest.mark.asyncio
async def test_background_drain_replies_lead_and_messages_prepend(env, monkeypatch):
    from types import SimpleNamespace

    jobs = [
        SimpleNamespace(id="j1", result="the delegate's words", result_author="proto", status="completed"),
        SimpleNamespace(
            id="j2",
            result="R" * (chat_mod._BG_RESULT_CAP + 5),
            result_author="",
            status="completed",
            deterministic=False,
            origin_incognito=False,
            origin_session="s1",
            subagent_type="researcher",
            description="dig",
        ),
    ]
    drained = []

    class _Store:
        def drain_pending(self, sid):
            drained.append(sid)
            return jobs

    monkeypatch.setattr(env.state, "background_mgr", SimpleNamespace(store=_Store()), raising=False)
    g = env.install(streams=[[text("r1", "synth")]])

    frames = await _run()

    assert frames[0] == (
        "room_reply",
        {"id": "j1", "author": "proto", "from": "assistant", "text": "the delegate's words", "ok": True},
    )
    assert frames[1:] == [("text", "synth"), ("done", "synth")]
    assert drained == ["s1"]
    msgs = g.stream_calls[0][0]["messages"]
    assert [type(m) for m in msgs] == [HumanMessage, HumanMessage, HumanMessage]
    assert msgs[0].additional_kwargs == {"lc_source": "room", "room": {"from": "proto"}}
    assert "<job-id>j2</job-id>" in msgs[1].content and "indexed and searchable via memory_recall" in msgs[1].content
    assert msgs[2].content == "hello"


@pytest.mark.asyncio
async def test_background_drain_failure_never_breaks_the_turn(env, monkeypatch):
    from types import SimpleNamespace

    class _Store:
        def drain_pending(self, sid):
            raise RuntimeError("db locked")

    monkeypatch.setattr(env.state, "background_mgr", SimpleNamespace(store=_Store()), raising=False)
    g = env.install(streams=[[text("r1", "fine")]])

    assert await _run() == [("text", "fine"), ("done", "fine")]
    assert [m.content for m in g.stream_calls[0][0]["messages"]] == ["hello"]


# ── _run_native_turn: request metadata, HITL, empty replies ───────────────────


@pytest.mark.asyncio
async def test_request_metadata_threads_into_the_graph_input(env):
    g = env.install(streams=[[text("r1", "a")]])

    await _run(
        request_metadata={
            "model": " gpt-x ",
            "reasoning_effort": "high",
            "incognito": True,
            "subagent_fence": ["read_file", 3],
        }
    )

    graph_input = g.stream_calls[0][0]
    assert {k: v for k, v in graph_input.items() if k != "messages"} == {
        "session_id": "s1",
        "incognito": True,
        "model": "gpt-x",
        "reasoning_effort": "high",
        "subagent_fence": ["read_file", "3"],
    }
    # Incognito: the trace keeps its structure but no content preview.
    assert "message_preview" not in env.trace.sessions[0]["metadata"]
    assert env.trace.sessions[0]["incognito"] is True


@pytest.mark.asyncio
async def test_a_non_list_fence_is_ignored(env):
    g = env.install(streams=[[text("r1", "a")]])

    await _run(request_metadata={"subagent_fence": "read_file"})

    assert "subagent_fence" not in g.stream_calls[0][0]


@pytest.mark.asyncio
async def test_operator_turn_parks_on_hitl_and_emits_no_done(env):
    g = env.install(streams=[[text("r1", "Let me ask."), set_interrupt({"question": "Which env?"})]])

    frames = await _run()

    assert frames == [("text", "Let me ask."), ("input_required", {"question": "Which env?"})]
    assert g.updates == []  # nothing recorded, nothing cleared
    assert env.trace.outputs == ["[input required] Which env?"]


@pytest.mark.asyncio
async def test_resume_feeds_the_answer_by_interrupt_id_and_skips_the_drain(env, monkeypatch):
    from types import SimpleNamespace

    drained = []

    class _Store:
        def drain_pending(self, sid):
            # Recorded, not raised: _drain_background swallows drain errors by design.
            drained.append(sid)
            return []

    monkeypatch.setattr(env.state, "background_mgr", SimpleNamespace(store=_Store()), raising=False)
    g = env.install(streams=[[text("r1", "Staging it is.")]])
    g.pending.append({"question": "Which env?"})

    frames = await _run("staging", resume=True)

    assert drained == []  # a resume never drains background completions (the next fresh turn does)

    assert frames == [("text", "Staging it is."), ("done", "Staging it is.")]
    graph_input = g.stream_calls[0][0]
    assert isinstance(graph_input, Command)
    assert g.resumes == [{"int-0": "staging"}]


@pytest.mark.asyncio
async def test_hitl_hold_reparks_a_fresh_message_without_running_the_graph(env):
    from graph import steering

    g = env.install(streams=[])
    g.pending.append({"question": "Which env?"})
    try:
        frames = await _run("also, use the fast path")

        assert frames == [("input_required", {"question": "Which env?"})]
        assert g.stream_calls == []
        assert steering.pending("s-hold") == 0 and steering.pending("s1") == 1
    finally:
        steering.forget("s1")


@pytest.mark.asyncio
async def test_hitl_hold_converts_a_marked_answer_into_a_resume(env):
    g = env.install(streams=[[text("r1", "thanks")]])
    g.pending.append({"kind": "form", "title": "Details", "steps": []})

    frames = await _run('{"env": "prod"}', request_metadata={"hitl_resume": True})

    assert frames == [("text", "thanks"), ("done", "thanks")]
    assert isinstance(g.stream_calls[0][0], Command)
    assert g.resumes == [{"int-0": '{"env": "prod"}'}]


@pytest.mark.asyncio
async def test_autonomous_turn_auto_answers_then_gives_up_and_clears(env):
    cap = turn_control._MAX_AUTONOMOUS_AUTOANSWERS
    # Every pass re-parks: the budget is spent, the interrupt cleared, the turn completes.
    streams = [[text("r0", "asking"), set_interrupt("Which env?")]] + [
        [set_interrupt("Still: which env?")] for _ in range(cap)
    ]
    g = env.install(streams=streams)

    frames = await _run("deploy", request_metadata={"origin": "scheduler"})

    # ODDITY: text streamed before an auto-answered interrupt never reaches `done` — the
    # paused pass yields `input_required` instead of `__raw__`, so its text is dropped
    # from the final answer (the live stream showed it).
    assert frames == [("text", "asking"), ("done", _EMPTY)]
    assert len(g.stream_calls) == cap + 1
    assert g.resumes == [{"int-0": turn_control._AUTONOMOUS_HITL_SENTINEL}] * cap
    assert g.updates == [
        ({"configurable": {"thread_id": "a2a:s1"}, "recursion_limit": LangGraphConfig().max_iterations}, None)
    ]


@pytest.mark.asyncio
async def test_empty_reply_falls_back_to_the_last_tool_output(env):
    env.install(
        streams=[
            [
                tool_start("t1", "wait"),
                tool_end("t1", "wait", tool_msg("Wait scheduled for 5m.", "tc1")),
                tool_start("t2", "noop"),
                tool_end("t2", "noop", tool_msg("", "tc2")),
            ]
        ]
    )

    frames = await _run()

    assert frames[-1] == ("done", "Wait scheduled for 5m.")


@pytest.mark.asyncio
async def test_empty_reply_with_no_tools_is_a_placeholder(env):
    env.install(streams=[[reasoning("r1", "hmm")]])

    assert await _run() == [("reasoning", "hmm"), ("done", _EMPTY)]


@pytest.mark.asyncio
async def test_leaked_reasoning_is_stripped_from_done_but_not_the_stream(env):
    env.install(streams=[[text("r1", "<scratch_pad>x</scratch_pad> Final. ")]])

    frames = await _run()

    # ODDITY: the live stream carries the leaked reasoning verbatim; only the terminal
    # `done` text (extract_output) is cleaned — the console reconciles to `done`.
    assert frames[0] == ("text", "<scratch_pad>x</scratch_pad> Final. ")
    assert frames[-1] == ("done", "Final.")


# ── goal mode ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_goal_kickoff_continuation_and_done_note(env, monkeypatch):
    goals = FakeGoals([("continue", "1/8 — not yet", "keep going"), ("done", "✅ goal met")])
    monkeypatch.setattr(env.state, "goal_controller", goals, raising=False)
    g = env.install(streams=[[text("r1", "draft")], [text("r2", "better")]])

    frames = await _run("ship it")

    assert frames == [
        ("text", "draft"),
        ("tool_start", "🎯 1/8 — not yet"),
        ("text", "better"),
        ("tool_start", "🎯 ✅ goal met"),
        ("done", "better\n\n---\n✅ goal met"),
    ]
    assert goals.kickoffs == ["ship it"]
    assert g.stream_calls[0][0]["messages"][-1].content == "KICKOFF<ship it>"
    assert g.stream_calls[1][0]["messages"][-1].content == "keep going"
    assert goals.evals == ["draft", "better"]
    # Same-session goal: the continuation reuses the turn's config.
    assert g.stream_calls[1][1] is g.stream_calls[0][1]


@pytest.mark.asyncio
async def test_goal_resume_is_not_rewritten_into_a_kickoff(env, monkeypatch):
    goals = FakeGoals([("done", "met")])
    monkeypatch.setattr(env.state, "goal_controller", goals, raising=False)
    g = env.install(streams=[[text("r1", "resumed")]])
    g.pending.append({"question": "Which env?"})

    frames = await _run("staging", resume=True)

    assert goals.kickoffs == []  # iteration 0, but a HITL answer is not the goal's first message
    assert g.resumes == [{"int-0": "staging"}]
    assert frames[-1] == ("done", "resumed\n\n---\nmet")


@pytest.mark.asyncio
async def test_goal_fresh_context_continuation_gets_a_scoped_thread(env, monkeypatch):
    goals = FakeGoals([("continue", "again", "iterate"), None], iteration=3, fresh=True)
    monkeypatch.setattr(env.state, "goal_controller", goals, raising=False)
    g = env.install(streams=[[text("r1", "one")], [text("r2", "")]])

    frames = await _run("go")

    assert goals.kickoffs == []  # iteration > 0: no kickoff rewrite
    assert g.stream_calls[1][1] == {
        "configurable": {"thread_id": "a2a:s1:goal-iter-4"},
        "recursion_limit": LangGraphConfig().max_iterations,
    }
    # The empty continuation keeps the previous answer; the last NOTE still appends.
    assert frames[-1] == ("done", "one\n\n---\nagain")


@pytest.mark.asyncio
async def test_goal_pauses_when_the_agent_handed_off_to_a_watch(env, monkeypatch):
    from types import SimpleNamespace

    goals = FakeGoals([("continue", "not yet", "more")])
    monkeypatch.setattr(env.state, "goal_controller", goals, raising=False)
    monkeypatch.setattr(
        env.state,
        "watch_controller",
        SimpleNamespace(list_watches=lambda: [SimpleNamespace(status="active", run_session="s1")]),
        raising=False,
    )
    g = env.install(streams=[[text("r1", "watching")]])

    frames = await _run()

    pause = "⏸ goal paused — handed off to a watch/schedule; will resume when it fires."
    assert frames == [
        ("text", "watching"),
        ("tool_start", "🎯 not yet"),
        ("tool_start", f"🎯 {pause}"),
        ("done", f"watching\n\n---\n{pause}"),
    ]
    assert len(g.stream_calls) == 1


@pytest.mark.asyncio
async def test_goal_loop_is_bounded_by_the_hard_cap(env, monkeypatch):
    goals = FakeGoals(forever=("continue", "nope", "again"))
    monkeypatch.setattr(env.state, "goal_controller", goals, raising=False)
    cap = LangGraphConfig().goal_max_iterations + 2
    env.install(streams=[[text("r0", "t0")]] + [[text(f"r{i}", f"t{i}")] for i in range(1, cap + 1)])

    frames = await _run()

    assert len(goals.evals) == cap
    assert frames[-1] == ("done", f"t{cap}\n\n---\nnope")


@pytest.mark.asyncio
async def test_goal_evaluate_none_ends_without_a_note(env, monkeypatch):
    monkeypatch.setattr(env.state, "goal_controller", FakeGoals([None]), raising=False)
    env.install(streams=[[text("r1", "plain")]])

    assert await _run() == [("text", "plain"), ("done", "plain")]


@pytest.mark.asyncio
async def test_goal_turn_is_autonomous_and_never_parks(env, monkeypatch):
    goals = FakeGoals([("done", "met")])
    monkeypatch.setattr(env.state, "goal_controller", goals, raising=False)
    g = env.install(streams=[[set_interrupt("what goal?")], [text("r1", "on it")]])

    frames = await _run()  # plain A2A, no autonomous origin

    assert ("input_required", {"question": "what goal?"}) not in frames
    assert g.resumes == [{"int-0": turn_control._AUTONOMOUS_HITL_SENTINEL}]
    assert frames[-1] == ("done", "on it\n\n---\nmet")


# ── _chat_langgraph_stream_impl: setup, tracing, errors, overflow, cancel ─────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("auth_err", "expected"),
    [
        (None, "setup required — finish the setup wizard before calling A2A endpoints"),
        ({"message": "Codex signed out"}, "Codex signed out"),
        ({}, "setup required — finish the setup wizard before calling A2A endpoints"),
        ({"message": ""}, "provider disconnected — reconnect from the console"),
    ],
)
async def test_graphless_turn_yields_one_error_frame(env, monkeypatch, auth_err, expected):
    monkeypatch.setattr(env.state, "graph", None, raising=False)
    monkeypatch.setattr(env.state, "graph_auth_error", auth_err, raising=False)

    assert await _run() == [("error", expected)]
    # ODDITY: the graphless error returns BEFORE trace_session opens, so no trace is
    # started and nothing is flushed for it (the wrapper still records the output).
    assert env.trace.sessions == [] and env.trace.flushes == 0


@pytest.mark.asyncio
async def test_trace_session_metadata_and_flush(env):
    env.install(streams=[[text("r1", "hi")]])

    await _run("hello sk-" + "A" * 40, caller_trace={"traceId": "T1", "spanId": "S1"})

    (sess,) = env.trace.sessions
    assert sess["name"] == "a2a-stream" and sess["session_id"] == "s1" and sess["incognito"] is False
    assert set(sess["metadata"]) == {"soul_rev", "message_preview", "caller_trace_id", "caller_span_id"}
    assert sess["metadata"]["caller_trace_id"] == "T1" and sess["metadata"]["caller_span_id"] == "S1"
    assert "A" * 40 not in sess["input"] and "A" * 40 not in sess["metadata"]["message_preview"]
    assert env.trace.flushes == 1
    assert env.trace.outputs == ["hi"]


@pytest.mark.asyncio
async def test_a_partial_caller_trace_stamps_only_what_it_has(env):
    env.install(streams=[[text("r1", "hi")]])

    await _run(caller_trace={"spanId": "S1"})

    assert "caller_trace_id" not in env.trace.sessions[0]["metadata"]
    assert env.trace.sessions[0]["metadata"]["caller_span_id"] == "S1"


@pytest.mark.asyncio
async def test_mid_stream_failure_yields_partial_frames_then_error_and_records(env):
    g = env.install(streams=[[text("r1", "partial"), Raise(ValueError("invalid api key"))]])

    frames = await _run("hello", "s-err")

    assert frames == [("text", "partial"), ("error", "invalid api key")]
    ((cfg, values),) = g.updates
    assert cfg == {"configurable": {"thread_id": "a2a:s-err"}}
    (msg,) = values["messages"]
    assert isinstance(msg, AIMessage) and msg.content == "**Error:** invalid api key"
    assert msg.additional_kwargs == {"protoagent_turn_failed": True}
    assert env.trace.outputs == ["[error] invalid api key"] and env.trace.flushes == 1
    assert turn_control.active_turns() == 0


@pytest.mark.asyncio
async def test_provider_stream_drop_is_a_clean_rate_limit_message(env):
    import httpx

    g = env.install(streams=[[text("r1", "par"), Raise(httpx.ReadError("closed"))]])

    frames = await _run()

    assert frames == [("text", "par"), ("error", chat_mod._PROVIDER_CLOSED_MSG)]
    assert g.updates[0][1]["messages"][0].content == f"**Error:** {chat_mod._PROVIDER_CLOSED_MSG}"


@pytest.mark.asyncio
async def test_record_failed_turn_failure_does_not_mask_the_error(env):
    g = env.install(streams=[[Raise(ValueError("boom"))]])

    async def _bad_update(config, values):
        raise RuntimeError("checkpointer down")

    g.aupdate_state = _bad_update

    assert await _run() == [("error", "boom")]


@pytest.fixture
def compaction(monkeypatch):
    """Spy on graph.compaction_op.compact_thread (the external boundary)."""
    import graph.compaction_op as cop

    calls: list[dict] = []
    result = {"value": {"removed": 12, "archived": True}}

    async def _compact(graph, checkpointer, knowledge_store, cfg, thread_id, session_id, *, force, keep_recent):
        lock = turn_control._thread_lock(thread_id)
        calls.append(
            {
                "thread_id": thread_id,
                "session_id": session_id,
                "force": force,
                "keep_recent": keep_recent,
                "locked": lock.locked(),
            }
        )
        v = result["value"]
        if isinstance(v, BaseException):
            raise v
        return v

    monkeypatch.setattr(cop, "compact_thread", _compact)
    return calls, result


@pytest.mark.asyncio
async def test_overflow_compacts_then_retries_once_with_the_recovery_prompt(env, compaction):
    calls, _ = compaction
    g = env.install(streams=[[Raise(ValueError(_OVERFLOW))], [text("r1", "recovered")]])

    frames = await _run("big ask", "s-ovf", request_metadata={"model": "m1"})

    assert frames == [("tool_start", chat_mod._OVERFLOW_NOTICE), ("text", "recovered"), ("done", "recovered")]
    assert calls == [
        {"thread_id": "a2a:s-ovf", "session_id": "s-ovf", "force": True, "keep_recent": 10, "locked": True}
    ]
    assert env.overflow_recoveries == [1]
    retry_input = g.stream_calls[1][0]
    assert retry_input["messages"][-1].content == chat_mod._OVERFLOW_RETRY_PROMPT
    assert retry_input["model"] == "m1"  # the retry keeps the request's metadata
    assert g.updates == []  # a recovered turn is not a failed one


@pytest.mark.asyncio
async def test_overflow_retry_failure_surfaces_and_records_the_second_error(env, compaction):
    calls, _ = compaction
    g = env.install(streams=[[Raise(ValueError(_OVERFLOW))], [text("r1", "half"), Raise(ValueError("second failure"))]])

    frames = await _run()

    assert frames == [("tool_start", chat_mod._OVERFLOW_NOTICE), ("text", "half"), ("error", "second failure")]
    assert len(calls) == 1 and len(g.stream_calls) == 2
    assert [v["messages"][0].content for _, v in g.updates] == ["**Error:** second failure"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [{"refused": True, "reason": "busy"}, {"removed": 0, "reason": "too_short"}, RuntimeError("summarizer down")],
)
async def test_overflow_without_a_shrink_surfaces_the_original_error(env, compaction, result):
    calls, res = compaction
    res["value"] = result
    g = env.install(streams=[[Raise(ValueError(_OVERFLOW))]])

    frames = await _run()

    assert frames == [("error", _OVERFLOW)]
    assert len(calls) == 1 and len(g.stream_calls) == 1
    assert env.overflow_recoveries == []


@pytest.mark.asyncio
async def test_overflow_compaction_uses_a_smaller_configured_keep(env, compaction, monkeypatch):
    calls, _ = compaction
    monkeypatch.setattr(env.state, "graph_config", LangGraphConfig(compaction_keep_messages=4), raising=False)
    env.install(streams=[[Raise(ValueError(_OVERFLOW))], [text("r1", "ok")]])

    await _run()

    assert calls[0]["keep_recent"] == 4


@pytest.mark.asyncio
async def test_overflow_recovery_needs_a_checkpointer(env, compaction, monkeypatch):
    calls, _ = compaction
    monkeypatch.setattr(env.state, "checkpointer", None, raising=False)
    env.install(streams=[[Raise(ValueError(_OVERFLOW))]])

    assert await _run() == [("error", _OVERFLOW)]
    assert calls == []


@pytest.mark.asyncio
async def test_a_provider_drop_that_mentions_the_context_window_is_not_compacted(env, compaction):
    import httpx

    calls, _ = compaction
    env.install(streams=[[Raise(httpx.ReadError(_OVERFLOW))]])

    assert await _run() == [("error", chat_mod._PROVIDER_CLOSED_MSG)]
    assert calls == []


@pytest.mark.asyncio
async def test_cancellation_propagates_without_an_error_frame_or_record(env):
    g = env.install(streams=[[text("r1", "partial"), Raise(asyncio.CancelledError())]])
    seen = []

    with pytest.raises(asyncio.CancelledError):
        async for f in chat_mod._chat_langgraph_stream("hello", "s-cancel"):
            seen.append(f)

    assert seen == [("text", "partial")]
    assert g.updates == []  # CancelledError is not an Exception: no failed-turn record
    assert env.trace.flushes == 1  # but the trace is still flushed
    assert turn_control.active_turns() == 0  # and the idle beacon is balanced
    assert not turn_control._thread_lock("a2a:s-cancel").locked()


@pytest.mark.asyncio
async def test_consumer_closing_early_is_silent(env):
    g = env.install(streams=[[text("r1", "one"), text("r1", "two"), text("r1", "three")]])

    agen = chat_mod._chat_langgraph_stream("hello", "s-close")
    first = await agen.__anext__()
    await agen.aclose()

    assert first == ("text", "one")
    assert g.updates == []  # an abandoned turn is not a failed one
    assert turn_control.active_turns() == 0  # the wrapper's own finally ran
    # The wrapper closes the impl inside its own aclose (#3870): the trace is flushed and
    # the per-thread lock released before aclose() returns — no GC/finalizer involved.
    assert env.trace.flushes == 1
    assert not turn_control._thread_lock("a2a:s-close").locked()


@pytest.fixture
def turn_streams(monkeypatch):
    """Wrap ``turn_stream._run_turn_stream`` (chat.py calls it through the module) so a test
    can see each graph turn's event loop EXIT — its frame unwound, however it ended."""
    import contextlib
    import types

    real = turn_stream_mod._run_turn_stream
    rec = types.SimpleNamespace(started=0, exited=0)

    async def _spy(*args, **kwargs):
        rec.started += 1
        try:
            async with contextlib.aclosing(real(*args, **kwargs)) as frames:
                async for frame in frames:
                    yield frame
        finally:
            rec.exited += 1

    monkeypatch.setattr(turn_stream_mod, "_run_turn_stream", _spy)
    return rec


@pytest.mark.asyncio
async def test_early_close_closes_the_inner_turn_generators_before_aclose_returns(env, monkeypatch, turn_streams):
    """#3877: every async-generator hop on the streaming path is closed inside the outer
    close — the native turn's ``goal_turn`` scope is reset and its event loop has exited by
    the time ``aclose()`` returns, with no GC/finalizer involved."""
    from graph.goals.goal_turn import in_goal_turn

    monkeypatch.setattr(env.state, "goal_controller", FakeGoals([("done", "met")]), raising=False)
    env.install(streams=[[text("r1", "one"), text("r1", "two")]])

    agen = chat_mod._chat_langgraph_stream("hello", "s-close-inner")
    first = await agen.__anext__()
    assert first == ("text", "one")
    assert in_goal_turn()  # mid-turn: the native turn's goal_turn scope is open
    await agen.aclose()

    assert turn_streams.started == 1
    assert turn_streams.exited == 1  # _run_native_turn → _run_turn_stream hop closed
    assert not in_goal_turn()  # impl → _run_native_turn hop closed: goal_turn's finally ran


@pytest.mark.asyncio
async def test_early_close_during_a_goal_continuation_closes_it(env, monkeypatch, turn_streams):
    from graph.goals.goal_turn import in_goal_turn

    goals = FakeGoals([("continue", "not yet", "keep going"), ("done", "met")])
    monkeypatch.setattr(env.state, "goal_controller", goals, raising=False)
    env.install(streams=[[text("r1", "draft")], [text("r2", "better"), text("r2", " still")]])

    agen = chat_mod._chat_langgraph_stream("ship it", "s-close-goal")
    seen = [await agen.__anext__() for _ in range(3)]
    assert seen[-1] == ("text", "better")  # inside the continuation turn
    await agen.aclose()

    assert turn_streams.started == 2
    assert turn_streams.exited == 2  # the continuation's _run_turn_stream hop closed
    assert not in_goal_turn()


@pytest.mark.asyncio
async def test_early_close_during_the_overflow_retry_closes_it(env, compaction, turn_streams):
    env.install(streams=[[Raise(ValueError(_OVERFLOW))], [text("r1", "recovered"), text("r1", " more")]])

    agen = chat_mod._chat_langgraph_stream("big ask", "s-close-ovf")
    seen = [await agen.__anext__() for _ in range(2)]
    assert seen == [("tool_start", chat_mod._OVERFLOW_NOTICE), ("text", "recovered")]
    await agen.aclose()

    assert turn_streams.started == 2
    assert turn_streams.exited == 2  # the retry's _run_native_turn hop closed
    assert not turn_control._thread_lock("a2a:s-close-ovf").locked()


@pytest.mark.asyncio
async def test_the_native_turn_runs_under_the_thread_lock(env):
    seen = []
    g = env.install(streams=[[text("r1", "x")]])
    g.on_call = lambda graph, config: seen.append(
        turn_control._thread_lock(config["configurable"]["thread_id"]).locked()
    )

    await _run("hello", "s-lock")

    assert seen == [True]
    assert not turn_control._thread_lock("a2a:s-lock").locked()


@pytest.mark.asyncio
async def test_a_custom_thread_resolver_keys_the_turn(env, monkeypatch):
    monkeypatch.setattr(
        env.state, "thread_id_resolver", lambda md, sid: f"proj-{md.get('project')}:{sid}", raising=False
    )
    g = env.install(streams=[[text("r1", "x")]])

    await _run("hello", "s-res", request_metadata={"project": "p9"})

    assert g.stream_calls[0][1]["configurable"]["thread_id"] == "proj-p9:s-res"


@pytest.mark.asyncio
async def test_record_failed_turn_keys_the_default_thread_even_under_a_custom_resolver(env, monkeypatch):
    """ODDITY: ``record_failed_turn`` resolves the thread with NO request metadata, so a
    resolver that scopes off metadata records the failure on a different thread than the
    one the turn ran on."""
    monkeypatch.setattr(
        env.state, "thread_id_resolver", lambda md, sid: f"proj-{md.get('project')}:{sid}", raising=False
    )
    g = env.install(streams=[[Raise(ValueError("boom"))]])

    await _run("hello", "s-res", request_metadata={"project": "p9"})

    assert g.stream_calls[0][1]["configurable"]["thread_id"] == "proj-p9:s-res"
    assert g.updates[0][0] == {"configurable": {"thread_id": "proj-None:s-res"}}


@pytest.mark.asyncio
async def test_json_tool_args_are_compact_and_capped(env):
    big = {"blob": "y" * 5000}
    env.install(streams=[[model_end("r1", tool_calls=[("tc1", "write", big)]), text("r2", "k")]])

    frames = await _run()

    assert frames[0] == (
        "tool_start",
        {"id": "tc1", "name": "write", "input": json.dumps(big)[: chat_mod._TOOL_PREVIEW_CHARS]},
    )


@pytest.mark.asyncio
async def test_a_telemetry_failure_never_breaks_the_turn(env, monkeypatch, compaction):
    from observability import metrics

    def _boom(*a, **k):
        raise RuntimeError("prometheus down")

    monkeypatch.setattr(metrics, "record_llm_call", _boom)
    monkeypatch.setattr(metrics, "record_overflow_recovery", _boom)
    env.install(streams=[[Raise(ValueError(_OVERFLOW))], [text("r1", "ok"), model_end("r1", usage=(1, 1, 0, 0))]])

    frames = await _run()

    # The usage frame still reaches the caller after the metrics call raised.
    assert frames == [
        ("tool_start", chat_mod._OVERFLOW_NOTICE),
        ("text", "ok"),
        ("usage", _usage(1, 1, 0, 0, "resp-model")),
        ("done", "ok"),
    ]


@pytest.mark.asyncio
async def test_debug_delta_logging_does_not_change_the_frames(env, caplog):
    import logging

    caplog.set_level(logging.DEBUG, logger="protoagent.server")
    env.install(streams=[[text("r1", "hello world")]])

    assert await _run() == [("text", "hello world"), ("done", "hello world")]
    assert any("[stream-delta]" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_caller_trace_with_only_a_trace_id(env):
    env.install(streams=[[text("r1", "hi")]])

    await _run(caller_trace={"traceId": "T1"})

    assert env.trace.sessions[0]["metadata"]["caller_trace_id"] == "T1"
    assert "caller_span_id" not in env.trace.sessions[0]["metadata"]
