"""Memory-inspector routes (ADR 0069 D7) — /api/memory/* exposes the session
summaries behind the <prior_sessions> digest (list/get/delete, traversal-safe
ids) and the always-injected hot-memory chunks (list/edit/delete, pinned to
domain="hot")."""

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from operator_api.memory_routes import register_memory_routes


def _client(monkeypatch, tmp_path, *, knowledge=None):
    import runtime.state as rs

    monkeypatch.setenv("MEMORY_PATH", str(tmp_path))  # memory_path() resolves per call
    monkeypatch.setattr(rs.STATE, "knowledge_store", knowledge, raising=False)
    app = FastAPI()
    register_memory_routes(app)
    return TestClient(app)


def _write(d, sid, messages, **extra):
    (d / f"{sid}.json").write_text(
        json.dumps({"session_id": sid, "timestamp": "2026-07-01T00:00:00Z", "messages": messages, **extra})
    )


# ── session summaries ─────────────────────────────────────────────────────────


def test_sessions_list_returns_digest_fields(tmp_path, monkeypatch):
    _write(tmp_path, "chat-abc", [{"role": "user", "content": "plan the launch"}, {"role": "assistant", "content": "ok"}])
    c = _client(monkeypatch, tmp_path)
    rows = c.get("/api/memory/sessions").json()["sessions"]
    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == "chat-abc"
    assert row["surface"] == "chat"
    assert row["topic"] == "plan the launch"  # first USER message only
    assert row["message_count"] == 2
    assert row["timestamp"] == "2026-07-01T00:00:00Z"
    assert row["size_bytes"] > 0
    assert "rendered" not in row  # list stays digest-sized


def test_sessions_list_newest_first_and_skips_non_json(tmp_path, monkeypatch):
    import os

    _write(tmp_path, "s-old", [{"role": "user", "content": "old"}])
    _write(tmp_path, "s-new", [{"role": "user", "content": "new"}])
    (tmp_path / "junk.txt").write_text("not a summary")
    old = tmp_path / "s-old.json"
    os.utime(old, (old.stat().st_atime, old.stat().st_mtime - 100))
    c = _client(monkeypatch, tmp_path)
    rows = c.get("/api/memory/sessions").json()["sessions"]
    assert [r["session_id"] for r in rows] == ["s-new", "s-old"]


def test_sessions_list_empty_dir(tmp_path, monkeypatch):
    c = _client(monkeypatch, tmp_path)
    assert c.get("/api/memory/sessions").json() == {"sessions": []}


def test_session_get_renders_full_summary(tmp_path, monkeypatch):
    _write(tmp_path, "background:job1", [{"role": "user", "content": "check the feeds"}], final_output="all quiet")
    c = _client(monkeypatch, tmp_path)
    body = c.get("/api/memory/sessions/background:job1").json()["session"]
    assert body["session_id"] == "background:job1"
    assert body["surface"] == "background"
    # rendered = format_session_summary — the same expansion recall_session returns
    assert "check the feeds" in body["rendered"]
    assert "all quiet" in body["rendered"]


def test_session_get_unknown_404(tmp_path, monkeypatch):
    c = _client(monkeypatch, tmp_path)
    assert c.get("/api/memory/sessions/nope").status_code == 404


def test_session_id_guard_rejects_unsafe_ids(tmp_path, monkeypatch):
    # The recall_session filename guard: [A-Za-z0-9._:-] only, so a crafted id
    # (encoded separators, spaces) can't escape the memory dir. An encoded "/"
    # never even routes to the handler (404); everything else that does route
    # is rejected by the guard (400). Both never touch the filesystem.
    outside = tmp_path / "secret.json"  # sibling of the memory dir below
    outside.write_text("{}")
    memdir = tmp_path / "memory"
    memdir.mkdir()
    c = _client(monkeypatch, memdir)
    assert c.get("/api/memory/sessions/..%2Fsecret").status_code in (400, 404)
    assert c.delete("/api/memory/sessions/..%2Fsecret").status_code in (400, 404)
    for bad in ("..%5C..%5Cconfig", "a%20b", "~root"):
        assert c.get(f"/api/memory/sessions/{bad}").status_code == 400
        assert c.delete(f"/api/memory/sessions/{bad}").status_code == 400
    assert outside.exists()  # no traversal attempt reached outside the dir


def test_session_id_guard_charset():
    from graph.middleware.memory import is_safe_session_id

    assert is_safe_session_id("chat-1_a.b:c")
    for bad in ("", "../x", "a/b", "a\\b", "a b", "a\x00b"):
        assert not is_safe_session_id(bad)


def test_session_delete(tmp_path, monkeypatch):
    _write(tmp_path, "s1", [{"role": "user", "content": "bye"}])
    c = _client(monkeypatch, tmp_path)
    assert c.delete("/api/memory/sessions/s1").json() == {"deleted": True, "session_id": "s1"}
    assert not (tmp_path / "s1.json").exists()
    assert c.delete("/api/memory/sessions/s1").status_code == 404  # already gone


# ── hot memory ────────────────────────────────────────────────────────────────


class _HotKS:
    """Store double: dict rows (the LayeredKnowledgeStore list_chunks shape),
    tracking add/delete like the knowledge-routes CRUD double."""

    def __init__(self):
        self.rows = [
            {"id": 3, "content": "operator prefers dark mode", "domain": "hot", "created_at": "2026-07-01", "source": "console"}
        ]
        self.added: list[tuple] = []
        self.deleted: list[int] = []
        self.next_id = 9

    def list_chunks(self, domain=None, limit=50, **kwargs):
        assert domain == "hot"  # the inspector never lists other domains
        return self.rows

    def add_chunk(self, content, domain="general", **kwargs):
        self.added.append((content, domain, kwargs))
        return self.next_id

    def delete_by_id(self, chunk_id):
        self.deleted.append(chunk_id)
        return True


def test_hot_list(tmp_path, monkeypatch):
    c = _client(monkeypatch, tmp_path, knowledge=_HotKS())
    body = c.get("/api/memory/hot").json()
    assert body["enabled"] is True
    row = body["chunks"][0]
    assert (row["id"], row["content"], row["created_at"], row["source"]) == (
        3,
        "operator prefers dark mode",
        "2026-07-01",
        "console",
    )


def test_hot_edit_pins_domain_and_adds_before_deleting(tmp_path, monkeypatch):
    ks = _HotKS()
    c = _client(monkeypatch, tmp_path, knowledge=ks)
    body = c.put("/api/memory/hot/3", json={"content": "prefers light mode"}).json()
    assert body == {"enabled": True, "id": 9, "replaced": True}
    content, domain, kwargs = ks.added[0]
    assert (content, domain) == ("prefers light mode", "hot")  # never demoted out of hot
    assert kwargs["source_type"] == "operator"
    assert ks.deleted == [3]


def test_hot_edit_keeps_old_row_when_add_fails(tmp_path, monkeypatch):
    ks = _HotKS()
    ks.add_chunk = lambda *a, **k: None  # store rejects the revision
    c = _client(monkeypatch, tmp_path, knowledge=ks)
    assert c.put("/api/memory/hot/3", json={"content": "x"}).status_code == 400
    assert ks.deleted == []  # the original survives


def test_hot_edit_unknown_id_404(tmp_path, monkeypatch):
    ks = _HotKS()
    c = _client(monkeypatch, tmp_path, knowledge=ks)
    assert c.put("/api/memory/hot/99", json={"content": "x"}).status_code == 404
    assert ks.added == [] and ks.deleted == []


def test_hot_delete_only_resolves_hot_ids(tmp_path, monkeypatch):
    ks = _HotKS()
    c = _client(monkeypatch, tmp_path, knowledge=ks)
    assert c.delete("/api/memory/hot/3").json() == {"enabled": True, "deleted": True}
    assert ks.deleted == [3]
    # An id that is NOT a hot chunk 404s — this surface can't delete arbitrary KB rows.
    assert c.delete("/api/memory/hot/42").status_code == 404
    assert ks.deleted == [3]


def test_hot_disabled_without_store(tmp_path, monkeypatch):
    c = _client(monkeypatch, tmp_path)
    assert c.get("/api/memory/hot").json() == {"enabled": False, "chunks": []}
    assert c.delete("/api/memory/hot/1").json() == {"enabled": False, "deleted": False}
    assert c.put("/api/memory/hot/1", json={"content": "x"}).json() == {"enabled": False, "id": None}
