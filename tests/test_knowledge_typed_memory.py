"""Typed memory schema (#3072): additive columns for memory classification.

Tests the additive migration (memory_kind, subject, review_state, expires_at)
and the filter plumbing across all three store types without changing delivery
behavior (owned by #3187). The ADR 0108 D4 slice — the ``delivery_policy``
column, hot→always write inference, and the one-shot backfill that classifies
legacy rows from ``domain`` — is covered at the bottom.
"""

from __future__ import annotations

import sqlite3

import pytest

from knowledge.hybrid_store import HybridKnowledgeStore
from knowledge.layered import LayeredKnowledgeStore
from knowledge.store import KnowledgeStore


# ── basic add + retrieve ─────────────────────────────────────────────────────


def test_add_chunk_with_memory_kind(tmp_path):
    """New typed-memory fields are stored and returned on the Chunk."""
    store = KnowledgeStore(tmp_path / "kb.db")
    cid = store.add_chunk(
        "The operator prefers dark mode",
        domain="preferences",
        memory_kind="profile",
        subject="operator",
        review_state="confirmed",
        expires_at="2027-01-01T00:00:00+00:00",
    )
    assert cid is not None
    chunks = store.list_chunks(domain="preferences", limit=1)
    assert len(chunks) == 1
    c = chunks[0]
    assert c.id == cid
    assert c.memory_kind == "profile"
    assert c.subject == "operator"
    assert c.review_state == "confirmed"
    assert c.expires_at == "2027-01-01T00:00:00+00:00"
    # as_dict includes the new fields
    d = c.as_dict()
    assert d["memory_kind"] == "profile"
    assert d["subject"] == "operator"
    assert d["review_state"] == "confirmed"
    assert d["expires_at"] == "2027-01-01T00:00:00+00:00"


def test_add_chunk_without_kind_is_stamped_by_the_store(tmp_path):
    """ADR 0108 D7.1: an omitted ``memory_kind`` / ``review_state`` is stamped by
    the store (kind from the domain, verdict from who wrote it — an unstamped
    writer is the least-trusted tier, so ``pending``); the fields the store
    can't infer (``subject``, ``expires_at``) stay NULL. Supersedes #3205's
    omitted-stays-NULL contract."""
    store = KnowledgeStore(tmp_path / "kb.db")
    cid = store.add_chunk("plain fact", domain="general")
    assert cid is not None
    c = store.list_chunks(limit=1)[0]
    assert c.memory_kind == "reference"
    assert c.subject is None
    assert c.review_state == "pending"
    assert c.expires_at is None
    d = c.as_dict()
    assert d["memory_kind"] == "reference"
    assert d["subject"] is None
    assert d["review_state"] == "pending"
    assert d["expires_at"] is None


def test_add_chunk_stamps_kind_and_review_state_by_tier(tmp_path):
    """Creation stamps (ADR 0108 D7.1): kind comes from domain + source_type via the
    same table the backfill used; review_state from the writer's trust tier —
    operator (3) → confirmed, agent (2) and external/unknown (1) → pending.
    Explicit caller values always win."""
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("console fact", domain="general", source_type="operator")
    store.add_chunk("agent note", domain="general", source_type="conversation")
    store.add_chunk("web page", domain="general", source_type="web")
    store.add_chunk("unstamped", domain="general")
    store.add_chunk("hot pin", domain="hot", source_type="operator")
    store.add_chunk(
        "explicit wins", domain="general", source_type="operator", memory_kind="decision", review_state="rejected"
    )
    by = {c.content: c for c in store.list_chunks(limit=10)}
    assert (by["console fact"].memory_kind, by["console fact"].review_state) == ("reference", "confirmed")
    assert (by["agent note"].memory_kind, by["agent note"].review_state) == ("note", "pending")
    assert (by["web page"].memory_kind, by["web page"].review_state) == ("reference", "pending")
    assert (by["unstamped"].memory_kind, by["unstamped"].review_state) == ("reference", "pending")
    assert (by["hot pin"].memory_kind, by["hot pin"].review_state) == ("standing", "confirmed")
    assert by["hot pin"].delivery_policy == "always"
    assert (by["explicit wins"].memory_kind, by["explicit wins"].review_state) == ("decision", "rejected")
    assert all(c.subject is None and c.expires_at is None for c in by.values())


def test_set_review_state_on_plain_hybrid_and_layered_stores(tmp_path):
    """The operator's verdict (ADR 0108 D7.2): confirm / reject / re-open by id;
    normalized; ValueError for an unknown state (caller bug), False for an
    unknown id. On a layered store the verdict targets the PRIVATE tier only."""
    plain = KnowledgeStore(tmp_path / "p.db")
    cid = plain.add_chunk("agent guess", domain="d", source_type="conversation")
    assert plain.list_chunks(limit=1)[0].review_state == "pending"
    assert plain.set_review_state(cid, "confirmed") is True
    assert plain.list_chunks(limit=1)[0].review_state == "confirmed"
    assert plain.set_review_state(cid, " REJECTED ") is True  # normalized
    assert plain.list_chunks(limit=1)[0].review_state == "rejected"
    assert plain.list_chunks(limit=1)[0].content == "agent guess"  # rejecting never deletes
    assert plain.set_review_state(999, "confirmed") is False
    with pytest.raises(ValueError):
        plain.set_review_state(cid, "maybe")

    hybrid = HybridKnowledgeStore(tmp_path / "h.db", embed_fn=_const_embed)
    hid = hybrid.add_chunk("hybrid guess", domain="d")
    assert hybrid.set_review_state(hid, "confirmed") is True
    assert hybrid.list_chunks(limit=1)[0].review_state == "confirmed"

    priv = KnowledgeStore(tmp_path / "priv.db")
    commons = KnowledgeStore(tmp_path / "commons.db")
    layered = LayeredKnowledgeStore(priv, commons)
    pid = priv.add_chunk("private row", domain="d")
    commons.add_chunk("commons row", domain="d")
    assert layered.set_review_state(pid, "confirmed") is True
    assert priv.list_chunks(limit=1)[0].review_state == "confirmed"
    assert commons.list_chunks(limit=1)[0].review_state == "pending"  # never touched


def test_invalidate_chunk_records_the_superseded_by_chain(tmp_path):
    """ADR 0108 D7.3: a supersede names its replacement in ``invalidation_reason``
    (``superseded_by:<id>``); the legacy call keeps the NULL reason. Both drop
    out of valid listings and stay reachable through ``include_invalidated``."""
    store = KnowledgeStore(tmp_path / "kb.db")
    old = store.add_chunk("deploys on Fridays after standup", domain="fact")
    new = store.add_chunk("deploys on Fridays after lunch", domain="fact")
    assert store.invalidate_chunk(old, superseded_by=new) is True
    assert store.invalidate_chunk(old, superseded_by=new) is False  # already invalidated
    row = store.get_chunk(old)
    assert row["invalidated_at"] and row["invalidation_reason"] == f"superseded_by:{new}"
    legacy = store.add_chunk("unchained supersede", domain="fact")
    assert store.invalidate_chunk(legacy) is True
    assert store.get_chunk(legacy)["invalidation_reason"] is None
    assert {c.id for c in store.list_chunks(domain="fact", limit=10)} == {new}
    audit = {c.id for c in store.list_chunks(domain="fact", limit=10, include_invalidated=True)}
    assert audit == {old, new, legacy}


# ── migration ────────────────────────────────────────────────────────────────


def test_migration_adds_columns(tmp_path):
    """A DB created without the typed-memory columns gets them added on next open."""
    path = tmp_path / "old.db"
    # Simulate a pre-#3072 schema: chunks table without the new columns.
    db = sqlite3.connect(str(path))
    db.execute(
        "CREATE TABLE chunks ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, "
        "domain TEXT NOT NULL DEFAULT 'general', heading TEXT, source TEXT, "
        "source_type TEXT, finding_type TEXT, namespace TEXT, epoch TEXT, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
        "invalidated_at TEXT, invalidation_reason TEXT)"
    )
    db.execute(
        "INSERT INTO chunks (content, domain, created_at, updated_at) "
        "VALUES ('legacy row', 'general', 'x', 'x')"
    )
    db.commit()
    db.close()

    # Opening the store triggers the migration.
    store = KnowledgeStore(path)
    store.add_chunk("new typed row", domain="general", memory_kind="fact", subject="project-x")

    rows = {c.content: c for c in store.list_chunks(limit=10)}
    # The pre-#3072 row went through BOTH migrations: the typed columns were
    # added, then the ADR 0108 D4 backfill classified it from its domain
    # (general + non-conversation source_type → "reference"; not hot → no policy).
    assert rows["legacy row"].memory_kind == "reference"
    assert rows["legacy row"].subject is None
    assert rows["legacy row"].delivery_policy is None
    assert rows["new typed row"].memory_kind == "fact"
    assert rows["new typed row"].subject == "project-x"
    assert rows["new typed row"].delivery_policy is None

    # Indexes were created.
    idx_db = sqlite3.connect(str(path))
    indexes = {r[1] for r in idx_db.execute("PRAGMA index_list(chunks)")}
    idx_db.close()
    assert "idx_chunks_memory_kind" in indexes
    assert "idx_chunks_review_state" in indexes
    assert "idx_chunks_delivery_policy" in indexes


# ── search filters ───────────────────────────────────────────────────────────


def test_search_filter_by_kind(tmp_path):
    """search(memory_kind=...) returns only chunks of that kind."""
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("operator likes coffee", domain="prefs", memory_kind="profile")
    store.add_chunk("deploy on Friday", domain="prefs", memory_kind="decision")
    store.add_chunk("untyped preference", domain="prefs")

    hits = store.search("preference coffee deploy Friday", k=10, memory_kind="profile")
    assert len(hits) == 1
    assert hits[0]["memory_kind"] == "profile"

    # Unfiltered sees all.
    all_hits = store.search("preference coffee deploy Friday", k=10)
    assert len(all_hits) == 3


def test_search_filter_by_kind_like_fallback(tmp_path):
    """The LIKE fallback path also respects memory_kind."""
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("alpha fact", domain="d", memory_kind="fact")
    store.add_chunk("alpha decision", domain="d", memory_kind="decision")
    store._fts_available = False  # force LIKE path

    hits = store.search("alpha", k=10, memory_kind="fact")
    assert len(hits) == 1
    assert hits[0]["memory_kind"] == "fact"


def test_search_filter_by_review_state(tmp_path):
    """search(review_state=...) returns only chunks with that state."""
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("confirmed insight", domain="d", memory_kind="fact", review_state="confirmed")
    store.add_chunk("candidate insight", domain="d", memory_kind="fact", review_state="candidate")

    hits = store.search("insight", k=10, review_state="confirmed")
    assert len(hits) == 1
    assert hits[0]["review_state"] == "confirmed"


# ── list filters ─────────────────────────────────────────────────────────────


def test_list_filter_by_kind(tmp_path):
    """list_chunks(memory_kind=...) returns only chunks of that kind."""
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("profile entry", domain="d", memory_kind="profile")
    store.add_chunk("note entry", domain="d", memory_kind="note")
    store.add_chunk("untyped entry", domain="d")

    profile_chunks = store.list_chunks(memory_kind="profile")
    assert len(profile_chunks) == 1
    assert profile_chunks[0].memory_kind == "profile"

    note_chunks = store.list_chunks(memory_kind="note")
    assert len(note_chunks) == 1
    assert note_chunks[0].memory_kind == "note"


def test_list_filter_by_review_state(tmp_path):
    """list_chunks(review_state=...) returns only chunks with that state."""
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("confirmed", domain="d", review_state="confirmed")
    store.add_chunk("rejected", domain="d", review_state="rejected")
    store.add_chunk("unreviewed", domain="d")

    confirmed = store.list_chunks(review_state="confirmed")
    assert len(confirmed) == 1
    assert confirmed[0].review_state == "confirmed"


# ── hybrid store passthrough ─────────────────────────────────────────────────


def _const_embed(text: str) -> list[float]:
    return [1.0, 0.0]


def test_hybrid_store_passthrough(tmp_path):
    """HybridKnowledgeStore passes typed-memory fields through to the base."""
    store = HybridKnowledgeStore(tmp_path / "kb.db", embed_fn=_const_embed)
    cid = store.add_chunk(
        "hybrid typed fact",
        domain="d",
        memory_kind="standing",
        subject="the-project",
        review_state="candidate",
    )
    assert cid is not None

    # Searchable with the kind filter (both FTS and vector paths).
    hits = store.search("hybrid typed", k=5, memory_kind="standing")
    assert any(r["id"] == cid for r in hits)

    # No hits for a different kind.
    misses = store.search("hybrid typed", k=5, memory_kind="profile")
    assert not any(r["id"] == cid for r in misses)


def test_hybrid_search_review_state_filters_both_rankings(tmp_path):
    """memory_kind and review_state filter both FTS5 and vector rankings."""
    store = HybridKnowledgeStore(tmp_path / "kb.db", embed_fn=_const_embed)
    store.add_chunk("gamma delta", domain="d", memory_kind="fact", review_state="confirmed")
    store.add_chunk("gamma delta too", domain="d", memory_kind="fact", review_state="candidate")

    # Filter by review_state — vector-only path (no shared tokens with query).
    hits = store.search("zzzzz", k=5, review_state="confirmed")
    assert len(hits) == 1
    assert hits[0]["review_state"] == "confirmed"


# ── layered store passthrough ────────────────────────────────────────────────


def test_layered_store_passthrough(tmp_path):
    """LayeredKnowledgeStore passes typed-memory fields through on writes and reads."""
    private = KnowledgeStore(tmp_path / "private.db")
    commons = KnowledgeStore(tmp_path / "commons.db")
    layered = LayeredKnowledgeStore(private, commons)

    # Write via the layered store (targets private via __getattr__).
    layered.add_chunk("layered typed fact", domain="d", memory_kind="episode", subject="session-42")

    # Verify on the private store directly.
    c = private.list_chunks(domain="d", limit=1)[0]
    assert c.memory_kind == "episode"
    assert c.subject == "session-42"

    # Search with kind filter through the layered store.
    hits = layered.search("layered typed", k=5, memory_kind="episode")
    assert len(hits) == 1
    assert hits[0]["content"] == "layered typed fact"

    # No hits for a different kind.
    assert layered.search("layered typed", k=5, memory_kind="profile") == []


def test_layered_search_filters_both_tiers(tmp_path):
    """memory_kind filter is passed through to both private and commons tiers."""
    private = KnowledgeStore(tmp_path / "private.db")
    commons = KnowledgeStore(tmp_path / "commons.db")
    layered = LayeredKnowledgeStore(private, commons)

    private.add_chunk("private fact", domain="d", memory_kind="fact")
    commons.add_chunk("commons decision", domain="d", memory_kind="decision")

    fact_hits = layered.search("private commons fact decision", k=10, memory_kind="fact")
    assert len(fact_hits) == 1
    assert fact_hits[0]["content"] == "private fact"

    decision_hits = layered.search("private commons fact decision", k=10, memory_kind="decision")
    assert len(decision_hits) == 1
    assert decision_hits[0]["content"] == "commons decision"


# ── existing rows unaffected ─────────────────────────────────────────────────


def test_existing_rows_unaffected(tmp_path):
    """Legacy rows (no typed-memory fields) remain fully searchable and listable."""
    store = KnowledgeStore(tmp_path / "kb.db")
    # Write a legacy-style row (no typed fields).
    cid = store.add_chunk(
        "I remember the old days",
        domain="general",
        heading="nostalgia",
        source="conversation",
        source_type="chat",
        namespace="ns1",
        epoch="e1",
    )
    assert cid is not None

    # All existing fields intact.
    c = store.list_chunks(limit=1)[0]
    assert c.content == "I remember the old days"
    assert c.domain == "general"
    assert c.heading == "nostalgia"
    assert c.source == "conversation"
    assert c.source_type == "chat"
    assert c.namespace == "ns1"
    assert c.epoch == "e1"
    # Stamped on write since ADR 0108 D7: general + a chat source → reference,
    # written by the agent tier → pending. Nothing the store can't infer is guessed.
    assert c.memory_kind == "reference"
    assert c.subject is None
    assert c.review_state == "pending"
    assert c.expires_at is None

    # Searchable.
    hits = store.search("old days", k=5)
    assert len(hits) == 1
    assert hits[0]["id"] == cid

    # Unfiltered list still includes it.
    all_chunks = store.list_chunks(limit=50)
    assert any(ch.id == cid for ch in all_chunks)

    # Kind-filtered search excludes it (NULL != 'fact').
    typed_hits = store.search("old days", k=5, memory_kind="fact")
    assert len(typed_hits) == 0


# ── add_document passthrough ─────────────────────────────────────────────────


def test_add_document_passes_typed_fields(tmp_path):
    """add_document forwards typed-memory kwargs to each chunk's add_chunk."""
    store = KnowledgeStore(tmp_path / "kb.db")
    ids = store.add_document(
        "doc-sized content for classification",
        domain="d",
        memory_kind="reference",
        subject="api-docs",
        review_state="confirmed",
        expires_at="2027-06-01T00:00:00+00:00",
    )
    assert len(ids) >= 1
    c = store.list_chunks(domain="d", limit=1)[0]
    assert c.memory_kind == "reference"
    assert c.subject == "api-docs"
    assert c.review_state == "confirmed"
    assert c.expires_at == "2027-06-01T00:00:00+00:00"


def test_promote_preserves_typed_fields(tmp_path):
    """promote copies typed-memory fields from private to commons."""
    from knowledge.layered import LayeredKnowledgeStore

    priv = KnowledgeStore(tmp_path / "priv.db")
    commons = KnowledgeStore(tmp_path / "commons.db")
    store = LayeredKnowledgeStore(priv, commons)
    priv.add_chunk(
        "profile fact",
        domain="hot",
        memory_kind="profile",
        subject="operator",
        review_state="confirmed",
        expires_at="2027-01-01T00:00:00+00:00",
    )
    chunks = priv.list_chunks()
    assert len(chunks) == 1
    store.promote(chunks[0].id)
    commons_chunks = commons.list_chunks()
    assert len(commons_chunks) == 1
    c = commons_chunks[0]
    assert c.memory_kind == "profile"
    assert c.subject == "operator"
    assert c.review_state == "confirmed"
    assert c.expires_at == "2027-01-01T00:00:00+00:00"
    # Inferred from domain="hot" on the private write, forwarded by promote (ADR 0108 D4).
    assert c.delivery_policy == "always"


# ── delivery_policy + backfill (ADR 0108 D4) ─────────────────────────────────


def test_add_chunk_with_delivery_policy(tmp_path):
    """delivery_policy is stored, listed, searched and serialized like the other typed fields."""
    store = KnowledgeStore(tmp_path / "kb.db")
    cid = store.add_chunk("ask before deploying", domain="d", memory_kind="standing", delivery_policy="on_demand")
    assert cid is not None
    c = store.list_chunks(domain="d", limit=1)[0]
    assert c.delivery_policy == "on_demand"
    assert c.as_dict()["delivery_policy"] == "on_demand"
    assert store.search("deploying", k=5)[0]["delivery_policy"] == "on_demand"


def test_hot_domain_forces_always_policy(tmp_path):
    """A domain="hot" write is stamped "always" whatever the caller passed —
    the reader keys on the domain, so a hot row IS always-on and the column
    must say so (no hot+non-always rows can exist). Other domains stay NULL
    (= retrieved)."""
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("pinned fact", domain="hot")
    store.add_chunk("plain fact", domain="general")
    store.add_chunk("hot claiming otherwise", domain="hot", delivery_policy="on_demand")
    by_content = {c.content: c for c in store.list_chunks(limit=10)}
    assert by_content["pinned fact"].delivery_policy == "always"
    assert by_content["plain fact"].delivery_policy is None
    assert by_content["hot claiming otherwise"].delivery_policy == "always"
    assert store.list_chunks(domain="hot") == store.list_chunks(domain="hot", delivery_policy="always")
    # The kind stamp (ADR 0108 D7.1) rides the same table: a hot row is "standing".
    assert by_content["pinned fact"].memory_kind == "standing"


def test_list_and_search_filter_by_delivery_policy(tmp_path):
    """list_chunks / search(delivery_policy=...) match the value — and because
    NULL *means* retrieved, "retrieved" matches the untyped row too; FTS and
    LIKE paths alike."""
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("alpha always", domain="d", delivery_policy="always")
    store.add_chunk("alpha on demand", domain="d", delivery_policy="on_demand")
    store.add_chunk("alpha untyped", domain="d")

    always = store.list_chunks(delivery_policy="always")
    assert [c.content for c in always] == ["alpha always"]
    assert [c.content for c in store.list_chunks(delivery_policy="retrieved")] == ["alpha untyped"]
    assert len(store.list_chunks()) == 3

    hits = store.search("alpha", k=10, delivery_policy="on_demand")
    assert [h["content"] for h in hits] == ["alpha on demand"]
    assert [h["content"] for h in store.search("alpha", k=10, delivery_policy="retrieved")] == ["alpha untyped"]
    assert len(store.search("alpha", k=10)) == 3

    store._fts_available = False  # the LIKE fallback respects both rules too
    like_hits = store.search("alpha", k=10, delivery_policy="always")
    assert [h["content"] for h in like_hits] == ["alpha always"]
    assert [h["content"] for h in store.search("alpha", k=10, delivery_policy="retrieved")] == ["alpha untyped"]


def test_hybrid_search_filters_delivery_policy_both_rankings(tmp_path):
    """delivery_policy filters the vector ranking as well as FTS5 — an
    off-policy chunk can't surface as a vector-only hit — and "retrieved"
    reaches NULL rows on the vector path too."""
    store = HybridKnowledgeStore(tmp_path / "kb.db", embed_fn=_const_embed)
    store.add_chunk("gamma delta", domain="d", delivery_policy="always")
    store.add_chunk("gamma delta too", domain="d", delivery_policy="on_demand")
    store.add_chunk("gamma delta untyped", domain="d")
    # Vector-only path (no shared tokens with the query).
    hits = store.search("zzzzz", k=5, delivery_policy="always")
    assert [h["content"] for h in hits] == ["gamma delta"]
    assert [h["content"] for h in store.search("zzzzz", k=5, delivery_policy="retrieved")] == ["gamma delta untyped"]
    # FTS path.
    fts_hits = store.search("gamma", k=5, delivery_policy="on_demand")
    assert [h["content"] for h in fts_hits] == ["gamma delta too"]


def test_chunk_from_row_ignores_unknown_columns(tmp_path):
    """A row from a DB a newer build has widened (a column this build doesn't
    know) still builds a Chunk — the unknown column is dropped, not fatal."""
    from knowledge.store import Chunk

    c = Chunk.from_row(
        {
            "id": 1, "content": "x", "domain": "general", "heading": None, "source": None,
            "source_type": None, "finding_type": None, "created_at": "t", "updated_at": "t",
            "future_column_from_d7": "whatever",
        }
    )
    assert c.id == 1 and c.delivery_policy is None
    assert not hasattr(c, "future_column_from_d7")

    # End to end: the store reads a table carrying an extra column.
    path = tmp_path / "wide.db"
    store = KnowledgeStore(path)
    store.add_chunk("survives", domain="general")
    db = sqlite3.connect(str(path))
    db.execute("ALTER TABLE chunks ADD COLUMN future_column_from_d7 TEXT")
    db.commit()
    db.close()
    assert [c.content for c in KnowledgeStore(path).list_chunks(limit=5)] == ["survives"]


def test_layered_search_forwards_delivery_policy_to_both_tiers(tmp_path):
    private = KnowledgeStore(tmp_path / "private.db")
    commons = KnowledgeStore(tmp_path / "commons.db")
    layered = LayeredKnowledgeStore(private, commons)
    private.add_chunk("private pin", domain="d", delivery_policy="always")
    commons.add_chunk("commons pin", domain="d", delivery_policy="always")
    commons.add_chunk("commons lazy", domain="d", delivery_policy="on_demand")

    always = layered.search("pin lazy private commons", k=10, delivery_policy="always")
    assert sorted(h["content"] for h in always) == ["commons pin", "private pin"]
    lazy = layered.search("pin lazy private commons", k=10, delivery_policy="on_demand")
    assert [h["content"] for h in lazy] == ["commons lazy"]


def test_add_document_passes_delivery_policy(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    ids = store.add_document("doc-sized content with a policy", domain="d", delivery_policy="on_demand")
    assert len(ids) >= 1
    assert store.list_chunks(domain="d", limit=1)[0].delivery_policy == "on_demand"


def _post_3205_db(path):
    """A DB exactly as PR #3205 left it: the four typed columns exist, no
    delivery_policy, every existing row untyped (NULL) — the backfill's input."""
    db = sqlite3.connect(str(path))
    db.execute(
        "CREATE TABLE chunks ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, "
        "domain TEXT NOT NULL DEFAULT 'general', heading TEXT, source TEXT, "
        "source_type TEXT, finding_type TEXT, namespace TEXT, epoch TEXT, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
        "invalidated_at TEXT, invalidation_reason TEXT, "
        "memory_kind TEXT, subject TEXT, review_state TEXT, expires_at TEXT)"
    )
    return db


# One row per ADR 0108 D4 backfill-table branch:
# (content, domain, source_type) → (memory_kind, delivery_policy).
_BACKFILL_CASES = [
    ("hot row", "hot", None, "standing", "always"),
    ("preferences row", "preferences", None, "profile", None),
    ("general conversation row", "general", "conversation", "note", None),
    ("general ingest row", "general", "ingest", "reference", None),
    ("general untyped-source row", "general", None, "reference", None),
    ("finding row", "finding", "chat", "fact", None),
    ("fact row", "fact", "extracted", "fact", None),
    ("conversation row", "conversation", "harvest", "note", None),
    ("plugin freeform row", "loop-lessons", None, "legacy", None),
]


def test_backfill_classifies_legacy_rows_once(tmp_path):
    """Opening a #3205-era DB classifies every untyped row from its domain per
    the ADR 0108 D4 table, leaves already-typed cells alone, and never runs again."""
    path = tmp_path / "post3205.db"
    db = _post_3205_db(path)
    for content, domain, source_type, _kind, _policy in _BACKFILL_CASES:
        db.execute(
            "INSERT INTO chunks (content, domain, source_type, created_at, updated_at) VALUES (?, ?, ?, 'x', 'x')",
            (content, domain, source_type),
        )
    # Already typed by a #3205-era writer: memory_kind must survive, but the
    # hot domain still earns its policy (that cell was NULL).
    db.execute(
        "INSERT INTO chunks (content, domain, memory_kind, created_at, updated_at) "
        "VALUES ('typed hot row', 'hot', 'decision', 'x', 'x')"
    )
    db.commit()
    db.close()

    store = KnowledgeStore(path)  # first open under the D4 build → backfill
    rows = {c.content: c for c in store.list_chunks(limit=50)}
    for content, _domain, _source_type, kind, policy in _BACKFILL_CASES:
        assert rows[content].memory_kind == kind, content
        assert rows[content].delivery_policy == policy, content
    assert rows["typed hot row"].memory_kind == "decision"
    assert rows["typed hot row"].delivery_policy == "always"
    assert store.get_meta("typed_memory_backfill")  # stamped done

    # One-shot: a row written AFTER the backfill is typed by the write path
    # (ADR 0108 D7.1 — the same table), and a reopen changes nothing.
    store.add_chunk("post-backfill write", domain="general")
    reopened = KnowledgeStore(path)
    again = {c.content: c for c in reopened.list_chunks(limit=50)}
    assert again["post-backfill write"].memory_kind == "reference"
    assert again["post-backfill write"].delivery_policy is None
    assert again["post-backfill write"].review_state == "pending"
    for content, _domain, _source_type, kind, policy in _BACKFILL_CASES:  # unchanged
        assert again[content].memory_kind == kind
        assert again[content].delivery_policy == policy


def test_backfill_retries_until_stamped(tmp_path):
    """The done-marker is the _kb_meta stamp, not the column's existence: with
    the column present but no stamp (a pass that died after the ALTER), the next
    open classifies what's still NULL — and an already-typed cell is untouched."""
    path = tmp_path / "retry.db"
    db = _post_3205_db(path)
    db.execute("ALTER TABLE chunks ADD COLUMN delivery_policy TEXT")  # column born, pass never finished
    db.execute("INSERT INTO chunks (content, domain, created_at, updated_at) VALUES ('orphan hot', 'hot', 'x', 'x')")
    db.execute(
        "INSERT INTO chunks (content, domain, memory_kind, delivery_policy, created_at, updated_at) "
        "VALUES ('fully typed', 'general', 'profile', 'on_demand', 'x', 'x')"
    )
    db.commit()
    db.close()

    store = KnowledgeStore(path)
    rows = {c.content: c for c in store.list_chunks(limit=10)}
    assert (rows["orphan hot"].memory_kind, rows["orphan hot"].delivery_policy) == ("standing", "always")
    assert (rows["fully typed"].memory_kind, rows["fully typed"].delivery_policy) == ("profile", "on_demand")
    assert store.get_meta("typed_memory_backfill")


def test_fresh_db_is_stamped_without_rows(tmp_path):
    """A brand-new store has the column from the schema and nothing to classify —
    it's stamped on first open so later opens never rescan."""
    store = KnowledgeStore(tmp_path / "fresh.db")
    assert store.get_meta("typed_memory_backfill")
    store.add_chunk("later write", domain="loop-lessons")
    # Typed by the write path (ADR 0108 D7.1), not by a rescan — an unmapped
    # domain is "legacy" either way.
    assert KnowledgeStore(tmp_path / "fresh.db").list_chunks(limit=1)[0].memory_kind == "legacy"


def test_grace_sweep_never_reaps_superseded_rows(tmp_path):
    """The bulk-delete grace sweep (#1770) matches its own marker only: a row
    invalidated with a ``superseded_by:<id>`` chain (ADR 0108 D7.3) is audit
    history and survives ``purge_invalidated(0)``; the bulk-deleted row goes."""
    store = KnowledgeStore(tmp_path / "kb.db")
    old = store.add_chunk("old fact", domain="fact")
    new = store.add_chunk("new fact", domain="fact")
    assert store.invalidate_chunk(old, superseded_by=new)
    store.add_chunk("bulk ingested", domain="docs", source="doc.pdf", source_type="pdf")
    assert store.invalidate_by_source("doc.pdf") == 1
    assert store.purge_invalidated(0) == 1  # only the bulk-deleted row
    audit = {c.id for c in store.list_chunks(domain="fact", limit=10, include_invalidated=True)}
    assert audit == {old, new}
    assert store.get_chunk(old)["invalidation_reason"] == f"superseded_by:{new}"


# ── review-state backfill + store-side normalization (ADR 0108 D7, review round) ──


def test_review_state_backfill_stamps_legacy_rows_once(tmp_path):
    """A #3242-shaped DB (all five typed columns, D4 stamp already set, NULL
    review_state everywhere) gets a SECOND one-shot pass on first open: the tier
    rule stamps every NULL verdict, a verdict already set survives, the D4 pass
    does not re-run, and a later NULL never triggers a rescan."""
    path = tmp_path / "post3242.db"
    db = sqlite3.connect(str(path))
    db.execute(
        "CREATE TABLE chunks ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, "
        "domain TEXT NOT NULL DEFAULT 'general', heading TEXT, source TEXT, "
        "source_type TEXT, finding_type TEXT, namespace TEXT, epoch TEXT, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
        "invalidated_at TEXT, invalidation_reason TEXT, "
        "memory_kind TEXT, subject TEXT, review_state TEXT, expires_at TEXT, delivery_policy TEXT)"
    )
    db.execute("CREATE TABLE _kb_meta (key TEXT PRIMARY KEY, value TEXT)")
    db.execute("INSERT INTO _kb_meta(key, value) VALUES ('typed_memory_backfill', 'd4-stamp')")
    for content, source_type in (("console fact", "operator"), ("agent note", "conversation"), ("web page", "web"), ("unstamped", None)):
        db.execute(
            "INSERT INTO chunks (content, domain, source_type, memory_kind, created_at, updated_at) "
            "VALUES (?, 'general', ?, 'reference', 'x', 'x')",
            (content, source_type),
        )
    db.execute(
        "INSERT INTO chunks (content, domain, source_type, memory_kind, review_state, created_at, updated_at) "
        "VALUES ('already rejected', 'general', 'conversation', 'note', 'rejected', 'x', 'x')"
    )
    db.commit()
    db.close()

    store = KnowledgeStore(path)  # first open under the D7 build
    verdicts = {c.content: c.review_state for c in store.list_chunks(limit=10)}
    assert verdicts == {
        "console fact": "confirmed",
        "agent note": "pending",
        "web page": "pending",
        "unstamped": "pending",
        "already rejected": "rejected",
    }
    assert store.get_meta("review_state_backfill")
    assert store.get_meta("typed_memory_backfill") == "d4-stamp"  # the D4 pass did not re-run

    # One-shot: a NULL written behind the store's back after the stamp is not rescanned.
    raw = sqlite3.connect(str(path))
    raw.execute("INSERT INTO chunks (content, domain, created_at, updated_at) VALUES ('raw after stamp', 'general', 'x', 'x')")
    raw.commit()
    raw.close()
    again = {c.content: c.review_state for c in KnowledgeStore(path).list_chunks(limit=10)}
    assert again["raw after stamp"] is None
    assert again["console fact"] == "confirmed" and again["already rejected"] == "rejected"


def test_review_state_backfill_covers_a_pre_3205_db_too(tmp_path):
    """A DB that predates every typed column goes through D4's pass (kind/policy)
    AND D7's pass (verdict) on the same first open."""
    path = tmp_path / "old.db"
    db = sqlite3.connect(str(path))
    db.execute(
        "CREATE TABLE chunks ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, "
        "domain TEXT NOT NULL DEFAULT 'general', heading TEXT, source TEXT, "
        "source_type TEXT, finding_type TEXT, namespace TEXT, epoch TEXT, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, invalidated_at TEXT, invalidation_reason TEXT)"
    )
    db.execute("INSERT INTO chunks (content, domain, source_type, created_at, updated_at) VALUES ('old operator row', 'hot', 'operator', 'x', 'x')")
    db.execute("INSERT INTO chunks (content, domain, created_at, updated_at) VALUES ('old anonymous row', 'general', 'x', 'x')")
    db.commit()
    db.close()
    rows = {c.content: c for c in KnowledgeStore(path).list_chunks(limit=10)}
    assert (rows["old operator row"].memory_kind, rows["old operator row"].delivery_policy, rows["old operator row"].review_state) == ("standing", "always", "confirmed")
    assert (rows["old anonymous row"].memory_kind, rows["old anonymous row"].delivery_policy, rows["old anonymous row"].review_state) == ("reference", None, "pending")


def test_add_chunk_normalizes_review_state_and_expires_at(tmp_path, caplog):
    """Store-side normalization: the verdict is folded to the canonical spelling
    and an unknown one falls back to the tier rule (logged); ``expires_at`` is
    stored as UTC ISO with a ``+00:00`` offset (naive = UTC), and an unparseable
    value is dropped (logged) rather than stored as text no comparison could use."""
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("cased", domain="d", source_type="operator", review_state=" Rejected ")
    store.add_chunk("bogus", domain="d", source_type="operator", review_state="maybe")
    store.add_chunk("naive", domain="d", expires_at="2027-03-01T12:00:00")
    store.add_chunk("zulu", domain="d", expires_at="2027-03-01T12:00:00Z")
    store.add_chunk("offset", domain="d", expires_at="2027-03-01T14:00:00+02:00")
    store.add_chunk("garbage", domain="d", expires_at="next tuesday")
    by = {c.content: c for c in store.list_chunks(limit=10)}
    assert by["cased"].review_state == "rejected"
    assert by["bogus"].review_state == "confirmed"  # tier rule (operator) after the refusal
    assert by["naive"].expires_at == "2027-03-01T12:00:00+00:00"
    assert by["zulu"].expires_at == "2027-03-01T12:00:00+00:00"
    assert by["offset"].expires_at == "2027-03-01T12:00:00+00:00"
    assert by["garbage"].expires_at is None
    assert "unknown review_state" in caplog.text
    assert "unparseable expires_at" in caplog.text


def test_superseded_by_id_helper():
    from knowledge.store import _BULK_DELETE_REASON, superseded_by_id

    assert superseded_by_id("superseded_by:17") == 17
    assert superseded_by_id(None) is None
    assert superseded_by_id("") is None
    assert superseded_by_id(_BULK_DELETE_REASON) is None
    assert superseded_by_id("superseded_by:zz") is None


def test_promote_confirms_the_commons_copy(tmp_path):
    """Promotion IS the operator's curation: the commons copy is confirmed by
    construction; the private row keeps its own verdict."""
    priv = KnowledgeStore(tmp_path / "priv.db")
    commons = KnowledgeStore(tmp_path / "commons.db")
    layered = LayeredKnowledgeStore(priv, commons)
    pid = priv.add_chunk("pending private", domain="d", source_type="conversation")
    assert priv.list_chunks(limit=1)[0].review_state == "pending"
    assert layered.promote(pid) is not None
    assert commons.list_chunks(limit=1)[0].review_state == "confirmed"
    assert priv.list_chunks(limit=1)[0].review_state == "pending"
