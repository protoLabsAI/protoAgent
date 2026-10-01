"""#3957 review — a per-turn model pick must not stick to the thread.

``model`` is a checkpointed state channel. Both drivers stamped it only when the turn had
a pick, so a turn WITHOUT one (the console's "Default" sends nothing) inherited the previous
turn's pick from the checkpoint. With an unbuildable pick now failing the turn explicitly,
one bad pick broke every later turn on the chat — goal continuations, scheduled and A2A
turns included — and "Default" could not recover it.

These run the REAL drivers against a REAL ``create_agent`` graph (checkpointer, the real
``ProtoAgentState`` and ``ModelOverrideMiddleware``); only the chat model is a fake and
``create_llm`` is patched (no real provider client is ever built).
"""

from __future__ import annotations

import contextlib
import importlib

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from graph.config import LangGraphConfig
from graph.middleware.model_override import ModelOverrideMiddleware
from graph.state import ProtoAgentState

chat_mod = importlib.import_module("server.chat")

_BAD = "nonexistent-provider:bogus"
_GOOD = "gateway:picked-model"


CALLS: list[str] = []  # the model each graph model call ran on
_REJECTED = "gateway:revoked-key-model"  # builds fine; the provider answers 401


class _Fake(GenericFakeChatModel):
    model_name: str = "default-model"

    def _generate(self, *a, **k):
        CALLS.append(self.model_name)
        return super()._generate(*a, **k)


class _Unauthorized(Exception):
    status_code = 401


class _Rejecting(GenericFakeChatModel):
    model_name: str = _REJECTED

    def _generate(self, *a, **k):
        CALLS.append(f"rejected:{self.model_name}")
        raise _Unauthorized("Error code: 401 - token revoked")


def _model(name: str) -> _Fake:
    return _Fake(messages=iter([AIMessage(content=f"answer from {name}")] * 50), model_name=name)


@pytest.fixture
def env(monkeypatch):
    from observability import tracing

    import runtime.state as rs

    def _create_llm(config, *, model_name=None, reasoning_effort=None):
        if model_name == _BAD:
            raise RuntimeError("Missing credentials")
        if model_name == _REJECTED:
            return _Rejecting(messages=iter([]), model_name=_REJECTED)
        return _model(model_name or "default-model")

    monkeypatch.setattr("graph.llm.create_llm", _create_llm)
    CALLS.clear()
    monkeypatch.setattr("server.goal_loop._PROBE_OK", {}, raising=False)  # no probe result leaks across tests
    cfg = LangGraphConfig()
    graph = create_agent(
        model=_model("default-model"),
        tools=[],
        middleware=[ModelOverrideMiddleware(cfg)],
        state_schema=ProtoAgentState,
        checkpointer=InMemorySaver(),
    )
    for attr, val in {
        "graph": graph,
        "telemetry_store": None,
        "goal_controller": None,
        "background_mgr": None,
        "watch_controller": None,
        "scheduler": None,
        "graph_auth_error": None,
        "thread_id_resolver": None,
        "checkpointer": object(),
        "knowledge_store": None,
        "graph_config": cfg,
    }.items():
        monkeypatch.setattr(rs.STATE, attr, val, raising=False)

    @contextlib.asynccontextmanager
    async def trace_session(*_a, **_k):
        yield None

    monkeypatch.setattr(tracing, "trace_session", trace_session)
    monkeypatch.setattr(tracing, "flush", lambda: None)
    monkeypatch.setattr(tracing, "set_session_output", lambda out: None)
    return graph


async def _sync_turn(session_id, model=None):
    out = await chat_mod.chat("hello", session_id, model=model, origin="api-chat")
    return out[-1]


async def _stream_turn(session_id, model=None):
    md = {"model": model} if model else {}
    frames = [f async for f in chat_mod._chat_langgraph_stream("hello", session_id, request_metadata=md)]
    return frames[-1]


@pytest.mark.asyncio
async def test_sync_a_bad_pick_does_not_break_the_next_no_pick_turn(env):
    first = await _sync_turn("s-sync", _BAD)
    assert first.get("error") and first["error"]["type"] == "invalid_request_error"

    second = await _sync_turn("s-sync")  # "Default": no pick sent

    assert not second.get("error"), second
    assert second["content"] == "answer from default-model"


@pytest.mark.asyncio
async def test_stream_a_bad_pick_does_not_break_the_next_no_pick_turn(env):
    first = await _stream_turn("s-stream", _BAD)
    assert first[0] == "error" and "not available" in first[1]

    second = await _stream_turn("s-stream")

    assert second == ("done", "answer from default-model")


@pytest.mark.asyncio
@pytest.mark.parametrize("turn", [_sync_turn, _stream_turn], ids=["sync", "stream"])
async def test_a_good_pick_applies_to_its_own_turn_only(env, turn):
    picked = await turn("s-good", _GOOD)
    default = await turn("s-good")

    text = (lambda r: r["content"] if isinstance(r, dict) else r[1])
    assert text(picked) == f"answer from {_GOOD}"
    assert text(default) == "answer from default-model"


@pytest.mark.asyncio
async def test_a_goal_records_the_setting_turns_pick(tmp_path):
    from graph.goals.controller import GoalController
    from graph.goals.store import GoalStore
    from graph.subagent_model import turn_model_scope

    ctrl = GoalController(config=None, store=GoalStore(base_dir=str(tmp_path)))
    with turn_model_scope(_GOOD):
        await ctrl.parse_control("/goal make it so", "s1", trusted=False)

    state = ctrl.active_goal("s1")
    assert state.model == _GOOD
    assert type(state).from_dict(state.to_dict()).model == _GOOD


def _goals(monkeypatch, *, model=""):
    """A goal that asks for one continuation, then is met; records model updates."""
    import runtime.state as rs
    from tests._turn_driver_fakes import FakeGoals

    class _Goals(FakeGoals):
        def remember_model(self, session_id, m):
            self.state.model = m

    goals = _Goals([("continue", "not yet", "keep going"), ("done", "met")], iteration=1)
    goals.state.model = model
    monkeypatch.setattr(rs.STATE, "goal_controller", goals, raising=False)
    return goals


def _text(r):
    return r["content"] if isinstance(r, dict) else r[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("turn", [_sync_turn, _stream_turn], ids=["sync", "stream"])
async def test_a_goal_continuation_runs_on_its_turns_pick(env, monkeypatch, turn):
    _goals(monkeypatch)

    await turn("s-cont", _GOOD)

    assert CALLS == [_GOOD, _GOOD]  # the initial pass AND the continuation


@pytest.mark.asyncio
@pytest.mark.parametrize("turn", [_sync_turn, _stream_turn], ids=["sync", "stream"])
async def test_a_no_pick_re_drive_runs_on_the_goals_model(env, monkeypatch, turn):
    """A watch / schedule fire or a "Default" message re-driving a goal carries no pick;
    the goal remembers the one it was set with (no longer inherited from the thread)."""
    _goals(monkeypatch, model=_GOOD)

    await turn("s-redrive")

    assert CALLS == [_GOOD, _GOOD]


@pytest.mark.asyncio
@pytest.mark.parametrize("turn", [_sync_turn, _stream_turn], ids=["sync", "stream"])
async def test_a_broken_inherited_goal_pick_falls_back_to_the_default_with_a_notice(env, monkeypatch, turn, caplog):
    """Review round 2 (A1): a goal set with a pick that later breaks must not hard-fail
    every re-drive that carries no pick of its own (a fire, a "Default" message)."""
    _goals(monkeypatch, model=_BAD)

    with caplog.at_level("WARNING"):
        out = await turn("s-broken-goal")

    assert CALLS == ["default-model", "default-model"]  # ran — on the default, every pass
    assert "The goal's model `nonexistent-provider:bogus` is unavailable" in _text(out)
    assert "/goal clear" in _text(out)
    assert any("goal's model" in r.getMessage() and r.levelname == "WARNING" for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("turn", [_sync_turn, _stream_turn], ids=["sync", "stream"])
async def test_an_explicit_pick_on_a_goal_turn_becomes_the_goals_model(env, monkeypatch, turn):
    goals = _goals(monkeypatch, model=_BAD)

    await turn("s-repick", _GOOD)

    assert goals.state.model == _GOOD  # the next fire uses it
    assert CALLS == [_GOOD, _GOOD]


@pytest.mark.asyncio
async def test_an_explicit_broken_pick_on_a_goal_turn_still_hard_fails(env, monkeypatch):
    _goals(monkeypatch, model=_GOOD)

    first = await _stream_turn("s-explicit-bad", _BAD)

    assert first[0] == "error" and "not available" in first[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("turn", [_sync_turn, _stream_turn], ids=["sync", "stream"])
async def test_a_workflow_on_a_no_pick_goal_turn_runs_on_the_goals_model(env, monkeypatch, turn):
    """Review round 2 (A3): ``turn_model_scope`` binds the EFFECTIVE pick, so a
    ``/<workflow>`` step (``sdk.run_subagent``) follows the model the lead would run on."""
    from graph.subagent_model import current_turn_model

    _goals(monkeypatch, model=_GOOD)
    dispatch = importlib.import_module("server.chat_dispatch")
    cc = dispatch._chat_commands
    seen: list[str] = []
    monkeypatch.setattr(cc, "_parse_workflow_command", lambda m: ("wf", {}) if m.startswith("/wf") else None)

    async def _run_parsed_workflow(name, inputs, *, on_step):
        seen.append(current_turn_model())
        return "wf done"

    monkeypatch.setattr(cc, "_run_parsed_workflow", _run_parsed_workflow)
    if turn is _sync_turn:
        await chat_mod.chat("/wf go", "s-wf", origin="api-chat")
    else:
        [f async for f in chat_mod._chat_langgraph_stream("/wf go", "s-wf", request_metadata={})]

    assert seen == [_GOOD]


@pytest.mark.asyncio
async def test_the_controller_remembers_an_explicit_pick(tmp_path):
    from graph.goals.controller import GoalController
    from graph.goals.store import GoalStore

    ctrl = GoalController(config=None, store=GoalStore(base_dir=str(tmp_path)))
    await ctrl.parse_control("/goal make it so", "s1", trusted=False)

    ctrl.remember_model("s1", _GOOD)

    assert ctrl.active_goal("s1").model == _GOOD
    assert GoalController(config=None, store=GoalStore(base_dir=str(tmp_path))).active_goal("s1").model == _GOOD


@pytest.mark.asyncio
@pytest.mark.parametrize("turn", [_sync_turn, _stream_turn], ids=["sync", "stream"])
async def test_an_inherited_pick_the_provider_rejects_falls_back_mid_turn(env, monkeypatch, turn, caplog):
    """An inherited pick can build and still be refused at call time (a revoked key, a
    token that expired since). That must not lock the goal into failing either: the call
    is retried on the default, later calls in the turn skip the pick, and the turn says so."""
    _goals(monkeypatch, model=_REJECTED)

    with caplog.at_level("WARNING"):
        out = await turn("s-rejected")

    assert CALLS == [f"rejected:{_REJECTED}", "default-model", "default-model"]
    assert f"The goal's model `{_REJECTED}` is unavailable" in _text(out)
    assert any("was rejected" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_an_explicit_pick_the_provider_rejects_still_fails(env):
    out = await _sync_turn("s-explicit-rejected", _REJECTED)

    assert out.get("error") and out["error"]["upstream_status"] == 401


@pytest.mark.asyncio
async def test_the_inherited_pick_probe_runs_off_the_event_loop_and_is_cached(env, monkeypatch):
    """CodeRabbit (goal_loop): building a native-OAuth client may refresh its token with a
    synchronous request — the probe must not run on the event loop, and a pick that just
    built fine is not re-probed every turn."""
    import threading

    import graph.llm as llm_mod

    real = llm_mod.create_llm
    probes: list[bool] = []

    def _spy(config, *, model_name=None, reasoning_effort=None):
        if model_name == _GOOD and reasoning_effort is None:
            probes.append(threading.current_thread() is threading.main_thread())
        return real(config, model_name=model_name, reasoning_effort=reasoning_effort)

    monkeypatch.setattr("graph.llm.create_llm", _spy)
    _goals(monkeypatch, model=_GOOD)

    await _sync_turn("s-probe")
    await _sync_turn("s-probe")

    # One probe across both turns (cached), and not on the loop's thread. (The middleware's
    # own build also lands here once, on the loop — it is cached for the graph's life.)
    probe_calls = [p for p in probes if p is False]
    assert len(probe_calls) == 1
