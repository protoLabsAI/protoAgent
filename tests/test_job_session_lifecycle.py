"""Background job sessions S3 (ADR 0129, refs #5110) — job-scoped context and safe
session retirement.

Two ``job:`` execution sessions each observe only their OWN working state (goal + plan,
tasks, directives, watches, scheduled waits) while intentionally agent-wide work and
origin provenance stay visible exactly as before; job sessions classify as background
(never ordinary chat) for compaction and session-summary purposes, and their summaries
persist under the cross-platform ``%3A``-encoded keys; and the retention sweep retires a
finished/cancelled job's session only at the ADR boundary — a pending decision, a
scheduled resume/continuation, or a claimed/unknown occurrence pins the checkpoint
thread past the normal age TTL.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage

import runtime.state as rs
from graph.checkpoint_prune import prune_checkpoints
from graph.middleware.compaction import CountingSummarizationMiddleware, _is_system_thread
from graph.projection import working_state_block
from scheduler.local import LocalScheduler
from server.maintenance_loops import _job_session_pins, _job_thread_protected

JOB_A = "job:aaaaaaaa-0000-0000-0000-00000000000a"
JOB_B = "job:bbbbbbbb-0000-0000-0000-00000000000b"
_FUTURE_ISO = "2099-01-01T00:00:00+00:00"


# ── fake STATE surfaces (the test_working_state.py shapes) ────────────────────────


class _GoalCtrl:
    def __init__(self, goals: dict, plans: dict):
        self._goals = goals
        self._store = SimpleNamespace(read_plan=lambda sid: plans.get(sid, ""))

    def active_goal(self, session_id):
        return self._goals.get(session_id)


class _Tasks:
    def __init__(self, items):
        self._items = items

    def list(self, *, include_closed=False):
        return self._items


class _Watches:
    def __init__(self, watches):
        self._watches = watches

    def list_watches(self):
        return self._watches


class _Sched:
    def __init__(self, jobs):
        self._jobs = jobs

    def list_jobs(self):
        return self._jobs


def _sjob(jid, prompt, *, context_id=None, origin_session=None, schedule="2026-11-07T18:40:00+00:00"):
    return SimpleNamespace(
        id=jid,
        prompt=prompt,
        schedule=schedule,
        next_fire="2026-11-07T18:40:00+00:00",
        context_id=context_id,
        origin_session=origin_session,
    )


def _watch(wid, run_session, run_prompt):
    return SimpleNamespace(
        id=wid,
        status="active",
        run_session=run_session,
        run_prompt=run_prompt,
        reaction_pending=False,
        interval_s=None,
        last_checked=None,
        status_line=lambda: f"watch [active] ({wid}) via llm: 'condition'",
    )


@pytest.fixture
def clear_state(monkeypatch):
    for attr in ("goal_controller", "tasks_store", "watch_controller", "scheduler", "directive_store",
                 "decision_store"):
        monkeypatch.setattr(rs.STATE, attr, None, raising=False)
    yield


def _two_job_fixtures(monkeypatch):
    goals = {
        JOB_A: SimpleNamespace(status="active", iteration=1, max_iterations=8, condition="ship the job A sweep"),
        JOB_B: SimpleNamespace(status="active", iteration=1, max_iterations=8, condition="rotate the job B logs"),
    }
    plans = {JOB_A: "plan-A: walk the queue", JOB_B: "plan-B: archive old rows"}
    monkeypatch.setattr(rs.STATE, "goal_controller", _GoalCtrl(goals, plans), raising=False)
    monkeypatch.setattr(
        rs.STATE,
        "tasks_store",
        _Tasks([
            {"status": "open", "id": "task-a", "priority": 1, "title": "job A step", "session_id": JOB_A},
            {"status": "open", "id": "task-b", "priority": 1, "title": "job B step", "session_id": JOB_B},
            {"status": "open", "id": "task-w", "priority": 2, "title": "agent-wide chore"},
        ]),
        raising=False,
    )
    monkeypatch.setattr(
        rs.STATE,
        "watch_controller",
        _Watches([
            _watch("w-a", JOB_A, "watch reaction A — file the report"),
            _watch("w-b", JOB_B, "watch reaction B — page the operator"),
        ]),
        raising=False,
    )
    monkeypatch.setattr(
        rs.STATE,
        "scheduler",
        _Sched([
            _sjob(f"wait:{JOB_A}", "resume instruction A — merge when green", context_id=JOB_A),
            _sjob(f"wait:{JOB_B}", "resume instruction B — rotate the logs", context_id=JOB_B),
            _sjob("digest", "post the morning digest", schedule="0 9 * * 1"),
        ]),
        raising=False,
    )


# ── two jobs: each sees only its own working state (ADR 0129 / ADR 0079) ─────────


def test_two_job_sessions_each_see_only_their_own_working_state(clear_state, monkeypatch):
    _two_job_fixtures(monkeypatch)

    block_a = working_state_block({"session_id": JOB_A})

    # Job A's own commitments, in full.
    assert "ship the job A sweep" in block_a and "plan-A: walk the queue" in block_a
    assert "resume instruction A — merge when green" in block_a
    assert "watch reaction A — file the report" in block_a
    assert "task-a" in block_a
    # Deliberately agent-wide work stays visible: the sessionless task and the recurring cron.
    assert "task-w" in block_a
    assert "- digest next=" in block_a
    # Nothing of job B's — not its plan, pending wait, watch reaction or tasks.
    assert "rotate the job B logs" not in block_a and "plan-B" not in block_a
    assert "resume instruction B" not in block_a and f"wait:{JOB_B}" not in block_a
    assert "watch reaction B" not in block_a and "w-b" not in block_a
    assert "task-b" not in block_a
    # The other job's work is a bounded count, labelled for sessions rather than chats.
    assert "OTHER SESSIONS: 1 pending schedule, 1 active watch, 1 open task (not shown here" in block_a
    assert "OTHER CHATS" not in block_a

    block_b = working_state_block({"session_id": JOB_B})
    assert "rotate the job B logs" in block_b and "resume instruction B — rotate the logs" in block_b
    assert "resume instruction A" not in block_b and "task-a" not in block_b
    assert "watch reaction A" not in block_b
    assert "OTHER SESSIONS: 1 pending schedule, 1 active watch, 1 open task (not shown here" in block_b


def test_chat_views_keep_their_pre_isolation_working_state(clear_state, monkeypatch):
    """A chat still reads the shared task list and the agent-wide compact schedule list
    (job-owned one-shots with no origin keep their pre-isolation visibility)."""
    _two_job_fixtures(monkeypatch)

    block = working_state_block({"session_id": "chat-x"})

    assert "task-a" in block and "task-b" in block and "task-w" in block
    assert "AGENT-WIDE SCHEDULES (not tied to a chat):" in block
    assert f"- wait:{JOB_A} next=" in block and f"- wait:{JOB_B} next=" in block
    assert "- digest next=" in block
    # Job-owned watches were already count-only from other sessions; the chat label stays.
    assert "OTHER CHATS: 2 active watches (not shown here" in block
    assert "OTHER SESSIONS" not in block


def test_origin_chat_sees_its_jobs_schedule_in_full(clear_state, monkeypatch):
    """Origin provenance is a separate membership: the creating chat and the execution
    session both get the full instruction; an unrelated job gets a count only."""
    job = _sjob("report", "origin instruction — summarize the inbox", context_id=JOB_A, origin_session="chat-orig")
    monkeypatch.setattr(rs.STATE, "scheduler", _Sched([job]), raising=False)

    for viewer in ("chat-orig", JOB_A):
        block = working_state_block({"session_id": viewer})
        assert "PENDING SCHEDULES (this chat):" in block
        assert "origin instruction — summarize the inbox" in block

    block_b = working_state_block({"session_id": JOB_B})
    assert "origin instruction" not in block_b
    assert "OTHER SESSIONS: 1 pending schedule (not shown here" in block_b


def test_directives_are_scoped_to_their_job_session(clear_state, monkeypatch, tmp_path):
    from graph.directives import DirectiveStore

    store = DirectiveStore(tmp_path / "directives")
    store.record(JOB_A, "only merge green PRs", author="operator")
    store.record(JOB_B, "never touch the prod database", author="operator")
    monkeypatch.setattr(rs.STATE, "directive_store", store, raising=False)

    block_a = working_state_block({"session_id": JOB_A})
    assert "only merge green PRs" in block_a
    assert "never touch the prod database" not in block_a

    block_b = working_state_block({"session_id": JOB_B})
    assert "never touch the prod database" in block_b
    assert "only merge green PRs" not in block_b


# ── background classification: compaction + session summaries ─────────────────────


def test_job_sessions_classify_as_background_not_chat():
    from graph.middleware.memory import _surface_for, is_safe_session_id

    assert _is_system_thread({"session_id": JOB_A})
    assert not _is_system_thread({"session_id": "chat-123"})
    assert _surface_for(JOB_A) == "background"
    assert _surface_for("chat-123") == "chat"
    assert is_safe_session_id(JOB_A)


def test_job_thread_id_fallback_classifies_background(monkeypatch):
    """A turn whose state lost its session_id still classifies by the ``a2a:job:``
    checkpoint thread, exactly as ``a2a:system:activity`` does."""
    import graph.middleware.compaction as compaction

    monkeypatch.setattr(compaction, "_thread_id", lambda: f"a2a:{JOB_A}")
    assert _is_system_thread({})
    monkeypatch.setattr(compaction, "_thread_id", lambda: "a2a:chat-123")
    assert not _is_system_thread({})


def _mw(system_thread_max_tokens: int, *, total_tokens: int) -> CountingSummarizationMiddleware:
    """The test_system_thread_compaction.py harness: the chat trigger is out of reach, the
    token counter is pinned, the summary is stubbed — only the system ceiling can fire."""
    mw = CountingSummarizationMiddleware(
        model=FakeMessagesListChatModel(responses=[AIMessage(content="noop")]),
        trigger=("tokens", 200_000),
        keep=("messages", 2),
        token_counter=lambda _msgs: total_tokens,
        system_thread_max_tokens=system_thread_max_tokens,
        history_store=None,
    )
    mw._create_summary = lambda _msgs: "SUMMARY"
    return mw


def _messages(n: int = 6) -> list:
    out: list = []
    for i in range(n):
        out.append(HumanMessage(content=f"ask {i}"))
        out.append(AIMessage(content=f"answer {i}"))
    return out


def _compacted(result) -> bool:
    msgs = (result or {}).get("messages") or []
    return any(isinstance(m, HumanMessage) and "SUMMARY" in str(m.content) for m in msgs)


def test_job_session_gets_the_bounded_background_compaction_policy():
    """40k tokens: under the 200k chat trigger, over the 30k background ceiling — a job
    session compacts where a chat of the same size does not (#4625 policy, ADR 0129 scope)."""
    mw = _mw(30_000, total_tokens=40_000)
    assert _compacted(mw.before_model({"messages": _messages(), "session_id": JOB_A}, None))
    assert mw.before_model({"messages": _messages(), "session_id": "chat-same-size"}, None) is None


def test_two_job_sessions_keep_independent_compaction_state():
    """A summarizer failure on job A pauses compaction for A ONLY — job B still compacts,
    and A stays in its own backoff window."""
    mw = _mw(30_000, total_tokens=40_000)

    def _boom(_msgs):
        raise RuntimeError("summarizer down")

    mw._create_summary = _boom
    assert mw.before_model({"messages": _messages(), "session_id": JOB_A}, None) is None

    mw._create_summary = lambda _msgs: "SUMMARY"
    assert _compacted(mw.before_model({"messages": _messages(), "session_id": JOB_B}, None))
    # A is still inside its own backoff window; B's success cleared only B's key.
    assert mw.before_model({"messages": _messages(), "session_id": JOB_A}, None) is None


def test_job_session_summary_persists_under_an_encoded_cross_platform_key(monkeypatch, tmp_path):
    from graph.middleware.memory import _persist_session, load_digest_pool, session_filename

    monkeypatch.setenv("MEMORY_PATH", str(tmp_path))
    state = {"session_id": JOB_A, "messages": [HumanMessage(content="run the sweep"), AIMessage(content="done")]}
    _persist_session(state, "trace-1")

    encoded = Path(tmp_path) / session_filename(JOB_A)
    assert encoded.name == f"{JOB_A.replace(':', '%3A')}.json"  # no raw ':' — valid on NTFS too
    assert encoded.is_file()
    assert not (Path(tmp_path) / f"{JOB_A}.json").exists()
    assert json.loads(encoded.read_text(encoding="utf-8"))["session_id"] == JOB_A

    # Unlike disposable ADR 0070 ``background:`` workers, a job session's summary is a
    # durable prior session: it lists in the digest, attributed to the background surface.
    pool, exists = load_digest_pool(str(tmp_path))
    assert exists
    entry = next(e for e in pool if e.session_id == JOB_A)
    assert "· background ·" in entry.line


# ── retirement: pins and the ADR boundary ─────────────────────────────────────────


def _make_scheduler(tmp_path: Path, agent: str = "lifecycle-test") -> LocalScheduler:
    return LocalScheduler(
        agent_name=agent,
        invoke_url="http://127.0.0.1:7870",
        api_key="k",
        bearer_token="b",
        db_dir=tmp_path,
    )


def _park_occurrence(s: LocalScheduler, job, task_id: str = "task-parked") -> None:
    """Durably mark ``job``'s current fire as accepted-and-unresolved (the shape a parked
    input-required or still-running fire leaves in the ledger)."""
    db = sqlite3.connect(str(s.path))
    try:
        db.execute(
            "INSERT INTO occurrences (job_id, next_fire, message_id, state, created_at, expected_next, task_id) "
            "VALUES (?, ?, ?, 'claimed', ?, ?, ?)",
            (job.id, job.next_fire, f"msg-{job.id}", job.created_at, job.next_fire, task_id),
        )
        db.commit()
    finally:
        db.close()


def _settle_occurrences(s: LocalScheduler, job_id: str) -> None:
    db = sqlite3.connect(str(s.path))
    try:
        db.execute("UPDATE occurrences SET state = 'settled' WHERE job_id = ?", (job_id,))
        db.commit()
    finally:
        db.close()


def test_recurring_job_session_lives_until_cancelled(tmp_path):
    """A recurring job's session follows the job's lifetime: pinned between fires, released
    on cancellation — while the owner mapping survives as an attribution tombstone."""
    s = _make_scheduler(tmp_path)
    job = s.add_job("sweep the queue", "0 9 * * *")
    assert job.context_id.startswith("job:")
    assert job.context_id in s.execution_session_pins()

    assert s.cancel_job(job.id)
    assert job.context_id not in s.execution_session_pins()
    db = sqlite3.connect(str(s.path))
    try:
        row = db.execute(
            "SELECT context_id FROM job_sessions WHERE job_id = ? AND created_at = ?",
            (job.id, job.created_at),
        ).fetchone()
    finally:
        db.close()
    assert row is not None and row[0] == job.context_id  # history/feed attribution intact


def test_unresolved_occurrence_pins_a_cancelled_jobs_session(tmp_path):
    """Cancellation stops future triggers but cannot erase accepted work: a claimed
    occurrence keeps the session pinned until it settles; settlement releases it."""
    s = _make_scheduler(tmp_path)
    job = s.add_job("one shot", _FUTURE_ISO)
    _park_occurrence(s, job)
    assert s.cancel_job(job.id)

    assert job.context_id in s.execution_session_pins()
    _settle_occurrences(s, job.id)
    assert job.context_id not in s.execution_session_pins()


def test_scheduled_resume_pins_the_owner_session(tmp_path):
    """A pending wait continuation targeting an owner session keeps that session pinned
    even after its root job is gone — one-shot roots stay unfinished while continuations
    exist (ADR 0129)."""
    s = _make_scheduler(tmp_path)
    root = s.add_job("root job", "0 9 * * *")
    wait = s.add_job("resume the job later", _FUTURE_ISO, context_id=root.context_id)
    assert s.cancel_job(root.id)

    assert root.context_id in s.execution_session_pins()
    assert s.cancel_job(wait.id)
    assert root.context_id not in s.execution_session_pins()


def test_maintenance_pins_compose_decisions_watches_and_the_ledger(clear_state, monkeypatch):
    """server-side pins = the scheduler's ledger + pending decisions + armed watch
    continuations; an unreadable ledger protects every job session (fails closed)."""
    monkeypatch.setattr(rs.STATE, "scheduler", SimpleNamespace(execution_session_pins=lambda: set()), raising=False)
    monkeypatch.setattr(
        rs.STATE,
        "decision_store",
        SimpleNamespace(list=lambda *, state=None: [SimpleNamespace(session_id=JOB_A)]),
        raising=False,
    )
    monkeypatch.setattr(
        rs.STATE,
        "watch_controller",
        _Watches([_watch("w-b", JOB_B, "react"), _watch("w-chat", "chat-1", "react")]),
        raising=False,
    )
    assert _job_session_pins() == {JOB_A, JOB_B}

    # No execution ledger (no scheduler, or a backend without one) → indeterminable.
    monkeypatch.setattr(rs.STATE, "scheduler", None, raising=False)
    assert _job_session_pins() is None
    monkeypatch.setattr(rs.STATE, "scheduler", SimpleNamespace(), raising=False)
    assert _job_session_pins() is None


def test_job_thread_protection_rules():
    assert _job_thread_protected(f"a2a:{JOB_A}", {JOB_A})
    assert _job_thread_protected(JOB_A, {JOB_A})
    assert not _job_thread_protected(f"a2a:{JOB_B}", {JOB_A})
    # Unknown pins fail closed for job threads only; other threads keep their retention.
    assert _job_thread_protected(f"a2a:{JOB_B}", None)
    assert not _job_thread_protected("a2a:chat-1", None)
    assert not _job_thread_protected("a2a:system:activity", {JOB_A})


def _uuid6(unix_s: float, seq: int) -> str:
    ticks = int(unix_s * 1e7) + 0x01B21DD213814000
    th, tm, tl = (ticks >> 28) & 0xFFFFFFFF, (ticks >> 12) & 0xFFFF, ticks & 0x0FFF
    return f"{th:08x}-{tm:04x}-6{tl:03x}-8000-{seq:012x}"


def _seed_checkpoints(db: str, threads: dict[str, float]) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE checkpoints (thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT, "
            "parent_checkpoint_id TEXT, type TEXT, checkpoint BLOB, metadata BLOB)"
        )
        conn.execute(
            "CREATE TABLE writes (thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT, "
            "task_id TEXT, idx INT, channel TEXT, type TEXT, value BLOB)"
        )
        for tid, stamp in threads.items():
            for i in range(3):
                conn.execute(
                    "INSERT INTO checkpoints VALUES (?,?,?,?,?,?,?)",
                    (tid, "", _uuid6(stamp + i, i), None, "", b"{}", b"{}"),
                )
        conn.commit()
    finally:
        conn.close()


def _thread_count(db: str, thread_id: str) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id=?", (thread_id,)).fetchone()[0]
    finally:
        conn.close()


def test_age_ttl_retires_only_unpinned_job_sessions(tmp_path):
    """The existing TTL path archives a finished/cancelled job's thread but never a pinned
    one; chat threads keep their retention exactly. Once the pin resolves, the same policy
    retires the session at the documented boundary."""
    now = time.time()
    db = str(tmp_path / "c.db")
    old = now - 40 * 86400
    _seed_checkpoints(
        db,
        {f"a2a:{JOB_A}": old, f"a2a:{JOB_B}": old, "a2a:chat-old": old, "a2a:chat-live": now - 60},
    )

    pins = {JOB_A}
    res = prune_checkpoints(
        db,
        keep_per_thread=50,
        max_age_seconds=30 * 86400,
        now=now,
        protected=lambda tid: _job_thread_protected(tid, pins),
    )
    assert res["threads_deleted"] == 2  # chat-old + job B — never the pinned job A
    assert _thread_count(db, f"a2a:{JOB_A}") == 3
    assert _thread_count(db, f"a2a:{JOB_B}") == 0
    assert _thread_count(db, "a2a:chat-old") == 0
    assert _thread_count(db, "a2a:chat-live") == 3

    # The decision is answered / the occurrence settles → the pin resolves, and the
    # SAME existing retention retires the session on the next sweep.
    pins.clear()
    prune_checkpoints(
        db,
        keep_per_thread=50,
        max_age_seconds=30 * 86400,
        now=now,
        protected=lambda tid: _job_thread_protected(tid, pins),
    )
    assert _thread_count(db, f"a2a:{JOB_A}") == 0


def test_indeterminable_pins_protect_every_job_session(tmp_path):
    """When the pin set cannot be established the TTL retires NO job session (it cannot
    tell a finished one from one whose pin it merely failed to read) — chats still age out."""
    now = time.time()
    db = str(tmp_path / "c.db")
    old = now - 40 * 86400
    _seed_checkpoints(db, {f"a2a:{JOB_A}": old, "a2a:chat-old": old})

    prune_checkpoints(
        db,
        keep_per_thread=50,
        max_age_seconds=30 * 86400,
        now=now,
        protected=lambda tid: _job_thread_protected(tid, None),
    )
    assert _thread_count(db, f"a2a:{JOB_A}") == 3
    assert _thread_count(db, "a2a:chat-old") == 0
