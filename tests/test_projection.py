"""Shared context projection (ADR 0108 D8, #3189).

Characterization first: ``projection_native_golden.json`` freezes what the
native ``KnowledgeMiddleware.compose_context`` produced BEFORE the composer was
extracted into ``graph/projection.py``. The refactor must reproduce it byte for
byte — the golden is the proof, not a description.

Regenerate deliberately (never to make a red test green):

    PROTOAGENT_UPDATE_GOLDENS=1 uv run python -m pytest tests/test_projection.py

No network, no model calls, no checkpointer — fakes at the store / skills-index /
STATE boundary, the injection log stubbed at the middleware seam.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import HumanMessage

from graph.middleware.knowledge import KnowledgeMiddleware


_GOLDEN = Path(__file__).parent / "fixtures" / "projection_native_golden.json"


# ---------------------------------------------------------------------------
# Deterministic fakes — one fixed world for every path
# ---------------------------------------------------------------------------


class _FakeStore:
    """Knowledge-store stub: id-attributed hot memory + a canned search."""

    def __init__(self, *, hot_entries=(), results=()):
        self.hot_entries = list(hot_entries)
        self.results = list(results)
        self.search_calls: list[tuple] = []

    def get_hot_memory_entries(self, max_chars: int = 6000) -> list[tuple[int, str]]:
        return list(self.hot_entries)

    def get_hot_memory(self, max_chars: int = 6000) -> str:
        return "\n".join(piece for _, piece in self.hot_entries)

    def search(self, query: str, k: int = 5, **kwargs) -> list[dict]:
        self.search_calls.append((query, k, kwargs))
        return list(self.results)


class _FakeIndex:
    """Skills-index stub matching the always-on surface (ADR 0060)."""

    def __init__(self, rows):
        self.rows = list(rows)

    def skill_summaries(self, limit=None):
        return self.rows[:limit] if limit is not None else list(self.rows)

    def discoverable_count(self) -> int:
        return len(self.rows)


class _GoalCtrl:
    def __init__(self, goal, plan: str):
        self._goal = goal
        self._store = SimpleNamespace(read_plan=lambda sid: plan)

    def active_goal(self, session_id):
        return self._goal


class _Tasks:
    def list(self, *, include_closed=False):
        return [
            {"status": "open", "id": "t-1", "priority": 1, "title": "cut the tag", "session_id": "sess-1"},
            {"status": "open", "id": "t-2", "priority": 2, "title": "write notes", "session_id": "other"},
        ]


class _Watch:
    status = "active"

    def status_line(self) -> str:
        return "w-1 CI on main (active)"


class _Watches:
    def list_watches(self):
        return [_Watch()]


class _Sched:
    def list_jobs(self):
        return [SimpleNamespace(id="job-1", next_fire="2026-09-01T09:00", prompt="post the standup")]


_GOAL = SimpleNamespace(status="active", iteration=2, max_iterations=5, condition="ship the release")
_PLAN = "1. tag from main\n2. write the notes"

_HOT = [(7, "[ops] deploys go out Fridays"), (9, "operator prefers dark mode")]
_RAG = [
    {
        "id": 31,
        "preview": "release: tag from main after CI is green",
        "domain": "general",
        "source_type": "operator",
        "created_at": "2026-08-01T10:00:00+00:00",
    },
    {
        "id": 32,
        "preview": "notes go in changelog.d",
        "domain": "claude-import",
        "source_type": "ingest",
        "created_at": "2026-07-15T08:30:00+00:00",
    },
]
_SKILLS = [
    {"name": "release", "description": "cut a release", "slash": "release"},
    {"name": "standup", "description": "status report", "slash": ""},
]
_DIGEST = (
    "<prior_sessions>\n"
    "  <!-- digest -->\n"
    "- s1 · 2026-08-20 · chat · release prep · 12 msgs\n"
    "- s2 · 2026-08-19 · a2a · notes · 4 msgs\n"
    "</prior_sessions>"
)
_DIGEST_IDS = ["s1", "s2"]
_QUERY = "what is the deploy day?"


def _state(**extra) -> dict:
    return {"messages": [HumanMessage(content=_QUERY)], "session_id": "sess-1", **extra}


def _install_working_state(monkeypatch) -> None:
    import runtime.state as rs

    monkeypatch.setattr(rs.STATE, "goal_controller", _GoalCtrl(_GOAL, _PLAN), raising=False)
    monkeypatch.setattr(rs.STATE, "tasks_store", _Tasks(), raising=False)
    monkeypatch.setattr(rs.STATE, "watch_controller", _Watches(), raising=False)
    monkeypatch.setattr(rs.STATE, "scheduler", _Sched(), raising=False)


def _clear_working_state(monkeypatch) -> None:
    import runtime.state as rs

    for attr in ("goal_controller", "tasks_store", "watch_controller", "scheduler"):
        monkeypatch.setattr(rs.STATE, attr, None, raising=False)


def _native_middleware(monkeypatch, *, store, index) -> tuple[KnowledgeMiddleware, list]:
    """The middleware exactly as agent.py builds it, with the digest pinned
    (no disk) and the injection-log write captured (no sqlite)."""
    mw = KnowledgeMiddleware(
        knowledge_store=store,
        top_k=5,
        skills_index=index,
        skills_top_k=24,
        skills_index_chars=8192,
        inject_namespaces=None,
        inject_min_trust=1,
    )
    mw._prior_sessions_cache = _DIGEST
    mw._prior_sessions_ids = list(_DIGEST_IDS)
    mw._prior_sessions_loaded_at = time.monotonic()
    recorded: list = []
    monkeypatch.setattr(mw, "_record_injection", lambda *a, **k: recorded.append((a, k)))
    return mw, recorded


# ---------------------------------------------------------------------------
# Characterization — the native output is frozen
# ---------------------------------------------------------------------------


def test_native_compose_context_matches_golden(monkeypatch):
    """``KnowledgeMiddleware.compose_context`` reproduces the pre-extraction
    projection byte for byte: digest → hot → RAG inside one <injected_memory>
    envelope, then the skills index, then <working_state>."""
    _install_working_state(monkeypatch)
    store = _FakeStore(hot_entries=_HOT, results=_RAG)
    mw, recorded = _native_middleware(monkeypatch, store=store, index=_FakeIndex(_SKILLS))

    result = mw.compose_context(_state(), None, record=True)
    actual = {"context": result["context"], "context_sections": result["context_sections"]}

    if os.environ.get("PROTOAGENT_UPDATE_GOLDENS"):
        _GOLDEN.write_text(json.dumps(actual, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    expected = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    assert actual == expected
    # The real path records exactly once, id-attributed.
    assert len(recorded) == 1
    args, _ = recorded[0]
    _state_arg, _parts, digest_ids, hot_ids, rag_ids = args
    assert (digest_ids, hot_ids, rag_ids) == (_DIGEST_IDS, [7, 9], [31, 32])
    # The RAG search saw the last human message.
    assert store.search_calls and store.search_calls[0][0] == _QUERY
