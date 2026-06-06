"""Tests for hot-memory (always-on domain='hot' facts)."""


from langchain_core.messages import HumanMessage

from graph.middleware.knowledge import KnowledgeMiddleware
from knowledge.store import KnowledgeStore


def test_get_hot_memory_empty_when_none(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    assert store.get_hot_memory() == ""


def test_get_hot_memory_returns_hot_chunks(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("operator prefers metric units", domain="hot", heading="prefs")
    store.add_chunk("a normal note", domain="general")  # not hot
    out = store.get_hot_memory()
    assert "operator prefers metric units" in out
    assert "[prefs]" in out
    assert "a normal note" not in out  # only domain=hot


def test_get_hot_memory_respects_budget(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("x" * 100, domain="hot")
    store.add_chunk("y" * 100, domain="hot")
    out = store.get_hot_memory(max_chars=120)
    # Only one ~100-char chunk fits under the 120 budget.
    assert len(out) <= 120


def test_middleware_injects_hot_memory(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("deploys go out Fridays", domain="hot", heading="ops")
    km = KnowledgeMiddleware(knowledge_store=store)
    km._prior_sessions_cache = ""  # skip session loading
    result = km.before_model({"messages": [HumanMessage(content="anything")]}, runtime=None)
    assert result is not None
    assert "Always-on facts (hot memory)" in result["context"]
    assert "deploys go out Fridays" in result["context"]


def test_middleware_no_hot_memory_no_block(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    km = KnowledgeMiddleware(knowledge_store=store)
    km._prior_sessions_cache = ""
    result = km.before_model({"messages": [HumanMessage(content="hi")]}, runtime=None)
    if result is not None:
        assert "hot memory" not in result.get("context", "").lower()
