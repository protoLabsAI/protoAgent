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
    out = await _wait_tool(sched).ainvoke({"seconds": 300, "then": "check the upload", "state": {}})
    assert out == "Wait scheduled: 5 minutes. Will resume to: check the upload"
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
    out = asyncio.run(_wait_tool(sched).ainvoke({"seconds": 2700, "then": "Check the board.", "state": {}}))
    assert wait_turn_reply(out) == "I'll pick this back up in 45 minutes."
