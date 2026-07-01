"""ADR 0069 D5: memory_recall / memory_list cite each row's provenance —
source session, stored date (date precision), and namespace, when set."""

from __future__ import annotations

import asyncio

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


# ── memory_recall renders the citation ──────────────────────────────────────


def test_memory_recall_cites_source_date_and_namespace(tmp_path):
    ks = KnowledgeStore(db_path=str(tmp_path / "kb.db"))
    ks.add_chunk("The operator prefers teal", domain="fact", source="a2a:chat-42", namespace="proj-x")
    stored = ks.list_chunks(limit=1)[0]

    recall = _by_name(_build_memory_tools(ks))["memory_recall"]
    out = asyncio.run(recall.ainvoke({"query": "teal"}))
    assert "The operator prefers teal" in out
    assert "src: a2a:chat-42" in out
    assert stored.created_at[:10] in out  # date precision, from the row itself
    assert "ns: proj-x" in out


def test_memory_recall_citation_omits_unset_source(tmp_path):
    ks = KnowledgeStore(db_path=str(tmp_path / "kb.db"))
    ks.add_chunk("gateway alias is protolabs/reasoning", domain="general")
    stored = ks.list_chunks(limit=1)[0]

    recall = _by_name(_build_memory_tools(ks))["memory_recall"]
    out = asyncio.run(recall.ainvoke({"query": "gateway alias"}))
    assert "src:" not in out and "ns:" not in out
    assert stored.created_at[:10] in out  # date still cited


# ── memory_list gets the same treatment (src/ns; created_at already leads) ──


def test_memory_list_cites_source_and_namespace(tmp_path):
    ks = KnowledgeStore(db_path=str(tmp_path / "kb.db"))
    ks.add_chunk("summary of the auth thread", domain="conversation", source="a2a:chat-9", namespace="proj-y")

    memory_list = _by_name(_build_memory_tools(ks))["memory_list"]
    out = asyncio.run(memory_list.ainvoke({}))
    assert "src: a2a:chat-9" in out
    assert "ns: proj-y" in out
