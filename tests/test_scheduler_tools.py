"""`schedule_task` stamps the originating chat session onto ONE-SHOT jobs so the
fire resumes that conversation (same-session resume, ADR 0053) — while cron jobs
deliberately stay context-free and land in the Activity thread (#2939)."""

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

    def add_job(self, prompt, schedule, *, job_id=None, timezone=None, context_id=None):
        self.last_context_id = context_id
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
