"""Tests for compaction (SummarizationMiddleware) + routing (ModelFallbackMiddleware) wiring."""

import yaml

from graph.agent import _build_middleware, _parse_compaction_trigger, _resolve_aux_model
from graph.config import LangGraphConfig


def test_resolve_aux_model_precedence():
    """specific override > routing.aux_model > main model (None)."""
    cfg = LangGraphConfig()
    assert _resolve_aux_model(cfg, "") is None            # no aux set → main model
    cfg.aux_model = "protolabs/fast"
    assert _resolve_aux_model(cfg, "") == "protolabs/fast"        # falls back to aux
    assert _resolve_aux_model(cfg, "explicit") == "explicit"     # specific wins
    assert _resolve_aux_model(cfg, "  ") == "protolabs/fast"     # blank/whitespace → aux


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
    assert any(m.__class__.__name__ == "SummarizationMiddleware" for m in mw)


def test_compaction_fraction_trigger_falls_back_without_model_profile(monkeypatch):
    """A `fraction:` trigger needs the model's context-window profile, which a
    custom gateway alias lacks — langchain raises at construction. The wiring
    must degrade to a message-count trigger, not crash the whole graph at load.
    Regression: defaulting compaction on would otherwise brick custom-model forks."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    cfg = LangGraphConfig()  # default trigger "fraction:0.8"; model alias has no profile
    assert cfg.compaction_trigger.startswith("fraction:")
    mw = _build_middleware(cfg, knowledge_store=None)  # must not raise
    assert any(m.__class__.__name__ == "SummarizationMiddleware" for m in mw)


def test_compaction_wired_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({"compaction": {"enabled": True, "trigger": "tokens:100000", "keep_messages": 30}}))
    cfg = LangGraphConfig.from_yaml(p)
    assert cfg.compaction_enabled and cfg.compaction_keep_messages == 30
    mw = _build_middleware(cfg, knowledge_store=None)
    assert any(m.__class__.__name__ == "SummarizationMiddleware" for m in mw)


def test_routing_off_by_default(monkeypatch):
    # Default-on compaction builds a summarizer LLM, which needs a key.
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    mw = _build_middleware(LangGraphConfig(), knowledge_store=None)
    assert not any(m.__class__.__name__ == "ModelFallbackMiddleware" for m in mw)


def test_routing_wired_with_fallbacks(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({"routing": {"fallback_models": ["claude-haiku-4-5", "gpt-5"]}}))
    cfg = LangGraphConfig.from_yaml(p)
    assert cfg.routing_fallback_models == ["claude-haiku-4-5", "gpt-5"]
    mw = _build_middleware(cfg, knowledge_store=None)
    assert any(m.__class__.__name__ == "ModelFallbackMiddleware" for m in mw)
