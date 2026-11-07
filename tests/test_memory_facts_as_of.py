"""Fact consolidation with ``[as of YYYY-MM-DD]`` leads, over the core MemoryStore (ADR 0125).

Harvested facts are dated to the conversation. The date must not decide whether two facts are
"the same": the lead adds about five tokens, and before this was measured on the body, an
undated legacy fact and its dated twin scored 0.44 (below the supersede band) and the same
fact got stored twice. Harvest order is not chronological either (the TTL sweep retires
threads in DB order; a delete-with-harvest runs any time), so a revision harvested LATER can
be dated EARLIER than the fact it matches and must not supersede it."""

from __future__ import annotations

from graph.memory_facts import consolidate_and_store, _split_as_of
from knowledge.memories import MemoryStore


class _Store(MemoryStore):
    """Exercise the real atomic funnel, with historical seed dates."""

    def __init__(self, path, rows):
        super().__init__(path / "agent.db")
        for row in rows:
            as_of, body = _split_as_of(row["content"])
            self.add_with_supersession(row["content"], kind="fact", provenance="extracted", subject=body, as_of=as_of)
        with self._write() as db:
            db.execute("UPDATE memories SET created_at = '2026-01-01T00:00:00+00:00'")

    @property
    def superseded(self):
        return [(r.id, r.superseded_by) for r in self.list(include_superseded=True) if r.superseded_by]


def test_dated_twin_supersedes_an_undated_legacy_fact(tmp_path):
    kb = _Store(tmp_path, [{"id": 1, "content": "The user prefers teal."}])
    counts = consolidate_and_store(kb, ["[as of 2026-09-10] The user prefers teal."])
    assert counts == {"added": 1, "skipped": 0, "superseded": 1}
    assert kb.superseded == [(1, 2)]


def test_same_fact_same_date_is_skipped(tmp_path):
    kb = _Store(tmp_path, [{"id": 1, "content": "[as of 2026-09-10] The user prefers teal."}])
    counts = consolidate_and_store(kb, ["[as of 2026-09-10] The user prefers teal."])
    assert counts == {"added": 0, "skipped": 1, "superseded": 0}


def test_older_date_never_replaces_a_newer_one(tmp_path):
    kb = _Store(tmp_path, [{"id": 1, "content": "[as of 2026-09-10] The user prefers teal."}])
    counts = consolidate_and_store(kb, ["[as of 2026-08-11] The user prefers teal."])
    assert counts == {"added": 0, "skipped": 1, "superseded": 0}
    assert kb.superseded == []


def test_revision_in_the_supersede_band_still_supersedes(tmp_path):
    old = "[as of 2026-08-11] The user runs DeepSeek-V4-Flash and MiniCPM-V-4.6 as model endpoints."
    new = "[as of 2026-09-13] The user runs Qwen3.8-27B and MiniCPM-V-4.6 as model endpoints."
    kb = _Store(tmp_path, [{"id": 7, "content": old}])
    counts = consolidate_and_store(kb, [new])
    assert counts["superseded"] == 1 and kb.superseded == [(1, 2)]


# ── the date rule in the revision band [0.6, 0.85) (#3494 re-review) ───────────────
_NEWER = "[as of 2026-09-10] The user runs Qwen3.8-27B and MiniCPM-V-4.6 as model endpoints."
_OLDER = "[as of 2026-08-11] The user runs DeepSeek-V4-Flash and MiniCPM-V-4.6 as model endpoints."


def test_the_scenario_lands_in_the_revision_band():
    from graph.memory_facts import _DEDUP_JACCARD, _SUPERSEDE_JACCARD, _jaccard, _split_as_of, _tokens

    score = _jaccard(_tokens(_split_as_of(_NEWER)[1]), _tokens(_split_as_of(_OLDER)[1]))
    assert _SUPERSEDE_JACCARD <= score < _DEDUP_JACCARD


def test_an_older_revision_harvested_later_does_not_supersede_the_newer_fact(tmp_path):
    kb = _Store(tmp_path, [{"id": 7, "content": _NEWER}])
    counts = consolidate_and_store(kb, [_OLDER])
    assert counts == {"added": 1, "skipped": 0, "superseded": 0}
    assert kb.superseded == [(2, 1)]
    assert [r.content for r in kb.list()] == [_NEWER]


def test_an_older_revision_does_not_supersede_the_newer_fact_in_the_real_store(tmp_path):
    from knowledge.memories import MemoryStore

    ms = MemoryStore(tmp_path / "agent.db")
    # Thread B (last active 09-10) is retired first, e.g. deleted from the console with
    # harvest; thread A (last active 08-11) is retired later by the TTL sweep.
    consolidate_and_store(ms, [_NEWER], source="thread-B")
    counts = consolidate_and_store(ms, [_OLDER], source="thread-A")
    valid = [r.content for r in ms.list(kind="fact", provenance="extracted", limit=50)]
    assert counts == {"added": 1, "skipped": 0, "superseded": 0}
    assert valid == [_NEWER]


def test_a_same_date_revision_still_supersedes(tmp_path):
    same_day = "[as of 2026-09-10] The user runs DeepSeek-V4-Flash and MiniCPM-V-4.6 as model endpoints."
    kb = _Store(tmp_path, [{"id": 7, "content": _NEWER}])
    counts = consolidate_and_store(kb, [same_day])
    assert counts["superseded"] == 1 and kb.superseded == [(1, 2)]


def test_a_newer_revision_supersedes_in_the_real_store(tmp_path):
    from knowledge.memories import MemoryStore

    ms = MemoryStore(tmp_path / "agent.db")
    consolidate_and_store(ms, [_OLDER], source="thread-A")
    counts = consolidate_and_store(ms, [_NEWER], source="thread-B")
    valid = [r.content for r in ms.list(kind="fact", provenance="extracted", limit=50)]
    assert counts == {"added": 1, "skipped": 0, "superseded": 1}
    assert valid == [_NEWER]


def test_an_undated_revision_still_supersedes_a_dated_fact(tmp_path):
    undated = "The user runs DeepSeek-V4-Flash and MiniCPM-V-4.6 as model endpoints."
    kb = _Store(tmp_path, [{"id": 7, "content": _NEWER}])
    counts = consolidate_and_store(kb, [undated])
    assert counts["superseded"] == 1 and kb.superseded == [(1, 2)]


def test_a_dated_revision_still_supersedes_an_undated_fact(tmp_path):
    undated = "The user runs Qwen3.8-27B and MiniCPM-V-4.6 as model endpoints."
    kb = _Store(tmp_path, [{"id": 7, "content": undated}])
    counts = consolidate_and_store(kb, [_OLDER])
    assert counts["superseded"] == 1 and kb.superseded == [(1, 2)]
