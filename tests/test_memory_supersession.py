"""ADR 0125 P3-A: a save on the same subject+kind supersedes the live row, returns what
it replaced and the live rows on that subject, and the inspector can revert one.

Deterministic — there is no model at write time (ADR 0069's finding). Supersession is a
subject/kind/scope match done inside the write transaction; the superseded row stays for
audit and never delivers.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from knowledge.memories import MemoryStore
from knowledge.store import KnowledgeStore
from operator_api.memory_routes import register_memory_routes
from tools.lg_tools import _build_memory_tools


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_DB_PATH", raising=False)
    return MemoryStore(tmp_path / "agent.db")


def add(store, content="a fact", **kw):
    return store.add(content, **{"kind": "fact", "provenance": "operator", **kw})


# ── r1 — a second save on the same subject+kind supersedes the first ─────────


def test_second_save_supersedes_first_on_same_subject(store):
    old = add(store, "comp floor $110K", subject="comp-floor")
    new = add(store, "comp floor $120K", subject="comp-floor")
    assert old != new
    # exactly one live row, the newest
    assert [e.id for e in store.list()] == [new]
    # the older row carries superseded_by = the new id and is still readable
    assert store.get(old).superseded_by == new
    assert store.get(new).superseded_by is None
    assert store.get(old).content == "comp floor $110K"  # get still returns it
    # and it reappears under include_superseded=True
    assert {e.id for e in store.list(include_superseded=True)} == {old, new}


def test_add_with_supersession_returns_the_replaced_rows(store):
    old = add(store, "floor v1", subject="comp-floor")
    new_id, replaced = store.add_with_supersession(
        "floor v2", kind="fact", provenance="operator", subject="comp-floor"
    )
    assert [r.id for r in replaced] == [old]
    assert replaced[0].content == "floor v1"
    assert store.get(old).superseded_by == new_id
    # a first save on a brand-new subject replaces nothing
    _, nothing = store.add_with_supersession("fresh", kind="fact", provenance="operator", subject="rent")
    assert nothing == []


# ── r2 — kind / scope / null-subject boundaries and the normalisation fold ───


def test_different_kind_or_scope_or_null_subject_does_not_supersede(store):
    base = add(store, "floor fact", subject="comp-floor", kind="fact")
    other_kind = add(store, "floor decision", subject="comp-floor", kind="decision")
    assert store.get(base).superseded_by is None  # a different kind does not supersede
    scoped = add(store, "scoped floor", subject="comp-floor", kind="fact", scope="proj-x")
    assert store.get(base).superseded_by is None  # a different scope does not supersede
    assert store.get(scoped).superseded_by is None
    # a null subject never supersedes and is never superseded
    n1 = add(store, "anon one", subject=None)
    n2 = add(store, "anon two", subject=None)
    assert store.get(n1).superseded_by is None and store.get(n2).superseded_by is None
    assert {e.id for e in store.list()} == {n2, n1, scoped, other_kind, base}  # all five live


def test_subject_match_is_case_and_whitespace_folded(store):
    old = add(store, "floor v1", subject="Comp Floor ")
    new = add(store, "floor v2", subject="comp floor")
    assert store.get(old).superseded_by == new  # 'Comp Floor ' folds onto 'comp floor'


def test_a_rejected_live_row_is_not_superseded(store):
    # review_state='rejected' is excluded from the supersession sweep (it is not a live row).
    rejected = add(store, "bad floor", subject="comp-floor", review_state="rejected")
    new = add(store, "good floor", subject="comp-floor")
    assert store.get(rejected).superseded_by is None
    assert store.get(new).superseded_by is None


# ── r3 — the memory_save tool names the replaced row + the other live rows ───


def _save_tool(tmp_path):
    path = str(tmp_path / "agent.db")
    knowledge = KnowledgeStore(db_path=path)
    memory = MemoryStore(db_path=path)
    cfg = SimpleNamespace(knowledge_hot_write_confirm=False)
    tools = {t.name: t for t in _build_memory_tools(knowledge, cfg, memory_store=memory)}
    return tools["memory_save"], memory


def test_memory_save_reports_replaced_and_other_live_rows(tmp_path):
    save, memory = _save_tool(tmp_path)
    # a decision on the same subject a fact save must NOT supersede, but SHOULD surface
    decision = memory.add("hold the floor for Q3", kind="decision", provenance="operator", subject="comp-floor")
    old = memory.add("comp floor $110K", kind="fact", provenance="agent", subject="comp-floor")

    out = asyncio.run(save.ainvoke({"content": "comp floor $120K", "kind": "fact", "subject": "comp-floor"}))

    new = memory.list(kind="fact")[0].id  # the only live fact after supersession
    assert out.startswith(f"Saved memory #{new}")
    # names the replaced id + a short excerpt of its old content
    assert f"Replaced #{old}" in out and "comp floor $110K" in out
    # lists the other live row on the subject (the decision) with its kind
    assert "Also live on 'comp-floor'" in out and f"#{decision} (decision)" in out
    # and the supersession really happened
    assert memory.get(old).superseded_by == new


def test_memory_save_without_subject_reports_neither(tmp_path):
    save, memory = _save_tool(tmp_path)
    out = asyncio.run(save.ainvoke({"content": "a loose fact", "kind": "fact"}))
    assert out.startswith("Saved memory #") and "Replaced" not in out and "Also live" not in out


# ── r4 — revert flips live/superseded; the route 404s a non-superseded row ───


def test_revert_supersession_flips_live_and_superseded(store):
    old = add(store, "v1", subject="x")
    new = add(store, "v2", subject="x")
    assert store.get(old).superseded_by == new

    assert store.revert_supersession(old) is True
    assert store.get(old).superseded_by is None  # old is live again
    assert store.get(new).superseded_by == old  # new is now the superseded one
    assert [e.id for e in store.list()] == [old]  # exactly one live row

    # a live, never-superseded row has nothing to revert; an unknown id likewise
    solo = add(store, "lonely", subject="y")
    assert store.revert_supersession(solo) is False
    assert store.revert_supersession(999_999) is False


def _route_client(tmp_path, monkeypatch, store):
    import runtime.state as rs

    monkeypatch.setenv("MEMORY_PATH", str(tmp_path))  # keep the route off the real box
    monkeypatch.setattr(rs.STATE, "memory_store", store, raising=False)
    monkeypatch.setattr(rs.STATE, "knowledge_store", None, raising=False)
    monkeypatch.setattr(rs.STATE, "history_store", None, raising=False)
    app = FastAPI()
    register_memory_routes(app)
    return TestClient(app)


@pytest.mark.platform_sensitive
def test_revert_route_reverts_and_404s_when_not_superseded(tmp_path, monkeypatch):
    store = MemoryStore(db_path=str(tmp_path / "mem.db"))
    old = store.add("v1", kind="fact", provenance="operator", subject="comp-floor", delivery="always")
    new = store.add("v2", kind="fact", provenance="operator", subject="comp-floor", delivery="always")
    assert store.get(old).superseded_by == new
    c = _route_client(tmp_path, monkeypatch, store)

    r = c.post(f"/api/memory/memories/{old}/revert")
    assert r.status_code == 200
    assert r.json() == {"enabled": True, "id": old, "reverted": True}
    assert store.get(old).superseded_by is None
    assert store.get(new).superseded_by == old

    # the now-live old row is no longer superseded → 404; an unknown id → 404
    assert c.post(f"/api/memory/memories/{old}/revert").status_code == 404
    assert c.post("/api/memory/memories/999999/revert").status_code == 404


@pytest.mark.platform_sensitive
def test_revert_route_degrades_without_store(tmp_path, monkeypatch):
    c = _route_client(tmp_path, monkeypatch, None)
    assert c.post("/api/memory/memories/1/revert").json() == {"enabled": False, "id": None, "reverted": False}


# ── r5 — a superseded row never delivers (always_on or deliverable search) ───


def test_superseded_row_never_delivers(store):
    old = add(store, "searchable old floor", subject="comp-floor", delivery="always")
    new = add(store, "searchable new floor", subject="comp-floor", delivery="always")
    assert store.get(old).superseded_by == new
    # the always-on block drops the superseded row
    always = store.always_on(1000)
    assert "new floor" in always and "old floor" not in always
    # deliverable search never returns it, even asked to include superseded rows
    assert old not in [e.id for e in store.search("searchable", 20, deliverable=True)]
    assert new in [e.id for e in store.search("searchable", 20, deliverable=True)]
    assert old not in [e.id for e in store.search("searchable", 20, include_superseded=True, deliverable=True)]
