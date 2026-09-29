"""Characterization of the NON-STREAMING turn driver (epic #3804, phase A).

Pins today's observable behaviour of ``chat()`` → ``_chat_langgraph`` →
``_chat_langgraph_impl`` before the split refactor: the returned assistant dicts, and
what reaches the boundaries — graph inputs + configs, the failed-turn record, the
telemetry row (``record_local_turn``), and the trace. Goldens of CURRENT behaviour,
oddities included (flagged ``ODDITY``). The graph is the only fake
(``tests/_turn_driver_fakes.ScriptedGraph``) besides external services.
"""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command

from graph.config import LangGraphConfig
from tests._turn_driver_fakes import FakeGoals, Invoke, Raise, ScriptedGraph, TraceSpy, set_interrupt, turn_result

chat_mod = importlib.import_module("server.chat")
turn_control = importlib.import_module("server.turn_control")
turn_telemetry = importlib.import_module("server.turn_telemetry")

_OVERFLOW = "Error code: 400 - This model's maximum context length is 128000 tokens."
_NO_REPLY = (
    "**Error:** the turn produced no reply — it may have stalled or been "
    "interrupted. Nothing was returned for this request; retry it. "
    "(This is not the previous turn's answer.)"
)
_PAUSE = "⏸ goal paused — handed off to a watch/schedule; will resume when it fires."


@pytest.fixture
def env(monkeypatch):
    from observability import metrics

    import runtime.state as rs

    class Env:
        pass

    e = Env()
    e.trace = TraceSpy().install(monkeypatch)
    e.rows = []

    def _record(sink, *, session_id, origin, state, started):
        cb = sink.get("usage_cb")
        e.rows.append(
            {
                "session_id": session_id,
                "origin": origin,
                "state": state,
                "usage": dict(getattr(cb, "usage_metadata", None) or {}) if cb is not None else None,
            }
        )

    monkeypatch.setattr(turn_telemetry, "record_local_turn", _record)
    e.overflow_recoveries = []
    monkeypatch.setattr(metrics, "record_overflow_recovery", lambda: e.overflow_recoveries.append(1))
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

    def install(invokes=()):
        e.graph = ScriptedGraph(invokes=invokes)
        monkeypatch.setattr(rs.STATE, "graph", e.graph, raising=False)
        return e.graph

    e.install = install
    e.state = rs.STATE
    yield e
    # A driver making a graph call the test didn't script must fail the test even when the
    # driver's own error handling swallowed the fake's AssertionError into a frame/record.
    graph = getattr(e, "graph", None)
    assert graph is None or not graph.overrun, "driver made an unscripted graph call"


def _cfg(sid="s1"):
    return {"thread_id": f"a2a:{sid}"}


def _usage(p, c):
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


# ── happy path golden ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_golden_plain_answer(env):
    g = env.install([Invoke(turn_result(AIMessage(content="The answer.")), usage=[("m1", 100, 20)])])

    out = await chat_mod.chat("hello", "s1", origin="v1")

    assert out == [{"role": "assistant", "content": "The answer.", "usage": _usage(100, 20)}]
    ((graph_input, config),) = g.invoke_calls
    assert [(type(m), m.content) for m in graph_input["messages"]] == [(HumanMessage, "hello")]
    # incognito + fence are stamped EVERY turn (the channels persist); model only when set.
    assert {k: v for k, v in graph_input.items() if k != "messages"} == {
        "session_id": "s1",
        "incognito": False,
        "subagent_fence": [],
    }
    assert config["configurable"] == _cfg("s1")
    assert config["recursion_limit"] == LangGraphConfig().max_iterations
    assert len(config["callbacks"]) == 1
    assert env.rows == [
        {
            "session_id": "s1",
            "origin": "v1",
            "state": "completed",
            "usage": {"m1": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}},
        }
    ]
    (sess,) = env.trace.sessions
    assert sess["name"] == "chat" and set(sess["metadata"]) == {"soul_rev", "message_preview"}
    assert env.trace.outputs == ["The answer."] and env.trace.flushes == 1
    assert turn_control.active_turns() == 0


@pytest.mark.asyncio
async def test_overrides_are_stamped_into_the_graph_input(env):
    g = env.install([turn_result(AIMessage(content="ok"))] * 2)

    await chat_mod.chat("hi", "s1", model="gpt-x", incognito=True, tool_fence=["read_file"])
    await chat_mod.chat("hi", "s1", model="   ")

    first, second = (c[0] for c in g.invoke_calls)
    assert {k: v for k, v in first.items() if k != "messages"} == {
        "session_id": "s1",
        "model": "gpt-x",
        "incognito": True,
        "subagent_fence": ["read_file"],
    }
    assert "model" not in second  # a blank override is no override
    assert "message_preview" not in env.trace.sessions[0]["metadata"]  # incognito trace


@pytest.mark.asyncio
async def test_responses_api_content_blocks_are_flattened(env):
    env.install([turn_result(AIMessage(content=[{"type": "text", "text": "Block reply."}]))])

    out = await chat_mod.chat("hi", "s1")

    assert out[0]["content"] == "Block reply."


# ── empty replies ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_turn_with_no_reply_never_returns_the_previous_answer(env):
    env.install([turn_result()])

    out = await chat_mod.chat("hi", "s1")

    # A no-reply turn is a FAILED turn (#3873): it carries the structured `error` every other
    # failure return does, so the telemetry row counts it failed (and /v1 answers an error).
    assert out == [
        {
            "role": "assistant",
            "content": _NO_REPLY,
            "usage": _usage(0, 0),
            "error": {
                "message": "the turn produced no reply — it may have stalled or been interrupted; retry it",
                "type": "server_error",
                "upstream_status": None,
                "exception": None,
            },
        }
    ]
    assert env.rows[0]["state"] == "failed"


@pytest.mark.asyncio
async def test_a_tool_only_turn_answers_with_the_last_tool_text(env):
    env.install(
        [
            turn_result(
                AIMessage(content="", tool_calls=[{"id": "t", "name": "wait", "args": {}}]),
                ToolMessage(content="Wait scheduled.", tool_call_id="t"),
            )
        ]
    )

    out = await chat_mod.chat("hi", "s1")

    assert out[0]["content"] == "Wait scheduled."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"question": "Which env?"}, "Which env?"),
        ({"kind": "approval", "title": "Run rm -rf build?"}, "Run rm -rf build?"),
        ("", "The agent needs input to continue."),
    ],
)
async def test_a_turn_parked_on_hitl_echoes_the_question(env, value, expected):
    env.install([Invoke(turn_result(), steps=[set_interrupt(value)], usage=[("m1", 5, 1)])])

    out = await chat_mod.chat("hi", "s1")

    assert out == [{"role": "assistant", "content": f"🙋 **Input needed:** {expected}", "usage": _usage(5, 1)}]


# ── HITL hold / resume ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hitl_hold_queues_the_message_and_echoes_the_pending_ask(env):
    from graph import steering

    g = env.install([])
    g.pending.append({"question": "Which env?"})
    try:
        out = await chat_mod.chat("use the fast path", "s1")

        assert out == [
            {
                "role": "assistant",
                "content": (
                    "🙋 **Input needed first:** Which env?\n\n"
                    "_(Your message is queued — the agent gets it right after you answer.)_"
                ),
            }
        ]
        assert g.invoke_calls == []
        assert steering.pending("s1") == 1
        assert turn_control.active_turns() == 0  # the hold path released the turn counter
    finally:
        steering.forget("s1")


@pytest.mark.asyncio
async def test_hitl_resume_answers_the_pending_interrupt_by_id(env):
    g = env.install([turn_result(AIMessage(content="Proceeding."))])
    g.pending.append({"question": "Which env?"})

    out = await chat_mod.chat("staging", "s1", hitl_resume=True)

    assert out[0]["content"] == "Proceeding."
    assert isinstance(g.invoke_calls[0][0], Command)
    assert g.resumes == [{"int-0": "staging"}]


@pytest.mark.asyncio
async def test_hitl_resume_with_nothing_pending_is_a_fresh_turn(env):
    g = env.install([turn_result(AIMessage(content="ok"))])

    await chat_mod.chat("staging", "s1", hitl_resume=True)

    assert g.invoke_calls[0][0]["messages"][0].content == "staging"


# ── goal mode ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_goal_kickoff_continuation_and_done_note(env, monkeypatch):
    goals = FakeGoals([("continue", "1/8 — not yet", "keep going"), ("done", "✅ goal met")])
    monkeypatch.setattr(env.state, "goal_controller", goals, raising=False)
    g = env.install(
        [
            Invoke(turn_result(AIMessage(content="draft")), usage=[("m1", 10, 1)]),
            Invoke(turn_result(AIMessage(content="better")), usage=[("m1", 20, 2)]),
        ]
    )

    out = await chat_mod.chat("ship it", "s1")

    assert out == [{"role": "assistant", "content": "better\n\n---\n✅ goal met", "usage": _usage(30, 3)}]
    assert goals.kickoffs == ["ship it"]
    assert g.invoke_calls[0][0]["messages"][0].content == "KICKOFF<ship it>"
    cont_input, cont_config = g.invoke_calls[1]
    assert [m.content for m in cont_input["messages"]] == ["keep going"]
    assert {k: v for k, v in cont_input.items() if k != "messages"} == {
        "session_id": "s1",
        "incognito": False,
        "subagent_fence": [],
    }
    assert cont_config["configurable"] == _cfg("s1")
    assert cont_config["callbacks"] == g.invoke_calls[0][1]["callbacks"]  # usage keeps counting
    assert goals.evals == ["draft", "better"]


@pytest.mark.asyncio
async def test_goal_fresh_context_continuation_gets_a_scoped_thread_and_the_usage_callback(env, monkeypatch):
    goals = FakeGoals([("continue", "again", "iterate"), None], iteration=3, fresh=True)
    monkeypatch.setattr(env.state, "goal_controller", goals, raising=False)
    g = env.install([turn_result(AIMessage(content="one")), turn_result()])

    out = await chat_mod.chat("go", "s1")

    assert goals.kickoffs == []
    cont_config = g.invoke_calls[1][1]
    assert cont_config["configurable"] == {"thread_id": "a2a:s1:goal-iter-4"}
    assert cont_config["recursion_limit"] == LangGraphConfig().max_iterations
    assert cont_config["callbacks"] == g.invoke_calls[0][1]["callbacks"]
    assert out[0]["content"] == "one\n\n---\nagain"  # empty continuation keeps the answer


@pytest.mark.asyncio
async def test_goal_pauses_on_a_watch_handoff(env, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(env.state, "goal_controller", FakeGoals([("continue", "not yet", "more")]), raising=False)
    monkeypatch.setattr(
        env.state,
        "watch_controller",
        SimpleNamespace(list_watches=lambda: [SimpleNamespace(status="active", run_session="s1")]),
        raising=False,
    )
    g = env.install([turn_result(AIMessage(content="watching"))])

    out = await chat_mod.chat("go", "s1")

    assert out[0]["content"] == f"watching\n\n---\n{_PAUSE}"
    assert len(g.invoke_calls) == 1


@pytest.mark.asyncio
async def test_goal_loop_is_bounded_by_the_hard_cap(env, monkeypatch):
    goals = FakeGoals(forever=("continue", "nope", "again"))
    monkeypatch.setattr(env.state, "goal_controller", goals, raising=False)
    cap = LangGraphConfig().goal_max_iterations + 2
    env.install([turn_result(AIMessage(content=f"t{i}")) for i in range(cap + 1)])

    out = await chat_mod.chat("go", "s1")

    assert len(goals.evals) == cap
    assert out[0]["content"] == f"t{cap}\n\n---\nnope"


@pytest.mark.asyncio
async def test_goal_evaluate_none_ends_without_a_note(env, monkeypatch):
    monkeypatch.setattr(env.state, "goal_controller", FakeGoals([None]), raising=False)
    env.install([turn_result(AIMessage(content="plain"))])

    assert (await chat_mod.chat("go", "s1"))[0]["content"] == "plain"


@pytest.mark.asyncio
async def test_goal_turn_auto_answers_a_hitl_park(env, monkeypatch):
    monkeypatch.setattr(env.state, "goal_controller", FakeGoals([("done", "met")]), raising=False)
    g = env.install(
        [Invoke(turn_result(), steps=[set_interrupt("what goal?")]), turn_result(AIMessage(content="on it"))]
    )

    out = await chat_mod.chat("go", "s1")

    assert out[0]["content"] == "on it\n\n---\nmet"
    # Keyed by interrupt id, like the streaming driver (#3872 — was a bare resume value).
    assert g.resumes == [{"int-0": turn_control._AUTONOMOUS_HITL_SENTINEL}]
    assert g.updates == []


@pytest.mark.asyncio
async def test_goal_turn_keeps_the_pre_interrupt_text_when_the_resumed_pass_says_nothing(env, monkeypatch):
    """#3873 / #3891 F3, the non-streaming side: the text of a pass that parked at an
    auto-answered interrupt is part of THIS turn's messages (a resume adds no Human turn),
    so the reply's last-AI-message rule keeps it when the resumed pass adds no text — the
    same answer the streaming driver's `done` now carries (it used to drop it)."""
    monkeypatch.setattr(env.state, "goal_controller", FakeGoals([("done", "met")]), raising=False)
    g = env.install(
        [
            Invoke(turn_result(AIMessage(content="asking")), steps=[set_interrupt("which env?")]),
            turn_result(AIMessage(content="asking"), AIMessage(content="")),
        ]
    )

    out = await chat_mod.chat("go", "s1")

    assert out[0]["content"] == "asking\n\n---\nmet"
    assert "error" not in out[0]
    assert g.resumes == [{"int-0": turn_control._AUTONOMOUS_HITL_SENTINEL}]


class _StrictResumeGraph(ScriptedGraph):
    """LangGraph's rules: a bare ``Command(resume=value)`` with more than one pending
    interrupt is an error, and an id-keyed resume answers THAT interrupt. Ids are stable
    for an interrupt's life (assigned when it pends, kept after others are answered), so
    re-answering an id that was already answered is caught rather than accepted."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._ids: list[str] = []  # parallel to self.pending
        self._next_id = 0

    def _sync_ids(self):
        while len(self._ids) < len(self.pending):
            self._ids.append(f"int-{self._next_id}")
            self._next_id += 1

    def _answer_resume(self, graph_input):
        if not isinstance(graph_input, Command):
            return
        self._sync_ids()
        resume = graph_input.resume
        if not isinstance(resume, dict):
            if len(self.pending) > 1:
                raise RuntimeError("bare resume with multiple pending interrupts")
            super()._answer_resume(graph_input)
            del self._ids[:1]
            return
        self.resumes.append(resume)
        for key in resume:
            if key not in self._ids:
                raise RuntimeError(f"resume for unknown or already-answered interrupt {key!r}")
            i = self._ids.index(key)
            del self._ids[i]
            del self.pending[i]

    async def aget_state(self, config):
        self._sync_ids()
        interrupts = [SimpleNamespace(id=i, value=v) for i, v in zip(self._ids, self.pending)]
        return SimpleNamespace(tasks=(), interrupts=interrupts)


@pytest.mark.asyncio
async def test_goal_turn_auto_answers_parallel_interrupts_one_id_at_a_time(env, monkeypatch):
    """#3872: two gated tool calls in one turn pend two interrupts at once; the goal
    auto-answer resumes each BY ID (the first pending one), never with a bare value."""
    monkeypatch.setattr(env.state, "goal_controller", FakeGoals([("done", "met")]), raising=False)
    g = _StrictResumeGraph(
        invokes=[
            Invoke(turn_result(), steps=[set_interrupt("approve a?"), set_interrupt("approve b?")]),
            turn_result(),
            turn_result(AIMessage(content="both done")),
        ]
    )
    monkeypatch.setattr(env.state, "graph", g, raising=False)

    out = await chat_mod.chat("go", "s1")

    assert out[0]["content"] == "both done\n\n---\nmet"
    assert g.resumes == [
        {"int-0": turn_control._AUTONOMOUS_HITL_SENTINEL},
        {"int-1": turn_control._AUTONOMOUS_HITL_SENTINEL},
    ]
    assert g.updates == []  # nothing left to clear


@pytest.mark.asyncio
async def test_goal_turn_gives_up_and_clears_after_the_auto_answer_budget(env, monkeypatch):
    cap = turn_control._MAX_AUTONOMOUS_AUTOANSWERS
    monkeypatch.setattr(env.state, "goal_controller", FakeGoals([None]), raising=False)
    g = env.install(
        [Invoke(turn_result(), steps=[set_interrupt("q0")])]
        + [Invoke(turn_result(), steps=[set_interrupt(f"q{i}")]) for i in range(1, cap + 1)]
    )

    out = await chat_mod.chat("go", "s1")

    assert len(g.invoke_calls) == cap + 1
    assert len(g.updates) == 1 and g.updates[0][1] is None  # the stranded interrupt is cleared
    assert out[0]["content"] == _NO_REPLY


@pytest.mark.asyncio
async def test_goal_continuations_hold_the_base_thread_lock(env, monkeypatch):
    goals = FakeGoals([("continue", "n", "more"), ("done", "d")], fresh=True, iteration=1)
    monkeypatch.setattr(env.state, "goal_controller", goals, raising=False)
    seen = []
    g = env.install([turn_result(AIMessage(content="a")), turn_result(AIMessage(content="b"))])
    g.on_call = lambda graph, config: seen.append(turn_control._thread_lock("a2a:s1").locked())

    await chat_mod.chat("go", "s1")

    assert seen == [True, True]
    assert not turn_control._thread_lock("a2a:s1").locked()


# ── failures ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_failure_returns_an_error_bubble_records_it_and_bills_what_was_spent(env):
    class _Upstream(Exception):
        status_code = 401

    g = env.install([Invoke(usage=[("m1", 7, 0)], raises=_Upstream("bad key"))])

    out = await chat_mod.chat("hello", "s-err", origin="v1")

    assert out == [
        {
            "role": "assistant",
            "content": "**Error:** bad key",
            "error": {
                "message": "bad key",
                "type": "authentication_error",
                "upstream_status": 401,
                "exception": "_Upstream",
            },
        }
    ]
    ((cfg, values),) = g.updates
    assert cfg == {"configurable": _cfg("s-err")}
    assert values["messages"][0].content == "**Error:** bad key"
    assert values["messages"][0].additional_kwargs == {"protoagent_turn_failed": True}
    assert env.rows == [
        {
            "session_id": "s-err",
            "origin": "v1",
            "state": "failed",
            "usage": {"m1": {"input_tokens": 7, "output_tokens": 0, "total_tokens": 7}},
        }
    ]
    assert env.trace.outputs == ["**Error:** bad key"] and env.trace.flushes == 1


@pytest.fixture
def compaction(monkeypatch):
    import graph.compaction_op as cop

    calls: list[dict] = []

    async def _compact(graph, checkpointer, knowledge_store, cfg, thread_id, session_id, *, force, keep_recent):
        calls.append(
            {
                "thread_id": thread_id,
                "force": force,
                "keep_recent": keep_recent,
                "locked": turn_control._thread_lock(thread_id).locked(),
            }
        )
        return {"removed": 3}

    monkeypatch.setattr(cop, "compact_thread", _compact)
    return calls


@pytest.mark.asyncio
async def test_overflow_retry_skips_the_hold_and_keeps_the_turn_overrides(env, compaction):
    g = env.install([Raise(ValueError(_OVERFLOW)), turn_result(AIMessage(content="recovered"))])

    out = await chat_mod.chat("big", "s1", model="m-x")

    assert out[0]["content"] == "recovered"
    assert compaction == [{"thread_id": "a2a:s1", "force": True, "keep_recent": 10, "locked": True}]
    assert env.overflow_recoveries == [1]
    retry = g.invoke_calls[1][0]
    assert retry["messages"][0].content == chat_mod._OVERFLOW_RETRY_PROMPT and retry["model"] == "m-x"


@pytest.mark.asyncio
async def test_overflow_retry_does_not_hold_even_when_an_interrupt_is_pending(env, compaction):
    """The retry runs the recovery prompt, never the hold — even if the failed turn left
    the thread parked."""

    def _park_then_overflow(graph):
        graph.pending.append({"question": "stale ask"})
        raise ValueError(_OVERFLOW)

    g = env.install([Invoke(steps=[_park_then_overflow]), turn_result(AIMessage(content="recovered"))])

    out = await chat_mod.chat("big", "s1")

    assert out[0]["content"] == "recovered"
    assert len(g.invoke_calls) == 2


@pytest.mark.asyncio
async def test_cancellation_propagates_and_is_billed_as_failed(env):
    g = env.install([Raise(asyncio.CancelledError())])

    with pytest.raises(asyncio.CancelledError):
        await chat_mod.chat("hello", "s-cancel", origin="api-chat")

    assert g.updates == []  # no failed-turn record for a cancellation
    assert [r["state"] for r in env.rows] == ["failed"]
    assert env.trace.flushes == 1
    assert turn_control.active_turns() == 0
    assert not turn_control._thread_lock("a2a:s-cancel").locked()


# ── graphless / short-circuit / direct helpers ────────────────────────────────


@pytest.mark.asyncio
async def test_graphless_chat_returns_the_setup_message_and_no_telemetry(env, monkeypatch):
    monkeypatch.setattr(env.state, "graph", None, raising=False)

    out = await chat_mod.chat("hello", "s1")

    assert out[0]["content"].startswith("**Setup required.**")
    assert env.rows == [] and env.trace.sessions == []

    monkeypatch.setattr(env.state, "graph_auth_error", {"message": "Codex signed out."}, raising=False)
    out = await chat_mod.chat("hello", "s1")
    assert out[0]["content"].startswith("**Signed out.** Codex signed out.")


@pytest.mark.asyncio
async def test_a_short_circuit_returns_without_a_graph_turn_and_is_traced(env):
    g = env.install([])

    out = await chat_mod.chat("/no-such-command", "s1")

    assert g.invoke_calls == []
    assert len(out) == 1 and out[0]["role"] == "assistant" and "usage" not in out[0]
    assert env.trace.outputs == [out[0]["content"]]
    # The wrapper still writes a telemetry call, with no usage collector (nothing spent).
    assert env.rows == [{"session_id": "s1", "origin": "local", "state": "completed", "usage": None}]


@pytest.mark.asyncio
async def test_the_impl_runs_without_a_telemetry_sink(env):
    env.install([turn_result(AIMessage(content="ok"))])

    out = await chat_mod._chat_langgraph_impl("hi", "s1")

    assert out[0]["content"] == "ok"


@pytest.mark.asyncio
async def test_record_failed_turn_guards(env, monkeypatch):
    g = env.install([])

    assert await chat_mod.record_failed_turn("s1", "   ") is False
    assert g.updates == []
    assert await chat_mod.record_failed_turn("s1", "**Error:** x") is True
    monkeypatch.setattr(env.state, "graph", None, raising=False)
    assert await chat_mod.record_failed_turn("s1", "**Error:** x") is False


@pytest.mark.asyncio
async def test_force_compact_without_a_graph_is_a_no_op(env, monkeypatch):
    monkeypatch.setattr(env.state, "graph", None, raising=False)

    assert await chat_mod._force_compact_for_overflow("a2a:s1", "s1") is False


# ── /v1 route: a no-reply turn is an HTTP error, like every other failed turn ──


def test_v1_answers_a_no_reply_turn_with_an_error_status_not_a_200(env, monkeypatch):
    """#3873 at the route: the REAL driver behind ``/v1/chat/completions`` (only the graph
    is fake). A turn that produced no reply used to answer 200 with the ``**Error:**`` text
    as the completion; it now takes the same ``_v1_error_response`` path as a turn that
    raised with no upstream status — a 500 ``server_error``, nothing that looks like an
    answer."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import operator_api.chat_routes as cr

    env.install([turn_result()])
    monkeypatch.setattr(cr, "agent_name", lambda: "protoagent")
    app = FastAPI()
    cr.register_chat_routes(app, ui="none")

    r = TestClient(app).post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "go"}]})

    assert r.status_code == 500
    body = r.json()
    assert "choices" not in body
    assert body["error"]["type"] == "server_error"
    assert body["error"]["upstream_status"] is None
    assert "produced no reply" in body["error"]["message"]
    assert env.rows[0]["state"] == "failed" and env.rows[0]["origin"] == "v1"
