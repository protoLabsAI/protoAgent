"""ADR 0125 P2-A1: memory persistence, eligibility, retrieval and lifecycle."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import fields
import sqlite3

import pytest

from knowledge.memories import MemoryStore
from knowledge.history import HistoryStore
from knowledge.store import KnowledgeStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_DB_PATH", raising=False)
    return MemoryStore(tmp_path / "agent.db")


def add(store, content="searchable", **kw):
    return store.add(content, **{"kind": "fact", "provenance": "operator", **kw})


def test_schema_idempotency_and_shared_database(store):
    kb = KnowledgeStore(store.path)
    history = HistoryStore(store.path)
    chunk_id = kb.add_chunk("document")
    history_id = history.add("archive", session_id="s", kind="archive")
    id = add(store)
    with closing(store._connect()) as db:
        columns = {r[1] for r in db.execute("PRAGMA table_info(memories)")}
        assert columns == {
            "id",
            "kind",
            "subject",
            "heading",
            "content",
            "delivery",
            "review_state",
            "provenance",
            "scope",
            "legacy_domain",
            "valid_from",
            "superseded_by",
            "expires_at",
            "source_session",
            "thread_id",
            "invalidated_at",
            "invalidation_reason",
            "created_at",
            "updated_at",
        }
        assert "domain" not in columns
        before = db.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
        MemoryStore(store.path)
        assert before == db.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
        assert db.execute("SELECT COUNT(*) FROM chunks WHERE id = ?", (chunk_id,)).fetchone()[0] == 1
    assert history.get(history_id).content == "archive"
    assert MemoryStore(store.path).get(id) == store.get(id)


@pytest.mark.parametrize("kind", ["profile", "standing", "fact", "decision"])
def test_roundtrip(store, kind):
    id = add(
        store,
        kind=kind,
        heading="Heading",
        subject="operator",
        delivery="always",
        scope="private",
        source_session="session",
        thread_id="thread",
        legacy_domain="conversation",
    )
    entry = store.get(id)
    assert (entry.heading, entry.subject, entry.scope) == ("Heading", "operator", "private")
    assert (entry.source_session, entry.thread_id, entry.legacy_domain) == ("session", "thread", "conversation")
    assert entry.updated_at == entry.created_at
    assert store.search("Heading") == store.list(kind=kind, delivery="always", review_state="confirmed") == [entry]
    assert store.always_on(100) == "[Heading] searchable"
    assert store.always_on(2) == ""
    assert store.count() == 1
    assert store.stats()[kind] == 1
    assert store.stats()["total"] == 1
    assert store.delete(id)
    assert not store.delete(id)
    assert store.get(id) is None
    assert store.search("searchable") == store.list() == []
    assert store.count() == 0


@pytest.mark.parametrize("field", ["kind", "delivery", "review_state", "provenance"])
def test_constraints(store, field):
    with pytest.raises(sqlite3.IntegrityError):
        add(store, **{field: "invalid"})
    assert store.count() == 0


@pytest.mark.parametrize(
    "provenance,expected", [("operator", "confirmed"), ("agent", "pending"), ("extracted", "pending")]
)
def test_review_defaults_and_pending_delivery(store, provenance, expected):
    id = add(store, provenance=provenance, delivery="always")
    assert store.get(id).review_state == expected
    assert store.search("searchable", deliverable=True) == [store.get(id)]
    assert store.always_on(100) == "searchable"
    assert store.get(add(store, provenance=provenance, review_state="confirmed")).review_state == "confirmed"


@pytest.mark.parametrize("hybrid", [False, True])
def test_eligibility_applies_to_both_rankings(store, hybrid):
    if hybrid:
        store = MemoryStore(store.path, embed_fn=lambda _: [1.0, 0.0])
    live = add(store, delivery="always")
    superseded = add(store, delivery="always")
    invalidated = add(store, delivery="always")
    add(store, delivery="always", expires_at="2000-01-01")
    add(store, delivery="always", review_state="rejected")
    with store._write() as db:
        db.execute("UPDATE memories SET superseded_by = ? WHERE id = ?", (live, superseded))
        db.execute("UPDATE memories SET invalidated_at = '2026-01-01' WHERE id = ?", (invalidated,))
    for query in ["searchable", "unrelated"] if hybrid else ["searchable"]:
        assert [r.id for r in store.search(query, 20)] == [live]
        assert {r.id for r in store.search(query, 20, include_superseded=True)} == {live, superseded}
        assert [r.id for r in store.search(query, 20, include_superseded=True, deliverable=True)] == [live]
    assert store.always_on(100) == "searchable"
    assert [r.id for r in store.list()] == [live]


def test_hybrid_semantic_only_filters_and_delete(store):
    store = MemoryStore(store.path, embed_fn=lambda text: [0.0, 1.0] if text == "other" else [1.0, 0.0])
    target = add(store, "bicycle", kind="profile", delivery="on_demand")
    add(store, "other", kind="standing", review_state="pending")
    assert store.search("cycling", 1)[0].id == target
    assert store.search("cycling", kind="profile", delivery="on_demand", review_state="confirmed")[0].id == target
    assert store.search("cycling", kind="decision") == []
    assert store.delete(target)
    with closing(store._connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM memory_vectors WHERE memory_id = ?", (target,)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM memories_fts WHERE memories_fts MATCH 'bicycle'").fetchone()[0] == 0
        db.execute("INSERT INTO memories_fts(memories_fts, rank) VALUES ('integrity-check', 1)")
    assert target not in [r.id for r in store.search("cycling")]


def test_fts_updates_and_embedding_failure(store):
    def broken(_):
        raise RuntimeError("offline")

    store = MemoryStore(store.path, embed_fn=broken)
    id = add(store)
    assert store.search("searchable")[0].id == id
    with store._write() as db:
        db.execute("UPDATE memories SET heading = 'replacement', content = 'updated' WHERE id = ?", (id,))
    assert store.search("searchable") == []
    assert store.search("replacement")[0].id == id
    assert store.search(" ") == store.search("updated", 0) == store.search("updated", -1) == []
    assert store.list(limit=0) == store.list(limit=-1) == []


def test_always_on_strict_budget_and_delivery(store):
    add(store, "old", delivery="always")
    add(store, "new", delivery="always")
    add(store, "hidden", delivery="on_demand", legacy_domain="hot")
    assert store.always_on(7) == "new\nold"
    assert store.always_on(6) == "new"
    assert store.always_on(0) == store.always_on(-1) == ""


@pytest.mark.platform_sensitive
def test_concurrent_writers(store):
    kb = KnowledgeStore(store.path)

    def write(i):
        return add(store, str(i)) if i % 2 else kb.add_chunk(str(i))

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(write, range(40)))
    assert store.count() == 20


@pytest.mark.parametrize("backend", ["", "custom"])
def test_boot_reload_plugin_and_disabled(tmp_path, monkeypatch, backend):
    import graph.agent as ga
    import graph.config_io as cio
    from graph.plugins.loader import PluginLoadResult
    from runtime.state import STATE, AppState
    import server.agent_init as ai

    saved = {f.name: getattr(STATE, f.name) for f in fields(AppState)}
    monkeypatch.delenv("KNOWLEDGE_DB_PATH", raising=False)
    leaf = tmp_path / "config.yaml"
    db_path = tmp_path / "agent.db"

    def config(enabled=True):
        leaf.write_text(
            f"knowledge:\n  db_path: {db_path}\n  backend: '{backend}'\nmiddleware:\n  knowledge: {str(enabled).lower()}\nscheduler:\n  enabled: false\n"
        )

    config()
    monkeypatch.setattr(cio, "config_yaml_path", lambda: leaf)
    monkeypatch.setattr(cio, "is_setup_complete", lambda: True)
    for name in ("ensure_live_config", "ensure_live_soul", "apply_seed_merge"):
        monkeypatch.setattr(cio, name, lambda: None)
    plugin_store = object()
    plugins = PluginLoadResult()
    plugins.knowledge_stores["custom"] = lambda cfg: plugin_store
    monkeypatch.setattr(ai, "_build_plugins", lambda *a, **kw: plugins)
    monkeypatch.setattr(ai, "_build_mcp", lambda *a, **kw: ([], [], []))
    monkeypatch.setattr(ai, "_build_checkpointer", lambda cfg: None)
    monkeypatch.setattr(ai, "_build_background_manager", lambda cfg: None)
    monkeypatch.setattr(ai, "_build_skills_index", lambda *a, **kw: None)
    monkeypatch.setattr(ga, "create_agent_graph", lambda *a, **kw: object())
    try:
        ai._init_langgraph_agent()
        assert isinstance(STATE.memory_store, MemoryStore)
        assert STATE.memory_store.path == db_path
        if backend:
            assert STATE.knowledge_store is plugin_store
        else:
            assert STATE.knowledge_store.path == db_path
        id = STATE.memory_store.add("durable", provenance="operator", kind="fact")
        old = STATE.memory_store
        ok, message = ai._reload_langgraph_agent()
        assert ok, message
        assert isinstance(STATE.memory_store, MemoryStore)
        assert STATE.memory_store is not old
        assert STATE.memory_store.get(id).content == "durable"
        if backend:
            assert STATE.knowledge_store is plugin_store
        monkeypatch.setattr(ga, "create_agent_graph", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("failed")))
        old = STATE.memory_store
        assert not ai._reload_langgraph_agent()[0]
        assert STATE.memory_store is old
        monkeypatch.setattr(ga, "create_agent_graph", lambda *a, **kw: object())
        config(enabled=False)
        assert ai._reload_langgraph_agent()[0]
        assert STATE.memory_store is None
        ai._init_langgraph_agent()
        assert STATE.memory_store is None
    finally:
        for name, value in saved.items():
            setattr(STATE, name, value)


def test_builder_embeds_and_falls_back(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import graph.llm as llm
    from server.agent_init import _build_memory_store

    monkeypatch.delenv("KNOWLEDGE_DB_PATH", raising=False)
    config = SimpleNamespace(knowledge_db_path=tmp_path / "agent.db", knowledge_embeddings=True)
    monkeypatch.setattr(llm, "create_embed_fn", lambda _: lambda text: [1.0, 0.0])
    store = _build_memory_store(config)
    id = add(store, "bicycle")
    assert store.search("cycling")[0].id == id

    def broken(_):
        raise RuntimeError("no embed provider")

    monkeypatch.setattr(llm, "create_embed_fn", broken)
    fallback = _build_memory_store(config)
    assert fallback.search("bicycle")[0].id == id
    assert fallback.search("cycling") == []


def test_expiry_is_normalized_to_utc(store):
    id = add(store, expires_at="2100-01-01T03:00:00+03:00")
    assert store.get(id).expires_at == "2100-01-01T00:00:00+00:00"
    assert store.search("searchable", deliverable=True)[0].id == id
