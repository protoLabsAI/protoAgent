"""LayeredKnowledgeStore (ADR 0041 / bd-2wu) — commons ∪ private, write private, promote.

Runs against real (FTS5-only) KnowledgeStore backends on tmp DBs — the tier semantics
(union reads, private writes, promote/forget, tier tags) are what we pin, and a real
store is cheaper than faking both halves. Vector fusion is covered by the store's own
hybrid tests; here the fusion is exercised over FTS rank.
"""

from __future__ import annotations

from knowledge.layered import LayeredKnowledgeStore
from knowledge.store import KnowledgeStore


def _stores(tmp_path):
    private = KnowledgeStore(str(tmp_path / "priv.db"))
    commons = KnowledgeStore(str(tmp_path / "commons.db"))
    return private, commons


# ── store seams (the commons relies on these) ───────────────────────────────────


def test_unscoped_path_is_verbatim(tmp_path, monkeypatch):
    """scoped=False uses the path verbatim — the host-level commons every agent shares
    regardless of instance id. A scoped store's DEFAULT lives under the instance root."""
    import infra.paths as paths

    monkeypatch.delenv("KNOWLEDGE_DB_PATH", raising=False)
    monkeypatch.setenv("PROTOAGENT_BOX_ROOT", str(tmp_path))
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "agent-7")
    paths.reset_instance_paths()
    p = str(tmp_path / "commons" / "knowledge.db")
    assert str(KnowledgeStore(p, scoped=False).path) == p  # un-scoped, verbatim
    # ...whereas a scoped store's default namespaces under the instance root.
    assert KnowledgeStore(None, scoped=True).path == tmp_path / "agent-7" / "knowledge" / "agent.db"


def test_meta_roundtrip(tmp_path):
    s = KnowledgeStore(str(tmp_path / "k.db"))
    assert s.get_meta("embed_model") is None
    s.set_meta("embed_model", "protolabs/embed-v1")
    assert s.get_meta("embed_model") == "protolabs/embed-v1"
    s.set_meta("embed_model", "protolabs/embed-v2")  # upsert
    assert s.get_meta("embed_model") == "protolabs/embed-v2"


# ── LayeredKnowledgeStore ────────────────────────────────────────────────────────


def test_writes_go_to_private_only(tmp_path):
    private, commons = _stores(tmp_path)
    layered = LayeredKnowledgeStore(private, commons)
    layered.add_chunk("orbital mechanics for hohmann transfers", domain="finding")
    assert private.stats()["total"] == 1
    assert commons.stats()["total"] == 0  # commons untouched by a write


def test_search_unions_both_tiers_with_tier_tags(tmp_path):
    private, commons = _stores(tmp_path)
    private.add_chunk("private note about kestrel engines", domain="finding")
    commons.add_chunk("shared reference on kestrel turbopumps", domain="reference")
    layered = LayeredKnowledgeStore(private, commons)

    hits = layered.search("kestrel", k=5)
    tiers = {h["tier"] for h in hits}
    assert {"private", "commons"} <= tiers  # reads BOTH tiers
    assert any("private note" in h["content"] for h in hits)
    assert any("shared reference" in h["content"] for h in hits)


def test_promote_is_idempotent_and_forget(tmp_path):
    private, commons = _stores(tmp_path)
    layered = LayeredKnowledgeStore(private, commons)
    cid = private.add_chunk("the deploy runbook: drain, ship, verify, roll back on failure", domain="reference")

    rec = layered.promote(cid)
    assert rec is not None and rec["tier"] == "commons"
    assert commons.stats()["total"] == 1  # landed in the commons

    # Idempotent: re-promoting the same content doesn't duplicate.
    layered.promote(cid)
    assert commons.stats()["total"] == 1

    # The commons copy is searchable + tagged commons; private original is untouched.
    chunk = layered.list_chunks()  # union, tier-tagged Chunk rows
    assert {"private", "commons"} == {c.tier for c in chunk}

    commons_id = next(c.id for c in layered.list_chunks() if c.tier == "commons")
    assert layered.forget_from_commons(commons_id) is True
    assert commons.stats()["total"] == 0
    assert private.stats()["total"] == 1  # private untouched by forget


def test_promote_unknown_id_returns_none(tmp_path):
    private, commons = _stores(tmp_path)
    assert LayeredKnowledgeStore(private, commons).promote(9999) is None


def test_promote_forwards_typed_memory_fields(tmp_path):
    """promote() must carry memory_kind/subject/expires_at into the commons; the
    commons copy's verdict is ``confirmed`` by construction (ADR 0108 D7.2 —
    promotion IS the operator's curation), whatever the private row's was."""
    private, commons = _stores(tmp_path)
    cid = private.add_chunk(
        "user prefers terse replies",
        domain="general",
        memory_kind="standing",
        subject="operator",
        review_state="pending",
        expires_at="2027-01-01",  # date-only → stored as UTC ISO by the store
    )
    layered = LayeredKnowledgeStore(private, commons)
    rec = layered.promote(cid)
    assert rec is not None

    commons_chunks = commons.list_chunks()
    assert len(commons_chunks) == 1
    c = commons_chunks[0]
    assert c.memory_kind == "standing"
    assert c.subject == "operator"
    assert c.review_state == "confirmed"
    assert c.expires_at == "2027-01-01T00:00:00+00:00"
    assert private.list_chunks(limit=1)[0].review_state == "pending"  # the private verdict is its own


def test_stats_split_by_tier(tmp_path):
    private, commons = _stores(tmp_path)
    private.add_chunk("a", domain="finding")
    private.add_chunk("b", domain="finding")
    commons.add_chunk("c", domain="reference")
    st = LayeredKnowledgeStore(private, commons).stats()
    # Per-domain counts merged across tiers + the tier split + the grand total.
    assert st == {"finding": 2, "reference": 1, "total": 3, "private": 2, "commons": 1}


def test_stats_merges_a_domain_present_in_both_tiers(tmp_path):
    """A domain with rows in BOTH tiers sums; the split keys are always present.

    memory_stats and the snapshot seed's domain discovery treat every non-count
    key as a domain — on the old tier-only shape they saw "private"/"commons"
    as domains and no real ones."""
    private, commons = _stores(tmp_path)
    private.add_chunk("a", domain="reference")
    commons.add_chunk("b", domain="reference")
    commons.add_chunk("c", domain="reference")
    st = LayeredKnowledgeStore(private, commons).stats()
    assert st["reference"] == 3
    assert (st["private"], st["commons"], st["total"]) == (1, 2, 3)
    # Empty store: split keys still present, no phantom domains.
    empty = LayeredKnowledgeStore(*_stores(tmp_path / "empty")).stats()
    assert empty == {"total": 0, "private": 0, "commons": 0}


def test_hot_memory_delegates_to_private(tmp_path):
    """Unlisted methods (get_hot_memory, deletes, …) delegate to private via __getattr__."""
    private, commons = _stores(tmp_path)
    private.add_chunk("always-on operator fact", domain="hot")
    layered = LayeredKnowledgeStore(private, commons)
    assert "always-on operator fact" in layered.get_hot_memory()


# ── embed-model guard (one fleet, one embed model — bd-2wu) ─────────────────────


def _cfg(tmp_path, **over):
    from graph.config import LangGraphConfig

    base = dict(
        knowledge_embeddings=True,
        knowledge_scope="layered",
        embed_model="modelY",
        commons_path=str(tmp_path / "commons"),
        knowledge_db_path=str(tmp_path / "priv.db"),
    )
    base.update(over)
    return LangGraphConfig(**base)


def test_embed_model_mismatch_degrades_commons_to_fts5(tmp_path, monkeypatch):
    """A commons stamped with a DIFFERENT embed model is served FTS5-only (plain store),
    never vector-fused with incompatible embeddings."""
    import graph.llm as gl
    from knowledge.hybrid_store import HybridKnowledgeStore
    from knowledge.layered import LayeredKnowledgeStore
    from knowledge.store import KnowledgeStore
    from server.agent_init import _build_knowledge_store

    monkeypatch.setattr(gl, "create_embed_fn", lambda cfg: (lambda text: [0.1, 0.2, 0.3]))
    monkeypatch.setattr(gl, "create_embed_batch_fn", lambda cfg: None)
    # Pre-stamp the commons with a DIFFERENT model than this agent uses.
    KnowledgeStore(str(tmp_path / "commons" / "knowledge.db"), scoped=False).set_meta("embed_model", "modelX")

    store = _build_knowledge_store(_cfg(tmp_path))
    assert isinstance(store, LayeredKnowledgeStore)
    assert isinstance(store._private, HybridKnowledgeStore)  # this agent's own model → vectors
    # Commons degraded to plain FTS5 (a KnowledgeStore that is NOT a HybridKnowledgeStore).
    assert isinstance(store._commons, KnowledgeStore)
    assert not isinstance(store._commons, HybridKnowledgeStore)


def test_embed_model_match_keeps_commons_hybrid(tmp_path, monkeypatch):
    import graph.llm as gl
    from knowledge.hybrid_store import HybridKnowledgeStore
    from knowledge.store import KnowledgeStore
    from server.agent_init import _build_knowledge_store

    monkeypatch.setattr(gl, "create_embed_fn", lambda cfg: (lambda text: [0.1, 0.2, 0.3]))
    monkeypatch.setattr(gl, "create_embed_batch_fn", lambda cfg: None)
    # First agent claims the commons with its model; a matching agent gets a hybrid commons.
    KnowledgeStore(str(tmp_path / "commons" / "knowledge.db"), scoped=False).set_meta("embed_model", "modelY")

    store = _build_knowledge_store(_cfg(tmp_path))
    assert isinstance(store._commons, HybridKnowledgeStore)


def test_first_build_claims_the_commons_stamp(tmp_path, monkeypatch):
    """An unstamped commons is claimed with this agent's embed model on first build."""
    import graph.llm as gl
    from knowledge.store import KnowledgeStore
    from server.agent_init import _build_knowledge_store

    monkeypatch.setattr(gl, "create_embed_fn", lambda cfg: (lambda text: [0.1, 0.2, 0.3]))
    monkeypatch.setattr(gl, "create_embed_batch_fn", lambda cfg: None)

    _build_knowledge_store(_cfg(tmp_path))
    stamp = KnowledgeStore(str(tmp_path / "commons" / "knowledge.db"), scoped=False).get_meta("embed_model")
    assert stamp == "modelY"


# ── list_chunks keeps the backend's row TYPE (Liskov) ─────────────────────────
#
# Every consumer of list_chunks reads rows by attribute — memory_list, the fact
# consolidator, the snapshot seed. When the layered store returned tier-tagged
# dicts instead, all three broke the moment a commons was configured: the tool
# crashed, dedup degraded to add-only (swallowed by its except), and the seed
# exported nothing (getattr(dict, "content", "") is ""). These pin the contract
# from the consumers' side.


def test_list_chunks_returns_tier_tagged_chunk_rows(tmp_path):
    from knowledge.store import Chunk

    private, commons = _stores(tmp_path)
    private.add_chunk("private fact", domain="general")
    commons.add_chunk("shared fact", domain="general")
    layered = LayeredKnowledgeStore(private, commons)

    rows = layered.list_chunks()
    assert all(isinstance(r, Chunk) for r in rows)
    assert [(r.content, r.tier) for r in rows] == [("private fact", "private"), ("shared fact", "commons")]
    # The tier travels with as_dict() (the console's row shape) and a single-backend
    # store leaves it None — the field is stamped, not stored.
    assert [r.as_dict()["tier"] for r in rows] == ["private", "commons"]
    assert private.list_chunks()[0].tier is None
    assert private.list_chunks()[0].as_dict()["tier"] is None
    # Filters still reach both tiers.
    assert [r.tier for r in layered.list_chunks(domain="general")] == ["private", "commons"]
    assert layered.list_chunks(domain="nope") == []


def test_list_chunks_tags_dict_rows_from_a_custom_backend(tmp_path):
    """A custom KnowledgeBackend may yield dicts; the layered store keeps THAT shape too."""

    class _DictBackend:
        def __init__(self, rows):
            self._rows = rows

        def list_chunks(self, *a, **kw):
            return list(self._rows)

    layered = LayeredKnowledgeStore(
        _DictBackend([{"id": 1, "content": "p", "domain": "general"}]),
        _DictBackend([{"id": 1, "content": "c", "domain": "general"}]),
    )
    assert layered.list_chunks() == [
        {"id": 1, "content": "p", "domain": "general", "tier": "private"},
        {"id": 1, "content": "c", "domain": "general", "tier": "commons"},
    ]


def test_memory_list_tool_renders_on_a_layered_store(tmp_path):
    import asyncio

    from tools.lg_tools import _build_memory_tools

    private, commons = _stores(tmp_path)
    private.add_chunk("the operator prefers terse replies", domain="preferences", heading="style")
    commons.add_chunk("fleet-wide deploy window is Friday", domain="general")
    layered = LayeredKnowledgeStore(private, commons)
    tools = {t.name: t for t in _build_memory_tools(layered)}

    listed = asyncio.run(tools["memory_list"].ainvoke({}))
    assert "[preferences] style:" in listed and "terse replies" in listed
    assert "deploy window is Friday" in listed
    # memory_stats labels the tier split so the model doesn't take "private"/"commons"
    # for domains it could memory_list; domain lines are unchanged.
    stats = asyncio.run(tools["memory_stats"].ainvoke({}))
    assert "Total: 2" in stats
    assert "  tier private: 1" in stats and "  tier commons: 1" in stats
    assert "  preferences: 1" in stats and "  general: 1" in stats
    assert "  private: 1" not in stats.replace("tier private", "")


def test_fact_consolidation_dedups_on_a_layered_store(tmp_path):
    from graph.memory_facts import consolidate_and_store

    private, commons = _stores(tmp_path)
    layered = LayeredKnowledgeStore(private, commons)
    assert consolidate_and_store(layered, ["The operator prefers metric units"], namespace="p") == {
        "added": 1,
        "skipped": 0,
        "superseded": 0,
    }
    # Re-running the same fact must be a skip, not a second private row.
    counts = consolidate_and_store(layered, ["The operator prefers metric units"], namespace="p")
    assert counts == {"added": 0, "skipped": 1, "superseded": 0}
    assert len(private.list_chunks(domain="fact", limit=10)) == 1


def test_snapshot_seed_exports_layered_rows(tmp_path):
    from graph.snapshot_op import collect_knowledge_seed

    private, commons = _stores(tmp_path)
    private.add_chunk("How the deploy pipeline works", domain="reference", heading="deploys")
    commons.add_chunk("Fleet conventions", domain="reference")
    layered = LayeredKnowledgeStore(private, commons)

    seed = collect_knowledge_seed(layered, domains=["reference"])
    # PRIVATE tier only: the commons is host-shared curated knowledge that exists on the
    # destination independently (ADR 0091) — a commons row never rides along.
    assert seed.counts == {"reference": 1}
    assert "## deploys\n\nHow the deploy pipeline works" in seed.docs["reference"]
    assert "Fleet conventions" not in seed.docs["reference"]


def test_snapshot_seed_discovers_domains_on_a_layered_store(tmp_path):
    """With NO explicit ``domains=`` the seed discovers them from ``stats()`` — on the
    old tier-only stats shape it "discovered" private/commons, matched nothing, and
    exported empty (the second cause of the empty seed, after the dict rows).
    Memory domains stay dropped whatever the tiers hold, and a commons-only domain is
    discovered but has no private rows, so it is not exported."""
    from graph.snapshot_op import collect_knowledge_seed

    private, commons = _stores(tmp_path)
    private.add_chunk("How the deploy pipeline works", domain="reference")
    private.add_chunk("operator prefers dark mode", domain="hot")  # memory — never exported
    commons.add_chunk("Fleet conventions", domain="playbook")  # commons — never exported
    layered = LayeredKnowledgeStore(private, commons)

    seed = collect_knowledge_seed(layered)
    assert seed.counts == {"reference": 1}
    assert "How the deploy pipeline works" in seed.docs["reference"]
    assert set(seed.docs) == {"reference"}  # no hot, no playbook, no private/commons phantoms
