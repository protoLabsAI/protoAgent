"""Knowledge + playbooks routes (ADR 0023 phase 3 extraction) — registrar wires
the console's read-only Knowledge surface and degrades when a store is off."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from operator_api.knowledge_routes import _knowledge_row, register_knowledge_routes


def _client(monkeypatch, *, knowledge=None, skills=None):
    import runtime.state as rs

    monkeypatch.setattr(rs.STATE, "knowledge_store", knowledge, raising=False)
    monkeypatch.setattr(rs.STATE, "skills_index", skills, raising=False)
    app = FastAPI()
    register_knowledge_routes(app)
    return TestClient(app)


def test_disabled_when_stores_off(monkeypatch):
    c = _client(monkeypatch)
    assert c.get("/api/knowledge/search").json()["enabled"] is False
    assert c.get("/api/playbooks").json() == {"enabled": False, "playbooks": []}
    assert c.delete("/api/playbooks/1").json() == {"enabled": False, "deleted": False}


def test_knowledge_search_and_browse(monkeypatch):
    class _KS:
        def search(self, q, k=30, domain=None):
            return [{"id": 1, "heading": "H", "content": "C"}]

        def list_chunks(self, domain=None, limit=30):
            class _C:
                def as_dict(self_inner):
                    return {"id": 2, "content": "recent"}
            return [_C()]

        def stats(self):
            return {"chunks": 2}

    c = _client(monkeypatch, knowledge=_KS())
    hit = c.get("/api/knowledge/search?q=foo").json()
    assert hit["enabled"] and hit["results"][0]["id"] == 1 and hit["stats"] == {"chunks": 2}
    browse = c.get("/api/knowledge/search").json()  # empty q -> recent chunks
    assert browse["results"][0]["id"] == 2


# ── chunk CRUD (operator curation) ────────────────────────────────────────────
class _CrudKS:
    """Backend double tracking add/delete calls (the ADR 0031 protocol surface)."""

    def __init__(self, *, next_id=7, delete_ok=True):
        self.next_id = next_id
        self.delete_ok = delete_ok
        self.added: list[tuple] = []
        self.deleted: list[int] = []

    def add_chunk(self, content, domain="general", **kwargs):
        self.added.append((content, domain, kwargs))
        return self.next_id

    def delete_by_id(self, chunk_id):
        self.deleted.append(chunk_id)
        return self.delete_ok


def test_chunk_add(monkeypatch):
    ks = _CrudKS()
    c = _client(monkeypatch, knowledge=ks)
    body = c.post("/api/knowledge/chunks", json={"content": "fact", "domain": "ops", "heading": "H"}).json()
    # A backend with only add_chunk (the ADR 0031 surface, no add_document) gets
    # the un-chunked fallback: one write, id echoed in both `id` and `ids`.
    assert body == {"enabled": True, "id": 7, "ids": [7]}
    content, domain, kwargs = ks.added[0]
    assert (content, domain) == ("fact", "ops")
    # heading/source ride kwargs ONLY — the protocol guarantees nothing else positionally
    assert kwargs["heading"] == "H" and kwargs["source_type"] == "operator"


def test_chunk_add_requires_content(monkeypatch):
    c = _client(monkeypatch, knowledge=_CrudKS())
    assert c.post("/api/knowledge/chunks", json={"content": "  "}).status_code == 400


def test_chunk_update_adds_revision_before_deleting_old(monkeypatch):
    ks = _CrudKS(next_id=9)
    c = _client(monkeypatch, knowledge=ks)
    body = c.put("/api/knowledge/chunks/3", json={"content": "fixed fact"}).json()
    assert body == {"enabled": True, "id": 9, "replaced": True}
    assert ks.added and ks.deleted == [3]


def test_chunk_update_keeps_old_row_when_add_fails(monkeypatch):
    ks = _CrudKS()
    ks.add_chunk = lambda *a, **k: None  # store rejects the revision
    c = _client(monkeypatch, knowledge=ks)
    assert c.put("/api/knowledge/chunks/3", json={"content": "x"}).status_code == 400
    assert ks.deleted == []                                   # the original survives


def test_chunk_delete(monkeypatch):
    ks = _CrudKS()
    c = _client(monkeypatch, knowledge=ks)
    assert c.delete("/api/knowledge/chunks/4").json() == {"enabled": True, "deleted": True}
    assert ks.deleted == [4]


def test_chunk_crud_disabled_without_store(monkeypatch):
    c = _client(monkeypatch)
    assert c.post("/api/knowledge/chunks", json={"content": "x"}).json()["enabled"] is False
    assert c.delete("/api/knowledge/chunks/1").json() == {"enabled": False, "deleted": False}


def test_playbooks_sorted_pinned_first(monkeypatch):
    class _SK:
        def all_skills(self):
            return [
                {"id": 1, "source": "emitted", "confidence": 0.9, "prompt_template": "big"},
                {"id": 2, "source": "disk", "confidence": 0.1, "prompt_template": "big"},
            ]

    c = _client(monkeypatch, skills=_SK())
    pb = c.get("/api/playbooks").json()
    assert pb["enabled"] and [p["id"] for p in pb["playbooks"]] == [2, 1]  # disk pinned first
    assert "prompt_template" not in pb["playbooks"][0]  # stripped from list payload


def test_knowledge_row_preview_fallback():
    row = _knowledge_row({"heading": "Title", "content": "Body text"})
    assert row["preview"] == "Title: Body text" and row["domain"] == "general"


def test_playbooks_tier_passthrough(monkeypatch):
    """A layered index tags each skill with its tier (private|commons); the list
    payload must carry it so the surface can badge + gate Promote."""
    class _Layered:
        def all_skills(self):
            return [
                {"id": 1, "name": "a", "source": "emitted", "confidence": 0.5, "tier": "private", "prompt_template": "x"},
                {"id": 1, "name": "b", "source": "promoted", "confidence": 0.9, "tier": "commons", "prompt_template": "x"},
            ]

    c = _client(monkeypatch, skills=_Layered())
    pb = c.get("/api/playbooks").json()["playbooks"]
    assert {p["name"]: p["tier"] for p in pb} == {"a": "private", "b": "commons"}


def test_promote_route_layered(monkeypatch):
    """POST .../promote resolves the private skill by id and lifts it to the commons.
    The id is the private-DB rowid, so a commons row sharing that id is never picked."""
    promoted: list[str] = []

    class _Layered:
        def all_skills(self):
            return [
                {"id": 7, "name": "private-skill", "tier": "private"},
                {"id": 7, "name": "commons-skill", "tier": "commons"},
            ]

        def promote(self, name):
            promoted.append(name)
            return True

    c = _client(monkeypatch, skills=_Layered())
    r = c.post("/api/playbooks/7/promote").json()
    assert r == {"enabled": True, "promoted": True, "name": "private-skill"}
    assert promoted == ["private-skill"]  # the commons row with the same id was not promoted


def test_promote_route_unsupported_in_scoped_mode(monkeypatch):
    """A plain (non-layered) index has no commons to promote into — the route
    explains rather than 500s."""
    class _Plain:
        def all_skills(self):
            return [{"id": 1, "name": "a"}]

    c = _client(monkeypatch, skills=_Plain())
    r = c.post("/api/playbooks/1/promote").json()
    assert r["enabled"] is True and r["promoted"] is False and "layered" in r["error"]


def test_promote_route_disabled(monkeypatch):
    c = _client(monkeypatch)  # no skills index
    assert c.post("/api/playbooks/1/promote").json() == {"enabled": False, "promoted": False}
