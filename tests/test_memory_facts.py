"""ADR 0021 Phase 2: semantic fact extraction + consolidation + namespace.

The session-end pass distils durable facts (aux model), consolidates them
(dedup near-identical), and stamps a namespace so per-project scoping is a filter
later, not a migration.
"""

from __future__ import annotations

import asyncio

from graph.memory_facts import (
    _parse_facts,
    consolidate_and_store,
    extract_and_store_facts,
)
from knowledge.store import KnowledgeStore


# ── parsing (defensive JSON) ────────────────────────────────────────────────


def test_parse_facts_plain_array():
    assert _parse_facts('["a", "b"]') == ["a", "b"]


def test_parse_facts_fenced_and_prose_wrapped():
    raw = 'Sure! Here are the facts:\n```json\n["operator deploys on Fridays"]\n```'
    assert _parse_facts(raw) == ["operator deploys on Fridays"]


def test_parse_facts_empty_and_garbage():
    assert _parse_facts("[]") == []
    assert _parse_facts("no array here") == []
    assert _parse_facts('{"not": "a list"}') == []


def test_parse_facts_drops_blank_and_caps_length():
    out = _parse_facts('["", "  ", "x", "' + "y" * 999 + '"]')
    assert out[0] == "x"
    assert len(out[1]) <= 300


# ── consolidation + namespace ───────────────────────────────────────────────


def test_facts_stored_with_namespace_and_type(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    counts = consolidate_and_store(store, ["operator deploys on Fridays"], namespace="proj-a")
    assert counts == {"added": 1, "skipped": 0, "superseded": 0}
    facts = store.list_chunks(domain="fact", limit=10)
    assert len(facts) == 1
    assert facts[0].finding_type == "fact"
    assert facts[0].namespace == "proj-a"


def test_near_duplicate_facts_are_skipped(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    consolidate_and_store(store, ["The operator prefers metric units"], namespace="p")
    # Re-running with a near-identical fact must not append a second copy.
    counts = consolidate_and_store(store, ["The operator prefers metric units"], namespace="p")
    assert counts == {"added": 0, "skipped": 1, "superseded": 0}
    assert len(store.list_chunks(domain="fact", limit=10)) == 1


def test_distinct_facts_are_added(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    counts = consolidate_and_store(
        store,
        ["The gateway alias is protolabs/reasoning", "Releases are cut manually"],
        namespace="p",
    )
    assert counts == {"added": 2, "skipped": 0, "superseded": 0}


def test_namespace_scopes_dedup(tmp_path):
    # The same fact in a different namespace is not a duplicate.
    store = KnowledgeStore(tmp_path / "kb.db")
    consolidate_and_store(store, ["deploys on Fridays"], namespace="proj-a")
    counts = consolidate_and_store(store, ["deploys on Fridays"], namespace="proj-b")
    assert counts == {"added": 1, "skipped": 0, "superseded": 0}
    assert len(store.list_chunks(domain="fact", namespace="proj-a")) == 1
    assert len(store.list_chunks(domain="fact", namespace="proj-b")) == 1


def test_facts_carry_source_session(tmp_path):
    """ADR 0069 D5: a fact row links back to the session it was extracted from;
    without a session id the legacy "harvest" literal is kept (never empty)."""
    store = KnowledgeStore(tmp_path / "kb.db")
    consolidate_and_store(store, ["deploys on Fridays"], namespace="p", source="a2a:chat-42")
    consolidate_and_store(store, ["releases are manual"], namespace="p")
    by_content = {c.content: c for c in store.list_chunks(domain="fact", limit=10)}
    assert by_content["deploys on Fridays"].source == "a2a:chat-42"
    assert by_content["releases are manual"].source == "harvest"


def test_extract_and_store_facts_end_to_end(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")

    async def fake_extractor(transcript, config):
        assert "teal" in transcript
        return ["The user's favorite color is teal", "<scratch_pad>noise</scratch_pad>also a fact"]

    counts = asyncio.run(
        extract_and_store_facts(
            "User: my favorite color is teal",
            knowledge_store=store,
            config=object(),
            namespace="ns1",
            source="a2a:chat-7",
            extractor=fake_extractor,
        )
    )
    assert counts["added"] == 2
    facts = store.list_chunks(domain="fact", namespace="ns1", limit=10)
    # The store guardrail (ADR 0021 Phase 1) strips scratch_pad even here.
    assert all("scratch_pad" not in f.content.lower() for f in facts)
    # Provenance threads through the extraction path (ADR 0069 D5).
    assert all(f.source == "a2a:chat-7" for f in facts)


def test_extract_noop_without_store():
    counts = asyncio.run(extract_and_store_facts("x", knowledge_store=None, config=object()))
    assert counts == {"added": 0, "skipped": 0, "superseded": 0}


# ── supersession chain (ADR 0108 D7.3) ───────────────────────────────────────

_OLD = "operator deploys on fridays after standup"
_REVISED = "operator deploys on fridays after lunch"  # Jaccard 5/7 ≈ 0.71 — the supersede band


def test_supersede_inserts_first_then_chains_the_old_row(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    consolidate_and_store(store, [_OLD], namespace="p")
    old = store.list_chunks(domain="fact", limit=10)[0]
    counts = consolidate_and_store(store, [_REVISED], namespace="p")
    assert counts == {"added": 1, "skipped": 0, "superseded": 1}
    valid = store.list_chunks(domain="fact", limit=10)
    assert [c.content for c in valid] == [_REVISED]
    audit = store.get_chunk(old.id)
    assert audit["invalidated_at"]
    assert audit["invalidation_reason"] == f"superseded_by:{valid[0].id}"


def test_failed_insert_never_invalidates_the_old_fact(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    consolidate_and_store(store, [_OLD], namespace="p")

    class _Flaky:
        def list_chunks(self, **kw):
            return store.list_chunks(**kw)

        def invalidate_chunk(self, *a, **kw):
            return store.invalidate_chunk(*a, **kw)

        def add_chunk(self, *a, **kw):
            return None  # the insert fails

    counts = consolidate_and_store(_Flaky(), [_REVISED], namespace="p")
    assert counts == {"added": 0, "skipped": 0, "superseded": 0}
    assert [c.content for c in store.list_chunks(domain="fact", limit=10)] == [_OLD]  # still valid


def test_supersede_falls_back_on_a_backend_without_the_chain_kwarg(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    consolidate_and_store(store, [_OLD], namespace="p")

    class _Legacy:
        def list_chunks(self, **kw):
            return store.list_chunks(**kw)

        def add_chunk(self, *a, **kw):
            return store.add_chunk(*a, **kw)

        def invalidate_chunk(self, chunk_id):  # pre-D7 signature: no superseded_by
            return store.invalidate_chunk(chunk_id)

    counts = consolidate_and_store(_Legacy(), [_REVISED], namespace="p")
    assert counts["superseded"] == 1
    invalidated = [c for c in store.list_chunks(domain="fact", limit=10, include_invalidated=True) if c.invalidated_at]
    assert len(invalidated) == 1 and invalidated[0].invalidation_reason is None  # superseded, unchained
