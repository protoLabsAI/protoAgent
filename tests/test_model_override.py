"""Per-tab model + reasoning-effort override — ModelOverrideMiddleware + wiring.

A chat tab can pick its own model AND reasoning effort; they ride on the turn as
``state["model"]`` / ``state["reasoning_effort"]`` and this middleware swaps the
bound model for that turn (building/caching a client via create_llm, keyed per
(model, effort)). Unset → the configured default, untouched.
"""

from __future__ import annotations

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage

from graph.middleware import model_override as mo


# ── unit: the middleware ──────────────────────────────────────────────────────


class _FakeModel:
    def __init__(self, name):
        self.model_name = name


class _FakeReq:
    def __init__(self, state, model):
        self.state = state
        self.model = model

    def override(self, **kw):
        return _FakeReq(self.state, kw.get("model", self.model))


def _patch_create_llm(monkeypatch, recorder):
    def _fake(config, *, model_name=None, reasoning_effort=None):
        recorder.append((model_name, reasoning_effort))
        return _FakeModel(model_name)

    monkeypatch.setattr("graph.llm.create_llm", _fake)


def test_swaps_model_when_state_selects_one(monkeypatch):
    built: list = []
    _patch_create_llm(monkeypatch, built)
    mw = mo.ModelOverrideMiddleware(config=object())
    seen = {}
    req = _FakeReq(state={"model": "protolabs/fast"}, model=_FakeModel("protolabs/reasoning"))
    mw.wrap_model_call(req, lambda r: seen.setdefault("model", r.model))
    assert built == [("protolabs/fast", None)]
    assert seen["model"].model_name == "protolabs/fast"


def test_effort_rebuilds_even_without_a_model_change(monkeypatch):
    """An effort with no per-tab model still rebuilds the CURRENT model with that effort."""
    built: list = []
    _patch_create_llm(monkeypatch, built)
    mw = mo.ModelOverrideMiddleware(config=object())
    req = _FakeReq(state={"reasoning_effort": "high"}, model=_FakeModel("protolabs/reasoning"))
    mw.wrap_model_call(req, lambda r: None)
    assert built == [("protolabs/reasoning", "high")]


def test_caches_per_model_and_effort(monkeypatch):
    """The cache key is (model, effort) — distinct efforts on one model build separately."""
    built: list = []
    _patch_create_llm(monkeypatch, built)
    mw = mo.ModelOverrideMiddleware(config=object())
    for _ in range(2):
        mw.wrap_model_call(_FakeReq({"model": "m1", "reasoning_effort": "low"}, _FakeModel("d")), lambda r: None)
        mw.wrap_model_call(_FakeReq({"model": "m1", "reasoning_effort": "high"}, _FakeModel("d")), lambda r: None)
    assert built == [("m1", "low"), ("m1", "high")]  # two keys, each built once


def test_noop_when_no_model_selected(monkeypatch):
    built: list = []
    _patch_create_llm(monkeypatch, built)
    mw = mo.ModelOverrideMiddleware(config=object())
    seen = {}
    req = _FakeReq(state={}, model=_FakeModel("protolabs/reasoning"))
    mw.wrap_model_call(req, lambda r: seen.setdefault("model", r.model))
    assert built == []  # create_llm never called
    assert seen["model"].model_name == "protolabs/reasoning"  # unchanged


def test_noop_when_already_on_selected_model(monkeypatch):
    built: list = []
    _patch_create_llm(monkeypatch, built)
    mw = mo.ModelOverrideMiddleware(config=object())
    req = _FakeReq(state={"model": "protolabs/fast"}, model=_FakeModel("protolabs/fast"))
    mw.wrap_model_call(req, lambda r: None)
    assert built == []  # no rebuild — already on it


def test_caches_built_clients(monkeypatch):
    built: list = []
    _patch_create_llm(monkeypatch, built)
    mw = mo.ModelOverrideMiddleware(config=object())
    for _ in range(3):
        mw.wrap_model_call(_FakeReq({"model": "m1"}, _FakeModel("d")), lambda r: None)
    assert built == [("m1", None)]  # built once, cached thereafter


@pytest.mark.asyncio
async def test_async_swaps_model(monkeypatch):
    built: list = []
    _patch_create_llm(monkeypatch, built)
    mw = mo.ModelOverrideMiddleware(config=object())
    seen = {}

    async def handler(r):
        seen["model"] = r.model

    await mw.awrap_model_call(_FakeReq({"model": "m2"}, _FakeModel("d")), handler)
    assert built == [("m2", None)] and seen["model"].model_name == "m2"


# ── integration: the override fires inside a real graph turn ───────────────────


class _ToolFake(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


@pytest.mark.asyncio
async def test_state_model_drives_the_turn(monkeypatch):
    from unittest.mock import patch
    from langgraph.checkpoint.memory import MemorySaver

    from graph.config import LangGraphConfig

    calls: list = []

    def _fake_create_llm(config, *, model_name=None, reasoning_effort=None):
        calls.append(model_name)
        return _ToolFake(messages=iter([AIMessage(content="ok")]))

    with patch("graph.agent.create_llm", _fake_create_llm), patch("graph.llm.create_llm", _fake_create_llm):
        from graph.agent import create_agent_graph

        g = create_agent_graph(LangGraphConfig(), include_subagents=False, checkpointer=MemorySaver())
        await g.ainvoke(
            {"messages": [HumanMessage("hi")], "session_id": "s1", "model": "protolabs/fast"},
            config={"configurable": {"thread_id": "t1"}},
        )
    # The middleware built a client for the tab's model during the turn.
    assert "protolabs/fast" in calls
