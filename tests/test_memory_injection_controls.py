"""ADR 0069 R2a/R2b — namespace-scoped auto-inject, incognito threads, and the
per-turn injection record.

Covers the three delivery-layer controls this round adds:

  1. D3a — ``knowledge.inject_namespaces``: the store's ``search`` filters by
     namespace (FTS5 + LIKE + hybrid vector paths), the middleware passes the
     filter through only when configured, and a legacy backend without the
     kwarg still gets the scope honored via post-filter.
  2. D3b — incognito: ``_persist_session`` skips, ``KnowledgeMiddleware``
     injects no memory (skills still inject), end-to-end through the REAL
     agent graph (state plumbing, not just the hooks).
  3. D6 — the injection log records id-attributed rows per model call, and
     ``GET /api/memory/injections`` returns them newest-first (incl. the
     empty-session-id edge = no filter).
"""

from __future__ import annotations

import json
import os

import pytest
from langchain_core.messages import HumanMessage

from graph.middleware.knowledge import KnowledgeMiddleware
from knowledge.store import KnowledgeStore


def _seed_namespaced(store: KnowledgeStore) -> None:
    store.add_chunk("gravity pulls apples in alpha", namespace="projects/alpha")
    store.add_chunk("gravity pulls apples in beta", namespace="projects/beta")
    store.add_chunk("gravity pulls apples unscoped")  # namespace=None


# ---------------------------------------------------------------------------
# 1) D3a — store-level namespace filtering
# ---------------------------------------------------------------------------


def test_search_namespace_filter_fts(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    assert store._fts_available  # this test exercises the FTS5 path
    _seed_namespaced(store)

    all_hits = store.search("gravity apples")
    assert len(all_hits) == 3  # None = unfiltered (today's behavior)

    alpha = store.search("gravity apples", namespace=["projects/alpha"])
    assert [r["namespace"] for r in alpha] == ["projects/alpha"]

    # "" in the filter matches UN-namespaced rows alongside the named scope.
    with_blank = store.search("gravity apples", namespace=["projects/alpha", ""])
    assert {r["namespace"] for r in with_blank} == {"projects/alpha", None}

    # Empty list = no predicate (same as None), not "match nothing".
    assert len(store.search("gravity apples", namespace=[])) == 3


def test_search_namespace_filter_like_fallback(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    _seed_namespaced(store)
    store._fts_available = False  # force the LIKE fallback path

    alpha = store.search("gravity apples", namespace=["projects/alpha"])
    assert [r["namespace"] for r in alpha] == ["projects/alpha"]
    with_blank = store.search("gravity apples", namespace=["projects/beta", ""])
    assert {r["namespace"] for r in with_blank} == {"projects/beta", None}


def test_hybrid_search_namespace_filters_vector_ranking(tmp_path):
    """A vector-only hit outside the scope must not leak through RRF fusion."""
    from knowledge.hybrid_store import HybridKnowledgeStore

    store = HybridKnowledgeStore(tmp_path / "kb.db", embed_fn=lambda text: [1.0, 0.0])
    _seed_namespaced(store)

    hits = store.search("gravity apples", namespace=["projects/alpha"])
    assert hits and {r["namespace"] for r in hits} == {"projects/alpha"}


# ---------------------------------------------------------------------------
# 1b) D3a — middleware passes the filter through (and only when configured)
# ---------------------------------------------------------------------------


class _CapturingStore:
    """Stub store recording the search kwargs the middleware passes."""

    def __init__(self, results=None, accepts_namespace=True):
        self.calls: list[dict] = []
        self._results = results or []
        self._accepts_namespace = accepts_namespace

    def search(self, query, k=5, **kwargs):
        if not self._accepts_namespace and "namespace" in kwargs:
            raise TypeError("search() got an unexpected keyword argument 'namespace'")
        self.calls.append({"query": query, "k": k, **kwargs})
        return self._results


def _mw(store, **kw):
    mw = KnowledgeMiddleware(knowledge_store=store, **kw)
    # Pin the prior-sessions cache fresh so before_model never reads real disk.
    import time

    mw._prior_sessions_cache = ""
    mw._prior_sessions_loaded_at = time.monotonic()
    return mw


def test_middleware_omits_namespace_when_unconfigured():
    store = _CapturingStore()
    mw = _mw(store)  # inject_namespaces default = unfiltered
    mw.before_model({"messages": [HumanMessage(content="q")]}, runtime=None)
    assert store.calls and "namespace" not in store.calls[0]


def test_middleware_passes_namespace_when_configured():
    store = _CapturingStore()
    mw = _mw(store, inject_namespaces=["projects/alpha", ""])
    mw.before_model({"messages": [HumanMessage(content="q")]}, runtime=None)
    assert store.calls[0]["namespace"] == ["projects/alpha", ""]


def test_middleware_post_filters_for_legacy_backend():
    """A backend whose search() predates the namespace kwarg still gets the
    configured scope enforced — via retry + post-filter on the hit's field."""
    in_scope = {"table": "chunks", "preview": "in", "id": 1, "namespace": "projects/alpha"}
    out_of_scope = {"table": "chunks", "preview": "out", "id": 2, "namespace": "projects/beta"}
    store = _CapturingStore(results=[in_scope, out_of_scope], accepts_namespace=False)
    mw = _mw(store, inject_namespaces=["projects/alpha"])
    result = mw.before_model({"messages": [HumanMessage(content="q")]}, runtime=None)
    assert "in" in result["context"] and "out" not in result["context"]


# ---------------------------------------------------------------------------
# 2) D3b — incognito: no persistence, no injection (skills still inject)
# ---------------------------------------------------------------------------


def test_incognito_skips_session_persistence(tmp_path, monkeypatch):
    import graph.middleware.memory as memmod

    monkeypatch.setenv("MEMORY_PATH", str(tmp_path))
    monkeypatch.setattr(memmod, "_PERSISTENCE_DISABLED", False, raising=False)

    state = {
        "session_id": "sess-incog",
        "incognito": True,
        "messages": [HumanMessage(content="secret question")],
    }
    memmod._persist_session(state, trace_id="t1")
    assert not any(f.endswith(".json") for f in os.listdir(tmp_path))

    # Sanity: the same state WITHOUT the flag persists (the skip is the flag,
    # not some other precondition).
    memmod._persist_session({**state, "incognito": False}, trace_id="t1")
    assert any("sess-incog" in f for f in os.listdir(tmp_path))


class _FakeSkillsIndex:
    def skill_summaries(self, limit=5):
        return [{"name": "triage", "description": "triage things"}]

    def discoverable_count(self):
        return 1


def test_incognito_skips_injection_but_keeps_skills(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("deploys go out Fridays", domain="hot")
    store.add_chunk("gravity pulls apples")
    mw = _mw(store, skills_index=_FakeSkillsIndex())
    mw._prior_sessions_cache = "<prior_sessions>\n  x\n</prior_sessions>"

    normal = mw.before_model({"messages": [HumanMessage(content="gravity apples")]}, runtime=None)
    assert "<injected_memory>" in normal["context"]

    incog = mw.before_model(
        {"messages": [HumanMessage(content="gravity apples")], "incognito": True}, runtime=None
    )
    assert "<injected_memory>" not in incog["context"]
    assert "<prior_sessions>" not in incog["context"]
    assert "hot memory" not in incog["context"]
    assert "<available_skills>" in incog["context"]  # capability, not memory


@pytest.mark.asyncio
async def test_incognito_end_to_end_real_graph(tmp_path, monkeypatch):
    """Drive the REAL agent graph with incognito in the turn input (as the chat
    entry paths stamp it) and assert both middlewares honored it — proving the
    state channel plumbs through LangGraph, not just the hooks."""
    from unittest.mock import patch

    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.memory import MemorySaver

    import graph.agent as agentmod
    import graph.middleware.memory as memmod
    from graph.config import LangGraphConfig

    class _Fake(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    monkeypatch.setenv("MEMORY_PATH", str(memory_dir))
    monkeypatch.setattr(memmod, "_PERSISTENCE_DISABLED", False, raising=False)
    monkeypatch.setattr(agentmod, "SessionSummaryMiddleware", memmod.SessionSummaryMiddleware, raising=False)

    store = KnowledgeStore(str(tmp_path / "kb.db"))
    store.add_chunk("deploys go out Fridays", domain="hot")

    with patch("graph.agent.create_llm", lambda *a, **k: _Fake(messages=iter([AIMessage(content="ok")]))):
        from graph.agent import create_agent_graph

        g = create_agent_graph(
            LangGraphConfig(),
            knowledge_store=store,
            include_subagents=False,
            checkpointer=MemorySaver(),
        )
    result = await g.ainvoke(
        {"messages": [HumanMessage(content="hi")], "session_id": "sess-incog", "incognito": True},
        config={"configurable": {"thread_id": "sess-incog"}},
    )
    assert "<injected_memory>" not in (result.get("context") or "")
    assert not os.listdir(memory_dir), "incognito turn must not persist a session summary"


# ---------------------------------------------------------------------------
# 3) D6 — the injection record + its read route
# ---------------------------------------------------------------------------


def test_injection_log_records_attributed_ids(tmp_path, monkeypatch):
    from observability.injection_log import injection_log

    # Prior-sessions digest from a real summary file so digest ids are exercised.
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "sess-prev.json").write_text(
        json.dumps(
            {
                "session_id": "sess-prev",
                "messages": [{"role": "user", "content": "earlier topic"}],
                "timestamp": "2026-07-01T00:00:00+00:00",
            }
        )
    )
    monkeypatch.setenv("MEMORY_PATH", str(memory_dir))

    store = KnowledgeStore(tmp_path / "kb.db")
    hot_id = store.add_chunk("deploys go out Fridays", domain="hot")
    rag_id = store.add_chunk("gravity pulls apples")

    mw = KnowledgeMiddleware(knowledge_store=store)
    result = mw.before_model(
        {"messages": [HumanMessage(content="gravity apples")], "session_id": "sess-now"}, runtime=None
    )
    assert "<injected_memory>" in result["context"]

    rows = injection_log().recent()
    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == "sess-now"
    assert row["digest_session_ids"] == ["sess-prev"]
    assert row["hot_chunk_ids"] == [hot_id]
    assert row["rag_chunk_ids"] == [rag_id]
    assert row["approx_tokens"] >= 1
    assert row["ts"]


def test_incognito_writes_no_injection_row(tmp_path):
    from observability.injection_log import injection_log

    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("deploys go out Fridays", domain="hot")
    mw = _mw(store)
    mw.before_model(
        {"messages": [HumanMessage(content="q")], "session_id": "s", "incognito": True}, runtime=None
    )
    assert injection_log().recent() == []


def _injections_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from operator_api.injection_routes import register_injection_routes

    app = FastAPI()
    register_injection_routes(app)
    return TestClient(app)


def test_injections_route_newest_first_and_filters():
    from observability.injection_log import injection_log

    log = injection_log()
    log.record(session_id="sess-a", rag_chunk_ids=[1], approx_tokens=10)
    log.record(session_id="sess-b", hot_chunk_ids=[7], approx_tokens=20)
    log.record(session_id="", digest_session_ids=["old"], approx_tokens=5)  # no identity

    c = _injections_client()

    # Empty session_id (omitted or blank) = no filter, newest-first.
    body = c.get("/api/memory/injections").json()
    assert [r["session_id"] for r in body["injections"]] == ["", "sess-b", "sess-a"]
    assert body["injections"][1]["hot_chunk_ids"] == [7]  # JSON columns decoded
    blank = c.get("/api/memory/injections", params={"session_id": "  "}).json()
    assert len(blank["injections"]) == 3

    # Filtered to one session.
    only_a = c.get("/api/memory/injections", params={"session_id": "sess-a"}).json()
    assert [r["session_id"] for r in only_a["injections"]] == ["sess-a"]
    assert only_a["injections"][0]["rag_chunk_ids"] == [1]

    # Limit clamps to at least 1 and applies newest-first.
    limited = c.get("/api/memory/injections", params={"limit": 1}).json()
    assert [r["session_id"] for r in limited["injections"]] == [""]


def test_injections_route_empty_log():
    body = _injections_client().get("/api/memory/injections").json()
    assert body == {"injections": []}
