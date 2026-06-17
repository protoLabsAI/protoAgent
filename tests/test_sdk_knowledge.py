"""graph.sdk.knowledge_search / knowledge_add — the plugin↔knowledge channel (ADR 0043)."""

from __future__ import annotations

import pytest


class _FakeStore:
    def __init__(self):
        self.calls = []

    def search(self, query, k=5, *, domain=None):
        self.calls.append(("search", query, k, domain))
        return [{"preview": "a lesson", "domain": domain or "general", "score": 0.9}]

    def add_chunk(self, content, domain="general", heading=None):
        self.calls.append(("add", content, domain, heading))
        return 42


@pytest.mark.asyncio
async def test_knowledge_search_wraps_the_store(monkeypatch):
    from graph import sdk

    store = _FakeStore()
    monkeypatch.setattr(sdk.STATE, "knowledge_store", store, raising=False)
    out = await sdk.knowledge_search("golden map", k=3, domain="loop-lessons")
    assert out and out[0]["preview"] == "a lesson"
    assert store.calls == [("search", "golden map", 3, "loop-lessons")]


@pytest.mark.asyncio
async def test_knowledge_add_wraps_the_store(monkeypatch):
    from graph import sdk

    store = _FakeStore()
    monkeypatch.setattr(sdk.STATE, "knowledge_store", store, raising=False)
    cid = await sdk.knowledge_add("a lesson", domain="loop-lessons", heading="golden-map")
    assert cid == 42
    assert store.calls == [("add", "a lesson", "loop-lessons", "golden-map")]


@pytest.mark.asyncio
async def test_knowledge_ops_degrade_to_noop_without_a_store(monkeypatch):
    from graph import sdk

    monkeypatch.setattr(sdk.STATE, "knowledge_store", None, raising=False)
    assert await sdk.knowledge_search("x") == []
    assert await sdk.knowledge_add("x", domain="loop-lessons") is None
