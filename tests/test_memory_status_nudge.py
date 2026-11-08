"""ADR 0125 D5 (2026-11-08 refinement) — work status is not memory.

``memory_save`` REFUSES a save that reads as a work-status log (a release cut, a PR
merged or verified, a CI/QA state, a triage outcome, or a subject-less line with a work
reference and a past-tense event) and says where that fact already lives. Lessons and
rules still save, including ones that cite a PR. The detector is deterministic (regex,
no LLM). The ``memory_save`` description and the operating model carry the matching
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
    _WORTH_SAVING_RULE,
    _build_memory_tools,
    looks_like_status_line,
    status_refusal,
    work_status_kind,
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

# Not a work-status line — fails on exactly one of the three subject-less conditions.
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


# ── the audited status logs and lessons (protoEngineer, 2026-11-08) ─────────
#
# Shortened from real rows. Every status log here was saved under a fresh subject, so the
# old subject-less-only nudge never fired and D4 supersession never retired one.

_AUDITED_STATUS = [
    ("release v0.231.0: I cut protoAgent v0.231.0 on 2026-11-08 (UTC). It was MINOR. Lessons: "
     "never trust a green branch.", "release v0.231.0", "release"),
    ("protoAgent v0.218.0 cut end to end by me on 2026-10-26: release PR #4526 merged.",
     "protoAgent-releases", "release"),
    ("Notes on the bump to v0.229.0, IN PROGRESS: stopped at the desktop build.", "release v0.229.0", "release"),
    ("I verified projectBoard-plugin PR558 (bd-t2l0, fixes #554) merged with seven passing checks "
     "and promoted QA PASS at head a1902d807c62.", "projectBoard-plugin", "ci"),
    ("2026-10-21 triage sweep: terminal-plugin PR23 (bd-uffl) merged with QA promoted WARN.",
     "terminal-plugin", "ci"),
    ("protoAgent triage 2026-08-19: 15 open issues scored and organized.", None, "triage"),
    ("Task cards initiative (#217) COMPLETE — 2026-08-23.", None, "triage"),
    ("I merged #4318 at head cac39166 after review.", "chat-tabbar", "pr"),
]

_AUDITED_KEEP = [
    ("Lesson from epic #4076: a board project whose base_branch is not main needs a LOCAL "
     "tracking branch for that base.", "projectBoard-plugin"),
    ("Correction to my bd-f171 focus lesson (PR #4318, merged 2026-10-17): picking a session "
     "returns focus to the trigger.", "chat-tabbar-focus"),
    ("The plugin settings UX standard lives in docs/guides/plugin-settings.md (merged in PR #4491).",
     "plugin-settings-standard"),
    ("Rule from Josh (2026-10-29): never record a demo clip in jobCoach.", "careercoach-demo-clips"),
    ("Correction (2026-11-05): protobanana-plugin DOES tag releases now; Latest is v0.7.10.",
     "protobanana-plugin-versioning"),
    ("When boarding features that touch the same repo, ALWAYS map the file-overlap graph first.", None),
    ("Josh prefers squash merges and a changelog fragment per PR.", "josh-merges"),
]


@pytest.mark.parametrize("content,subject,kind", _AUDITED_STATUS)
def test_audited_status_logs_are_classified(content, subject, kind):
    assert work_status_kind(content, subject) == kind


@pytest.mark.parametrize("content,subject", _AUDITED_KEEP)
def test_audited_lessons_and_facts_are_not_status(content, subject):
    assert work_status_kind(content, subject) is None


# ── memory_save: refuses status, saves lessons ────────────────────────────────


@pytest.mark.parametrize("content,subject,kind", _AUDITED_STATUS)
def test_status_log_is_refused_and_nothing_is_written(tmp_path, content, subject, kind):
    save, memory = _save_tool(tmp_path)
    args = {"content": content} | ({"subject": subject} if subject else {})
    out = asyncio.run(save.ainvoke(args))
    assert out == status_refusal(kind)
    assert out.startswith("Not saved:")
    assert memory.list(limit=5) == []


def test_refusal_names_where_the_fact_lives():
    assert "git tag" in status_refusal("release")
    assert "checks" in status_refusal("ci")
    assert "board" in status_refusal("triage")
    assert "PR" in status_refusal("pr")
    assert "Lesson:" in status_refusal("pr")


def test_subjectless_status_line_is_refused(tmp_path):
    save, memory = _save_tool(tmp_path)
    out = asyncio.run(save.ainvoke({"content": "merged #4553 at b83d744a"}))
    assert out.startswith("Not saved:")
    assert memory.list(limit=1) == []


def test_shared_profile_proposal_is_refused_too(tmp_path):
    path = str(tmp_path / "agent.db")
    cfg = SimpleNamespace(knowledge_hot_write_confirm=False, memory_shared_profile=True)
    tools = _build_memory_tools(KnowledgeStore(db_path=path), cfg, memory_store=MemoryStore(db_path=path))
    save = next(t for t in tools if t.name == "memory_save")
    out = asyncio.run(save.ainvoke({"content": "release v1.2.0 cut today", "kind": "standing", "shared": True}))
    assert out.startswith("Not saved:")


@pytest.mark.parametrize("content,subject", _AUDITED_KEEP)
def test_lessons_and_facts_still_save(tmp_path, content, subject):
    save, memory = _save_tool(tmp_path)
    args = {"content": content} | ({"subject": subject} if subject else {})
    out = asyncio.run(save.ainvoke(args))
    assert out.startswith("Saved memory #")
    assert memory.list(limit=1)[0].content == content


def test_subject_keeps_a_plain_reference_line_saveable(tmp_path):
    save, _memory = _save_tool(tmp_path)
    out = asyncio.run(save.ainvoke({"content": "merged #4553 at b83d744a", "subject": "release-process"}))
    assert out.startswith("Saved memory #")


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
