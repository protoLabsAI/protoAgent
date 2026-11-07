"""ADR 0069 D5 / ADR 0125 D5: provenance citations on recall.

``knowledge_search`` cites a chunk's source / stored date / namespace / trust tier;
``memory_save`` stamps a memory's ``source_session`` from the injected graph state (never
the model), and ``memory_search`` / ``memory_list`` cite it back."""

from __future__ import annotations

import asyncio

from knowledge.layered import LayeredKnowledgeStore
from knowledge.memories import MemoryStore
from knowledge.store import KnowledgeStore
from tools.lg_tools import _build_memory_tools, _memory_citation


def _by_name(tools):
    return {t.name: t for t in tools}


# ── the citation helper itself ──────────────────────────────────────────────


def test_memory_citation_full_and_partial():
    assert (
        _memory_citation(source="a2a:chat-42", created_at="2026-07-01T09:30:00+00:00", namespace="proj-x")
        == " (src: a2a:chat-42, 2026-07-01, ns: proj-x)"
    )
    # Unset fields are simply omitted — no dangling separators.
    assert _memory_citation(created_at="2026-07-01T09:30:00+00:00") == " (2026-07-01)"
    assert _memory_citation(source="a2a:chat-42") == " (src: a2a:chat-42)"
    # Nothing to cite → no suffix at all (no empty parens).
    assert _memory_citation() == ""


# ── knowledge_search cites a chunk's provenance (source / date / namespace) ──


def test_knowledge_search_cites_source_date_and_namespace(tmp_path):
    ks = KnowledgeStore(db_path=str(tmp_path / "kb.db"))
    ks.add_chunk("The operator prefers teal", domain="fact", source="a2a:chat-42", namespace="proj-x")
    stored = ks.list_chunks(limit=1)[0]

    search = _by_name(_build_memory_tools(ks))["knowledge_search"]
    out = asyncio.run(search.ainvoke({"query": "teal"}))
    assert "The operator prefers teal" in out
    assert "src: a2a:chat-42" in out
    assert stored.created_at[:10] in out  # date precision, from the row itself
    assert "ns: proj-x" in out


def test_knowledge_search_citation_omits_unset_source(tmp_path):
    ks = KnowledgeStore(db_path=str(tmp_path / "kb.db"))
    ks.add_chunk("gateway alias is protolabs/reasoning", domain="general")
    stored = ks.list_chunks(limit=1)[0]

    search = _by_name(_build_memory_tools(ks))["knowledge_search"]
    out = asyncio.run(search.ainvoke({"query": "gateway alias"}))
    assert "src:" not in out and "ns:" not in out
    assert stored.created_at[:10] in out  # date still cited


# ── knowledge_search(collection=) scopes to one collection (#2161) ────────────


def test_knowledge_search_collection_filter_scopes_results(tmp_path):
    """``knowledge_search(query, collection=...)`` restricts to ONE collection, so an agent
    can separate its own record from inherited/imported knowledge (#2161)."""
    ks = KnowledgeStore(db_path=str(tmp_path / "kb.db"))
    ks.add_chunk("my own note: shipped the parser refactor", domain="general")
    ks.add_chunk("this codebase shipped ADR 0033 (imported)", domain="claude-import")

    search = _by_name(_build_memory_tools(ks))["knowledge_search"]
    imported = asyncio.run(search.ainvoke({"query": "shipped", "collection": "claude-import"}))
    assert "ADR 0033" in imported and "[claude-import]" in imported
    assert "my own note" not in imported

    own = asyncio.run(search.ainvoke({"query": "shipped", "collection": "general"}))
    assert "my own note" in own
    assert "ADR 0033" not in own


def test_knowledge_search_unscoped_returns_all_collections(tmp_path):
    """Default (no collection) searches every collection, each hit tagged."""
    ks = KnowledgeStore(db_path=str(tmp_path / "kb.db"))
    ks.add_chunk("own: shipped the parser", domain="general")
    ks.add_chunk("imported: shipped ADR 0033", domain="claude-import")
    search = _by_name(_build_memory_tools(ks))["knowledge_search"]
    out = asyncio.run(search.ainvoke({"query": "shipped"}))
    assert "[general]" in out and "[claude-import]" in out


def test_shared_knowledge_search_marks_the_commons_handle_shared(tmp_path):
    """On a layered store a private hit leads with a deletable ``#id``; a commons hit is
    tagged ``commons:<id> (shared)`` (its id lives in the commons tier's own space)."""
    private = KnowledgeStore(str(tmp_path / "private.db"))
    commons = KnowledgeStore(str(tmp_path / "commons.db"))
    private_id = private.add_chunk("private seedbox setting")
    shared_id = commons.add_chunk("shared seedbox guidance")
    assert private_id == shared_id  # each store allocates its own IDs
    search = _by_name(_build_memory_tools(LayeredKnowledgeStore(private, commons)))["knowledge_search"]
    out = asyncio.run(search.ainvoke({"query": "seedbox"}))
    lines = out.splitlines()
    assert next(line for line in lines if "private seedbox" in line).startswith(f"#{private_id} ")
    shared = next(line for line in lines if "shared seedbox" in line)
    assert shared.startswith(f"commons:{shared_id} ")
    assert "(shared)" in shared


# ── memory_save stamps source_session; memory_search / memory_list cite it ────
#
# The citation tests above assert the read side renders `src:`; memory_save stamps a
# memory's originating session from the INJECTED graph state (ADR 0069 D5 / ADR 0125),
# so the model can neither forge it nor omit it, and it round-trips to the reader.


def _memory_tools(tmp_path):
    mem = MemoryStore(db_path=str(tmp_path / "agent.db"))
    return _by_name(_build_memory_tools(None, memory_store=mem)), mem


def test_memory_save_stamps_the_session_it_ran_in(tmp_path):
    tools, mem = _memory_tools(tmp_path)
    out = asyncio.run(
        tools["memory_save"].ainvoke(
            {
                "content": "the operator prefers tabs over spaces",
                "kind": "profile",
                "state": {"session_id": "a2a:chat-77"},
            }
        )
    )
    assert out.startswith("Saved memory #")
    assert mem.list(limit=1)[0].source_session == "a2a:chat-77"


def test_memory_save_provenance_reaches_the_citation(tmp_path):
    """End to end: what the write stamped is what a later search cites."""
    tools, _mem = _memory_tools(tmp_path)
    asyncio.run(
        tools["memory_save"].ainvoke({"content": "deploys go out on Thursdays", "state": {"session_id": "a2a:chat-88"}})
    )
    out = asyncio.run(tools["memory_search"].ainvoke({"query": "deploys Thursdays"}))
    assert "src: a2a:chat-88" in out
    assert "prov=agent" in out


def test_memory_list_cites_source_and_scope(tmp_path):
    tools, mem = _memory_tools(tmp_path)
    mem.add("summary of the auth thread", kind="fact", provenance="agent", source_session="a2a:chat-9", scope="proj-y")
    out = asyncio.run(tools["memory_list"].ainvoke({}))
    assert "src: a2a:chat-9" in out
    assert "ns: proj-y" in out


def test_memory_save_without_state_has_no_bogus_source(tmp_path):
    """No graph state (tool driven outside a turn) — stores fine, no bogus source."""
    tools, mem = _memory_tools(tmp_path)
    out = asyncio.run(tools["memory_save"].ainvoke({"content": "a fact with no session"}))
    assert out.startswith("Saved memory #")
    stored = mem.list(limit=1)[0]
    assert not stored.source_session  # NULL, not the string "None" or an empty-ish sentinel
    assert _memory_citation(source=stored.source_session) == ""


def test_memory_save_provenance_is_not_model_settable(tmp_path):
    """The model cannot forge provenance: it isn't an argument, and the injected state is
    hidden from the tool schema the model sees."""
    tools, _mem = _memory_tools(tmp_path)
    save = tools["memory_save"]
    assert "provenance" not in save.args
    assert "source" not in save.args and "source_session" not in save.args
    assert "state" not in save.args
