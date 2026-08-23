"""`schedule_task` stamps the originating chat session onto ONE-SHOT jobs so the
fire resumes that conversation (same-session resume, ADR 0053) — while cron jobs
deliberately stay context-free and land in the Activity thread (#2939).

It ALSO stamps ``origin_session`` on BOTH crons and one-shots (#2990): distinct from
``context_id`` (where the fire runs), it's where the fire's RESULT is delivered — so
even a cron firing into Activity reports back to the chat that created it."""

from __future__ import annotations

import pytest

from tools.lg_tools import _build_scheduler_tools


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
