"""GET /api/knowledge/collections + the ?collection= search filter (ADR 0125 P2
slice 6, #4630) over a real ``KnowledgeStore``.

Covers the collection counts, the NULL/empty-collection grouping under the
``knowledge_ingest`` default ("general"), the search collection filter, the
unknown-collection empty result, and that agent memory (the separate ``memories``
table) never leaks into the collection listing.
"""

from __future__ import annotations

import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient

from knowledge.memories import MemoryStore
from knowledge.store import KnowledgeStore
from operator_api.knowledge_routes import register_knowledge_routes

_TOKEN = "protoagent"  # an FTS-searchable token every knowledge chunk shares


def _wire(monkeypatch, store):
    import runtime.state as rs

    monkeypatch.setattr(rs.STATE, "knowledge_store", store, raising=False)
    monkeypatch.setattr(rs.STATE, "skills_index", None, raising=False)
    app = FastAPI()
    register_knowledge_routes(app)
    return TestClient(app)


def _set_collection(store, chunk_id, value):
    db = sqlite3.connect(str(store.path))
    try:
        db.execute("UPDATE chunks SET collection = ? WHERE id = ?", (value, chunk_id))
        db.commit()
    finally:
        db.close()


def _add(store, content, *, collection=..., domain="general", source=None, source_type="pdf", invalidate=False):
    """Add a chunk, then stamp its ``collection`` column directly (``add_chunk`` writes
    ``domain`` only — the P2-B boot migration is what backfills ``collection``).
    ``collection=...`` (the default sentinel) leaves it NULL, as a fresh ingest does."""
    cid = store.add_chunk(content, domain=domain, source=source, source_type=source_type)
    assert cid is not None
    if collection is not ...:
        _set_collection(store, cid, collection)
    if invalidate:
        assert store.invalidate_chunk(cid)
    return cid


def _seed(store):
    # necromunda: 3 valid chunks across 2 distinct sources, + 1 invalidated (excluded).
    _add(store, f"{_TOKEN} gang rules", collection="necromunda", domain="docs", source="rulebook.pdf")
    _add(store, f"{_TOKEN} weapon list", collection="necromunda", domain="docs", source="rulebook.pdf")
    _add(store, f"{_TOKEN} house faq", collection="necromunda", domain="docs", source="faq.html")
    _add(store, f"{_TOKEN} retired edition", collection="necromunda", domain="docs", source="old.pdf", invalidate=True)
    # research: 1 chunk, 1 source.
    _add(store, f"{_TOKEN} findings", collection="research", domain="research", source="paper.pdf")
    # general bucket = explicit "general" + NULL collection + empty-string collection.
    _add(store, f"{_TOKEN} pasted note", collection="general", domain="general", source="pasted")
    _add(store, f"{_TOKEN} untagged ingest", domain="general", source="pasted2")  # collection stays NULL
    _add(store, f"{_TOKEN} blank bucket", collection="", domain="notes", source=None)
    # conversation history is NOT a collection (P2-B leaves its collection NULL, and
    # list_collections excludes domain='conversation' outright). No search token, so it
    # also can't surface in a keyword search.
    _add(store, "chat transcript line", domain="conversation", source="chat")


def test_collections_counts_grouping_and_ordering(monkeypatch, tmp_path):
    store = KnowledgeStore(tmp_path / "agent.db")
    _seed(store)
    client = _wire(monkeypatch, store)

    body = client.get("/api/knowledge/collections").json()
    assert body["enabled"] is True
    rows = body["collections"]
    by = {c["collection"]: c for c in rows}

    # Only knowledge collections — conversation history and the invalidated row are gone.
    assert set(by) == {"general", "necromunda", "research"}
    # Counts per collection.
    assert by["necromunda"]["chunks"] == 3
    assert by["necromunda"]["sources"] == 2  # rulebook.pdf, faq.html (old.pdf invalidated)
    assert by["research"]["chunks"] == 1
    assert by["research"]["sources"] == 1
    # NULL + "" + explicit "general" all fold into one "general" bucket.
    assert by["general"]["chunks"] == 3
    assert by["general"]["sources"] == 2  # pasted, pasted2 (the empty-source row isn't counted)
    # A last-ingested time is reported for every collection.
    assert all(isinstance(c["last_ingested_at"], str) and c["last_ingested_at"] for c in rows)
    # Biggest first, ties broken by name: general(3), necromunda(3), research(1).
    assert [c["collection"] for c in rows] == ["general", "necromunda", "research"]


def test_search_collection_filter(monkeypatch, tmp_path):
    store = KnowledgeStore(tmp_path / "agent.db")
    _seed(store)
    client = _wire(monkeypatch, store)

    # Scoped to necromunda: only its 3 valid chunks (the invalidated one never matches).
    hit = client.get(f"/api/knowledge/search?q={_TOKEN}&collection=necromunda").json()
    assert hit["enabled"] is True
    contents = {r["content"] for r in hit["results"]}
    assert contents == {f"{_TOKEN} gang rules", f"{_TOKEN} weapon list", f"{_TOKEN} house faq"}

    # "general" folds in the NULL/empty-collection rows too (mirrors the grouping).
    gen = client.get(f"/api/knowledge/search?q={_TOKEN}&collection=general").json()
    assert {r["content"] for r in gen["results"]} == {
        f"{_TOKEN} pasted note",
        f"{_TOKEN} untagged ingest",
        f"{_TOKEN} blank bucket",
    }

    # No filter → every collection's matching chunks (7 valid token-bearing chunks).
    all_hits = client.get(f"/api/knowledge/search?q={_TOKEN}").json()
    assert len(all_hits["results"]) == 7


def test_unknown_collection_is_empty_not_404(monkeypatch, tmp_path):
    store = KnowledgeStore(tmp_path / "agent.db")
    _seed(store)
    client = _wire(monkeypatch, store)

    resp = client.get(f"/api/knowledge/search?q={_TOKEN}&collection=does-not-exist")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["results"] == []


def test_memories_rows_never_appear(monkeypatch, tmp_path):
    store = KnowledgeStore(tmp_path / "agent.db")
    _seed(store)
    # A durable memory (ADR 0125) lives in the memories table, not chunks.
    MemoryStore(store.path).add("a remembered fact", kind="fact", provenance="agent", subject="memories-bucket")
    client = _wire(monkeypatch, store)

    rows = client.get("/api/knowledge/collections").json()["collections"]
    # The memory introduces no collection, and no chunk count moves: still 3 + 1 + 3 = 7.
    assert {c["collection"] for c in rows} == {"general", "necromunda", "research"}
    assert sum(c["chunks"] for c in rows) == 7


def test_collections_disabled_when_store_off(monkeypatch):
    client = _wire(monkeypatch, None)
    assert client.get("/api/knowledge/collections").json() == {"enabled": False, "collections": []}


def test_collections_empty_on_backend_without_the_method(monkeypatch):
    """A plugin KnowledgeBackend (ADR 0031) need not implement ``list_collections`` —
    the route getattr-guards it and degrades to an empty list, never a 500."""

    class _MinimalBackend:
        def search(self, q, k=30, domain=None):
            return []

        def stats(self):
            return {}

    client = _wire(monkeypatch, _MinimalBackend())
    body = client.get("/api/knowledge/collections").json()
    assert body == {"enabled": True, "collections": []}
