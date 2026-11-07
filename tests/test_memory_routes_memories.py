"""Memory-inspector routes read the core MemoryStore (ADR 0125 P2-D4).

After the boot migration moved the always-on hot rows out of the knowledge chunk
store into MemoryStore (P2-D1), these routes serve them: GET /api/memory/hot lists
the ``delivery='always'`` rows (tagged ``store: "memories"`` with the MemoryStore
always-on ``injecting`` flag), PUT edits one in place, DELETE hard-deletes an always
row only, POST /api/memory/memories/{id}/review lands an operator verdict, and
/api/memory/stats splits the counts three ways. With ``STATE.memory_store`` None every
route degrades to ``enabled: false``, exactly as before the switch-over.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from operator_api.memory_routes import register_memory_routes

pytestmark = pytest.mark.platform_sensitive


def _store(tmp_path, name="mem.db"):
    from knowledge.memories import MemoryStore

    return MemoryStore(db_path=str(tmp_path / name))


def _client(monkeypatch, tmp_path, *, memory=None, knowledge=None, history=None):
    import runtime.state as rs

    monkeypatch.setenv("MEMORY_PATH", str(tmp_path))  # keep every route off the real box
    monkeypatch.setattr(rs.STATE, "memory_store", memory, raising=False)
    monkeypatch.setattr(rs.STATE, "knowledge_store", knowledge, raising=False)
    monkeypatch.setattr(rs.STATE, "history_store", history, raising=False)
    app = FastAPI()
    register_memory_routes(app)
    return TestClient(app)


# ── GET /api/memory/hot (r1) ──────────────────────────────────────────────────


def test_hot_lists_always_rows_with_injecting_window(tmp_path, monkeypatch):
    # r1: a migrated hot row (delivery='always') lists with injecting=true when it is
    # inside the capped always-on window, and a row pushed past the 6000-char cap lists
    # with injecting=false — the flag read off the SAME window the projection renders.
    store = _store(tmp_path)
    out = store.add("older always fact", kind="fact", provenance="operator", delivery="always")
    inw = store.add("x" * 5999, kind="fact", provenance="operator", delivery="always")  # newest, fills the cap
    c = _client(monkeypatch, tmp_path, memory=store)

    body = c.get("/api/memory/hot").json()
    assert body["enabled"] is True
    rows = {r["id"]: r for r in body["chunks"]}
    assert set(rows) == {f"memory:{inw}", f"memory:{out}"}
    assert all(r["store"] == "memories" for r in rows.values())  # tagged to its backend
    assert rows[f"memory:{inw}"]["injecting"] is True  # inside the always-on cap
    assert rows[f"memory:{out}"]["injecting"] is False  # pushed past the 6000-char window
    # the console-facing shape the migrated row carries
    row = rows[f"memory:{inw}"]
    assert (row["memory_kind"], row["review_state"]) == ("fact", "confirmed")
    assert row["content"] == "x" * 5999


def test_hot_keeps_inactive_rows_visible_for_review(tmp_path, monkeypatch):
    store = _store(tmp_path)
    active = store.add("current pin", kind="fact", provenance="operator", delivery="always")
    rejected = store.add("reject this pin", kind="fact", provenance="operator", delivery="always")
    expired = store.add(
        "expired pin", kind="fact", provenance="operator", delivery="always", expires_at="2000-01-01T00:00:00Z"
    )
    superseded = store.add("old pin", kind="fact", provenance="operator", delivery="always")
    invalidated = store.add("forgotten pin", kind="fact", provenance="operator", delivery="always")
    with store._write() as db:
        db.execute("UPDATE memories SET superseded_by = ? WHERE id = ?", (active, superseded))
        db.execute("UPDATE memories SET invalidated_at = '2000-01-01' WHERE id = ?", (invalidated,))
    # Newer non-hot rows must not consume the inspector's limit.
    store.add("retrieved fact", kind="fact", provenance="operator", delivery="retrieved")
    store.add("on demand fact", kind="fact", provenance="operator", delivery="on_demand")
    monkeypatch.setattr("operator_api.memory_routes._HOT_LIST_LIMIT", 4)
    c = _client(monkeypatch, tmp_path, memory=store)

    assert c.post(f"/api/memory/memories/{rejected}/review", json={"state": "rejected"}).status_code == 200
    rows = {r["id"]: r for r in c.get("/api/memory/hot").json()["chunks"]}
    assert set(rows) == {f"memory:{mid}" for mid in (active, rejected, expired, superseded)}
    assert rows[f"memory:{active}"]["injecting"] is True
    for mid in (rejected, expired, superseded):
        assert rows[f"memory:{mid}"]["injecting"] is False

    # The visible rejected handle still supports editing and explicit re-opening.
    handle = rows[f"memory:{rejected}"]["id"]
    assert c.put(f"/api/memory/hot/{handle}", json={"content": "corrected pin"}).status_code == 200
    row = next(r for r in c.get("/api/memory/hot").json()["chunks"] if r["id"] == handle)
    assert (row["content"], row["review_state"], row["injecting"]) == ("corrected pin", "rejected", False)
    assert c.post(f"/api/memory/memories/{rejected}/review", json={"state": "confirmed"}).status_code == 200
    row = next(r for r in c.get("/api/memory/hot").json()["chunks"] if r["id"] == handle)
    assert row["review_state"] == "confirmed" and row["injecting"] is True


def test_hot_disabled_when_memory_store_is_none(tmp_path, monkeypatch):
    # r5 (hot half): no memory store and no chunk store → the enabled:false shape.
    c = _client(monkeypatch, tmp_path)
    assert c.get("/api/memory/hot").json() == {"enabled": False, "chunks": []}


def test_hot_lists_memories_alongside_backend_chunks(tmp_path, monkeypatch):
    # Additive: a core memory and a backend hot chunk both appear in one listing (the
    # knowledge store can still carry its own pins), each tagged to its own store.
    from knowledge.store import KnowledgeStore

    store = _store(tmp_path)
    store.add("a core memory", kind="fact", provenance="operator", delivery="always")
    ks = KnowledgeStore(str(tmp_path / "kb.db"))
    ks.add_chunk("a backend hot chunk", domain="hot", source="console")
    c = _client(monkeypatch, tmp_path, memory=store, knowledge=ks)

    rows = c.get("/api/memory/hot").json()["chunks"]
    assert {r["content"] for r in rows} >= {"a core memory", "a backend hot chunk"}
    assert "memories" in {r.get("store") for r in rows}  # the core memory
    assert any(r.get("store") != "memories" for r in rows)  # the knowledge chunk too


# ── PUT /api/memory/hot/{id} (r2) ─────────────────────────────────────────────


def test_put_edits_in_place_and_confirms(tmp_path, monkeypatch):
    # r2: the same id comes back, content + FTS update in place (no add-then-delete),
    # and an operator edit confirms a pending row.
    store = _store(tmp_path)
    mid = store.add("original pending", kind="fact", provenance="agent", delivery="always")
    assert store.get(mid).review_state == "pending"
    c = _client(monkeypatch, tmp_path, memory=store)

    body = c.put(f"/api/memory/hot/memory:{mid}", json={"content": "revised zebra content"}).json()
    assert body == {"enabled": True, "id": f"memory:{mid}", "replaced": True}

    entry = store.get(mid)
    assert entry.content == "revised zebra content"
    assert entry.review_state == "confirmed"  # an edit is an operator write
    assert entry.delivery == "always"  # delivery is preserved
    assert store.count() == 1  # edited in place — no second row left behind
    # FTS now matches the new content and not the old
    assert [e.id for e in store.search("zebra")] == [mid]
    assert store.search("original") == []


def test_put_keeps_a_rejected_row_rejected(tmp_path, monkeypatch):
    # r2: a rejected pin stays rejected — an edit is not a review.
    store = _store(tmp_path)
    mid = store.add("bad memory", kind="fact", provenance="agent", delivery="always", review_state="rejected")
    c = _client(monkeypatch, tmp_path, memory=store)

    assert c.put(f"/api/memory/hot/memory:{mid}", json={"content": "still bad"}).status_code == 200
    entry = store.get(mid)
    assert entry.content == "still bad"
    assert entry.review_state == "rejected"


def test_put_unknown_and_empty_content_and_no_store(tmp_path, monkeypatch):
    store = _store(tmp_path)
    mid = store.add("a memory", kind="fact", provenance="operator", delivery="always")
    c = _client(monkeypatch, tmp_path, memory=store)
    assert c.put("/api/memory/hot/memory:999999", json={"content": "x"}).status_code == 404
    assert c.put(f"/api/memory/hot/memory:{mid}", json={"content": "   "}).status_code == 400
    # r5 (put half): no store → the enabled:false shape, nothing touched.
    c2 = _client(monkeypatch, tmp_path)
    assert c2.put("/api/memory/hot/memory:1", json={"content": "x"}).json() == {
        "enabled": False,
        "id": None,
        "replaced": False,
    }


# ── DELETE /api/memory/hot/{id} (r3) ──────────────────────────────────────────


def test_delete_refuses_retrieved_and_removes_always(tmp_path, monkeypatch):
    # r3: a retrieved row is not reachable from the Hot panel (404, survives); an always
    # row is hard-deleted; a second delete of the gone row is 404.
    store = _store(tmp_path)
    retrieved = store.add("a retrieved memory", kind="fact", provenance="operator", delivery="retrieved")
    always = store.add("an always memory", kind="fact", provenance="operator", delivery="always")
    c = _client(monkeypatch, tmp_path, memory=store)

    assert c.delete(f"/api/memory/hot/memory:{retrieved}").status_code == 404
    assert store.get(retrieved) is not None  # the retrieved row is untouched

    assert c.delete(f"/api/memory/hot/memory:{always}").json() == {"enabled": True, "deleted": True}
    assert store.get(always) is None
    assert c.delete(f"/api/memory/hot/memory:{always}").status_code == 404


def test_delete_degrades_without_store(tmp_path, monkeypatch):
    c = _client(monkeypatch, tmp_path)
    assert c.delete("/api/memory/hot/memory:1").json() == {"enabled": False, "deleted": False}


# ── POST /api/memory/memories/{id}/review (r4) ────────────────────────────────


def test_memories_review_sets_state_and_validates(tmp_path, monkeypatch):
    # r4: the verdict lands on the row; a bad state is 400 and an unknown id is 404;
    # rejecting keeps the row (never deletes).
    store = _store(tmp_path)
    mid = store.add("agent guess", kind="fact", provenance="agent", delivery="always")  # starts pending
    c = _client(monkeypatch, tmp_path, memory=store)

    r = c.post(f"/api/memory/memories/{mid}/review", json={"state": "confirmed"})
    assert r.status_code == 200
    assert r.json() == {"enabled": True, "id": mid, "review_state": "confirmed"}
    assert store.get(mid).review_state == "confirmed"

    # normalized, and rejecting never deletes
    rr = c.post(f"/api/memory/memories/{mid}/review", json={"state": " Rejected "})
    assert rr.status_code == 200 and rr.json()["review_state"] == "rejected"
    assert store.get(mid).content == "agent guess"

    assert c.post(f"/api/memory/memories/{mid}/review", json={"state": "maybe"}).status_code == 400
    assert c.post(f"/api/memory/memories/{mid}/review", json={}).status_code == 400
    assert c.post("/api/memory/memories/999999/review", json={"state": "confirmed"}).status_code == 404


def test_memories_review_degrades_without_store(tmp_path, monkeypatch):
    # r5 (review half): memory store off → the same enabled:false shape its chunk sibling uses.
    c = _client(monkeypatch, tmp_path)
    assert c.post("/api/memory/memories/1/review", json={"state": "confirmed"}).json() == {
        "enabled": False,
        "id": None,
        "review_state": None,
    }


# ── GET /api/memory/stats (r5) ────────────────────────────────────────────────


def test_stats_split_three_ways(tmp_path, monkeypatch):
    # r5: memory / knowledge / history are three separate blocks with their own counts.
    from knowledge.history import HistoryStore
    from knowledge.store import KnowledgeStore

    store = _store(tmp_path)
    store.add("a profile", kind="profile", provenance="operator", delivery="always")
    store.add("a standing fact", kind="standing", provenance="operator", delivery="always")
    ks = KnowledgeStore(str(tmp_path / "kb.db"))
    ks.add_chunk("a knowledge chunk", domain="general")
    hs = HistoryStore(db_path=str(tmp_path / "hist.db"))
    hs.add("an archive", kind="archive", session_id="chat-1")
    c = _client(monkeypatch, tmp_path, memory=store, knowledge=ks, history=hs)

    body = c.get("/api/memory/stats").json()
    assert set(body) == {"memory", "knowledge", "history"}
    assert body["memory"]["total"] == 2
    assert (body["memory"]["profile"], body["memory"]["standing"]) == (1, 1)
    assert body["knowledge"]["total"] == 1
    assert body["history"]["total"] == 1


def test_stats_each_store_off_is_an_empty_block(tmp_path, monkeypatch):
    c = _client(monkeypatch, tmp_path)
    assert c.get("/api/memory/stats").json() == {"memory": {}, "knowledge": {}, "history": {}}
