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
from graph.projection import ProjectionOptions


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


# ---------------------------------------------------------------------------
# The standalone composer — same world, same bytes
# ---------------------------------------------------------------------------


def _golden() -> dict:
    return json.loads(_GOLDEN.read_text(encoding="utf-8"))


def _options() -> ProjectionOptions:
    return ProjectionOptions(top_k=5, skills_top_k=24, skills_index_chars=8192)


def _pin_disk_digest(monkeypatch) -> None:
    """The default digest loader reads disk; pin it to the golden's digest."""
    import graph.middleware.memory as mem

    monkeypatch.setattr(mem, "load_prior_sessions_digest", lambda *a, **k: (_DIGEST, list(_DIGEST_IDS)))


def test_standalone_composer_reproduces_native_golden(monkeypatch):
    """compose_projected_context() with the same inputs is the native projection."""
    from graph.projection import compose_projected_context

    _install_working_state(monkeypatch)
    store = _FakeStore(hot_entries=_HOT, results=_RAG)
    recorded: list = []

    projected = compose_projected_context(
        _QUERY,
        store,
        _FakeIndex(_SKILLS),
        {"session_id": "sess-1"},
        incognito=False,
        record=True,
        options=_options(),
        prior_sessions=lambda: (_DIGEST, list(_DIGEST_IDS)),
        record_fn=lambda *a: recorded.append(a),
    )
    golden = _golden()
    assert projected.text == golden["context"]
    assert projected.sections == golden["context_sections"]
    assert not projected.empty
    assert projected.as_legacy_dict() == golden
    assert (projected.digest_ids, projected.hot_ids, projected.rag_ids) == (_DIGEST_IDS, [7, 9], [31, 32])
    assert projected.sources == ["prior_sessions", "hot:2", "knowledge:2", "skills:2", "working_state"]
    assert len(recorded) == 1


def test_external_runtime_delta_is_the_native_projection(monkeypatch):
    """ADR 0108 D8 parity: runtime/context.py's volatile delta for the same world is
    byte-identical to what KnowledgeMiddleware injects — the external path now carries
    the <injected_memory> envelope, hot memory, trust-ranked hits, the budgeted skill
    index, and <working_state>."""
    from runtime.context import assemble_context

    _install_working_state(monkeypatch)
    _pin_disk_digest(monkeypatch)
    cfg = SimpleNamespace(knowledge_top_k=5, skills_top_k=24)  # no api_base → 8KB skill budget
    store = _FakeStore(hot_entries=_HOT, results=_RAG)

    ctx = assemble_context(
        cfg,
        query=_QUERY,
        knowledge_store=store,
        skills_index=_FakeIndex(_SKILLS),
        state={"session_id": "sess-1"},
        record=False,
    )
    assert ctx.volatile_delta == _golden()["context"]
    assert ctx.sources == ["prior_sessions", "hot:2", "knowledge:2", "skills:2", "working_state"]
    assert ctx.stable_prefix  # the persona half is untouched by the delta


def test_external_incognito_suppresses_memory_keeps_skills_and_working_state(monkeypatch):
    from runtime.context import assemble_context

    _install_working_state(monkeypatch)
    _pin_disk_digest(monkeypatch)
    store = _FakeStore(hot_entries=[(1, "secret fact")], results=_RAG)

    ctx = assemble_context(
        SimpleNamespace(knowledge_top_k=5, skills_top_k=24),
        query=_QUERY,
        knowledge_store=store,
        skills_index=_FakeIndex(_SKILLS),
        state={"session_id": "sess-1"},
        incognito=True,
        record=False,
    )
    delta = ctx.volatile_delta
    assert "<injected_memory>" not in delta
    assert "secret fact" not in delta and "<prior_sessions>" not in delta and "changelog.d" not in delta
    assert store.search_calls == []  # no RAG search at all on an incognito turn
    assert "<available_skills>" in delta and "<working_state>" in delta
    assert ctx.sources == ["skills:2", "working_state"]


# ---------------------------------------------------------------------------
# Injection log — recorded exactly when a turn happened
# ---------------------------------------------------------------------------


def test_default_recorder_writes_attributed_row_only_when_record_true(monkeypatch):
    from graph.projection import compose_projected_context
    import observability.injection_log as il

    rows: list[dict] = []
    monkeypatch.setattr(il, "injection_log", lambda: SimpleNamespace(record=lambda **kw: rows.append(kw)))
    _clear_working_state(monkeypatch)
    store = _FakeStore(hot_entries=_HOT, results=_RAG)

    compose_projected_context(
        _QUERY, store, None, {"session_id": "sess-1"},
        record=False, options=_options(), prior_sessions=lambda: (_DIGEST, list(_DIGEST_IDS)),
    )
    assert rows == []  # speculative: the full layer ran, nothing claims a turn happened

    compose_projected_context(
        _QUERY, store, None, {"session_id": "sess-1"},
        record=True, options=_options(), prior_sessions=lambda: (_DIGEST, list(_DIGEST_IDS)),
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == "sess-1"
    assert row["digest_session_ids"] == _DIGEST_IDS
    assert row["hot_chunk_ids"] == [7, 9]
    assert row["rag_chunk_ids"] == [31, 32]
    assert row["approx_tokens"] >= 1


def test_context_assembler_records_attributable_turns_only(monkeypatch):
    """A bound assembler's calls are real turns (record by default) — but only when a
    session id makes the row attributable; a bare assemble_context() is a composition
    and must never fabricate a record."""
    import graph.projection as proj
    from runtime.context import ContextAssembler, assemble_context

    calls: list = []
    monkeypatch.setattr(proj, "record_injection", lambda *a: calls.append(a))
    _clear_working_state(monkeypatch)
    _pin_disk_digest(monkeypatch)
    cfg = SimpleNamespace(knowledge_top_k=5, skills_top_k=24)
    store = _FakeStore(hot_entries=_HOT, results=_RAG)

    assemble_context(cfg, query=_QUERY, knowledge_store=store)
    assert calls == []

    # No session id → not attributable → composed and delivered, never recorded.
    ContextAssembler(config=cfg, knowledge_store=store).assemble(query=_QUERY)
    ContextAssembler(config=cfg, knowledge_store=store, session_id="").assemble(query=_QUERY)
    assert calls == []

    ContextAssembler(config=cfg, knowledge_store=store, session_id="s-1").assemble(query=_QUERY)
    assert len(calls) == 1 and calls[0][0] == {"session_id": "s-1"}

    # A per-call id works too; record=False wins over any id.
    ContextAssembler(config=cfg, knowledge_store=store).assemble(query=_QUERY, session_id="s-2")
    assert len(calls) == 2 and calls[1][0] == {"session_id": "s-2"}
    ContextAssembler(config=cfg, knowledge_store=store, record=False, session_id="s-1").assemble(query=_QUERY)
    assert len(calls) == 2


def test_assembler_records_attributed_row_via_default_recorder(monkeypatch):
    """Through the real recorder seam: the row carries the assembler's session id, and
    an assembler without one writes nothing."""
    import observability.injection_log as il
    from runtime.context import ContextAssembler

    rows: list[dict] = []
    monkeypatch.setattr(il, "injection_log", lambda: SimpleNamespace(record=lambda **kw: rows.append(kw)))
    _clear_working_state(monkeypatch)
    _pin_disk_digest(monkeypatch)
    cfg = SimpleNamespace(knowledge_top_k=5, skills_top_k=24)
    store = _FakeStore(hot_entries=_HOT, results=_RAG)

    ContextAssembler(config=cfg, knowledge_store=store).assemble(query=_QUERY)
    assert rows == []
    ContextAssembler(config=cfg, knowledge_store=store, session_id="s-1").assemble(query=_QUERY)
    assert [r["session_id"] for r in rows] == ["s-1"]
    assert rows[0]["hot_chunk_ids"] == [7, 9] and rows[0]["rag_chunk_ids"] == [31, 32]


def test_digest_loader_not_invoked_on_incognito_or_goal_turns(monkeypatch):
    """The composer asks for the digest only on turns that can use it. (main refreshed
    the middleware's TTL cache unconditionally, incognito and goal turns included; the
    per-turn OUTPUT for a given cache state is unchanged — only the refresh timing is.)"""
    from graph.goals.goal_turn import goal_turn
    from graph.projection import compose_projected_context

    _clear_working_state(monkeypatch)
    store = _FakeStore(hot_entries=_HOT, results=_RAG)
    calls: list = []

    def loader():
        calls.append(1)
        return _DIGEST, list(_DIGEST_IDS)

    kw = dict(record=False, options=_options(), prior_sessions=loader)
    compose_projected_context(_QUERY, store, None, {}, incognito=True, **kw)
    assert calls == []
    with goal_turn():
        compose_projected_context(_QUERY, store, None, {}, **kw)
    assert calls == []
    assert "<prior_sessions>" in compose_projected_context(_QUERY, store, None, {}, **kw).text
    assert calls == [1]


def test_from_config_matches_agent_py_hand_wiring(monkeypatch):
    """Drift guard: graph/agent.py hand-wires KnowledgeMiddleware(...) from config;
    ProjectionOptions.from_config(config) must land on the same options — including
    the 2%-of-window-as-chars skill budget. Follow-up (D6 #3187): agent.py passes
    options=ProjectionOptions.from_config(config) so there is one wiring."""
    import graph.model_window as mwin

    monkeypatch.setattr(mwin, "context_window_for", lambda config, model_name=None: 128_000)
    cfg = SimpleNamespace(
        api_base="http://gateway",
        knowledge_top_k=7,
        skills_top_k=3,
        knowledge_inject_namespaces=["", "project:x"],
        knowledge_inject_min_trust=2,
    )
    window = mwin.context_window_for(cfg)
    mw = KnowledgeMiddleware(  # graph/agent.py's exact formula
        None,
        top_k=cfg.knowledge_top_k,
        skills_index=None,
        skills_top_k=cfg.skills_top_k,
        skills_index_chars=int(window * 0.02 * 4) if window else 8192,
        inject_namespaces=cfg.knowledge_inject_namespaces,
        inject_min_trust=cfg.knowledge_inject_min_trust,
    )
    assert mw._options() == ProjectionOptions.from_config(cfg)
    assert mw._options().skills_index_chars == int(128_000 * 0.02 * 4)


# ---------------------------------------------------------------------------
# Options — one delivery policy, read the way agent.py wires it
# ---------------------------------------------------------------------------


def test_options_from_config_mirrors_the_native_wiring(monkeypatch):
    assert ProjectionOptions.from_config(None) == ProjectionOptions()

    cfg = SimpleNamespace(
        knowledge_top_k=7,
        knowledge_inject_namespaces=["", "project:x"],
        knowledge_inject_min_trust=2,
        skills_top_k=3,
    )
    opts = ProjectionOptions.from_config(cfg)
    assert opts.top_k == 7
    assert opts.inject_namespaces == ("", "project:x")
    assert opts.inject_min_trust == 2
    assert opts.skills_top_k == 3
    assert opts.skills_index_chars == 8192  # no gateway profile → the 8KB fallback

    # An explicit 0 keeps its meaning for BOTH knobs (skills: list none; knowledge: no
    # auto-injected hits) — agent.py passes 0 through, so must we; a floor below 1 clamps.
    assert ProjectionOptions.from_config(SimpleNamespace(skills_top_k=0)).skills_top_k == 0
    assert ProjectionOptions.from_config(SimpleNamespace(knowledge_top_k=0)).top_k == 0
    assert ProjectionOptions.from_config(SimpleNamespace(knowledge_inject_min_trust=0)).inject_min_trust == 1

    # With a model window the skill budget is ~2% of it as chars, like graph/agent.py.
    import graph.model_window as mw

    monkeypatch.setattr(mw, "context_window_for", lambda config, model_name=None: 200_000)
    assert ProjectionOptions.from_config(cfg).skills_index_chars == int(200_000 * 0.02 * 4)


def test_projected_context_empty_shape():
    from graph.projection import ProjectedContext

    empty = ProjectedContext()
    assert empty.empty
    assert empty.as_legacy_dict() == {"context": "", "context_sections": []}


# ---------------------------------------------------------------------------
# Rules the composer owns — independent of which runtime calls it
# ---------------------------------------------------------------------------


def test_no_query_means_no_rag_search(monkeypatch):
    from graph.projection import compose_projected_context

    _clear_working_state(monkeypatch)
    store = _FakeStore(hot_entries=_HOT, results=_RAG)
    projected = compose_projected_context(
        "", store, None, {}, record=False, options=_options(), prior_sessions=lambda: ("", []),
    )
    assert store.search_calls == []
    assert "[Relevant knowledge" not in projected.text
    assert "[Always-on facts (hot memory):]" in projected.text
    assert projected.sources == ["hot:2"]


def test_goal_turn_suppresses_the_digest_only(monkeypatch):
    from graph.goals.goal_turn import goal_turn
    from graph.projection import compose_projected_context

    _clear_working_state(monkeypatch)
    store = _FakeStore(hot_entries=_HOT, results=_RAG)
    kw = dict(record=False, options=_options(), prior_sessions=lambda: (_DIGEST, list(_DIGEST_IDS)))

    with goal_turn():
        projected = compose_projected_context(_QUERY, store, None, {}, **kw)
    assert "<prior_sessions>" not in projected.text
    assert projected.digest_ids == []
    assert "[Always-on facts (hot memory):]" in projected.text and projected.rag_ids == [31, 32]
    assert projected.sources == ["hot:2", "knowledge:2"]

    assert "<prior_sessions>" in compose_projected_context(_QUERY, store, None, {}, **kw).text


def test_hot_memory_fallback_for_a_backend_without_the_entries_reader(monkeypatch):
    from graph.projection import compose_projected_context

    _clear_working_state(monkeypatch)

    class _LegacyStore:
        def get_hot_memory(self, max_chars: int = 6000) -> str:
            return "operator prefers dark mode"

        def search(self, query, k=5, **kw):
            return []

    projected = compose_projected_context(
        "", _LegacyStore(), None, {}, record=False, options=_options(), prior_sessions=lambda: ("", []),
    )
    assert "[Always-on facts (hot memory):]\noperator prefers dark mode" in projected.text
    assert projected.hot_ids == []  # un-attributed, but still delivered
    assert projected.sections[0]["label"] == "Injected memory"  # no id-attributed count to show
    assert projected.sources == ["hot"]


def test_namespace_scope_and_trust_floor_on_the_extracted_search(monkeypatch):
    """search_scoped over-fetches 3× under a trust floor and post-filters a legacy
    backend without the namespace kwarg; rank_by_trust drops below-floor hits and
    stable-sorts the rest by tier."""
    from graph.projection import rank_by_trust, search_scoped

    class _Legacy:
        def __init__(self):
            self.calls = []

        def search(self, query, k=5):  # predates the namespace kwarg
            self.calls.append(k)
            return [
                {"id": 1, "namespace": "", "source_type": "ingest", "preview": "ext"},
                {"id": 2, "namespace": "project:x", "source_type": "operator", "preview": "op"},
                {"id": 3, "namespace": "other", "source_type": "operator", "preview": "elsewhere"},
            ]

    store = _Legacy()
    opts = ProjectionOptions(top_k=2, inject_namespaces=("", "project:x"), inject_min_trust=2)
    hits = search_scoped(store, "q", opts)
    assert store.calls == [6]  # top_k × 3 under a floor
    assert [h["id"] for h in hits] == [1, 2]  # the out-of-scope namespace is post-filtered

    ranked = rank_by_trust(hits, opts)
    assert [h["id"] for h in ranked] == [2]  # ingest (tier 1) is below the floor of 2

    # Floor 1 keeps everything, operator-authored first, in-tier order preserved.
    assert [h["id"] for h in rank_by_trust(hits, ProjectionOptions(top_k=5))] == [2, 1]
