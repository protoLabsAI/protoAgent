"""ADR 0069 D8 — trust-tiered injection + hot-memory write visibility.

Covers the four pieces this round adds:

  1. The deterministic tier map (``knowledge/trust.py``): every source_type
     the in-tree writers stamp ranks into the right tier; unknown/unstamped
     values land in the lowest tier (least trust by default, not benefit of
     the doubt).
  2. Auto-inject delivery policy (``KnowledgeMiddleware``): low tiers are
     DOWN-WEIGHTED (stable-sorted below higher tiers, in-tier relevance
     preserved), ``knowledge.inject_min_trust`` EXCLUDES tiers below the
     floor (default 1 = nothing excluded), the candidate pool over-fetches
     only when a floor is active, and every injected line carries its tier
     label. ``memory_recall`` / ``memory_list`` cite the tier too.
  3. Hot-memory write visibility: any write that creates a ``domain="hot"``
     chunk emits ``memory.hot_written`` on the plugin event bus (ADR 0039
     HOST seam) — whoever wrote it.
  4. The optional confirm gate (``knowledge.hot_write_confirm``): when on,
     the agent's ``memory_ingest`` refuses hot writes with a clear error;
     off (default) keeps today's behavior; non-hot domains are never gated.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from langchain_core.messages import HumanMessage

from graph.middleware.knowledge import KnowledgeMiddleware
from knowledge.store import KnowledgeStore
from knowledge.trust import DEFAULT_TRUST_TIER, tier_label, trust_label, trust_tier
from tools.lg_tools import _build_memory_tools


def _by_name(tools):
    return {t.name: t for t in tools}


def _mw(store, **kw):
    mw = KnowledgeMiddleware(knowledge_store=store, **kw)
    mw._prior_sessions_cache = ""  # pin fresh so before_model never reads real disk
    import time

    mw._prior_sessions_loaded_at = time.monotonic()
    return mw


# ---------------------------------------------------------------------------
# 1) The tier map — every discovered writer source_type ranks deterministically
# ---------------------------------------------------------------------------


def test_trust_tier_map_covers_every_writer_source_type():
    # Tier 3 — operator surfaces (operator_api knowledge/memory routes).
    assert trust_tier("operator") == 3
    assert trust_tier("manual") == 3
    # Tier 2 — agent-derived writers: graph/memory_facts.py ("extracted"),
    # graph/conversation_harvest.py ("harvest"), memory_ingest + compaction
    # archives ("conversation"), knowledge/store.add_finding default ("chat").
    for st in ("extracted", "harvest", "conversation", "chat"):
        assert trust_tier(st) == 2, st
    # Tier 1 — every ingestion/engine.py extraction type + the generic aliases.
    for st in ("text", "markdown", "pdf", "html", "audio", "video", "image", "youtube", "web", "ingest", "external"):
        assert trust_tier(st) == 1, st


def test_unknown_or_missing_source_type_gets_least_trust():
    assert DEFAULT_TRUST_TIER == 1
    assert trust_tier(None) == 1
    assert trust_tier("") == 1
    assert trust_tier("some-fork-invented-this") == 1


def test_tier_lookup_is_case_and_whitespace_insensitive():
    assert trust_tier("Operator") == 3
    assert trust_tier("  EXTRACTED ") == 2


def test_tier_labels():
    assert trust_label("operator") == "operator"
    assert trust_label("extracted") == "agent"
    assert trust_label("youtube") == "external"
    assert trust_label(None) == "external"
    assert tier_label(99) == "external"  # unknown tier clamps to the low label


# ---------------------------------------------------------------------------
# 2) Auto-inject policy — down-weighting, the min-trust floor, visible tiers
# ---------------------------------------------------------------------------


def _seed_three_tiers(store: KnowledgeStore) -> None:
    # Insertion order is LOW trust first, so a pass-through order would put
    # external content on top — the down-weighting must flip it.
    store.add_chunk("gravity fact from a youtube transcript", source_type="youtube")
    store.add_chunk("gravity fact the agent extracted", source_type="extracted")
    store.add_chunk("gravity fact the operator wrote", source_type="operator")


def _context(mw, query="gravity fact"):
    result = mw.before_model({"messages": [HumanMessage(content=query)]}, runtime=None)
    return (result or {}).get("context", "")


def test_injection_down_weights_low_tiers(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    _seed_three_tiers(store)
    ctx = _context(_mw(store))
    op = ctx.index("operator wrote")
    ag = ctx.index("agent extracted")
    ext = ctx.index("youtube transcript")
    assert op < ag < ext  # higher trust always ranks above lower


def test_down_weighting_is_stable_within_a_tier(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    # Two same-tier hits: retrieval order between them must be preserved.
    store.add_chunk("gravity fact alpha from the operator", source_type="operator")
    store.add_chunk("gravity fact beta from the operator", source_type="operator")
    raw = store.search("gravity fact")
    ctx = _context(_mw(store))
    first, second = raw[0]["content"], raw[1]["content"]
    assert ctx.index(first[:30]) < ctx.index(second[:30])


def test_inject_min_trust_default_excludes_nothing(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    _seed_three_tiers(store)
    ctx = _context(_mw(store))  # default floor = 1
    assert "youtube transcript" in ctx and "agent extracted" in ctx and "operator wrote" in ctx


def test_inject_min_trust_2_excludes_ingested_content(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    _seed_three_tiers(store)
    store.add_chunk("gravity fact with no source stamp")  # unknown → tier 1
    ctx = _context(_mw(store, inject_min_trust=2))
    assert "operator wrote" in ctx and "agent extracted" in ctx
    assert "youtube transcript" not in ctx
    assert "no source stamp" not in ctx  # unknown is excluded like external


def test_inject_min_trust_3_keeps_operator_rows_only(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    _seed_three_tiers(store)
    ctx = _context(_mw(store, inject_min_trust=3))
    assert "operator wrote" in ctx
    assert "agent extracted" not in ctx and "youtube transcript" not in ctx


def test_min_trust_never_gates_memory_recall(tmp_path):
    """Excluded-from-injection content stays reachable on demand via the tool."""
    store = KnowledgeStore(tmp_path / "kb.db")
    _seed_three_tiers(store)
    recall = _by_name(_build_memory_tools(store))["memory_recall"]
    out = asyncio.run(recall.ainvoke({"query": "gravity fact"}))
    assert "youtube transcript" in out  # tier 1 still recallable


class _CapturingStore:
    def __init__(self, results=None):
        self.calls: list[dict] = []
        self._results = results or []

    def search(self, query, k=5, **kwargs):
        self.calls.append({"query": query, "k": k, **kwargs})
        return self._results


def test_pool_over_fetches_only_when_a_floor_is_active():
    store = _CapturingStore()
    _context(_mw(store, top_k=4))  # floor 1 → plain top_k (today's behavior)
    assert store.calls[0]["k"] == 4
    store2 = _CapturingStore()
    _context(_mw(store2, top_k=4, inject_min_trust=2))  # floor → 3× pool
    assert store2.calls[0]["k"] == 12


def test_filtered_pool_still_trims_to_top_k():
    hits = [
        {"table": "chunks", "preview": f"hit {i}", "id": i, "source_type": "operator"} for i in range(6)
    ]
    store = _CapturingStore(results=hits)
    ctx = _context(_mw(store, top_k=2, inject_min_trust=2))
    assert "hit 0" in ctx and "hit 1" in ctx and "hit 2" not in ctx


# ---------------------------------------------------------------------------
# 2b) The tier is visible — injected lines + memory_recall / memory_list
# ---------------------------------------------------------------------------


def test_injected_lines_carry_trust_label(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    _seed_three_tiers(store)
    ctx = _context(_mw(store))
    assert "trust: operator" in ctx
    assert "trust: agent" in ctx
    assert "trust: external" in ctx


def test_injected_lines_tag_source_domain_not_the_table(tmp_path):
    """#2161: an auto-injected hit shows its source DOMAIN (e.g. `claude-import`),
    not the physical table (always "chunks"). Without this the model reads inherited/
    imported knowledge back as its own memory ("attribution collapse")."""
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("gravity accelerates objects at 9.8 m/s^2", domain="claude-import", source_type="extracted")
    ctx = _context(_mw(store), query="gravity")
    assert "[claude-import]" in ctx  # inherited-domain tag is visible on recall
    assert "[chunks]" not in ctx  # the meaningless physical-table tag is gone


def test_memory_recall_and_list_cite_trust_label(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("the deploy day is Friday", domain="fact", source_type="extracted")
    tools = _by_name(_build_memory_tools(store))
    out = asyncio.run(tools["memory_recall"].ainvoke({"query": "deploy day"}))
    assert "trust: agent" in out
    listed = asyncio.run(tools["memory_list"].ainvoke({}))
    assert "trust: agent" in listed


def test_memory_ingest_stamps_conversation_source_type(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    ingest = _by_name(_build_memory_tools(store))["memory_ingest"]
    out = asyncio.run(ingest.ainvoke({"content": "operator takes coffee black"}))
    assert out.startswith("Stored chunk")
    row = store.list_chunks(limit=1)[0]
    assert row.source_type == "conversation"  # agent-derived tier, not unknown
    assert trust_tier(row.source_type) == 2


# ---------------------------------------------------------------------------
# 3) Hot-memory write visibility — memory.hot_written on the bus
# ---------------------------------------------------------------------------


def _capture_bus(monkeypatch):
    from graph.plugins.host import HOST

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(HOST, "publish", lambda topic, data: events.append((topic, data)))
    return events


def test_hot_write_emits_bus_event(tmp_path, monkeypatch):
    events = _capture_bus(monkeypatch)
    store = KnowledgeStore(tmp_path / "kb.db")
    cid = store.add_chunk("always-on fact", "hot", heading="pin", source="console", source_type="operator")
    assert cid is not None
    assert len(events) == 1
    topic, data = events[0]
    assert topic == "memory.hot_written"
    assert data["chunk_id"] == cid
    assert data["source"] == "console"
    assert data["source_type"] == "operator"
    assert data["preview"].startswith("always-on fact")


def test_non_hot_write_emits_nothing(tmp_path, monkeypatch):
    events = _capture_bus(monkeypatch)
    store = KnowledgeStore(tmp_path / "kb.db")
    assert store.add_chunk("a normal fact", "general") is not None
    assert events == []


def test_hot_write_survives_missing_bus(tmp_path, monkeypatch):
    """No wired publisher (unit tests / standalone use) must never break a write."""
    from graph.plugins.host import HOST

    monkeypatch.setattr(HOST, "publish", None)
    store = KnowledgeStore(tmp_path / "kb.db")
    assert store.add_chunk("always-on fact", "hot") is not None


def test_hot_write_survives_raising_bus(tmp_path, monkeypatch):
    from graph.plugins.host import HOST

    def _boom(topic, data):
        raise RuntimeError("bus down")

    monkeypatch.setattr(HOST, "publish", _boom)
    store = KnowledgeStore(tmp_path / "kb.db")
    assert store.add_chunk("always-on fact", "hot") is not None  # write still lands


def test_agent_tool_hot_write_emits_event(tmp_path, monkeypatch):
    """The choke point is store-level, so the agent's tool path emits too."""
    events = _capture_bus(monkeypatch)
    store = KnowledgeStore(tmp_path / "kb.db")
    ingest = _by_name(_build_memory_tools(store))["memory_ingest"]
    out = asyncio.run(ingest.ainvoke({"content": "pin this", "domain": "hot"}))
    assert out.startswith("Stored chunk")
    assert [t for t, _ in events] == ["memory.hot_written"]
    assert events[0][1]["source_type"] == "conversation"


# ---------------------------------------------------------------------------
# 4) The confirm gate — knowledge.hot_write_confirm
# ---------------------------------------------------------------------------


def test_confirm_gate_off_by_default_agent_hot_write_lands(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    ingest = _by_name(_build_memory_tools(store, graph_config=SimpleNamespace(knowledge_hot_write_confirm=False)))[
        "memory_ingest"
    ]
    out = asyncio.run(ingest.ainvoke({"content": "pin this", "domain": "hot"}))
    assert out.startswith("Stored chunk")
    assert store.list_chunks(domain="hot", limit=5)


def test_confirm_gate_on_refuses_agent_hot_write_with_clear_error(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    ingest = _by_name(_build_memory_tools(store, graph_config=SimpleNamespace(knowledge_hot_write_confirm=True)))[
        "memory_ingest"
    ]
    out = asyncio.run(ingest.ainvoke({"content": "pin this", "domain": "hot"}))
    assert out.startswith("Error:")
    assert "operator" in out  # tells the model to ask, not to retry
    assert store.list_chunks(domain="hot", limit=5) == []  # nothing parked/stored


def test_confirm_gate_on_leaves_other_domains_alone(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    ingest = _by_name(_build_memory_tools(store, graph_config=SimpleNamespace(knowledge_hot_write_confirm=True)))[
        "memory_ingest"
    ]
    out = asyncio.run(ingest.ainvoke({"content": "a general note", "domain": "general"}))
    assert out.startswith("Stored chunk")


def test_confirm_gate_on_operator_route_still_writes(tmp_path, monkeypatch):
    """The gate binds the AGENT's write path only — the operator's console
    routes call the store directly (stamping source_type="operator") and must
    keep working, event still emitted."""
    events = _capture_bus(monkeypatch)
    store = KnowledgeStore(tmp_path / "kb.db")
    # What operator_api/memory_routes.py's hot edit does, minus the HTTP shell.
    cid = store.add_chunk("operator pin", "hot", source="console", source_type="operator")
    assert cid is not None
    assert [t for t, _ in events] == ["memory.hot_written"]
