"""ADR 0125 H1a: core history storage and runtime lifecycle."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import fields
import sqlite3

import pytest

from knowledge.history import HistoryStore
from knowledge.store import KnowledgeStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_DB_PATH", raising=False)
    return HistoryStore(tmp_path / "agent.db")


def test_schema_and_reopen(store):
    with closing(sqlite3.connect(store.path)) as db:
        assert [r[1] for r in db.execute("PRAGMA table_info(history)")] == [
            "id", "session_id", "thread_id", "surface", "kind", "heading", "content",
            "span_start", "span_end", "created_at", "namespace", "invalidated_at", "invalidation_reason",
        ]
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables == {"history", "sqlite_sequence", "history_fts", "history_fts_data",
                          "history_fts_idx", "history_fts_docsize", "history_fts_config"}
        assert "fts5" in db.execute("SELECT sql FROM sqlite_master WHERE name='history_fts'").fetchone()[0]
    id = store.add("persisted", session_id="s", kind="archive")
    assert HistoryStore(store.path).get(id).content == "persisted"
    assert len(HistoryStore(store.path).search("persisted")) == 1


@pytest.mark.parametrize("kind", ["archive", "summary", "report"])
def test_roundtrip(store, kind):
    content = "complete transcript " * 2000
    id = store.add(content, session_id="session", thread_id="thread:goal-iter-1", kind=kind,
                   heading="distinctheading", surface="chat", span_start="start", span_end="end",
                   created_at="2026-10-28T00:00:00+00:00", namespace="chat-archive:session")
    entry = store.get(id)
    assert entry.content == content
    assert entry.kind == kind
    assert entry.thread_id == "thread:goal-iter-1"
    assert entry.surface == "chat"
    assert (entry.span_start, entry.span_end) == ("start", "end")
    assert entry.created_at == "2026-10-28T00:00:00+00:00"
    assert entry.namespace == "chat-archive:session"
    assert store.list_for_session("session") == [entry]
    assert store.list_for_session("session", kinds=[kind]) == [entry]
    assert store.list_for_session("session", kinds=[]) == []
    assert store.list_for_session("other") == []
    assert store.search("distinctheading", 1) == [entry]
    assert store.search("transcript", 1) == [entry]
    assert store.get(id + 1) is None


def test_filters_and_constraints(store):
    for kind in ("archive", "summary", "report"):
        store.add("matching", session_id="s", kind=kind)
    assert {e.kind for e in store.search("matching", 10)} == {"archive", "summary", "report"}
    assert len(store.search("matching", 2)) == 2
    assert [e.kind for e in store.list_for_session("s", ["report"])] == ["report"]
    assert store.search(" ") == store.search("matching", 0) == store.search("matching", -1) == []
    with pytest.raises(sqlite3.IntegrityError):
        store.add("bad kind", session_id="s", kind="invalid")
    assert len(store.list_for_session("s")) == 3


def test_external_transaction_invalidation_restore_and_fts_delete(store):
    id = store.add("searchable", session_id="s", kind="archive")
    kb = KnowledgeStore(store.path)
    with closing(kb._connect()) as db, db:
        db.execute("UPDATE history SET invalidated_at = ?, invalidation_reason = ? WHERE id = ?",
                   ("2026-10-28T00:00:00+00:00", "forget_pending:s", id))
    assert store.search("searchable") == store.list_for_session("s") == []
    assert store.get(id) is None
    with closing(kb._connect()) as db, db:
        db.execute("UPDATE history SET invalidated_at = NULL, invalidation_reason = NULL WHERE id = ?", (id,))
    assert store.search("searchable")[0].id == id
    with closing(kb._connect()) as db, db:
        db.execute("UPDATE history SET content = 'replacement' WHERE id = ?", (id,))
    assert store.search("searchable") == []
    assert store.search("replacement")[0].id == id
    with closing(kb._connect()) as db, db:
        db.execute("DELETE FROM history WHERE id = ?", (id,))
        db.execute("INSERT INTO history_fts(history_fts, rank) VALUES ('integrity-check', 1)")
    assert store.search("replacement") == []


@pytest.mark.platform_sensitive
def test_concurrent_knowledge_and_history(store):
    kb = KnowledgeStore(store.path)
    def write(i):
        if i % 2:
            return HistoryStore(store.path).add(f"history {i}", session_id="s", kind="report")
        return kb.add_chunk(f"knowledge {i}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(write, range(40)))
    assert len(store.list_for_session("s")) == 20
    with closing(store._connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 20
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


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
        leaf.write_text(f"knowledge:\n  db_path: {db_path}\n  backend: '{backend}'\nmiddleware:\n  knowledge: {str(enabled).lower()}\nscheduler:\n  enabled: false\n")
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
        assert isinstance(STATE.history_store, HistoryStore)
        assert STATE.history_store.path == db_path
        if backend:
            assert STATE.knowledge_store is plugin_store
        else:
            assert STATE.knowledge_store.path == db_path
        id = STATE.history_store.add("durable", session_id="s", kind="summary")
        old = STATE.history_store
        ok, message = ai._reload_langgraph_agent()
        assert ok, message
        assert isinstance(STATE.history_store, HistoryStore)
        assert STATE.history_store is not old
        assert STATE.history_store.get(id).content == "durable"
        if backend:
            assert STATE.knowledge_store is plugin_store
        monkeypatch.setattr(ga, "create_agent_graph", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("failed")))
        old = STATE.history_store
        assert not ai._reload_langgraph_agent()[0]
        assert STATE.history_store is old
        monkeypatch.setattr(ga, "create_agent_graph", lambda *a, **kw: object())
        config(enabled=False)
        assert ai._reload_langgraph_agent()[0]
        assert STATE.history_store is None
        ai._init_langgraph_agent()
        assert STATE.history_store is None
    finally:
        for name, value in saved.items():
            setattr(STATE, name, value)
