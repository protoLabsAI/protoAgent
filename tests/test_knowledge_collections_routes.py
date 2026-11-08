"""GET /api/knowledge/collections + the ?collection= search filter (ADR 0125 P2
slice 6, #4630) over a real ``KnowledgeStore``.

Covers the collection counts, the NULL/empty-collection grouping under the
``knowledge_ingest`` default ("general"), the search collection filter, the
unknown-collection empty result, and that agent memory (the separate ``memories``
table) never leaks into the collection listing.
"""

from __future__ import annotations

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


def _add(store, content, *, collection=..., domain="general", source=None, source_type="pdf", invalidate=False):
    """Add a chunk through the REAL write path: ``add_chunk`` now writes the
    ``collection`` column itself (ADR 0125 slice 6), so a fresh ingest lands in its
    collection immediately — no direct SQL stamp / boot backfill. ``collection=...``
    (the sentinel) passes nothing, leaving it NULL as an untagged write does."""
    kw = {} if collection is ... else {"collection": collection}
    cid = store.add_chunk(content, domain=domain, source=source, source_type=source_type, **kw)
    assert cid is not None
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


async def test_fresh_ingest_lands_in_its_collection_without_backfill(monkeypatch, tmp_path):
    """A chunk written by the ingest op is listed under — and findable in — its
    collection IMMEDIATELY, with no boot backfill.

    knowledge_ingest maps its ``collection`` onto ``domain``; the op stamps the
    ``collection`` column with the same value. Were the column left NULL (its state
    until a reboot backfilled it), the chunk would list under "general", be missing
    from ``?collection=recipes``, and surface under ``?collection=general`` instead.
    """
    from ops import OpContext
    from ops.knowledge import IngestSource, ingest

    store = KnowledgeStore(tmp_path / "agent.db")
    await ingest(
        IngestSource.from_text(f"{_TOKEN} sourdough starter method", title="Bread"),
        domain="recipes",
        ctx=OpContext(knowledge_store=store, graph_config=None),
    )
    client = _wire(monkeypatch, store)

    # Listed under "recipes", not "general".
    rows = client.get("/api/knowledge/collections").json()["collections"]
    assert {c["collection"] for c in rows} == {"recipes"}
    assert rows[0]["chunks"] == 1

    # Findable under ?collection=recipes; NOT dumped into ?collection=general.
    scoped = client.get(f"/api/knowledge/search?q={_TOKEN}&collection=recipes").json()
    assert [r["content"] for r in scoped["results"]] == [f"{_TOKEN} sourdough starter method"]
    gen = client.get(f"/api/knowledge/search?q={_TOKEN}&collection=general").json()
    assert gen["results"] == []

    # And the column itself carries the value (not a reboot-dependent NULL).
    chunk = store.list_chunks(limit=5)[0].as_dict()
    assert chunk["domain"] == "recipes"
    db_collection = store.get_chunk(chunk["id"])["collection"]
    assert db_collection == "recipes"


def test_operator_add_lands_in_its_collection(monkeypatch, tmp_path):
    """A chunk the operator adds via POST /api/knowledge/chunks is listed under —
    and findable in — its collection IMMEDIATELY, not NULL ("general") until reboot.

    The console add route stamps the ``collection`` column with the chunk's domain,
    mirroring the ingest op and the P2-B boot backfill (``collection = domain``).
    Before the fix the column stayed NULL, so the add listed under "general", was
    missing from ``?collection=recipes``, and came back from ``?collection=general``.
    """
    store = KnowledgeStore(tmp_path / "agent.db")
    client = _wire(monkeypatch, store)

    resp = client.post(
        "/api/knowledge/chunks",
        json={"content": f"{_TOKEN} knead for ten minutes", "domain": "recipes"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True and body["id"] is not None

    # Listed under "recipes", not "general".
    rows = client.get("/api/knowledge/collections").json()["collections"]
    assert {c["collection"] for c in rows} == {"recipes"}

    # Findable under ?collection=recipes; NOT dumped into ?collection=general.
    scoped = client.get(f"/api/knowledge/search?q={_TOKEN}&collection=recipes").json()
    assert [r["content"] for r in scoped["results"]] == [f"{_TOKEN} knead for ten minutes"]
    gen = client.get(f"/api/knowledge/search?q={_TOKEN}&collection=general").json()
    assert gen["results"] == []

    # And the column itself carries the value (not a reboot-dependent NULL).
    assert store.get_chunk(body["id"])["collection"] == "recipes"


def test_edit_keeps_the_chunks_collection(monkeypatch, tmp_path):
    """Editing a chunk (PUT /api/knowledge/chunks/{id}) is a new revision; it keeps
    the chunk's collection rather than dropping it into "general".

    The edit copies ``collection`` onto the revision alongside the other lifecycle
    columns. Without it the edited chunk would be missing from ``?collection=necromunda``,
    counted under "general", and later mis-filed by the backfill when the collection
    differs from the domain (here ``necromunda`` vs ``docs``).
    """
    store = KnowledgeStore(tmp_path / "agent.db")
    cid = _add(store, f"{_TOKEN} gang rules", collection="necromunda", domain="docs", source="rulebook.pdf")
    client = _wire(monkeypatch, store)

    resp = client.put(f"/api/knowledge/chunks/{cid}", json={"content": f"{_TOKEN} revised gang rules"})
    assert resp.status_code == 200
    new_id = resp.json()["id"]
    assert new_id is not None

    # The revision still carries the collection on its own row (no reboot needed).
    revised = store.get_chunk(new_id)
    assert revised["collection"] == "necromunda"
    assert revised["content"] == f"{_TOKEN} revised gang rules"

    # Still listed under — and findable in — necromunda, never leaked to "general".
    rows = client.get("/api/knowledge/collections").json()["collections"]
    assert {c["collection"] for c in rows} == {"necromunda"}
    scoped = client.get(f"/api/knowledge/search?q={_TOKEN}&collection=necromunda").json()
    assert [r["content"] for r in scoped["results"]] == [f"{_TOKEN} revised gang rules"]
    gen = client.get(f"/api/knowledge/search?q={_TOKEN}&collection=general").json()
    assert gen["results"] == []


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
