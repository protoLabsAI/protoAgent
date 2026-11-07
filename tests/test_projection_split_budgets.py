"""Split memory/knowledge projection budgets (ADR 0125 P2-F, #4630).

After P2-D1 memory lives in the core ``MemoryStore`` and knowledge in
``KnowledgeStore`` chunks. The per-turn projection splits the auto-injected
layer into labelled sections — "Always-on memory", "Relevant memory",
"Relevant knowledge" — each with its OWN char slice of the projection budget,
carved deterministically and never shared. So a 1 200-chunk knowledge
collection can't push always-on/relevant memory out of the turn, nor can a pile
of memories starve the knowledge section. Every gate (incognito, trust floor,
namespace scope, injection logging with a per-layer view) applies to both
layers.

Fakes at the store boundary; ``STATE.memory_store`` is the retrieval source for
both memory sections (the composer reads it, like the always-on block does).
"""

from __future__ import annotations

import pytest

from graph.projection import compose_projected_context, ProjectionOptions
from knowledge.memories import MemoryStore
from knowledge.store import KnowledgeStore


_QUERY = "deployment schedule"


@pytest.fixture(autouse=True)
def _quiet_working_state(monkeypatch):
    """No working-state block, no goal turn — every test states what it wants
    and the projection is just the injected-memory layer plus (optionally) the
    skill index."""
    import runtime.state as rs

    for attr in ("goal_controller", "tasks_store", "watch_controller", "scheduler"):
        monkeypatch.setattr(rs.STATE, attr, None, raising=False)
    monkeypatch.setattr(rs.STATE, "memory_store", None, raising=False)


def _use_memory_store(monkeypatch, store: MemoryStore) -> None:
    import runtime.state as rs

    monkeypatch.setattr(rs.STATE, "memory_store", store, raising=False)


def _compose(knowledge_store, *, opts, incognito=False, record=False):
    return compose_projected_context(
        _QUERY,
        knowledge_store,
        None,  # no skills index — keep the projection to the memory/knowledge layer
        {"session_id": "s-1"},
        incognito=incognito,
        record=record,
        options=opts,
        prior_sessions=lambda *a, **k: ("", []),
    )


def _section(text: str, marker: str) -> str:
    """The part text for ``marker`` — up to the next blank-line break, and
    without the envelope-close tag when the part is last in the envelope."""
    if marker not in text:
        return ""
    tail = text[text.index(marker) :].split("\n\n", 1)[0]
    return tail.split("\n</injected_memory>", 1)[0]


def _knowledge_section(text: str) -> str:
    return _section(text, "[Relevant knowledge:]")


def _memory_section(text: str) -> str:
    return _section(text, "[Relevant memory:]")


# ── r1 — a huge knowledge collection cannot push memory out ───────────────────


def test_large_knowledge_collection_keeps_all_memories(tmp_path, monkeypatch):
    """1 200 matching knowledge chunks + 3 always-on + 2 matching retrieved
    memories: all 5 memories reach the projected prompt (ADR 0125 P2-F r1)."""
    ks = KnowledgeStore(tmp_path / "kb.db")
    for i in range(1200):
        ks.add_chunk(f"deployment schedule knowledge chunk number {i}", source_type="operator")

    mem = MemoryStore(tmp_path / "mem.db")
    always = ["always rule alpha: ship on green", "always rule beta: no Friday deploys", "always rule gamma: tag first"]
    for text in always:
        mem.add(text, kind="standing", provenance="operator", delivery="always")
    recalled = ["deployment schedule note one: window is 09:00", "deployment schedule note two: owner is ops"]
    for text in recalled:
        mem.add(text, kind="fact", provenance="operator", delivery="retrieved")
    _use_memory_store(monkeypatch, mem)

    # A budget large enough that nothing sheds — the point is the SEPARATE stores
    # and slices, not shedding. top_k high so the whole collection is pulled.
    p = _compose(ks, opts=ProjectionOptions(top_k=1200, budget_chars=400_000))

    for text in always:
        assert text in p.text, f"always-on memory missing: {text!r}"
    for text in recalled:
        assert text in p.text, f"recalled memory missing: {text!r}"
    assert len(p.rel_memory_ids) == 2  # both retrieved memories delivered
    assert "[Relevant knowledge:]" in p.text and p.rag_ids  # knowledge got its own section too
    assert "[Always-on memory:]" in p.text and "[Relevant memory:]" in p.text


# ── r2 — a pile of memories cannot starve the knowledge section ───────────────


def test_many_memories_keep_knowledge_its_section(tmp_path, monkeypatch):
    """50 matching memories: the knowledge layer still gets its own section
    within its slice — memory is capped at its 40% and can't consume it."""
    ks = KnowledgeStore(tmp_path / "kb.db")
    kn_ids = [ks.add_chunk(f"deployment schedule authoritative doc {i}", source_type="operator") for i in range(5)]

    mem = MemoryStore(tmp_path / "mem.db")
    for i in range(50):
        mem.add(f"deployment schedule memory fact {i} " + "x" * 60, kind="fact", provenance="operator")
    _use_memory_store(monkeypatch, mem)

    # Memories alone would far exceed the budget; the 40% memory slice caps them.
    p = _compose(ks, opts=ProjectionOptions(top_k=50, budget_chars=6000))

    assert "[Relevant knowledge:]" in p.text
    assert p.rag_ids  # knowledge survived — not starved by the memory flood
    assert set(p.rag_ids).issubset(set(kn_ids))
    # The memory layer was trimmed to its slice (it can't spill into knowledge's).
    assert any(o["label"] == "Relevant memory" for o in p.overflow)
    assert len(p.rel_memory_ids) < 50


# ── neither layer borrows the other's unused budget (no sharing, both ways) ───


def test_knowledge_cannot_take_an_empty_memory_slice(tmp_path, monkeypatch):
    """With no relevant memory at all, knowledge is STILL capped at its own slice
    — an unused memory budget is not lent to knowledge."""
    ks = KnowledgeStore(tmp_path / "kb.db")
    for i in range(40):
        ks.add_chunk(f"deployment schedule bulky knowledge {i} " + "k" * 120, source_type="operator")
    _use_memory_store(monkeypatch, MemoryStore(tmp_path / "mem.db"))  # empty → no relevant memory

    budget = 8000
    p = _compose(ks, opts=ProjectionOptions(top_k=40, budget_chars=budget))

    assert "[Relevant memory:]" not in p.text  # memory slice entirely unused
    # Knowledge was shed to fit its 60% slice even though 100% of memory's 40%
    # was free — the cap is a hard, non-shared ceiling.
    assert any(o["label"] == "RAG hits" for o in p.overflow)
    assert len(_knowledge_section(p.text)) <= int(budget * 0.60)


def test_memory_cannot_take_an_empty_knowledge_slice(tmp_path, monkeypatch):
    """With no knowledge at all, relevant memory is STILL capped at its own slice
    — an unused knowledge budget is not lent to memory."""
    ks = KnowledgeStore(tmp_path / "kb.db")  # empty → no knowledge hits

    mem = MemoryStore(tmp_path / "mem.db")
    for i in range(40):
        mem.add(f"deployment schedule bulky memory {i} " + "m" * 120, kind="fact", provenance="operator")
    _use_memory_store(monkeypatch, mem)

    budget = 8000
    p = _compose(ks, opts=ProjectionOptions(top_k=40, budget_chars=budget))

    assert "[Relevant knowledge:]" not in p.text  # knowledge slice entirely unused
    assert any(o["label"] == "Relevant memory" for o in p.overflow)
    assert len(_memory_section(p.text)) <= int(budget * 0.40)


# ── on_demand memory is reachable only via an explicit tool call, never injected ─


def test_on_demand_memory_never_auto_injects(tmp_path, monkeypatch):
    """``on_demand`` memories are reachable ONLY through an explicit ``memory_search``
    tool call — the relevant-memory layer must never auto-inject them. And because
    the delivery filter runs IN the store (before the top-k cut), several matching
    on_demand rows do not take result slots and thin the one retrieved memory
    (ADR 0125 P2-F)."""
    ks = KnowledgeStore(tmp_path / "kb.db")

    mem = MemoryStore(tmp_path / "mem.db")
    # Five on_demand matches that would, under a post-top-k filter, crowd the one
    # retrieved memory out of a single-slot top_k.
    for i in range(5):
        mem.add(f"deployment schedule on-demand secret {i}", kind="fact", provenance="operator", delivery="on_demand")
    mem.add("deployment schedule retrieved note", kind="fact", provenance="operator", delivery="retrieved")
    _use_memory_store(monkeypatch, mem)

    p = _compose(ks, opts=ProjectionOptions(top_k=1, budget_chars=None))

    assert "deployment schedule retrieved note" in p.text  # the retrieved memory survives the slot
    assert "on-demand secret" not in p.text  # no on_demand memory auto-injects
    assert len(p.rel_memory_ids) == 1


# ── r3 — the sections are labelled; knowledge cites [collection · source] ──────


def test_sections_labelled_and_knowledge_cites_collection_and_source(tmp_path, monkeypatch):
    ks = KnowledgeStore(tmp_path / "kb.db")
    ks.add_chunk("deployment schedule runbook", domain="ops-collection", source_type="operator")

    mem = MemoryStore(tmp_path / "mem.db")
    mem.add("standing deployment schedule policy", kind="standing", provenance="operator", delivery="always")
    mem.add("deployment schedule recalled detail", kind="fact", provenance="operator", delivery="retrieved")
    _use_memory_store(monkeypatch, mem)

    p = _compose(ks, opts=ProjectionOptions(top_k=5, budget_chars=None))

    assert "[Always-on memory:]" in p.text
    assert "[Relevant memory:]" in p.text
    assert "[Relevant knowledge:]" in p.text
    # Each knowledge hit cited as [collection · source].
    assert "[ops-collection · operator]" in p.text
    # The sections keep the single labelled envelope with id-attributed counts.
    assert p.sections[0]["label"].startswith("Injected memory")
    assert "1 memories" in p.sections[0]["label"]  # one always-on
    assert "1 recalled" in p.sections[0]["label"]  # one relevant memory
    assert "1 docs" in p.sections[0]["label"]  # one knowledge hit


# ── r4 — incognito, trust floor, namespace scope gate BOTH layers ─────────────


def test_incognito_suppresses_both_layers(tmp_path, monkeypatch):
    ks = KnowledgeStore(tmp_path / "kb.db")
    ks.add_chunk("deployment schedule secret doc", source_type="operator")

    mem = MemoryStore(tmp_path / "mem.db")
    mem.add("always secret rule", kind="standing", provenance="operator", delivery="always")
    mem.add("deployment schedule secret memory", kind="fact", provenance="operator", delivery="retrieved")
    _use_memory_store(monkeypatch, mem)

    p = _compose(ks, opts=ProjectionOptions(top_k=5, budget_chars=None), incognito=True)

    assert "<injected_memory>" not in p.text
    assert "[Always-on memory:]" not in p.text
    assert "[Relevant memory:]" not in p.text
    assert "[Relevant knowledge:]" not in p.text
    assert p.rel_memory_ids == [] and p.rag_ids == [] and p.hot_ids == []


def test_trust_floor_gates_both_layers(tmp_path, monkeypatch):
    """A ``knowledge.inject_min_trust`` floor drops below-floor hits from the
    memory layer AND the knowledge layer through the one shared trust ladder."""
    ks = KnowledgeStore(tmp_path / "kb.db")
    ks.add_chunk("deployment schedule operator doc", source_type="operator")  # tier 3
    ks.add_chunk("deployment schedule ingested doc", source_type="ingest")  # tier 1

    mem = MemoryStore(tmp_path / "mem.db")
    mem.add("deployment schedule operator memory", kind="fact", provenance="operator")  # tier 3
    mem.add("deployment schedule extracted memory", kind="fact", provenance="extracted")  # tier 2
    _use_memory_store(monkeypatch, mem)

    # Floor 3: only operator-authored content survives, in BOTH layers.
    p = _compose(ks, opts=ProjectionOptions(top_k=5, inject_min_trust=3, budget_chars=None))

    assert "deployment schedule operator memory" in p.text
    assert "deployment schedule extracted memory" not in p.text  # tier 2 dropped
    assert "deployment schedule operator doc" in p.text
    assert "deployment schedule ingested doc" not in p.text  # tier 1 dropped


def test_namespace_scope_gates_the_memory_layer(tmp_path, monkeypatch):
    """``knowledge.inject_namespaces`` scopes the relevant-memory layer on the
    memory's ``scope`` (ADR 0069 D3a), the same gate the knowledge layer honours."""
    ks = KnowledgeStore(tmp_path / "kb.db")

    mem = MemoryStore(tmp_path / "mem.db")
    mem.add("deployment schedule in-scope memory", kind="fact", provenance="operator", scope="project:x")
    mem.add("deployment schedule out-of-scope memory", kind="fact", provenance="operator", scope="project:y")
    _use_memory_store(monkeypatch, mem)

    p = _compose(ks, opts=ProjectionOptions(top_k=5, inject_namespaces=("project:x",), budget_chars=None))

    assert "deployment schedule in-scope memory" in p.text
    assert "deployment schedule out-of-scope memory" not in p.text


def test_injection_log_row_carries_layer(tmp_path, monkeypatch):
    """The injection-log row tags its injected ids by layer: memory (always-on +
    recalled) vs knowledge (ADR 0069 D6 + ADR 0125 P2-F)."""
    import observability.injection_log as il
    from observability.injection_log import InjectionLog

    log = InjectionLog(str(tmp_path / "inj.db"))
    monkeypatch.setattr(il, "injection_log", lambda: log)

    ks = KnowledgeStore(tmp_path / "kb.db")
    kn_id = ks.add_chunk("deployment schedule knowledge row", source_type="operator")

    mem = MemoryStore(tmp_path / "mem.db")
    always_id = mem.add("always deployment rule", kind="standing", provenance="operator", delivery="always")
    recalled_id = mem.add("deployment schedule recalled row", kind="fact", provenance="operator", delivery="retrieved")
    _use_memory_store(monkeypatch, mem)

    _compose(ks, opts=ProjectionOptions(top_k=5, budget_chars=None), record=True)

    row = log.recent()[0]
    assert row["layers"]["knowledge"] == [kn_id]
    assert f"memory:{always_id}" in row["layers"]["memory"]  # always-on memory id
    assert f"memory:{recalled_id}" in row["layers"]["memory"]  # recalled memory id
    # The layers partition the injected ids — no knowledge id leaks into memory.
    assert kn_id not in row["layers"]["memory"]
    assert row["retrieved_memory_ids"] == [f"memory:{recalled_id}"]
