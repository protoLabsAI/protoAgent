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


# ── the ceiling + per-turn window (brandLaunch context burn, 10-2026) ─────────────────
# On a 1M-window model "fraction:0.8" meant 800k: never reached while a thread grew to
# 478k prompt tokens per call. The absolute compaction.max_tokens ceiling is OR-ed in,
# and the fraction is sized off THIS turn's model's window, not the summarizer's.


def _summ(trigger, *, profile_window=1_000_000, tokens=0, **kw):
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from graph.middleware.compaction import CountingSummarizationMiddleware

    model = FakeListChatModel(responses=["summary"], profile={"max_input_tokens": profile_window} if profile_window else None)
    return CountingSummarizationMiddleware(
        model=model, trigger=trigger, keep=("messages", 2), token_counter=lambda _m: tokens, **kw
    )


def _msgs(n=6):
    from langchain_core.messages import AIMessage, HumanMessage

    return [HumanMessage("q") if i % 2 == 0 else AIMessage("a") for i in range(n)]


def test_trigger_clauses_or_the_ceiling_in():
    from graph.agent import compaction_trigger_clauses

    assert compaction_trigger_clauses("fraction:0.8", 200_000) == [("fraction", 0.8), ("tokens", 200_000)]
    assert compaction_trigger_clauses("messages:80", 200_000) == [("messages", 80), ("tokens", 200_000)]
    assert compaction_trigger_clauses("tokens:300000", 200_000) == ("tokens", 200_000)
    assert compaction_trigger_clauses("tokens:120000", 200_000) == ("tokens", 120_000)
    assert compaction_trigger_clauses("fraction:0.8", 0) == ("fraction", 0.8)  # 0 = no ceiling


def test_ceiling_fires_compaction_on_a_1m_window_model():
    from graph.agent import compaction_trigger_clauses

    clauses = compaction_trigger_clauses("fraction:0.8", 200_000)
    assert _summ(clauses, tokens=250_000)._should_summarize(_msgs(), 250_000)
    assert not _summ(clauses, tokens=150_000)._should_summarize(_msgs(), 150_000)
    # the old fraction-only trigger: 250k on a 1M window never fires (the bug)
    assert not _summ(("fraction", 0.8), tokens=250_000)._should_summarize(_msgs(), 250_000)


def test_fraction_follows_the_turns_model_window_not_the_summarizers():
    """The summarizer's profile says 1M; this turn's model (a per-chat override) has
    100k. 90k tokens is over 0.8 × 100k, so it fires for that turn only."""
    from graph.middleware.compaction import _TURN_WINDOW

    mw = _summ(("fraction", 0.8), tokens=90_000, window_resolver=lambda state: state.get("win"))
    token = mw._bind_turn_window({"win": 100_000})
    try:
        assert mw._get_profile_limits() == 100_000
        assert mw._should_summarize(_msgs(), 90_000)
    finally:
        _TURN_WINDOW.reset(token)
    assert mw._get_profile_limits() == 1_000_000  # outside the turn: the summarizer's
    assert not mw._should_summarize(_msgs(), 90_000)


def test_default_window_beats_the_summarizers_profile():
    """A cheap compaction.model's profile must not size the trigger for the main model."""
    mw = _summ(("fraction", 0.8), profile_window=32_000, default_window=272_000)
    assert mw._get_profile_limits() == 272_000


def test_before_model_compacts_at_the_ceiling():
    from graph.agent import compaction_trigger_clauses

    mw = _summ(compaction_trigger_clauses("fraction:0.8", 200_000), tokens=210_000, window_resolver=lambda s: 1_000_000)
    out = mw.before_model({"messages": _msgs(8)}, None)
    assert out is not None  # compacted: RemoveMessage(ALL) + summary + preserved


def test_no_known_window_falls_back_to_the_ceiling(monkeypatch):
    """No gateway, no profile, no family: the fraction can't be sized, so the absolute
    ceiling alone is the trigger (not a 60-message count)."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    import graph.model_window as mw_mod

    monkeypatch.setattr(mw_mod, "context_window_for", lambda *a, **k: None)
    monkeypatch.setattr(mw_mod, "context_window_for_turn", lambda *a, **k: None)
    cfg = LangGraphConfig()
    cfg.model_name = "protolabs/mystery"
    summ = [m for m in _build_middleware(cfg, knowledge_store=None) if isinstance(m, SummarizationMiddleware)]
    assert summ and summ[0].trigger == ("tokens", 200_000)


def test_compaction_max_tokens_parsed_from_yaml(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({"compaction": {"max_tokens": 150000}}))
    assert LangGraphConfig.from_yaml(p).compaction_max_tokens == 150000
    assert LangGraphConfig().compaction_max_tokens == 200_000


def test_compaction_strips_thinking_from_the_kept_tail():
    """Keep-tail compaction: the kept turns' thinking blocks were produced with the full
    history in front of them; Anthropic's preserved-thinking check 400s them after the
    rewrite. They are stripped; text and tool_use stay."""
    from langchain_core.messages import AIMessage, HumanMessage

    think = {"type": "thinking", "thinking": "", "signature": "sig"}
    msgs = [HumanMessage("q", id=f"h{i}") if i % 2 == 0 else AIMessage([think, {"type": "text", "text": "a"}], id=f"a{i}") for i in range(10)]
    mw = _summ(("messages", 4), tokens=1)
    out = mw.before_model({"messages": msgs}, None)
    assert out is not None
    kept = out["messages"][2:]
    assert kept, "the parent keeps the newest messages verbatim"
    for m in kept:
        if isinstance(m.content, list):
            assert all(b.get("type") != "thinking" for b in m.content)
            assert any(b.get("type") == "text" for b in m.content)
    assert msgs[-1].content[0]["type"] == "thinking"  # the stored history is not mutated


def test_without_thinking_keeps_text_and_tool_use():
    from langchain_core.messages import AIMessage, HumanMessage

    from graph.message_blocks import without_thinking

    m = AIMessage(
        [
            {"type": "thinking", "thinking": "x", "signature": "s"},
            {"type": "redacted_thinking", "data": "d"},
            {"type": "text", "text": "hi"},
            {"type": "tool_use", "id": "t1", "name": "f", "input": {}},
        ],
        tool_calls=[{"id": "t1", "name": "f", "args": {}}],
        id="ai-1",
    )
    out = without_thinking(m)
    assert [b["type"] for b in out.content] == ["text", "tool_use"]
    assert out.id == "ai-1" and out.tool_calls[0]["id"] == "t1"
    plain = HumanMessage("hi")
    assert without_thinking(plain) is plain
