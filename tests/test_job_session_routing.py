"""Background job sessions S2 (ADR 0129, refs #5110) — producers route independent
work onto isolated job-owned execution sessions, and ``system:activity`` stays a
summary/feed surface.

Driven through the REAL routing functions: the scheduler tools bound to a real
``LocalScheduler`` (a temporary jobs.db), ``graph.sdk.run_in_session`` /
``react_on``, the server's inbox now-fire, and the ``server.a2a._a2a_terminal``
terminal hook — no fake schedulers, no consumer payload fixtures.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI

from events import ACTIVITY_CONTEXT
from scheduler.local import LocalScheduler

_FUTURE_ISO = "2099-01-01T00:00:00+00:00"


def _make_scheduler(tmp_path: Path, agent: str = "jobs-test") -> LocalScheduler:
    return LocalScheduler(
        agent_name=agent,
        invoke_url="http://127.0.0.1:7870",
        api_key="k",
        bearer_token="b",
        db_dir=tmp_path,
    )


def _job_sessions_row(s: LocalScheduler, job_id: str, created_at: str):
    db = sqlite3.connect(str(s.path))
    db.row_factory = sqlite3.Row
    try:
        return db.execute(
            "SELECT * FROM job_sessions WHERE job_id = ? AND created_at = ?",
            (job_id, created_at),
        ).fetchone()
    finally:
        db.close()


def _occurrence(s: LocalScheduler, job_id: str):
    db = sqlite3.connect(str(s.path))
    db.row_factory = sqlite3.Row
    try:
        return db.execute("SELECT * FROM occurrences WHERE job_id = ?", (job_id,)).fetchone()
    finally:
        db.close()


def _park_occurrence(s: LocalScheduler, job, task_id: str = "task-parked") -> None:
    """Durably mark ``job``'s current fire as accepted-and-unresolved — the shape a
    parked (input-required) or still-running fire leaves in the ledger."""
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


@pytest.fixture(autouse=True)
def _clean_inbox_watch_state():
    """The in-process now-item turn watch is module-global; never leak it across tests."""
    import server.a2a as a2a

    for store in (a2a._INBOX_TURN_WATCH, a2a._INBOX_EARLY_TERMINALS, a2a._INBOX_RESTORE_OWED):
        store.clear()
    yield
    for store in (a2a._INBOX_TURN_WATCH, a2a._INBOX_EARLY_TERMINALS, a2a._INBOX_RESTORE_OWED):
        store.clear()


# ── producers: independent work gets a job-owned session ─────────────────────────


@pytest.mark.asyncio
async def test_cron_schedule_tool_routes_to_a_job_session_with_chat_origin(tmp_path):
    """The real schedule_task tool + real scheduler: a cron from a chat executes on an
    isolated ``job:`` session (never the caller chat, never Activity) while its
    ``origin_session`` still points result delivery at the creating chat."""
    from tools.lg_tools import _build_scheduler_tools

    s = _make_scheduler(tmp_path)
    schedule_task = next(t for t in _build_scheduler_tools(s) if t.name == "schedule_task")
    out = await schedule_task.ainvoke(
        {"prompt": "sweep the queue", "when": "0 9 * * *", "state": {"session_id": "chat-origin-1"}}
    )
    assert "Scheduled job" in out
    job = next(j for j in s.list_jobs())
    assert job.context_id.startswith("job:")
    assert job.context_id != ACTIVITY_CONTEXT
    assert job.origin_session == "chat-origin-1"


@pytest.mark.asyncio
async def test_one_shot_schedule_tool_stays_a_same_chat_continuation(tmp_path):
    """ADR 0129 keeps the deliberate continuations: a one-shot scheduled from a chat
    still resumes THAT chat (ADR 0053), not an isolated session."""
    from tools.lg_tools import _build_scheduler_tools

    s = _make_scheduler(tmp_path)
    schedule_task = next(t for t in _build_scheduler_tools(s) if t.name == "schedule_task")
    await schedule_task.ainvoke(
        {"prompt": "check the deploy", "when": _FUTURE_ISO, "state": {"session_id": "chat-origin-2"}}
    )
    job = next(j for j in s.list_jobs())
    assert job.context_id == "chat-origin-2"
    assert job.origin_session == "chat-origin-2"


def test_independent_producers_get_distinct_job_sessions(tmp_path, monkeypatch):
    """Two independent background enqueues — a react_on-style Activity default and a
    second unrelated one — isolate from each other AND from the Activity context."""
    import runtime.state as rs
    from graph.sdk import run_in_session

    s = _make_scheduler(tmp_path)
    monkeypatch.setattr(rs.STATE, "scheduler", s, raising=False)
    assert run_in_session(ACTIVITY_CONTEXT, "react to the deploy", job_id="rule-deploy")["ok"]
    assert run_in_session(ACTIVITY_CONTEXT, "react to the pager", job_id="rule-pager")["ok"]
    a, b = (s.get_job("rule-deploy"), s.get_job("rule-pager"))
    assert a.context_id.startswith("job:") and b.context_id.startswith("job:")
    assert a.context_id != b.context_id
    assert ACTIVITY_CONTEXT not in (a.context_id, b.context_id)


def test_react_on_activity_default_enqueues_an_isolated_job_session(tmp_path, monkeypatch):
    """Through the real react_on glue: the Activity DEFAULT requests an isolated owner
    (ADR 0129) while an explicit chat target remains a deliberate continuation."""
    import runtime.state as rs
    from graph.plugins.host import HOST
    from graph.sdk import react_on

    s = _make_scheduler(tmp_path)
    monkeypatch.setattr(rs.STATE, "scheduler", s, raising=False)
    handlers: dict = {}

    def _subscribe(topic, handler):
        handlers[topic] = handler
        return lambda: None

    monkeypatch.setattr(HOST, "on", _subscribe)

    unsub = react_on("ci.finished", prompt=lambda ev: "evaluate the build", job_id="ci-rule")
    handlers["ci.finished"]({"event": "ci.finished", "data": {}, "seq": 1})
    isolated = s.get_job("ci-rule")
    assert isolated.context_id.startswith("job:")
    assert isolated.context_id != ACTIVITY_CONTEXT
    unsub()

    unsub = react_on("ci.failed", prompt=lambda ev: "tell the chat", job_id="ci-chat-rule", session="chat-9")
    handlers["ci.failed"]({"event": "ci.failed", "data": {}, "seq": 2})
    assert s.get_job("ci-chat-rule").context_id == "chat-9"
    unsub()


# ── wait ownership: interactive stays in-chat, background stays on its owner ─────


def test_interactive_wait_keeps_the_chat_and_background_wait_stays_on_its_owner(tmp_path, monkeypatch):
    """ADR 0053 + ADR 0129: a wait from a chat resumes that chat under the one-pending-
    wait-per-thread key; a wait from a background job turn resumes on the OWNING job's
    execution session (``wait:<execution_session>``), rooted at the owner's generation —
    and two jobs' waits cannot replace each other."""
    import runtime.state as rs
    from tools.lg_tools import _build_scheduler_tools

    s = _make_scheduler(tmp_path)
    monkeypatch.setattr(rs.STATE, "scheduler", s, raising=False)
    wait = next(t for t in _build_scheduler_tools(s) if t.name == "wait")

    # Interactive: deliberate same-chat continuation, ADR 0053 untouched.
    out = asyncio.run(wait.ainvoke({"seconds": 60, "then": "re-check", "state": {"session_id": "chat-11"}}))
    assert out.startswith("Wait scheduled:")
    chat_wait = s.get_job("wait:chat-11")
    assert chat_wait.context_id == "chat-11"

    # Background: two independent cron jobs own two distinct execution sessions.
    root_a = s.add_job("sweep a", "0 9 * * *", job_id="cron-a")
    root_b = s.add_job("sweep b", "0 9 * * *", job_id="cron-b")
    assert root_a.context_id.startswith("job:") and root_b.context_id.startswith("job:")
    asyncio.run(wait.ainvoke({"seconds": 60, "then": "resume a", "state": {"session_id": root_a.context_id}}))
    asyncio.run(wait.ainvoke({"seconds": 60, "then": "resume b", "state": {"session_id": root_b.context_id}}))

    wait_a = s.get_job(f"wait:{root_a.context_id}")
    wait_b = s.get_job(f"wait:{root_b.context_id}")
    # Each background wait resumes on its OWNING job's session…
    assert wait_a.context_id == root_a.context_id
    assert wait_b.context_id == root_b.context_id
    # …rooted at the owner's generation (a continuation, not a new independent root)…
    row = _job_sessions_row(s, wait_a.id, wait_a.created_at)
    assert (row["root_job_id"], row["root_created_at"]) == ("cron-a", root_a.created_at)
    # …and neither superseded the other or the interactive chat wait.
    assert s.get_job("wait:chat-11") is not None
    assert wait_a.id != wait_b.id

    # A wait from an Activity-context turn gets a live isolated owner — never a hold,
    # never the shared Activity session (the wait tool reported success already).
    asyncio.run(wait.ainvoke({"seconds": 60, "then": "follow up", "state": {"session_id": ACTIVITY_CONTEXT}}))
    legacy_shaped = s.get_job(f"wait:{ACTIVITY_CONTEXT}")
    assert legacy_shaped.context_id.startswith("job:")
    assert legacy_shaped.execution_hold is None


def test_watch_created_by_a_job_turn_fires_back_into_its_owner(tmp_path, monkeypatch):
    """A watch armed during a background job's turn stamps that job's execution session
    as ``run_session``; its reaction (the real run_in_session path the controller calls)
    keeps that context and roots at the owner's generation."""
    import runtime.state as rs
    from graph.sdk import run_in_session

    s = _make_scheduler(tmp_path)
    monkeypatch.setattr(rs.STATE, "scheduler", s, raising=False)
    owner = s.add_job("poll the deploy", "0 9 * * *", job_id="cron-owner")
    assert owner.context_id.startswith("job:")

    res = run_in_session(owner.context_id, "Watch `w9` tripped: deploy done.", job_id="watch-w9")
    assert res["ok"]
    reaction = s.get_job("watch-w9")
    assert reaction.context_id == owner.context_id
    row = _job_sessions_row(s, reaction.id, reaction.created_at)
    assert (row["root_job_id"], row["root_created_at"]) == ("cron-owner", owner.created_at)


# ── parked jobs cannot be superseded ─────────────────────────────────────────────


def test_unrelated_watch_fire_cannot_supersede_a_parked_job(tmp_path, monkeypatch):
    """One watch reaction is parked (accepted occurrence, task handle, unresolved). An
    UNRELATED watch fires: it gets its own owner and claims normally — it neither joins
    nor disturbs the parked owner. A re-fire of the SAME watch replaces only the pending
    row; the parked occurrence survives byte-for-byte and the replacement queues behind
    the same owner (claim refused while the predecessor is unresolved)."""
    import runtime.state as rs
    from graph.sdk import run_in_session

    s = _make_scheduler(tmp_path)
    monkeypatch.setattr(rs.STATE, "scheduler", s, raising=False)

    assert run_in_session(ACTIVITY_CONTEXT, "react alpha", job_id="watch-alpha")["ok"]
    alpha_1 = s.get_job("watch-alpha")
    _park_occurrence(s, alpha_1, task_id="task-alpha-parked")

    # Unrelated watch: fresh owner, normal claim — the parked job holds only its own owner.
    assert run_in_session(ACTIVITY_CONTEXT, "react beta", job_id="watch-beta")["ok"]
    beta = s.get_job("watch-beta")
    assert beta.context_id.startswith("job:")
    assert beta.context_id != alpha_1.context_id
    assert s._claim_occurrence(beta) is not None

    # Same watch re-fires: the replacement rides the SAME owner, queued behind the
    # accepted predecessor — its claim is refused while the park is unresolved.
    assert run_in_session(ACTIVITY_CONTEXT, "react alpha again", job_id="watch-alpha")["ok"]
    alpha_2 = s.get_job("watch-alpha")
    assert alpha_2.created_at != alpha_1.created_at  # a distinct generation
    assert alpha_2.context_id == alpha_1.context_id  # …behind the same owner
    assert s._claim_occurrence(alpha_2) is None

    parked = _occurrence(s, "watch-alpha")
    assert parked["created_at"] == alpha_1.created_at
    assert parked["state"] == "claimed"
    assert parked["task_id"] == "task-alpha-parked"
    assert parked["message_id"] == f"msg-{alpha_1.id}"


# ── inbox: now + next (retry/recovery) delivery under one durable identity ───────


def _a2a_capture_app(posted: list, task_id: str = "task-ib-1", state: str = "TASK_STATE_SUBMITTED") -> FastAPI:
    app = FastAPI()

    @app.post("/a2a")
    async def _a2a(body: dict):
        posted.append(body)
        return {"result": {"task": {"id": task_id, "status": {"state": state}}}}

    return app


@pytest.mark.asyncio
async def test_inbox_now_fire_uses_a_durable_per_item_job_session(tmp_path, monkeypatch):
    """Distinct items get distinct isolated sessions; a retry of ONE item re-presents
    the SAME ``(contextId, messageId)`` so the A2A layer joins the accepted turn
    instead of processing the item twice. Provenance metadata is unchanged."""
    import runtime.state as rs
    import server.a2a as a2a
    from inbox import InboxStore

    store = InboxStore(str(tmp_path / "inbox.db"))
    posted: list = []
    monkeypatch.setattr(rs.STATE, "inbox_store", store, raising=False)
    monkeypatch.setattr(rs.STATE, "fastapi_app", _a2a_capture_app(posted), raising=False)
    monkeypatch.setattr(rs.STATE, "active_port", 9, raising=False)

    first = store.add("deploy done", priority="now", source="ci")
    second = store.add("disk is full", priority="now", source="pager")

    assert await a2a._fire_activity_from_inbox(first) is True
    assert await a2a._fire_activity_from_inbox(second) is True
    assert await a2a._fire_activity_from_inbox(first) is True  # a retry of the first

    msgs = [p["params"]["message"] for p in posted]
    assert all(m["contextId"].startswith("job:") for m in msgs)
    assert msgs[0]["contextId"] != msgs[1]["contextId"]  # distinct items, distinct sessions
    # The retry reuses the item's stable identity — context AND message id.
    assert msgs[2]["contextId"] == msgs[0]["contextId"]
    assert msgs[2]["messageId"] == msgs[0]["messageId"]
    assert msgs[0]["metadata"] == {
        "origin": "inbox",
        "inbox_id": first["id"],
        "inbox_source": "ci",
        "priority": "now",
        # created_at rides on every inbox message now (#5241); a just-queued item carries
        # its stamp but is well within the late-delivery threshold, so its text is unchanged.
        "created_at": first["created_at"],
    }
    assert msgs[0]["parts"] == [{"text": "deploy done"}]
    # Acceptance was recorded durably on the identity.
    assert store.execution_identity(first["id"])["task_id"] == "task-ib-1"


@pytest.mark.asyncio
async def test_recovery_redelivery_reuses_the_identity_of_the_first_send(tmp_path, monkeypatch):
    """The actual next-delivery drain (startup recovery): an item whose first accepted
    send was never marked delivered is re-delivered by ``recover_pending_now_inbox_items``
    with the SAME context and message id — the no-duplicate-processing guarantee — and
    then marked delivered exactly once."""
    import runtime.state as rs
    import server.a2a as a2a
    from inbox import InboxStore
    from server.agent_init import recover_pending_now_inbox_items

    store = InboxStore(str(tmp_path / "inbox.db"))
    posted: list = []
    monkeypatch.setattr(rs.STATE, "inbox_store", store, raising=False)
    monkeypatch.setattr(rs.STATE, "fastapi_app", _a2a_capture_app(posted), raising=False)
    monkeypatch.setattr(rs.STATE, "active_port", 9, raising=False)
    monkeypatch.setattr(rs.STATE, "storm_guard", None, raising=False)
    monkeypatch.setattr(rs.STATE, "inbox_now_delivery", a2a._fire_activity_from_inbox, raising=False)

    item = store.add("prod is down", priority="now", source="pager")
    # First delivery accepted, but the process "died" before mark_delivered.
    assert await a2a._fire_activity_from_inbox(item) is True
    assert len(posted) == 1

    result = await recover_pending_now_inbox_items(limit=8)
    assert result["accepted"] == 1
    assert len(posted) == 2
    first, second = (p["params"]["message"] for p in posted)
    assert second["contextId"] == first["contextId"]
    assert second["messageId"] == first["messageId"]
    assert store.list(priority_floor="later") == []  # delivered, nothing left pending


@pytest.mark.asyncio
async def test_a_terminally_failed_inbox_turn_retires_its_identity_for_a_fresh_retry(tmp_path, monkeypatch):
    """Identity is stable across UNCERTAIN sends but retired once the invocation
    provably settled: after the watched turn fails and the item is restored, the next
    delivery runs as a fresh invocation under a new session + message id (a retired
    execution UUID is never reused)."""
    import runtime.state as rs
    import server.a2a as a2a
    from inbox import InboxStore

    store = InboxStore(str(tmp_path / "inbox.db"))
    posted: list = []
    monkeypatch.setattr(rs.STATE, "inbox_store", store, raising=False)
    monkeypatch.setattr(rs.STATE, "fastapi_app", _a2a_capture_app(posted), raising=False)
    monkeypatch.setattr(rs.STATE, "active_port", 9, raising=False)
    monkeypatch.setattr(a2a._event_bus, "publish", lambda *_a, **_k: None)

    item = store.add("flaky page", priority="now", source="ci")
    assert await a2a._fire_activity_from_inbox(item) is True
    store.mark_delivered([item["id"]])

    # The watched turn ends terminally failed → restored once, identity retired.
    a2a._watch_inbox_turn("task-ib-1", item)
    a2a._inbox_turn_ended(type("O", (), {"origin": "inbox", "task_id": "task-ib-1", "state": "failed"})())
    for _ in range(200):
        if store.list(priority_floor="later"):
            break
        await asyncio.sleep(0.01)
    assert [i["id"] for i in store.list(priority_floor="later")] == [item["id"]]

    assert await a2a._fire_activity_from_inbox(item) is True
    first, retry = (p["params"]["message"] for p in posted)
    assert retry["contextId"].startswith("job:")
    assert retry["contextId"] != first["contextId"]  # a fresh invocation, not the dead task
    assert retry["messageId"] != first["messageId"]


# ── terminal routing: feed + origin reports by job provenance ────────────────────


def _outcome(**kw):
    from a2a_impl.executor import TurnOutcome

    base = dict(task_id="t-1", context_id="job:abc", state="completed", text="<output>done</output>")
    base.update(kw)
    return TurnOutcome(**base)


def _published(monkeypatch):
    import server.a2a as a2a

    events: list = []
    monkeypatch.setattr(a2a._event_bus, "publish", lambda topic, data, **_kw: events.append((topic, data)))
    return events


def test_scheduler_job_turn_feeds_activity_and_reports_to_its_origin_chat(tmp_path, monkeypatch):
    """A cron fire on its isolated session is NOT a resumed chat: no ``chat.resumed``.
    It appends an attributed feed entry — real execution context, job identity, origin
    chat — and still delivers the origin-chat report card (scheduler.completed)."""
    import runtime.state as rs
    import server.a2a as a2a
    from activity import ActivityLog

    s = _make_scheduler(tmp_path)
    job = s.add_job("sweep the queue", "0 9 * * *", job_id="cron-5", origin_session="chat-origin-5")
    assert job.context_id.startswith("job:")
    log = ActivityLog(str(tmp_path / "activity.db"))
    monkeypatch.setattr(rs.STATE, "scheduler", s, raising=False)
    monkeypatch.setattr(rs.STATE, "activity_log", log, raising=False)
    events = _published(monkeypatch)

    a2a._a2a_terminal(
        _outcome(context_id=job.context_id, origin="scheduler", trigger="cron-5", stimulus="sweep the queue")
    )

    assert not any(t == "chat.resumed" for t, _ in events)
    row = log.recent()[0]
    assert row["context_id"] == job.context_id  # deep-links the actual execution
    assert row["origin"] == "scheduler" and row["trigger"] == "cron-5"
    assert row["origin_session"] == "chat-origin-5"
    feed = next(d for t, d in events if t == "activity.message")
    assert feed["context_id"] == job.context_id
    assert feed["origin_session"] == "chat-origin-5"
    card = next(d for t, d in events if t == "scheduler.completed")
    assert card["origin_session"] == "chat-origin-5"
    assert card["activity_context"] == job.context_id
    assert card["job_id"] == "cron-5"


def test_job_turn_failure_reads_as_failed_on_the_feed(tmp_path, monkeypatch):
    """A job turn that died saying nothing still lands on the feed, labelled as the
    failure it was — it must not vanish or read as a success."""
    import runtime.state as rs
    from activity import ActivityLog

    import server.a2a as a2a

    log = ActivityLog(str(tmp_path / "activity.db"))
    monkeypatch.setattr(rs.STATE, "activity_log", log, raising=False)
    monkeypatch.setattr(rs.STATE, "scheduler", None, raising=False)
    events = _published(monkeypatch)

    a2a._a2a_terminal(
        _outcome(state="failed", text="", error="recursion limit reached", origin="watch", trigger="watch-w1")
    )

    assert not any(t == "chat.resumed" for t, _ in events)
    row = log.recent()[0]
    assert row["state"] == "failed"
    assert "Background job turn failed" in row["text"] and "recursion limit" in row["text"]


def test_inbox_job_turn_feeds_without_misclassifying_as_a_chat_resume(tmp_path, monkeypatch):
    """An inbox now-item's job turn appends its summary to the feed under its own
    execution context — never chat.resumed, never a scheduler report card."""
    import runtime.state as rs
    from activity import ActivityLog

    import server.a2a as a2a

    log = ActivityLog(str(tmp_path / "activity.db"))
    monkeypatch.setattr(rs.STATE, "activity_log", log, raising=False)
    monkeypatch.setattr(rs.STATE, "scheduler", None, raising=False)
    events = _published(monkeypatch)

    a2a._a2a_terminal(
        _outcome(context_id="job:inbox-item-7", origin="inbox", trigger="pager", priority="now", stimulus="prod down")
    )

    topics = [t for t, _ in events]
    assert "chat.resumed" not in topics
    assert "scheduler.completed" not in topics
    row = log.recent()[0]
    assert row["context_id"] == "job:inbox-item-7"
    assert row["origin"] == "inbox" and row["priority"] == "now"


def test_chat_session_turns_still_surface_as_resumed_chat(monkeypatch):
    """The deliberate continuations are untouched: a scheduler fire that ran IN a chat
    session (a wait resume, a same-chat one-shot) still publishes chat.resumed."""
    import runtime.state as rs

    import server.a2a as a2a

    monkeypatch.setattr(rs.STATE, "activity_log", None, raising=False)
    monkeypatch.setattr(rs.STATE, "scheduler", None, raising=False)
    events = _published(monkeypatch)

    a2a._a2a_terminal(_outcome(context_id="chat-42", origin="scheduler", trigger="wait:chat-42", text="resumed"))

    resumed = next(d for t, d in events if t == "chat.resumed")
    assert resumed["session_id"] == "chat-42"


def test_no_report_card_when_a_schedule_was_created_by_another_job(tmp_path, monkeypatch):
    """A cron created FROM a background job session has no chat reading its origin —
    no scheduler.completed ghost card; the feed entry carries the result."""
    import runtime.state as rs

    import server.a2a as a2a

    s = _make_scheduler(tmp_path)
    parent = s.add_job("parent", "0 9 * * *", job_id="cron-parent")
    child = s.add_job("child sweep", "0 9 * * *", job_id="cron-child", origin_session=parent.context_id)
    monkeypatch.setattr(rs.STATE, "scheduler", s, raising=False)
    monkeypatch.setattr(rs.STATE, "activity_log", None, raising=False)
    events = _published(monkeypatch)

    a2a._a2a_terminal(_outcome(context_id=child.context_id, origin="scheduler", trigger="cron-child"))

    assert not any(t == "scheduler.completed" for t, _ in events)
    assert not any(t == "chat.resumed" for t, _ in events)


def test_scheduler_lifecycle_events_carry_job_identity_and_origin(tmp_path, monkeypatch):
    """turn.started / turn.finished for a fire carry execution session, job identity
    (trigger) and origin attribution, so lifecycle consumers need no job lookup."""
    import httpx

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"result": {"status": {"state": "TASK_STATE_COMPLETED"}}}

    class _Client:
        def __init__(self, **_kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def post(self, *_a, **_kw):
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    events: list = []
    s = LocalScheduler(
        agent_name="jobs-test",
        invoke_url="http://127.0.0.1:7870",
        db_dir=tmp_path,
        event_publish=lambda t, d: events.append((t, d)),
    )
    job = s.add_job("sweep", "0 9 * * *", job_id="cron-lc", origin_session="chat-lc")
    asyncio.run(s._fire(job))

    started = next(d for t, d in events if t == "turn.started")
    assert started["session_id"] == job.context_id
    assert started["trigger"] == "cron-lc"
    assert started["origin_session"] == "chat-lc"
