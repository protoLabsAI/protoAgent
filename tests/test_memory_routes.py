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


def test_sessions_list_skips_corrupt_file(tmp_path, monkeypatch):
    (tmp_path / "bad.json").write_text("{not json")
    _write(tmp_path, "good", [{"role": "user", "content": "ok"}])
    c = _client(monkeypatch, tmp_path)
    rows = c.get("/api/memory/sessions").json()["sessions"]
    assert [r["session_id"] for r in rows] == ["good"]  # corrupt row skipped, not fatal


def test_sessions_list_in_digest_flags(tmp_path, monkeypatch):
    # The inspector lists what EXISTS on disk; in_digest says what the digest
    # loader actually injects. A legacy background:* file is listed but never
    # enters the digest (ADR 0070 D3 read-side filter) → in_digest False.
    _write(tmp_path, "chat-live", [{"role": "user", "content": "hi"}])
    _write(tmp_path, "background:job1", [{"role": "user", "content": "worker"}])
    c = _client(monkeypatch, tmp_path)
    rows = {r["session_id"]: r for r in c.get("/api/memory/sessions").json()["sessions"]}
    assert rows["chat-live"]["in_digest"] is True
    assert rows["background:job1"]["in_digest"] is False


def test_sessions_list_in_digest_false_beyond_window(tmp_path, monkeypatch):
    import os

    # 12 sessions with distinct mtimes: the digest carries the 10 newest
    # (middleware default), so the 2 oldest must report in_digest False.
    for i in range(12):
        sid = f"s-{i:02d}"
        _write(tmp_path, sid, [{"role": "user", "content": sid}])
        p = tmp_path / f"{sid}.json"
        os.utime(p, (p.stat().st_atime, p.stat().st_mtime - (12 - i) * 100))
    c = _client(monkeypatch, tmp_path)
    rows = c.get("/api/memory/sessions").json()["sessions"]
    flags = {r["session_id"]: r["in_digest"] for r in rows}
    assert flags["s-00"] is False and flags["s-01"] is False
    assert all(flags[f"s-{i:02d}"] for i in range(2, 12))


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


def test_session_get_corrupt_json_422(tmp_path, monkeypatch):
    (tmp_path / "bad.json").write_text("{not json")
    c = _client(monkeypatch, tmp_path)
    assert c.get("/api/memory/sessions/bad").status_code == 422


def test_session_routes_read_encoded_and_legacy_names(tmp_path, monkeypatch):
    # Windows-safe mapping: the writer encodes ':' as '%3A'; reads try the
    # encoded name first, then fall back to the legacy raw-':' name (files a
    # pre-encoding build wrote on POSIX). Deletes remove whichever exists.
    (tmp_path / "system%3Aactivity.json").write_text(
        json.dumps(
            {
                "session_id": "system:activity",
                "timestamp": "2026-07-01T00:00:00Z",
                "messages": [{"role": "user", "content": "encoded-name-body"}],
            }
        )
    )
    _write(tmp_path, "a2a:legacy", [{"role": "user", "content": "legacy-name-body"}])
    c = _client(monkeypatch, tmp_path)
    assert "encoded-name-body" in c.get("/api/memory/sessions/system:activity").json()["session"]["rendered"]
    assert "legacy-name-body" in c.get("/api/memory/sessions/a2a:legacy").json()["session"]["rendered"]
    assert c.delete("/api/memory/sessions/system:activity").json()["deleted"] is True
    assert not (tmp_path / "system%3Aactivity.json").exists()
    assert c.delete("/api/memory/sessions/a2a:legacy").json()["deleted"] is True
    assert not (tmp_path / "a2a:legacy.json").exists()


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


def test_session_delete_oserror_500(tmp_path, monkeypatch):
    _write(tmp_path, "s1", [{"role": "user", "content": "x"}])
    c = _client(monkeypatch, tmp_path)
    import operator_api.memory_routes as mr

    def _boom(path):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(mr.os, "remove", _boom)
    assert c.delete("/api/memory/sessions/s1").status_code == 500


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


def test_hot_list_store_error_returns_empty(tmp_path, monkeypatch):
    ks = _HotKS()

    def _boom(**kwargs):
        raise RuntimeError("db locked")

    ks.list_chunks = _boom
    c = _client(monkeypatch, tmp_path, knowledge=ks)
    assert c.get("/api/memory/hot").json() == {"enabled": True, "chunks": []}  # never 500 the console


class _HotKSEntries(_HotKS):
    """_HotKS plus the id-attributed injection reader the ``injecting`` flag
    derives from (the same get_hot_memory_entries the middleware injects)."""

    def __init__(self, window_ids):
        super().__init__()
        self._window = list(window_ids)

    def get_hot_memory_entries(self, max_chars=6000):
        return [(cid, "piece") for cid in self._window]


def test_hot_list_injecting_flags(tmp_path, monkeypatch):
    ks = _HotKSEntries(window_ids=[3])
    ks.rows = [
        {"id": 3, "content": "in window", "domain": "hot", "created_at": "2026-07-01", "source": "console"},
        {"id": 5, "content": "out of window", "domain": "hot", "created_at": "2026-06-01", "source": "console"},
    ]
    c = _client(monkeypatch, tmp_path, knowledge=ks)
    flags = {r["id"]: r["injecting"] for r in c.get("/api/memory/hot").json()["chunks"]}
    assert flags == {3: True, 5: False}


def test_hot_list_omits_injecting_without_reader(tmp_path, monkeypatch):
    # A custom backend without get_hot_memory_entries: the field is OMITTED
    # entirely (the console renders a missing flag as unknown — no badge).
    c = _client(monkeypatch, tmp_path, knowledge=_HotKS())
    rows = c.get("/api/memory/hot").json()["chunks"]
    assert rows and all("injecting" not in r for r in rows)


def test_hot_list_omits_injecting_when_reader_fails(tmp_path, monkeypatch):
    ks = _HotKSEntries(window_ids=[3])

    def _boom(max_chars=6000):
        raise RuntimeError("reader broke")

    ks.get_hot_memory_entries = _boom
    c = _client(monkeypatch, tmp_path, knowledge=ks)
    rows = c.get("/api/memory/hot").json()["chunks"]
    assert rows and all("injecting" not in r for r in rows)  # the list still serves


def test_hot_budget_break_marks_all_not_injecting(tmp_path, monkeypatch):
    # get_hot_memory_entries walks newest-first and BREAKS at the first
    # over-budget piece — one >6000-char newest chunk empties the whole
    # injection window, so every row (itself included) reports injecting=False.
    from knowledge.store import KnowledgeStore

    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("small older fact", domain="hot")
    store.add_chunk("x" * 6001, domain="hot")  # newest — over budget on its own
    assert store.get_hot_memory_entries() == []
    c = _client(monkeypatch, tmp_path, knowledge=store)
    rows = c.get("/api/memory/hot").json()["chunks"]
    assert len(rows) == 2
    assert all(r["injecting"] is False for r in rows)


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


def test_hot_edit_empty_content_400(tmp_path, monkeypatch):
    ks = _HotKS()
    c = _client(monkeypatch, tmp_path, knowledge=ks)
    for payload in ({}, {"content": ""}, {"content": "   "}):
        assert c.put("/api/memory/hot/3", json=payload).status_code == 400
    assert ks.added == [] and ks.deleted == []  # nothing touched the store


def test_hot_delete_only_resolves_hot_ids(tmp_path, monkeypatch):
    ks = _HotKS()
    c = _client(monkeypatch, tmp_path, knowledge=ks)
    assert c.delete("/api/memory/hot/3").json() == {"enabled": True, "deleted": True}
    assert ks.deleted == [3]
    # An id that is NOT a hot chunk 404s — this surface can't delete arbitrary KB rows.
    assert c.delete("/api/memory/hot/42").status_code == 404
    assert ks.deleted == [3]


def test_hot_excludes_commons_tier(tmp_path, monkeypatch):
    # On a LayeredKnowledgeStore, list_chunks unions private+commons with
    # PER-BACKEND ids while get_hot_memory/add_chunk/delete_by_id delegate to
    # private only. A commons hot row must not appear in the list (it never
    # injects) and — critically — its id must not pass the mutation gates:
    # delete_by_id would hit whatever PRIVATE row shares the numeric id.
    ks = _HotKS()
    ks.rows = [
        {**ks.rows[0], "tier": "private"},
        {"id": 7, "content": "shared fact", "domain": "hot", "created_at": "2026-07-01", "source": "commons", "tier": "commons"},
    ]
    c = _client(monkeypatch, tmp_path, knowledge=ks)
    assert [r["id"] for r in c.get("/api/memory/hot").json()["chunks"]] == [3]
    assert c.delete("/api/memory/hot/7").status_code == 404
    assert c.put("/api/memory/hot/7", json={"content": "x"}).status_code == 404
    assert ks.deleted == [] and ks.added == []  # private row 7 untouched


def test_hot_disabled_without_store(tmp_path, monkeypatch):
    c = _client(monkeypatch, tmp_path)
    assert c.get("/api/memory/hot").json() == {"enabled": False, "chunks": []}
    assert c.delete("/api/memory/hot/1").json() == {"enabled": False, "deleted": False}
    assert c.put("/api/memory/hot/1", json={"content": "x"}).json() == {
        "enabled": False,
        "id": None,
        "replaced": False,
    }
