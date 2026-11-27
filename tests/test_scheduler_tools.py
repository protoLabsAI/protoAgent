"""`schedule_task` stamps the originating chat session onto ONE-SHOT jobs so the
fire resumes that conversation (same-session resume, ADR 0053) — while cron jobs
deliberately stay context-free and land in the Activity thread (#2939).

It ALSO stamps ``origin_session`` on BOTH crons and one-shots (#2990): distinct from
``context_id`` (where the fire runs), it's where the fire's RESULT is delivered — so
even a cron firing into Activity reports back to the chat that created it."""

from __future__ import annotations

import pytest

from tools.lg_tools import _build_scheduler_tools


@pytest.fixture
def work_contexts(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from background.store import BackgroundStore
    from inbox.store import InboxStore
    from runtime.state import STATE
    from scheduler.local import LocalScheduler

    inbox = InboxStore(str(tmp_path / "inbox.db"))
    scheduler = LocalScheduler("origin-test", invoke_url="http://test", db_dir=tmp_path / "scheduler")
    background = BackgroundStore(str(tmp_path / "background.db"))
    monkeypatch.setattr(STATE, "inbox_store", inbox)
    monkeypatch.setattr(STATE, "scheduler", scheduler)
    monkeypatch.setattr(STATE, "background_mgr", SimpleNamespace(store=background))
    item = inbox.add("work", priority="now", origin_session="chat-a")
    cron = scheduler.add_job("work", "0 * * * *", origin_session="chat-a")
    bg = background.create(agent_name="origin-test", origin_session="chat-a",
                           subagent_type="test", description="work", prompt="work")
    return {
        "inbox": inbox.execution_identity(item["id"])["context_id"],
        "cron": cron.context_id,
        "background": f"background:{bg}",
        "chat": "chat-a",
        "missing_job": "job:missing",
        "missing_background": "background:missing",
    }


@pytest.mark.parametrize("source, expected", [
    ("inbox", "chat-a"), ("cron", "chat-a"), ("background", "chat-a"),
    ("chat", "chat-a"), ("missing_job", ""), ("missing_background", ""),
])
def test_task_create_binds_to_causing_conversation(work_contexts, tmp_path, source, expected):
    from tasks.store import TaskStore
    from tools.scheduler_tools import _build_task_tools

    store = TaskStore(str(tmp_path / "tasks.db"))
    create = next(t for t in _build_task_tools(store) if t.name == "task_create")
    out = create.invoke({"title": "Follow up", "state": {"session_id": work_contexts[source]}})
    assert out.startswith("Created ")
    assert store.list()[0]["session_id"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("source, expected", [
    ("inbox", "chat-a"), ("cron", "chat-a"), ("background", "chat-a"),
    ("missing_job", None), ("missing_background", None),
])
@pytest.mark.parametrize("when", ["2099-05-01T15:00:00+00:00", "0 9 * * *"])
async def test_schedule_inherits_origin_without_changing_execution(work_contexts, source, expected, when):
    context = work_contexts[source]
    sched = _FakeScheduler()
    out = await _schedule_tool(sched).ainvoke(
        {"prompt": "Follow up", "when": when, "state": {"session_id": context}}
    )
    assert out.startswith("Scheduled job ")
    assert sched.last_origin_session == expected
    assert sched.last_context_id == (None if when == "0 9 * * *" else context)


class _FakeJob:
    def __init__(self, next_fire: str):
        self.id = "job-1"
        self.next_fire = next_fire


class _FakeScheduler:
    def __init__(self):
        self.last_context_id: str | None = "__unset__"
        self.last_origin_session: str | None = "__unset__"
        self.last_ttl: str | None = "__unset__"
        self.last_max_fires: int | None = "__unset__"

    def add_job(
        self,
        prompt,
        schedule,
        *,
        job_id=None,
        timezone=None,
        context_id=None,
        origin_session=None,
        ttl=None,
        max_fires=None,
    ):
        self.last_context_id = context_id
        self.last_origin_session = origin_session
        self.last_ttl = ttl
        self.last_max_fires = max_fires
        return _FakeJob(schedule)

    def cancel_job(self, job_id):
        return False

    def list_jobs(self):
        return []


def _schedule_tool(sched):
    return next(t for t in _build_scheduler_tools(sched) if t.name == "schedule_task")


def _wait_tool(sched):
    return next(t for t in _build_scheduler_tools(sched) if t.name == "wait")


@pytest.mark.asyncio
async def test_one_shot_schedule_carries_the_originating_session():
    # The fix (#2939): a one-shot fire resumes the chat that scheduled it — the
    # session id from the injected graph state rides the job as ``context_id``,
    # exactly the `wait` pattern. Passing `state` directly mirrors what the
    # ToolNode injects at runtime.
    sched = _FakeScheduler()
    out = await _schedule_tool(sched).ainvoke(
        {"prompt": "check the deploy", "when": "2099-05-01T15:00:00+00:00", "state": {"session_id": "chat-xyz"}}
    )
    assert "Scheduled job" in out
    assert sched.last_context_id == "chat-xyz"


@pytest.mark.asyncio
async def test_cron_schedule_carries_no_context_even_with_a_session():
    # Crons must NOT resume a chat: a recurring job firing into a conversation the
    # operator closed days ago is wrong — recurring work belongs in Activity, so
    # the session id is deliberately dropped for cron schedules.
    sched = _FakeScheduler()
    out = await _schedule_tool(sched).ainvoke(
        {"prompt": "summarize logs", "when": "0 9 * * 1-5", "state": {"session_id": "chat-xyz"}}
    )
    assert "Scheduled job" in out
    assert sched.last_context_id is None


@pytest.mark.asyncio
async def test_one_shot_without_a_session_leaves_context_id_none(monkeypatch):
    # No injected state and no contextvar (e.g. an Activity-origin turn): the job
    # carries no context and the scheduler falls back to the Activity thread.
    import observability.tracing as tracing

    monkeypatch.setattr(tracing, "current_session_id", lambda: "")
    sched = _FakeScheduler()
    await _schedule_tool(sched).ainvoke({"prompt": "ping", "when": "2099-05-01T15:00:00+00:00"})
    assert sched.last_context_id is None


# ── #2990: origin_session stamping (where the RESULT is delivered) ──────────────


@pytest.mark.asyncio
async def test_cron_schedule_stamps_origin_session_for_result_delivery():
    # r1: a cron created from a chat stamps origin_session even though context_id
    # stays None — the fire runs in Activity, but its result is delivered back to
    # this chat as a ScheduledReportCard.
    sched = _FakeScheduler()
    await _schedule_tool(sched).ainvoke(
        {"prompt": "summarize logs", "when": "0 9 * * 1-5", "state": {"session_id": "chat-xyz"}}
    )
    assert sched.last_context_id is None  # fire still runs in Activity
    assert sched.last_origin_session == "chat-xyz"  # result comes back HERE


@pytest.mark.asyncio
async def test_one_shot_schedule_stamps_origin_session():
    # r1: one-shots stamp both — context_id (resume in-thread) and origin_session.
    sched = _FakeScheduler()
    await _schedule_tool(sched).ainvoke(
        {"prompt": "check the deploy", "when": "2099-05-01T15:00:00+00:00", "state": {"session_id": "chat-xyz"}}
    )
    assert sched.last_origin_session == "chat-xyz"


@pytest.mark.asyncio
async def test_schedule_without_a_session_leaves_origin_session_none(monkeypatch):
    # r6: no chat context (e.g. an Activity-origin turn) → origin_session None → no
    # delivery is attempted (backward compatible).
    import observability.tracing as tracing

    monkeypatch.setattr(tracing, "current_session_id", lambda: "")
    sched = _FakeScheduler()
    await _schedule_tool(sched).ainvoke({"prompt": "sweep", "when": "0 9 * * *"})
    assert sched.last_origin_session is None


# ── #4651: tell the user what failed / what will run before ending the turn ────────────
#
# QA saw an agent whose ENTIRE reply after image calls 524'd was "I'll pick this back up in
# 5 minutes" — the user was never told anything had failed. The guidance that fixes this has
# to land where the model actually reads it:
#   * ``schedule_task`` does NOT yield — the model is re-invoked after it and reads its RESULT,
#     so the guidance rides the result (and is stripped from the user-facing silent-turn
#     fallback by ``schedule_turn_reply``).
#   * ``wait`` DOES yield — ``WaitYieldMiddleware`` jumps the turn to ``end`` the moment the
#     successful wait result lands, so the model never reads that result this turn. Its
#     guidance therefore lives in the ``wait`` DOCSTRING (read before the tool is called); its
#     result stays clean.
# The machine prefixes ("Scheduled job … next at …", "Wait scheduled: …") that callers parse
# must stay intact.


@pytest.mark.asyncio
async def test_one_shot_schedule_result_appends_turn_ending_guidance():
    # r1: one-shot keeps the existing prefix and gains the failure/what-runs guidance. The model
    # reads this because schedule_task does not yield the turn.
    from tools.scheduler_tools import _ONESHOT_END_TURN_GUIDANCE

    sched = _FakeScheduler()
    out = await _schedule_tool(sched).ainvoke(
        {"prompt": "retry the upload", "when": "2099-05-01T15:00:00+00:00", "state": {"session_id": "chat-1"}}
    )
    assert out.startswith("Scheduled job")  # prefix preserved for existing callers
    assert out.endswith(_ONESHOT_END_TURN_GUIDANCE)
    assert "what failed" in out
    assert 'never reply with only "I\'ll pick this back up"' in out
    # The same rule is in the docstring (the tool description the model reads before calling).
    doc = " ".join(_schedule_tool(sched).description.lower().split())
    assert "what failed and what will run" in doc
    assert 'never reply with only "i\'ll pick this back up"' in doc


@pytest.mark.asyncio
async def test_cron_schedule_result_keeps_prefix_with_lighter_guidance():
    # r1: cron still starts with "Scheduled job" but carries the lighter wording — a
    # recurring sweep isn't a retry-after-failure, so no "what failed" phrasing.
    from tools.scheduler_tools import _CRON_END_TURN_GUIDANCE

    sched = _FakeScheduler()
    out = await _schedule_tool(sched).ainvoke({"prompt": "summarize logs", "when": "0 9 * * 1-5"})
    assert out.startswith("Scheduled job")
    assert out.endswith(_CRON_END_TURN_GUIDANCE)
    assert "what failed" not in out
    assert "scheduled" in out.lower()


@pytest.mark.asyncio
async def test_schedule_task_result_fallback_strips_model_only_guidance():
    # r1 / round-1 minor: if the turn ends silently right after schedule_task, the raw result is
    # the user-facing reply — so the model-only guidance must be stripped back to the clean
    # "Scheduled job … next at …" line the operator used to see.
    from graph.middleware.wait_yield import schedule_turn_reply

    sched = _FakeScheduler()
    out = await _schedule_tool(sched).ainvoke(
        {"prompt": "retry the upload", "when": "2099-05-01T15:00:00+00:00", "state": {"session_id": "chat-1"}}
    )
    cleaned = schedule_turn_reply(out)
    assert cleaned == "Scheduled job job-1 next at 2099-05-01T15:00:00+00:00."
    assert "Before ending your turn" not in cleaned


@pytest.mark.asyncio
async def test_wait_result_stays_clean_and_guidance_is_in_the_docstring():
    # r2: ``wait`` yields the turn, so the model never reads the wait RESULT that turn — the
    # result stays the clean "Wait scheduled: … Will resume to: …" status with NO model-only
    # text to leak. The guidance that actually reaches the model (before it calls the tool)
    # lives in the docstring, which is the tool description the model sees.
    sched = _FakeScheduler()
    out = await _wait_tool(sched).ainvoke({"seconds": 300, "then": "check the upload", "summary": "Started the work.", "state": {}})
    assert out == "Wait scheduled: 5 minutes.\nFor the user: Started the work.\nWill resume to: check the upload"
    assert "Before ending your turn" not in out
    assert "I'll pick this back up" not in out

    # Normalize whitespace: the docstring wraps the guidance across lines.
    doc = " ".join(_wait_tool(sched).description.lower().split())
    assert "why you're waiting" in doc
    assert "what happens on resume" in doc
    assert 'never reply with only "i\'ll pick this back up"' in doc


def test_wait_result_stays_parseable_by_wait_turn_reply():
    # The clean wait result is still what the middleware parses the duration out of for the
    # silent-turn fallback (coupling guard for #4651).
    import asyncio

    from graph.middleware.wait_yield import wait_turn_reply

    sched = _FakeScheduler()
    out = asyncio.run(_wait_tool(sched).ainvoke({"seconds": 2700, "then": "Check the board.", "summary": "Started the work.", "state": {}}))
    assert wait_turn_reply(out) == "Started the work. I'll pick this back up in 45 minutes."


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["59/15 * * * *", "*/15 * * * *", "0 9 * * 1", "15 7,12 * * *"])
async def test_schedule_cron_ambiguity_guard(when):
    sched = _FakeScheduler()
    out = await _schedule_tool(sched).ainvoke({"prompt": "Follow up", "when": when})
    if when.startswith("59/"):
        assert "`*/15` for every 15 minutes" in out
        assert "`59` for minute 59 only" in out
        assert sched.last_context_id == "__unset__"
    else:
        assert out.startswith("Scheduled job ")


# ── #5313: plugin-owned jobs are refused by cancel_schedule and flagged in list_schedules ──


class _ListedJob:
    def __init__(self, job_id: str):
        self.id = job_id
        self.next_fire = "2099-05-01T15:00:00+00:00"
        self.schedule = "0 * * * *"
        self.prompt = "sweep"


class _CancelRecordingScheduler(_FakeScheduler):
    def __init__(self, job_ids=()):
        super().__init__()
        self.canceled: list[str] = []
        self._job_ids = list(job_ids)

    def cancel_job(self, job_id):
        self.canceled.append(job_id)
        return True

    def list_jobs(self):
        return [_ListedJob(i) for i in self._job_ids]


def _tool(sched, name):
    return next(t for t in _build_scheduler_tools(sched) if t.name == name)


@pytest.fixture
def published(monkeypatch):
    from graph.plugins.host import HOST

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(HOST, "publish", lambda topic, payload: events.append((topic, payload)))
    return events


@pytest.mark.asyncio
@pytest.mark.parametrize("job_id", ["plugin:frank:frank-hourly-sweep", "  plugin:frank:frank-hourly-sweep\n"])
async def test_cancel_schedule_refuses_a_plugin_owned_job(published, job_id):
    # The frank friction: the cancel said "Canceled", the plugin re-armed the job, and the
    # agent told the operator the sweep was gone. Now the scheduler is never touched.
    sched = _CancelRecordingScheduler()
    out = await _tool(sched, "cancel_schedule").ainvoke({"job_id": job_id})
    assert out.startswith("Error:")
    assert "`frank` plugin" in out
    assert "config section (Settings, plugin `frank`)" in out
    assert "Canceled plugin:" not in out
    assert sched.canceled == []
    assert published == []


@pytest.mark.asyncio
async def test_cancel_schedule_refuses_a_plugin_id_with_no_job_segment(published):
    sched = _CancelRecordingScheduler()
    out = await _tool(sched, "cancel_schedule").ainvoke({"job_id": "plugin:"})
    assert out.startswith("Error:") and "owned by a plugin" in out
    assert sched.canceled == []
    assert published == []


@pytest.mark.asyncio
@pytest.mark.parametrize("job_id", ["job-1", "myplugin:frank:sweep", "daily-plugin:report"])
async def test_cancel_schedule_still_cancels_an_agent_job(published, job_id):
    sched = _CancelRecordingScheduler()
    out = await _tool(sched, "cancel_schedule").ainvoke({"job_id": job_id})
    assert out == f"Canceled {job_id}."
    assert sched.canceled == [job_id]
    assert published == [("scheduler.changed", {"job_id": job_id, "action": "canceled"})]


@pytest.mark.asyncio
async def test_list_schedules_marks_only_plugin_owned_rows():
    sched = _CancelRecordingScheduler(["plugin:frank:frank-hourly-sweep", "job-1"])
    out = await _tool(sched, "list_schedules").ainvoke({})
    plugin_row, agent_row = out.splitlines()
    expiry = "ttl=-  max_fires=-  fires=0"
    assert agent_row == f"job-1  next=2099-05-01T15:00:00+00:00  schedule='0 * * * *'  {expiry}  sweep"
    assert plugin_row == (
        f"plugin:frank:frank-hourly-sweep  next=2099-05-01T15:00:00+00:00  schedule='0 * * * *'  {expiry}  sweep"
        "  (plugin-owned: frank)"
    )


def test_plugin_job_owner_tracks_the_sdk_prefix():
    # One format, not two: the tool's check must agree with what the SDK mints.
    from graph.sdk import plugin_job_prefix
    from tools.scheduler_tools import _plugin_job_owner

    assert _plugin_job_owner(f"{plugin_job_prefix('frank')}frank-hourly-sweep") == "frank"
    assert _plugin_job_owner("job-1") is None
