"""ADR 0021 guardrail: the model's internal reasoning must never reach the
knowledge store.

Every write funnels through ``KnowledgeStore.add_chunk``, which strips
``<scratch_pad>``/``<think>`` defensively — so no writer (memory tools, ingest,
harvest, or a future one) can leak reasoning into the searchable base, where the
retrieval layer would recycle it into future prompts.
"""

from __future__ import annotations

from knowledge.store import KnowledgeStore


def _only_chunk(store: KnowledgeStore) -> str:
    chunks = store.list_chunks(limit=10)
    assert chunks, "expected a stored chunk"
    return chunks[0].content


def test_add_chunk_strips_scratch_pad_keeps_output(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    raw = (
        "<scratch_pad>The user wants the release cadence. Let me recall…</scratch_pad>\n"
        "<output>Releases are cut manually via workflow_dispatch.</output>"
    )
    store.add_chunk(raw, domain="finding")
    stored = _only_chunk(store)
    assert "scratch_pad" not in stored.lower()
    assert "Releases are cut manually" in stored


def test_add_chunk_strips_think_tags(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("<think>internal</think>The gateway alias is protolabs/reasoning.", domain="general")
    stored = _only_chunk(store)
    assert "<think>" not in stored and "internal" not in stored
    assert "protolabs/reasoning" in stored


def test_add_chunk_strips_orphan_open_scratch_pad(tmp_path):
    # Truncated mid-reasoning (max_tokens) — the orphan opener eats to end.
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("Visible fact.\n<scratch_pad>then it got cut off mid-thought", domain="general")
    stored = _only_chunk(store)
    assert "scratch_pad" not in stored.lower()
    assert "Visible fact." in stored


def test_add_finding_inherits_the_guardrail(tmp_path):
    # add_finding funnels through add_chunk, so it's covered too.
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_finding("<scratch_pad>noise</scratch_pad>Real finding.", finding_type="fact")
    stored = _only_chunk(store)
    assert "scratch_pad" not in stored.lower()
    assert "Real finding." in stored


def test_chunk_that_is_only_reasoning_is_dropped(tmp_path):
    # If stripping leaves nothing, nothing is stored (no empty-row pollution).
    store = KnowledgeStore(tmp_path / "kb.db")
    rid = store.add_chunk("<scratch_pad>pure internal reasoning, no output</scratch_pad>", domain="finding")
    assert rid is None
    assert store.list_chunks(limit=10) == []


def test_clean_content_passes_through_unchanged(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("A plain fact with no tags.", domain="general")
    assert _only_chunk(store) == "A plain fact with no tags."
