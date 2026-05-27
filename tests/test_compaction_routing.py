"""Tests for compaction (SummarizationMiddleware) + routing (ModelFallbackMiddleware) wiring."""

import yaml

from graph.agent import _build_middleware, _parse_compaction_trigger
from graph.config import LangGraphConfig


def test_parse_trigger():
    assert _parse_compaction_trigger("fraction:0.8") == ("fraction", 0.8)
    assert _parse_compaction_trigger("tokens:120000") == ("tokens", 120000)
    assert _parse_compaction_trigger("messages:80") == ("messages", 80)
    assert _parse_compaction_trigger("garbage") == ("fraction", 0.8)  # safe fallback


def test_compaction_off_by_default():
    mw = _build_middleware(LangGraphConfig(), knowledge_store=None)
    assert not any(m.__class__.__name__ == "SummarizationMiddleware" for m in mw)


def test_compaction_wired_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({"compaction": {"enabled": True, "trigger": "tokens:100000", "keep_messages": 30}}))
    cfg = LangGraphConfig.from_yaml(p)
    assert cfg.compaction_enabled and cfg.compaction_keep_messages == 30
    mw = _build_middleware(cfg, knowledge_store=None)
    assert any(m.__class__.__name__ == "SummarizationMiddleware" for m in mw)


def test_routing_off_by_default():
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
