"""Tests for KnowledgeIngestMiddleware."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from graph.middleware.knowledge_ingest import KnowledgeIngestMiddleware


def _req(name):
    return SimpleNamespace(tool_call={"name": name, "args": {}, "id": "c1"})


def _result(content):
    return SimpleNamespace(content=content)


def test_default_fallback_stores_raw_output():
    store = MagicMock()
    mw = KnowledgeIngestMiddleware(store)
    out = mw.wrap_tool_call(_req("web_search"), lambda r: _result("3 results found"))
    assert out.content == "3 results found"  # passthrough
    store.add_finding.assert_called_once()
    assert store.add_finding.call_args.kwargs["content"] == "3 results found"
    assert store.add_finding.call_args.kwargs["source"] == "tool:web_search"


def test_extractor_used_when_provided():
    store = MagicMock()
    mw = KnowledgeIngestMiddleware(store, extractor=lambda name, out: ["fact A", "fact B"])
    mw.wrap_tool_call(_req("t"), lambda r: _result("raw output"))
    assert store.add_finding.call_count == 2
    stored = [c.kwargs["content"] for c in store.add_finding.call_args_list]
    assert stored == ["fact A", "fact B"]


def test_skips_errors_and_empty():
    store = MagicMock()
    mw = KnowledgeIngestMiddleware(store)
    mw.wrap_tool_call(_req("t"), lambda r: _result("Error: boom"))
    mw.wrap_tool_call(_req("t"), lambda r: _result("Blocked by policy: x"))
    mw.wrap_tool_call(_req("t"), lambda r: _result("   "))
    store.add_finding.assert_not_called()


def test_ingest_tools_filter():
    store = MagicMock()
    mw = KnowledgeIngestMiddleware(store, ingest_tools=["keep"])
    mw.wrap_tool_call(_req("skip"), lambda r: _result("data"))
    store.add_finding.assert_not_called()
    mw.wrap_tool_call(_req("keep"), lambda r: _result("data"))
    store.add_finding.assert_called_once()


def test_extractor_failure_falls_back_and_never_raises():
    store = MagicMock()

    def boom(name, out):
        raise RuntimeError("llm down")

    mw = KnowledgeIngestMiddleware(store, extractor=boom)
    # no raise; falls back to storing raw output
    mw.wrap_tool_call(_req("t"), lambda r: _result("raw"))
    store.add_finding.assert_called_once()


def test_store_failure_never_raises():
    store = MagicMock()
    store.add_finding.side_effect = RuntimeError("db down")
    mw = KnowledgeIngestMiddleware(store)
    # must not propagate
    out = mw.wrap_tool_call(_req("t"), lambda r: _result("data"))
    assert out.content == "data"


@pytest.mark.asyncio
async def test_async_path():
    store = MagicMock()
    mw = KnowledgeIngestMiddleware(store)

    async def handler(r):
        return _result("async data")

    out = await mw.awrap_tool_call(_req("t"), handler)
    assert out.content == "async data"
    store.add_finding.assert_called_once()


def test_config_wires_ingest(tmp_path):
    import yaml
    from graph.config import LangGraphConfig
    from graph.agent import _build_middleware

    p = tmp_path / "c.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "middleware": {"ingest": True, "knowledge": False, "audit": False, "memory": False},
                "ingest": {"tools": ["web_search"]},
            }
        )
    )
    cfg = LangGraphConfig.from_yaml(p)
    assert cfg.ingest_enabled is True and cfg.ingest_tools == ["web_search"]
    mw = _build_middleware(cfg, knowledge_store=MagicMock())
    assert any(m.__class__.__name__ == "KnowledgeIngestMiddleware" for m in mw)
