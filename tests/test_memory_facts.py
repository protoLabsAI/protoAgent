"""ADR 0125 P2-D3: the session-end extractor consolidates facts into the core MemoryStore.

The semantic half of the session-end pass distils durable facts (aux model) and stores them
as ``kind='fact'``, ``provenance='extracted'``, ``review_state='pending'`` rows scoped to the
harvest ``namespace`` (the row's ``scope``). Near-identical facts are deduped and revisions
use the shared subject supersession funnel across provenances.
"""

from __future__ import annotations

import asyncio
import sqlite3

from graph.memory_facts import (
    _parse_facts,
    consolidate_and_store,
    extract_and_store_facts,
)
from knowledge.memories import MemoryStore


def _facts(store, **kw):
    """The currently-valid extracted fact rows, newest first."""
    return store.list(kind="fact", provenance="extracted", limit=50, **kw)


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


# ── consolidation into memories ─────────────────────────────────────────────


def test_facts_stored_with_scope_kind_and_provenance(tmp_path):
    store = MemoryStore(tmp_path / "agent.db")
    counts = consolidate_and_store(store, ["operator deploys on Fridays"], namespace="proj-a")
    assert counts == {"added": 1, "skipped": 0, "superseded": 0}
    rows = _facts(store)
    assert len(rows) == 1
    row = rows[0]
    assert (row.kind, row.provenance, row.review_state) == ("fact", "extracted", "pending")
    assert row.scope == "proj-a"
    # Extracted facts are surfaced by relevance, never pushed in front of the model.
    assert row.delivery == "retrieved"
    assert row.content == "operator deploys on Fridays"


def test_near_duplicate_facts_are_skipped(tmp_path):
    store = MemoryStore(tmp_path / "agent.db")
    consolidate_and_store(store, ["The operator prefers metric units"], namespace="p")
    # Re-running with a near-identical fact must not append a second copy.
    counts = consolidate_and_store(store, ["The operator prefers metric units"], namespace="p")
    assert counts == {"added": 0, "skipped": 1, "superseded": 0}
    assert len(_facts(store)) == 1


def test_distinct_facts_are_added(tmp_path):
    store = MemoryStore(tmp_path / "agent.db")
    counts = consolidate_and_store(
        store,
        ["The gateway alias is protolabs/reasoning", "Releases are cut manually"],
        namespace="p",
    )
    assert counts == {"added": 2, "skipped": 0, "superseded": 0}


def test_namespace_scopes_dedup(tmp_path):
    # The same fact in a different scope is not a duplicate.
    store = MemoryStore(tmp_path / "agent.db")
    consolidate_and_store(store, ["deploys on Fridays"], namespace="proj-a")
    counts = consolidate_and_store(store, ["deploys on Fridays"], namespace="proj-b")
    assert counts == {"added": 1, "skipped": 0, "superseded": 0}
    assert len(_facts(store, scope="proj-a")) == 1
    assert len(_facts(store, scope="proj-b")) == 1


def test_facts_carry_source_session_and_thread(tmp_path):
    """ADR 0069 D5: a fact row links back to the session/thread it was extracted from;
    without a source the legacy "harvest" literal is kept (never empty)."""
    store = MemoryStore(tmp_path / "agent.db")
    consolidate_and_store(store, ["deploys on Fridays"], namespace="p", source="a2a:chat-42")
    consolidate_and_store(store, ["releases are manual"], namespace="p")
    by_content = {r.content: r for r in _facts(store)}
    assert by_content["deploys on Fridays"].source_session == "a2a:chat-42"
    assert by_content["deploys on Fridays"].thread_id == "a2a:chat-42"
    assert by_content["releases are manual"].source_session == "harvest"
    assert by_content["releases are manual"].thread_id == "harvest"


def test_extract_and_store_facts_end_to_end(tmp_path):
    store = MemoryStore(tmp_path / "agent.db")

    async def fake_extractor(transcript, config):
        assert "teal" in transcript
        # The second fact carries leaked model reasoning the extractor must scrub (ADR 0021).
        return ["The user's favorite color is teal", "<scratch_pad>noise</scratch_pad>also a fact"]

    counts = asyncio.run(
        extract_and_store_facts(
            "User: my favorite color is teal",
            memory_store=store,
            config=object(),
            namespace="ns1",
            source="a2a:chat-7",
            extractor=fake_extractor,
        )
    )
    assert counts["added"] == 2
    rows = _facts(store, scope="ns1")
    assert len(rows) == 2
    # ADR 0021 storage guardrail: leaked <scratch_pad> reasoning is stripped before the row
    # lands. The chunk path got this from add_chunk; the memory path strips in consolidation.
    assert all("scratch_pad" not in r.content.lower() for r in rows)
    assert "also a fact" in {r.content for r in rows}
    # Provenance threads through the extraction path (ADR 0069 D5).
    assert all(r.provenance == "extracted" and r.source_session == "a2a:chat-7" for r in rows)


def test_extract_noop_without_store():
    counts = asyncio.run(extract_and_store_facts("x", memory_store=None, config=object()))
    assert counts == {"added": 0, "skipped": 0, "superseded": 0}


# ── leaked-reasoning storage guardrail (ADR 0021) ────────────────────────────


def test_leaked_reasoning_is_stripped_before_the_row(tmp_path):
    """The chunk path scrubbed <scratch_pad>/<think> inside add_chunk; MemoryStore.add inserts
    verbatim, so consolidation strips — recallable fact rows never carry leaked reasoning."""
    store = MemoryStore(tmp_path / "agent.db")
    counts = consolidate_and_store(store, ["<think>deciding…</think>The operator prefers metric units"], namespace="p")
    assert counts["added"] == 1
    row = _facts(store)[0]
    assert row.content == "The operator prefers metric units"
    assert "think" not in row.content.lower()


def test_a_fact_that_is_only_reasoning_is_dropped(tmp_path):
    """Empty after stripping ⇒ not stored (the add_chunk 'refuse empty content' guardrail)."""
    store = MemoryStore(tmp_path / "agent.db")
    counts = consolidate_and_store(
        store, ["<scratch_pad>just thinking, nothing durable here</scratch_pad>"], namespace="p"
    )
    assert counts == {"added": 0, "skipped": 0, "superseded": 0}
    assert _facts(store) == []


# ── supersession chain (ADR 0108 D7.3) ───────────────────────────────────────

_OLD = "operator deploys on fridays after standup"
_REVISED = "operator deploys on fridays after lunch"  # Jaccard 5/7 ≈ 0.71 — the supersede band


def test_supersede_inserts_first_then_chains_the_old_row(tmp_path):
    store = MemoryStore(tmp_path / "agent.db")
    consolidate_and_store(store, [_OLD], namespace="p")
    old = _facts(store)[0]
    counts = consolidate_and_store(store, [_REVISED], namespace="p")
    assert counts == {"added": 1, "skipped": 0, "superseded": 1}
    valid = _facts(store)
    assert [r.content for r in valid] == [_REVISED]
    # The old row is chained via superseded_by and kept for audit — invalidated_at stays
    # NULL (not deleted, not updated in place), so inspect()/include_superseded still show it.
    audit = store.get(old.id)
    assert audit.superseded_by == valid[0].id
    assert audit.invalidated_at is None
    everything = {r.content for r in store.list(kind="fact", provenance="extracted", limit=50, include_superseded=True)}
    assert everything == {_OLD, _REVISED}


def test_supersede_applies_within_one_batch(tmp_path):
    """The batch-local bookkeeping supersedes too: a later fact in the same call revises an
    earlier one."""
    store = MemoryStore(tmp_path / "agent.db")
    counts = consolidate_and_store(store, [_OLD, _REVISED], namespace="p")
    assert counts == {"added": 2, "skipped": 0, "superseded": 1}
    assert [r.content for r in _facts(store)] == [_REVISED]


def test_unrelated_facts_do_not_supersede(tmp_path):
    store = MemoryStore(tmp_path / "agent.db")
    consolidate_and_store(store, ["The gateway alias is protolabs/reasoning"], namespace="p")
    counts = consolidate_and_store(store, ["Releases are cut manually"], namespace="p")
    assert counts == {"added": 1, "skipped": 0, "superseded": 0}
    assert len(_facts(store)) == 2


def test_subjectless_agent_row_is_not_superseded(tmp_path):
    """Rows without a subject remain outside subject supersession."""
    store = MemoryStore(tmp_path / "agent.db")
    agent_id = store.add(_OLD, kind="fact", provenance="agent", scope="p")
    counts = consolidate_and_store(store, [_REVISED], namespace="p")
    assert counts == {"added": 1, "skipped": 0, "superseded": 0}
    assert store.get(agent_id).superseded_by is None
    live = {r.content for r in store.list(kind="fact", limit=50)}
    assert live == {_OLD, _REVISED}


def test_failed_insert_never_supersedes_the_old_fact(tmp_path):
    store = MemoryStore(tmp_path / "agent.db")
    consolidate_and_store(store, [_OLD], namespace="p")

    class _Flaky:
        def list(self, **kw):
            return store.list(**kw)

        def supersede(self, *a, **kw):
            return store.supersede(*a, **kw)

        def add_with_supersession(self, *a, **kw):
            return None, []  # the insert fails

    counts = consolidate_and_store(_Flaky(), [_REVISED], namespace="p")
    assert counts == {"added": 0, "skipped": 0, "superseded": 0}
    assert [r.content for r in _facts(store)] == [_OLD]  # still valid, nothing chained


def test_insert_that_raises_is_swallowed_and_keeps_the_old_fact(tmp_path):
    """MemoryStore.add RAISES on SQLite errors (the old add_chunk returned None). The failure
    must be swallowed and the old fact left valid — consolidation never raises, so a fact-write
    failure can't propagate into harvest_thread (after the summary wrote) and make the retire
    sweep keep the thread and re-harvest it."""
    store = MemoryStore(tmp_path / "agent.db")
    consolidate_and_store(store, [_OLD], namespace="p")

    class _Raising:
        def list(self, **kw):
            return store.list(**kw)

        def supersede(self, *a, **kw):
            return store.supersede(*a, **kw)

        def add_with_supersession(self, *a, **kw):
            raise sqlite3.OperationalError("database is locked")

    counts = consolidate_and_store(_Raising(), [_REVISED], namespace="p")
    assert counts == {"added": 0, "skipped": 0, "superseded": 0}
    assert [r.content for r in _facts(store)] == [_OLD]  # still valid, nothing chained


def test_funnel_failure_rolls_back_insert_and_chain(tmp_path, caplog):
    store = MemoryStore(tmp_path / "agent.db")
    consolidate_and_store(store, [_OLD], namespace="p")
    with store._write() as db:
        db.execute("""CREATE TRIGGER refuse_chain BEFORE UPDATE OF superseded_by ON memories
                      BEGIN SELECT RAISE(ABORT, 'chain failed'); END""")
    counts = consolidate_and_store(store, [_REVISED], namespace="p")
    assert counts == {"added": 0, "skipped": 0, "superseded": 0}
    assert [r.content for r in _facts(store, include_superseded=True)] == [_OLD]
    assert "prior facts left untouched" in caplog.text


def test_minimal_non_core_store_keeps_add_only_fallback(tmp_path):
    store = MemoryStore(tmp_path / "agent.db")
    consolidate_and_store(store, [_OLD], namespace="p")

    class Minimal:
        def list(self, **kwargs):
            return store.list(**kwargs)

        def add(self, *args, **kwargs):
            return store.add(*args, **kwargs)

    counts = consolidate_and_store(Minimal(), [_REVISED], namespace="p")
    assert counts == {"added": 1, "skipped": 0, "superseded": 0}
    assert len(_facts(store)) == 2


# ── provider reasoning never reaches a fact or a summary ─────────────────────


def test_text_of_drops_reasoning_and_thinking_blocks():
    from langchain_core.messages import AIMessage

    from graph.message_blocks import text_of

    msg = AIMessage(
        content=[
            {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "gAAAAABqopaque"},
            {"type": "thinking", "thinking": "", "signature": "CAQSsig"},
            {"type": "redacted_thinking", "data": "EqQBsecret"},
            {"type": "text", "text": "the answer"},
        ]
    )
    assert text_of(msg) == "the answer"


def test_default_extractor_parses_the_text_of_a_block_list(monkeypatch):
    import graph.agent
    import graph.llm
    from langchain_core.messages import AIMessage

    from graph.memory_facts import _default_extractor

    reply = AIMessage(
        content=[
            {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "gAAAAAB[opaque]"},
            {"type": "text", "text": '[{"subject": "deploy-day", "content": "Deploys happen on Fridays"}]'},
        ]
    )

    class _LLM:
        async def ainvoke(self, messages):
            return reply

    monkeypatch.setattr(graph.agent, "_resolve_aux_model", lambda config, name: "aux")
    monkeypatch.setattr(graph.llm, "create_llm", lambda config, model_name=None: _LLM())
    facts = asyncio.run(_default_extractor("User: we deploy on Fridays", object()))
    assert facts == [{"subject": "deploy-day", "content": "Deploys happen on Fridays"}]
