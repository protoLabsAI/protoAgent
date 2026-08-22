"""Tests for compaction (SummarizationMiddleware) + routing (ModelFallbackMiddleware) wiring."""

import asyncio
import logging

import pytest
import yaml
from langchain.agents.middleware import ModelFallbackMiddleware, SummarizationMiddleware

from graph.agent import (
    ObservableModelFallbackMiddleware,
    _build_middleware,
    _parse_compaction_trigger,
    _resolve_aux_model,
)
from graph.config import LangGraphConfig


def test_resolve_aux_model_precedence():
    """specific override > routing.aux_model > main model (None)."""
    cfg = LangGraphConfig()
    assert _resolve_aux_model(cfg, "") is None  # no aux set → main model
    cfg.aux_model = "protolabs/fast"
    assert _resolve_aux_model(cfg, "") == "protolabs/fast"  # falls back to aux
    assert _resolve_aux_model(cfg, "explicit") == "explicit"  # specific wins
    assert _resolve_aux_model(cfg, "  ") == "protolabs/fast"  # blank/whitespace → aux


def test_aux_model_parsed_from_routing_yaml(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({"routing": {"aux_model": "protolabs/fast"}}))
    cfg = LangGraphConfig.from_yaml(p)
    assert cfg.aux_model == "protolabs/fast"


def test_subagent_model_override_field_defaults_blank():
    from graph.subagents.config import SUBAGENT_REGISTRY

    assert getattr(SUBAGENT_REGISTRY["researcher"], "model", None) == ""


def test_parse_trigger():
    assert _parse_compaction_trigger("fraction:0.8") == ("fraction", 0.8)
    assert _parse_compaction_trigger("tokens:120000") == ("tokens", 120000)
    assert _parse_compaction_trigger("messages:80") == ("messages", 80)
    assert _parse_compaction_trigger("garbage") == ("fraction", 0.8)  # safe fallback


def test_compaction_on_by_default(monkeypatch):
    """Compaction is a default-on safety net against context overflow."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    cfg = LangGraphConfig()
    assert cfg.compaction_enabled
    mw = _build_middleware(cfg, knowledge_store=None)
    assert any(isinstance(m, SummarizationMiddleware) for m in mw)


def test_compaction_fraction_trigger_falls_back_without_model_profile(monkeypatch):
    """A `fraction:` trigger needs the model's context-window profile, which a
    custom gateway alias lacks — langchain raises at construction. The wiring
    must degrade to a message-count trigger, not crash the whole graph at load.
    Regression: defaulting compaction on would otherwise brick custom-model forks."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    cfg = LangGraphConfig()  # default trigger "fraction:0.8"; model alias has no profile
    assert cfg.compaction_trigger.startswith("fraction:")
    mw = _build_middleware(cfg, knowledge_store=None)  # must not raise
    assert any(isinstance(m, SummarizationMiddleware) for m in mw)


def test_compaction_wired_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({"compaction": {"enabled": True, "trigger": "tokens:100000", "keep_messages": 30}}))
    cfg = LangGraphConfig.from_yaml(p)
    assert cfg.compaction_enabled and cfg.compaction_keep_messages == 30
    mw = _build_middleware(cfg, knowledge_store=None)
    assert any(isinstance(m, SummarizationMiddleware) for m in mw)


def test_routing_off_by_default(monkeypatch):
    # Default-on compaction builds a summarizer LLM, which needs a key.
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    mw = _build_middleware(LangGraphConfig(), knowledge_store=None)
    assert not any(isinstance(m, ModelFallbackMiddleware) for m in mw)


def test_routing_wired_with_fallbacks(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({"routing": {"fallback_models": ["claude-haiku-4-5", "gpt-5"]}}))
    cfg = LangGraphConfig.from_yaml(p)
    assert cfg.routing_fallback_models == ["claude-haiku-4-5", "gpt-5"]
    mw = _build_middleware(cfg, knowledge_store=None)
    routing = [m for m in mw if isinstance(m, ModelFallbackMiddleware)]
    assert len(routing) == 1
    # The observable wrapper (#2956) is a drop-in subclass of the stock middleware.
    assert isinstance(routing[0], ObservableModelFallbackMiddleware)


# ── observable fallback (#2956) ────────────────────────────────────────────────
# The stock ModelFallbackMiddleware fails over silently; the wrapper must make a
# successful fallback loud (WARNING log + `model.fallback` bus event) without
# false-positives on a healthy turn.


class _FakeModel:
    """Stands in for a gateway chat model — only the name is consulted."""

    def __init__(self, name):
        self.model_name = name


class _FakeRequest:
    """Just enough of langchain's ModelRequest for the fallback loop: `.model`
    plus `.override(model=...)` returning a new request bound to the fallback."""

    def __init__(self, model="primary"):
        self.model = model

    def override(self, **overrides):
        return _FakeRequest(model=overrides.get("model", self.model))


def _primary_fails(request):
    if request.model == "primary":
        raise RuntimeError("primary down")
    return f"ok:{request.model.model_name}"


def test_fallback_emits_warning_log(caplog):
    mw = ObservableModelFallbackMiddleware(_FakeModel("fallback-a"))
    with caplog.at_level(logging.WARNING, logger="graph.agent"):
        result = mw.wrap_model_call(_FakeRequest(), _primary_fails)
    assert result == "ok:fallback-a"
    # The line names both the primary failure and the fallback model that served.
    assert "Fallback activated" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "fallback-a" in caplog.text


def test_fallback_emits_event(monkeypatch):
    from events import EventBus
    from graph.plugins.host import HOST

    bus = EventBus()
    received = []
    bus.subscribe_handler("model.fallback", received.append)
    monkeypatch.setattr(HOST, "publish", bus.publish)

    mw = ObservableModelFallbackMiddleware(_FakeModel("fallback-a"), _FakeModel("fallback-b"))
    assert mw.wrap_model_call(_FakeRequest(), _primary_fails) == "ok:fallback-a"
    assert len(received) == 1
    assert received[0]["event"] == "model.fallback"
    assert received[0]["data"] == {
        "primary_error": "RuntimeError",
        "fallback_model": "fallback-a",
        "fallback_index": 0,
    }


def test_fallback_async_path_emits_event(monkeypatch):
    from graph.plugins.host import HOST

    published = []
    monkeypatch.setattr(HOST, "publish", lambda topic, data: published.append((topic, data)))
    mw = ObservableModelFallbackMiddleware(_FakeModel("fallback-a"))

    async def _primary_fails_async(request):
        if request.model == "primary":
            raise TimeoutError("primary down")
        return "ok"

    assert asyncio.run(mw.awrap_model_call(_FakeRequest(), _primary_fails_async)) == "ok"
    assert published == [
        ("model.fallback", {"primary_error": "TimeoutError", "fallback_model": "fallback-a", "fallback_index": 0})
    ]


def test_no_fallback_no_event(monkeypatch, caplog):
    from graph.plugins.host import HOST

    published = []
    monkeypatch.setattr(HOST, "publish", lambda topic, data: published.append((topic, data)))
    mw = ObservableModelFallbackMiddleware(_FakeModel("fallback-a"))
    with caplog.at_level(logging.WARNING, logger="graph.agent"):
        assert mw.wrap_model_call(_FakeRequest(), lambda request: "primary-ok") == "primary-ok"
    assert published == []
    assert "Fallback" not in caplog.text


def test_unwired_bus_does_not_break_fallback(monkeypatch):
    # Outside a running server HOST.publish is None (tests, CLI) — the turn
    # must still be served by the fallback, observability degrading to the log.
    from graph.plugins.host import HOST

    monkeypatch.setattr(HOST, "publish", None)
    mw = ObservableModelFallbackMiddleware(_FakeModel("fallback-a"))
    assert mw.wrap_model_call(_FakeRequest(), _primary_fails) == "ok:fallback-a"


def test_all_fallbacks_fail_reraises_primary(monkeypatch):
    from graph.plugins.host import HOST

    published = []
    monkeypatch.setattr(HOST, "publish", lambda topic, data: published.append((topic, data)))
    mw = ObservableModelFallbackMiddleware(_FakeModel("fallback-a"))

    def _all_fail(request):
        if request.model == "primary":
            raise ValueError("primary down")
        raise RuntimeError("fallback down")

    with pytest.raises(ValueError, match="primary down"):
        mw.wrap_model_call(_FakeRequest(), _all_fail)
    assert published == []  # no success → no fallback event


def test_interrupt_bubbles_without_fallback(monkeypatch):
    # A GraphBubbleUp (HITL interrupt) is control flow, not a model failure —
    # it must propagate untouched, never triggering a fallback retry or event.
    from langgraph.errors import GraphBubbleUp

    from graph.plugins.host import HOST

    published = []
    monkeypatch.setattr(HOST, "publish", lambda topic, data: published.append((topic, data)))
    mw = ObservableModelFallbackMiddleware(_FakeModel("fallback-a"))

    calls = []

    def _interrupts(request):
        calls.append(request.model)
        raise GraphBubbleUp()

    with pytest.raises(GraphBubbleUp):
        mw.wrap_model_call(_FakeRequest(), _interrupts)
    assert calls == ["primary"]  # no fallback attempt
    assert published == []
