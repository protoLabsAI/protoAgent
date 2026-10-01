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


class _Fake(GenericFakeChatModel):
    model_name: str = "default-model"

    def _generate(self, *a, **k):
        CALLS.append(self.model_name)
        return super()._generate(*a, **k)


def _model(name: str) -> _Fake:
    return _Fake(messages=iter([AIMessage(content=f"answer from {name}")] * 50), model_name=name)


@pytest.fixture
def env(monkeypatch):
    from observability import tracing

    import runtime.state as rs

    def _create_llm(config, *, model_name=None, reasoning_effort=None):
        if model_name == _BAD:
            raise RuntimeError("Missing credentials")
        return _model(model_name or "default-model")

    monkeypatch.setattr("graph.llm.create_llm", _create_llm)
    CALLS.clear()
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


def test_goal_model_precedence():
    from types import SimpleNamespace

    from server.goal_loop import goal_model

    goal = SimpleNamespace(model=_GOOD)
    assert goal_model(goal, "") == _GOOD  # a re-drive with no pick keeps the goal's
    assert goal_model(goal, "other:pick") == "other:pick"  # the turn's own pick wins
    assert goal_model(None, "") == ""
    assert goal_model(SimpleNamespace(model=""), None) == ""


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
    """A goal that asks for one continuation, then is met."""
    import runtime.state as rs
    from tests._turn_driver_fakes import FakeGoals

    goals = FakeGoals([("continue", "not yet", "keep going"), ("done", "met")], iteration=1)
    goals.state.model = model
    monkeypatch.setattr(rs.STATE, "goal_controller", goals, raising=False)
    return goals


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
