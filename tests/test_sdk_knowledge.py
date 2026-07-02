"""graph.sdk.knowledge_search / knowledge_add / knowledge_purge — the plugin↔knowledge
channel (ADR 0043) + its lifecycle half (#1634: purge + epoch scoping)."""

from __future__ import annotations

import pytest


class _FakeStore:
    """A minimal ADR 0031 backend — deliberately PRE-#1634 (no epoch kwarg, no
    purge_domain), so these tests double as the backward-compat proof: the SDK's
    unfiltered paths must never forward the new kwargs at an old backend."""

    def __init__(self):
        self.calls = []

    def search(self, query, k=5, *, domain=None):
        self.calls.append(("search", query, k, domain))
        return [{"preview": "a lesson", "domain": domain or "general", "score": 0.9}]

    def add_chunk(self, content, domain="general", heading=None):
        self.calls.append(("add", content, domain, heading))
        return 42


class _LifecycleStore(_FakeStore):
    """A backend with the #1634 lifecycle surface: epoch-aware add/search + purge."""

    def search(self, query, k=5, *, domain=None, epoch=None):
        self.calls.append(("search", query, k, domain, epoch))
        return [{"preview": "an era lesson", "domain": domain or "general", "epoch": epoch}]

    def add_chunk(self, content, domain="general", heading=None, *, epoch=None):
        self.calls.append(("add", content, domain, heading, epoch))
        return 43

    def purge_domain(self, domain, *, before=None):
        self.calls.append(("purge", domain, before))
        return 7


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
    assert await sdk.knowledge_purge("loop-lessons") == 0


@pytest.mark.asyncio
async def test_knowledge_add_and_search_pass_epoch_when_set(monkeypatch):
    from graph import sdk

    store = _LifecycleStore()
    monkeypatch.setattr(sdk.STATE, "knowledge_store", store, raising=False)
    cid = await sdk.knowledge_add("route lesson", domain="st-routes", epoch="2026-06-29")
    out = await sdk.knowledge_search("route", k=2, domain="st-routes", epoch="2026-06-29")
    assert cid == 43
    assert out and out[0]["epoch"] == "2026-06-29"
    assert store.calls == [
        ("add", "route lesson", "st-routes", None, "2026-06-29"),
        ("search", "route", 2, "st-routes", "2026-06-29"),
    ]


@pytest.mark.asyncio
async def test_knowledge_purge_wraps_the_store(monkeypatch):
    from graph import sdk

    store = _LifecycleStore()
    monkeypatch.setattr(sdk.STATE, "knowledge_store", store, raising=False)
    assert await sdk.knowledge_purge("st-routes") == 7
    assert await sdk.knowledge_purge("st-routes", before="2026-06-01") == 7
    assert store.calls == [
        ("purge", "st-routes", None),
        ("purge", "st-routes", "2026-06-01"),
    ]


@pytest.mark.asyncio
async def test_knowledge_purge_degrades_on_a_backend_without_purge_domain(monkeypatch):
    # An ADR 0031 backend predating #1634 has no purge_domain — the SDK returns a
    # 0-count no-op instead of raising at the plugin.
    from graph import sdk

    store = _FakeStore()
    monkeypatch.setattr(sdk.STATE, "knowledge_store", store, raising=False)
    assert await sdk.knowledge_purge("loop-lessons") == 0
    assert store.calls == []  # never touched
