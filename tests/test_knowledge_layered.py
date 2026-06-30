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
    chunk = layered.list_chunks()  # union, tier-tagged
    assert {"private", "commons"} == {c["tier"] for c in chunk}

    commons_id = next(c["id"] for c in layered.list_chunks() if c["tier"] == "commons")
    assert layered.forget_from_commons(commons_id) is True
    assert commons.stats()["total"] == 0
    assert private.stats()["total"] == 1  # private untouched by forget


def test_promote_unknown_id_returns_none(tmp_path):
    private, commons = _stores(tmp_path)
    assert LayeredKnowledgeStore(private, commons).promote(9999) is None


def test_stats_split_by_tier(tmp_path):
    private, commons = _stores(tmp_path)
    private.add_chunk("a", domain="finding")
    private.add_chunk("b", domain="finding")
    commons.add_chunk("c", domain="reference")
    st = LayeredKnowledgeStore(private, commons).stats()
    assert st == {"private": 2, "commons": 1, "total": 3}


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
