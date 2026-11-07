"""ADR 0125 P3-B / D5 — work status is not memory.

``memory_save`` still SAVES a subject-less work-status line (a PR/issue, board card or
SHA with a past-tense event) but appends a nudge toward the board/issue/PR, where that
stays current. The detector is deterministic (regex, no LLM); the tool never refuses.
The ``memory_save`` description and the operating model carry the matching
"what's worth saving" rule.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from graph.prompts import _build_operating_model
from knowledge.memories import MemoryStore
from knowledge.store import KnowledgeStore
from tools.memory_tools import (
    _STATUS_LINE_NUDGE,
    _WORTH_SAVING_RULE,
    _build_memory_tools,
    looks_like_status_line,
)


def _save_tool(tmp_path):
    """A memory_save bound to a fresh core store (chunks + memories in one agent.db)."""
    path = str(tmp_path / "agent.db")
    knowledge = KnowledgeStore(db_path=path)
    memory = MemoryStore(db_path=path)
    cfg = SimpleNamespace(knowledge_hot_write_confirm=False, memory_shared_profile=False)
    save = next(t for t in _build_memory_tools(knowledge, cfg, memory_store=memory) if t.name == "memory_save")
    return save, memory


# ── looks_like_status_line: deterministic table (≥8 each way) ─────────────────

# Subject empty + a work reference (#num / bd-card / hex SHA) + a past-tense event word.
_STATUS_CASES = [
    ("merged #4553 at b83d744a", None),
    ("shipped bd-aknx2.g1", None),
    ("closed #42", None),
    ("reverted a1b2c3d", None),
    ("pushed 0123456 to main", None),
    ("deployed abcdef1 to prod", None),
    ("landed #999 last night", None),
    ("released v2 — tagged 1234567", None),
    ("opened #4631 for the P3-B work", None),
    ("rebased onto fedcba9 and force-pushed #88", None),
    ("", ""),  # degenerate: no subject but also no ref/event handled below
]

# Not a work-status line — fails on exactly one of the three conditions.
_NON_STATUS_CASES = [
    ("merged #4553 at b83d744a", "release-process"),  # has a subject
    ("Josh prefers squash merges", None),  # no work reference
    ("staging-db lives on host X", None),  # no work reference, no event
    ("fixed the login bug", None),  # event word but no work reference
    ("#4553 is the tracking issue", None),  # work reference but no past-tense event
    ("deployed the new homepage", None),  # event word but no work reference
    ("bd-aknx2.g1 is the current card", None),  # card id but no past-tense event
    ("prefers to squash and rebase before merging", None),  # present-tense, no ref
    ("shipped it", None),  # event word but no work reference
    ("use imperative mood in commit messages", None),  # neither ref nor event
]


@pytest.mark.parametrize("content,subject", _STATUS_CASES[:-1])
def test_status_lines_detected(content, subject):
    assert looks_like_status_line(content, subject) is True


@pytest.mark.parametrize("content,subject", _NON_STATUS_CASES)
def test_non_status_lines_not_detected(content, subject):
    assert looks_like_status_line(content, subject) is False


def test_empty_content_is_not_a_status_line():
    # Empty subject but nothing to reference → no false positive, no crash.
    assert looks_like_status_line("", "") is False
    assert looks_like_status_line("", None) is False


# ── memory_save: saves AND nudges, never refuses (r1) ─────────────────────────


def test_status_line_saves_and_returns_nudge(tmp_path):
    save, memory = _save_tool(tmp_path)
    out = asyncio.run(save.ainvoke({"content": "merged #4553 at b83d744a"}))
    # Saved as normal …
    assert out.startswith("Saved memory #")
    assert memory.list(limit=1)[0].content == "merged #4553 at b83d744a"
    # … and the nudge is appended (never a refusal).
    assert _STATUS_LINE_NUDGE in out


def test_non_status_save_has_no_nudge(tmp_path):
    save, memory = _save_tool(tmp_path)
    out = asyncio.run(
        save.ainvoke({"content": "Josh prefers squash merges", "kind": "profile", "subject": "josh-merges"})
    )
    assert out.startswith("Saved memory #")
    assert _STATUS_LINE_NUDGE not in out
    assert memory.list(limit=1)[0].content == "Josh prefers squash merges"


def test_subject_suppresses_nudge_even_for_a_work_line(tmp_path):
    save, _memory = _save_tool(tmp_path)
    out = asyncio.run(
        save.ainvoke({"content": "merged #4553 at b83d744a", "subject": "release-process"})
    )
    assert out.startswith("Saved memory #")
    assert _STATUS_LINE_NUDGE not in out


# ── the what's-worth-saving wording lives in both places (r3) ─────────────────

_WORTH_PHRASES = (
    "still be true and useful in a month",
    "Not release logs, PR states or card progress",
)


def test_memory_save_description_carries_the_rule(tmp_path):
    save, _memory = _save_tool(tmp_path)
    assert _WORTH_SAVING_RULE in save.description
    for phrase in _WORTH_PHRASES:
        assert phrase in save.description


def test_operating_model_carries_the_rule():
    op_model = _build_operating_model(None)
    for phrase in _WORTH_PHRASES:
        assert phrase in op_model
    # And only when the memory capability is actually bound (honesty).
    assert "Not release logs, PR states or card progress" not in _build_operating_model(frozenset({"set_goal"}))
