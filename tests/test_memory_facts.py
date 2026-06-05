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
    assert counts == {"added": 1, "skipped": 0}
    facts = store.list_chunks(domain="fact", limit=10)
    assert len(facts) == 1
    assert facts[0].finding_type == "fact"
    assert facts[0].namespace == "proj-a"


def test_near_duplicate_facts_are_skipped(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    consolidate_and_store(store, ["The operator prefers metric units"], namespace="p")
    # Re-running with a near-identical fact must not append a second copy.
    counts = consolidate_and_store(store, ["The operator prefers metric units"], namespace="p")
    assert counts == {"added": 0, "skipped": 1}
    assert len(store.list_chunks(domain="fact", limit=10)) == 1


def test_distinct_facts_are_added(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    counts = consolidate_and_store(
        store,
        ["The gateway alias is protolabs/reasoning", "Releases are cut manually"],
        namespace="p",
    )
    assert counts == {"added": 2, "skipped": 0}


def test_namespace_scopes_dedup(tmp_path):
    # The same fact in a different namespace is not a duplicate.
    store = KnowledgeStore(tmp_path / "kb.db")
    consolidate_and_store(store, ["deploys on Fridays"], namespace="proj-a")
    counts = consolidate_and_store(store, ["deploys on Fridays"], namespace="proj-b")
    assert counts == {"added": 1, "skipped": 0}
    assert len(store.list_chunks(domain="fact", namespace="proj-a")) == 1
    assert len(store.list_chunks(domain="fact", namespace="proj-b")) == 1


def test_extract_and_store_facts_end_to_end(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")

    async def fake_extractor(transcript, config):
        assert "teal" in transcript
        return ["The user's favorite color is teal", "<scratch_pad>noise</scratch_pad>also a fact"]

    counts = asyncio.run(extract_and_store_facts(
        "User: my favorite color is teal", knowledge_store=store, config=object(),
        namespace="ns1", extractor=fake_extractor,
    ))
    assert counts["added"] == 2
    facts = store.list_chunks(domain="fact", namespace="ns1", limit=10)
    # The store guardrail (ADR 0021 Phase 1) strips scratch_pad even here.
    assert all("scratch_pad" not in f.content.lower() for f in facts)


def test_extract_noop_without_store():
    counts = asyncio.run(extract_and_store_facts("x", knowledge_store=None, config=object()))
    assert counts == {"added": 0, "skipped": 0}
