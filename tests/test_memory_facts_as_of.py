"""Fact consolidation with ``[as of YYYY-MM-DD]`` leads.

Harvested facts are dated to the conversation. The date must not decide whether two
facts are "the same": the lead adds about five tokens, and before this was measured on
the body, an undated legacy fact and its dated twin scored 0.44 (below the supersede
band) and the same fact got stored twice."""

from __future__ import annotations

from graph.memory_facts import consolidate_and_store


class _Store:
    def __init__(self, rows):
        self.rows = [dict(r) for r in rows]
        self.invalidated: list[tuple[int, int | None]] = []

    def list_chunks(self, domain=None, namespace=None, limit=500):
        return [r for r in self.rows if not r.get("_invalid")]

    def add_chunk(self, content, **kw):
        rid = 1000 + len(self.rows)
        self.rows.append({"id": rid, "content": content})
        return rid

    def invalidate_chunk(self, chunk_id, superseded_by=None):
        for r in self.rows:
            if r.get("id") == chunk_id and not r.get("_invalid"):
                r["_invalid"] = True
                self.invalidated.append((chunk_id, superseded_by))
                return True
        return False


def test_dated_twin_supersedes_an_undated_legacy_fact():
    kb = _Store([{"id": 1, "content": "The user prefers teal."}])
    counts = consolidate_and_store(kb, ["[as of 2026-09-10] The user prefers teal."])
    assert counts == {"added": 1, "skipped": 0, "superseded": 1}
    assert kb.invalidated == [(1, 1001)]


def test_same_fact_same_date_is_skipped():
    kb = _Store([{"id": 1, "content": "[as of 2026-09-10] The user prefers teal."}])
    counts = consolidate_and_store(kb, ["[as of 2026-09-10] The user prefers teal."])
    assert counts == {"added": 0, "skipped": 1, "superseded": 0}


def test_older_date_never_replaces_a_newer_one():
    kb = _Store([{"id": 1, "content": "[as of 2026-09-10] The user prefers teal."}])
    counts = consolidate_and_store(kb, ["[as of 2026-08-11] The user prefers teal."])
    assert counts == {"added": 0, "skipped": 1, "superseded": 0}
    assert kb.invalidated == []


def test_revision_in_the_supersede_band_still_supersedes():
    old = "[as of 2026-08-11] The user runs DeepSeek-V4-Flash and MiniCPM-V-4.6 as model endpoints."
    new = "[as of 2026-09-13] The user runs Qwen3.8-27B and MiniCPM-V-4.6 as model endpoints."
    kb = _Store([{"id": 7, "content": old}])
    counts = consolidate_and_store(kb, [new])
    assert counts["superseded"] == 1 and kb.invalidated == [(7, 1001)]


def test_commons_row_dedups_because_it_cannot_be_invalidated_here():
    kb = _Store([{"id": 3, "tier": "commons", "content": "The user prefers teal."}])
    counts = consolidate_and_store(kb, ["[as of 2026-09-10] The user prefers teal."])
    assert counts == {"added": 0, "skipped": 1, "superseded": 0}
