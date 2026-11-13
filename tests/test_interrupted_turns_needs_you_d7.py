"""Interrupted turns D7 S7 (ADR 0129, #5149): the Needs-you recovery item.

For each occurrence S6 leaves ``interrupted_needs_operator`` the scheduler raises ONE
server-recorded decision in the job's execution session — Resume / Re-run / Drop, Re-run
and Drop only when no checkpoint holds this fire's turn — and answering it over the real
``/api/decisions`` routes performs the action exactly once (two concurrent answers: one
409). Each S6 auto-resume appends one Activity entry.

Built on the S6 stress harness (``tests/test_interrupted_turns_d7.py``): a real scheduler
over temp durable stores, the production executor/turn driver, a real compiled LangGraph
whose checkpointer and task store drop writes at the simulated SIGTERM, and the production
``InterruptedTurnRecovery`` — only the model and the process death are faked.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

import scheduler.local as scheduler_local
from a2a_impl.stores import RESTART_INTERRUPTED_TEXT
from graph.decisions import restart_recovery_ref
from runtime.state import STATE
from server.interrupted_turns import (
    INTERRUPTED_TOOL_RESULT,
    InterruptedTurnRecovery,
    _dangling_tool_calls,
)
from tests.test_interrupted_turns_d7 import (
    _harness_hygiene,  # noqa: F401 — autouse fixture: resets module globals around each test
    _REAL_ASYNC_CLIENT,
    _TEST_POLL_S,
    _Coordinator,
    _KillableSaver,
    _accepted_unresolved_claim,
    _boot_generation,
    _occurrence,
    _past_iso,
    _query,
    _quiesce_polls,
    _restart,
    _shutdown,
    _sigterm,
    _task_status,
    _wait_task_failed,
    _wait_until,
)


def _api_app():
    """The real decision routes on a bare app — the console's answer path."""
    from fastapi import FastAPI

    from operator_api.decision_routes import register_decision_routes

    app = FastAPI()
    register_decision_routes(app)
    return app


async def _answer(app, decision_id: str, option: str) -> httpx.Response:
    # The REAL client class: _route_httpx_to points the patched one at the A2A app.
    async with _REAL_ASYNC_CLIENT(transport=httpx.ASGITransport(app=app), base_url="http://api") as client:
        return await client.post(f"/api/decisions/{decision_id}/answer", json={"option_id": option})


async def _let_zombies_unwind() -> None:
    """Wait for every other task on the loop to finish before a restart revives the
    shared saver. The killed generation's turn unwinds through ``record_failed_turn``,
    whose write is dropped while the saver is dead — but a slow zombie still unwinding
    when the next generation flips ``saver.dead = False`` would land that write on the
    revived saver and rewrite the thread classification reads. A real SIGTERM takes the
    writer with it; this is purely a harness artifact."""
    me = asyncio.current_task()
    for _ in range(100):
        zombies = [t for t in asyncio.all_tasks() if t is not me and not t.done()]
        if not zombies:
            return
        await asyncio.wait(zombies, timeout=0.1)


async def _fire_and_kill(tmp_path, monkeypatch, agent: str, *, prompt, job_id, ready, checkpointed):
    """One killed first generation: boot, fire ``job_id``, wait for ``ready(coord)``, then
    SIGTERM — but only once ``checkpointed(messages)`` holds on the thread's durable
    checkpoint. ``ready`` alone races the checkpointer: a kill landing before the blocking
    point's write persists leaves an earlier checkpoint shape, which boot classification
    legitimately reads differently."""
    monkeypatch.setattr(scheduler_local, "_POLL_INTERVAL_S", _TEST_POLL_S)
    coord, saver = _Coordinator(), _KillableSaver()
    gen1 = _boot_generation(tmp_path, monkeypatch, coord, saver, agent)
    await gen1.sched.start()
    job = gen1.sched.add_job(prompt, _past_iso(), job_id=job_id)
    await _wait_until(lambda: ready(coord), message=f"{job_id}: the fire reached its blocking point")
    claim = await _accepted_unresolved_claim(gen1.sched, job_id)
    config = InterruptedTurnRecovery()._thread_config(job.context_id)
    for _ in range(500):
        snap = await STATE.graph.aget_state(config)
        if checkpointed((getattr(snap, "values", None) or {}).get("messages") or []):
            break
        await asyncio.sleep(0.02)
    else:
        raise AssertionError(f"{job_id}: the blocking point never reached the checkpoint")
    await _sigterm(coord, saver, gen1)
    await _let_zombies_unwind()
    return coord, saver, claim


async def _held_one_shot(tmp_path, monkeypatch, agent: str):
    """One killed generation mid-tool-call on an ordinary one-shot, restarted: the
    occurrence is held ``interrupted_needs_operator`` and its Needs-you decision raised."""
    coord, saver, claim = await _fire_and_kill(
        tmp_path, monkeypatch, agent, prompt="ONESHOT-TOOL archive the exports",
        job_id="job-oneshot", ready=lambda c: c.tool_runs.get("risky_step") == 1,
        checkpointed=lambda msgs: bool(_dangling_tool_calls(msgs)),  # the call is in flight
    )
    gen2 = await _restart(tmp_path, monkeypatch, coord, saver, agent, release=True)
    # The seam the answer routes reach the server through (wired at boot in production).
    monkeypatch.setattr(STATE, "interrupted_recovery", InterruptedTurnRecovery(), raising=False)
    owner = json.loads(claim["job_snapshot"])["context_id"]
    rec = await _wait_until(
        lambda: next((r for r in STATE.decision_store.list(session_id=owner) if r.pending), None),
        message="the Needs-you recovery decision was raised",
    )
    return coord, saver, claim, gen2, owner, rec


# ── r1: one decision naming the job, the step count and the last tool call ─────────


@pytest.mark.asyncio
async def test_interrupted_one_shot_raises_exactly_one_needs_you_decision(tmp_path, monkeypatch):
    coord, saver, claim, gen2, owner, rec = await _held_one_shot(tmp_path, monkeypatch, "s7-raise")
    try:
        pending = [r for r in STATE.decision_store.list(session_id=owner) if r.pending]
        assert [r.id for r in pending] == [rec.id]  # exactly one
        assert rec.question == (
            "ONESHOT-TOOL archive the exports was interrupted by a restart after 1 step. "
            "Last action: risky_step({}), and its effect is unknown."
        )
        assert [o["id"] for o in rec.options] == ["resume", "rerun", "drop"]
        assert restart_recovery_ref(rec) == {
            "job_id": "job-oneshot", "next_fire": claim["next_fire"],
            "created_at": claim["created_at"], "task_id": claim["task_id"],
        }
        # Idempotent per occurrence: a re-raise (another boot) shows no second card.
        await STATE.interrupted_recovery.raise_needs_operator_decision(
            job_id="job-oneshot", context_id=owner, task_id=claim["task_id"],
            next_fire=claim["next_fire"], created_at=claim["created_at"],
            prompt="ONESHOT-TOOL archive the exports",
        )
        assert len([r for r in STATE.decision_store.list(session_id=owner) if r.pending]) == 1
    finally:
        await _shutdown(coord, gen2)


# ── r2: Resume continues from the checkpoint, exactly once (concurrent → one 409) ──


@pytest.mark.asyncio
async def test_resume_answer_continues_from_checkpoint_exactly_once(tmp_path, monkeypatch):
    coord, saver, claim, gen2, owner, rec = await _held_one_shot(tmp_path, monkeypatch, "s7-resume")
    try:
        api = _api_app()
        first, second = await asyncio.gather(_answer(api, rec.id, "resume"), _answer(api, rec.id, "resume"))
        assert sorted((first.status_code, second.status_code)) == [200, 409]

        await _wait_until(
            lambda: (_occurrence(gen2.sched.path, "job-oneshot") or {"state": ""})["state"] == "settled",
            message="the resumed occurrence settled",
        )
        settled = _occurrence(gen2.sched.path, "job-oneshot")
        assert settled["task_state"] == "completed"
        assert settled["task_id"] == claim["task_id"]  # the ORIGINAL handle
        assert settled["message_id"] == claim["message_id"]  # never a resend
        state, text = _task_status(tmp_path, claim["task_id"])
        assert state == "TASK_STATE_COMPLETED"
        assert "done ONESHOT-TOOL" in text

        # The dangling call was reported as interrupted, never re-run — and ran ONCE in
        # total even with two concurrent answers.
        assert coord.tool_runs["risky_step"] == 1
        resumed_view = coord.views["ONESHOT-TOOL"][-1]
        assert ("tool", "risky_step", INTERRUPTED_TOOL_RESULT[:60]) in resumed_view
        assert sum(1 for e in resumed_view if e[0] == "human") == 1  # never a new prompt
        assert len(_query(gen2.sched.path, "SELECT * FROM occurrences WHERE job_id = 'job-oneshot'")) == 1
        answered = STATE.decision_store.get(rec.id)
        assert answered.state == "answered"
        assert answered.answer == "resume"
        await _wait_until(lambda: gen2.sched.get_job("job-oneshot") is None, message="the settled one-shot was removed")
    finally:
        await _shutdown(coord, gen2)


# ── r2: Resume stays honest however long the card parks ─────────────────────────────


@pytest.mark.asyncio
async def test_resume_after_ttl_sweep_remints_the_handle_and_drives(tmp_path, monkeypatch):
    """The held card parks indefinitely, but the disposition-failed task row ages out of
    the 24h TTL sweep. A Resume answered after that must still act: the handle is
    re-minted under the ORIGINAL task id and the drive's real outcome lands on it —
    never a silent settled-failed behind a 200."""
    from a2a_impl.stores import sweep_expired_tasks

    coord, saver, claim, gen2, owner, rec = await _held_one_shot(tmp_path, monkeypatch, "s7-sweep")
    try:
        await _wait_task_failed(tmp_path, claim["task_id"])  # released from the spared registry
        await sweep_expired_tasks(STATE.a2a_task_engine, now=datetime.now(UTC) + timedelta(hours=25))
        assert _task_status(tmp_path, claim["task_id"]) == ("", "")  # the failed row is gone

        response = await _answer(_api_app(), rec.id, "resume")
        assert response.status_code == 200
        await _wait_until(
            lambda: (_occurrence(gen2.sched.path, "job-oneshot") or {"state": ""})["state"] == "settled",
            message="the resumed occurrence settled",
        )
        settled = _occurrence(gen2.sched.path, "job-oneshot")
        assert settled["task_state"] == "completed"
        assert settled["task_id"] == claim["task_id"]  # the original handle, re-minted
        state, text = _task_status(tmp_path, claim["task_id"])
        assert state == "TASK_STATE_COMPLETED"
        assert "done ONESHOT-TOOL" in text
        rows = _query(tmp_path / "a2a-tasks.db", "SELECT context_id FROM tasks WHERE id = ?", (claim["task_id"],))
        assert [r["context_id"] for r in rows] == [owner]  # re-minted in the job's session
        assert coord.tool_runs["risky_step"] == 1  # the dangling call was reported, never re-run
        await _wait_until(lambda: gen2.sched.get_job("job-oneshot") is None, message="the settled one-shot was removed")
    finally:
        await _shutdown(coord, gen2)


@pytest.mark.asyncio
async def test_resume_that_can_no_longer_act_is_refused_and_keeps_the_card(tmp_path, monkeypatch):
    """A Resume whose checkpoint no longer holds this run's turn is refused BEFORE the
    ledger CAS: 400, the card stays pending (Re-run / Drop remain), the occurrence stays
    held and the handle untouched — the choice is never consumed into a silent failure."""
    coord, saver, claim, gen2, owner, rec = await _held_one_shot(tmp_path, monkeypatch, "s7-stale")
    try:
        real_graph = STATE.graph
        monkeypatch.setattr(STATE, "graph", None, raising=False)  # the checkpoint seam is gone
        refused = await _answer(_api_app(), rec.id, "resume")
        assert refused.status_code == 400
        assert "Re-run or Drop" in refused.json()["detail"]
        assert STATE.decision_store.get(rec.id).pending  # still answerable
        assert _occurrence(gen2.sched.path, "job-oneshot")["state"] == "interrupted_needs_operator"
        assert _task_status(tmp_path, claim["task_id"])[0] == "TASK_STATE_FAILED"  # never reopened

        # With the seam back, the SAME card still resumes normally.
        monkeypatch.setattr(STATE, "graph", real_graph, raising=False)
        answered = await _answer(_api_app(), rec.id, "resume")
        assert answered.status_code == 200
        await _wait_until(
            lambda: (_occurrence(gen2.sched.path, "job-oneshot") or {"state": ""})["state"] == "settled",
            message="the resumed occurrence settled",
        )
        assert _task_status(tmp_path, claim["task_id"])[0] == "TASK_STATE_COMPLETED"
    finally:
        await _shutdown(coord, gen2)


# ── r2: Re-run starts ONE fresh turn from the job's prompt ──────────────────────────


@pytest.mark.asyncio
async def test_rerun_answer_starts_one_fresh_turn(tmp_path, monkeypatch):
    coord, saver, claim, gen2, owner, rec = await _held_one_shot(tmp_path, monkeypatch, "s7-rerun")
    try:
        response = await _answer(_api_app(), rec.id, "rerun")
        assert response.status_code == 200
        await _wait_until(
            lambda: any(
                r["state"] == "settled" and r["task_state"] == "completed"
                for r in _query(gen2.sched.path, "SELECT * FROM occurrences WHERE job_id = 'job-oneshot'")
            ),
            message="the fresh fire settled",
        )
        rows = _query(gen2.sched.path, "SELECT * FROM occurrences WHERE job_id = 'job-oneshot'")
        assert len(rows) == 2  # one fresh occurrence — and only one
        old = next(r for r in rows if r["next_fire"] == claim["next_fire"])
        fresh = next(r for r in rows if r["next_fire"] != claim["next_fire"])
        assert old["state"] == "settled"
        assert "re-run" in old["diagnostic"]
        assert fresh["task_state"] == "completed"
        assert fresh["message_id"] != claim["message_id"]  # a fresh dispatch, not a resend
        assert fresh["task_id"] != claim["task_id"]
        # The original handle keeps its restart diagnosis; only the NEW turn ran.
        state, text = _task_status(tmp_path, claim["task_id"])
        assert state == "TASK_STATE_FAILED"
        assert RESTART_INTERRUPTED_TEXT in text
        assert coord.tool_runs["risky_step"] == 2
        final_view = coord.views["ONESHOT-TOOL"][-1]
        assert sum(1 for e in final_view if e[0] == "human") == 2  # the original + ONE fresh prompt
        # The dangling call was sealed with the interruption report before the fresh turn.
        assert ("tool", "risky_step", INTERRUPTED_TOOL_RESULT[:60]) in final_view
        assert STATE.decision_store.get(rec.id).answer == "rerun"
        await _wait_until(lambda: gen2.sched.get_job("job-oneshot") is None, message="the finished one-shot was removed")
    finally:
        await _shutdown(coord, gen2)


# ── r2: Drop settles the occurrence without running anything ────────────────────────


@pytest.mark.asyncio
async def test_drop_answer_settles_without_running(tmp_path, monkeypatch):
    coord, saver, claim, gen2, owner, rec = await _held_one_shot(tmp_path, monkeypatch, "s7-drop")
    try:
        response = await _answer(_api_app(), rec.id, "drop")
        assert response.status_code == 200
        dropped = await _wait_until(
            lambda: (lambda r: r if r and r["state"] == "settled" else None)(
                _occurrence(gen2.sched.path, "job-oneshot")
            ),
            message="the occurrence settled as dropped",
        )
        assert dropped["task_state"] == "dropped"
        assert dropped["task_id"] == claim["task_id"]
        assert gen2.sched.get_job("job-oneshot") is None  # the one-shot's row is finished

        # Nothing ran: no new model pass, no tool re-run, no new occurrence, and the
        # dead handle keeps its restart diagnosis.
        await _quiesce_polls(gen2.sched)
        assert len(coord.views["ONESHOT-TOOL"]) == 1
        assert coord.tool_runs["risky_step"] == 1
        assert len(_query(gen2.sched.path, "SELECT * FROM occurrences WHERE job_id = 'job-oneshot'")) == 1
        state, text = _task_status(tmp_path, claim["task_id"])
        assert state == "TASK_STATE_FAILED"
        assert RESTART_INTERRUPTED_TEXT in text
        assert STATE.decision_store.get(rec.id).answer == "drop"
    finally:
        await _shutdown(coord, gen2)


# ── r2: a settled release finishes the jobs row like every other settle ─────────────


def _held_claim(tmp_path, agent: str, job_id: str, schedule: str):
    """A scheduler whose ``job_id`` occurrence is held ``interrupted_needs_operator``."""
    sched = scheduler_local.LocalScheduler(
        agent_name=agent, invoke_url="http://127.0.0.1:7870", api_key="k", bearer_token="b", db_dir=tmp_path
    )
    job = sched.add_job("archive the exports", schedule, job_id=job_id)
    assert sched._claim_occurrence(job) is not None
    claim = _occurrence(sched.path, job_id)
    assert sched._mark_interrupted_occurrence(claim, "interrupted_needs_operator", "held")
    return sched, claim


def test_settled_release_finishes_the_one_shot_jobs_row(tmp_path):
    """``release_interrupted_occurrence(state='settled')`` must remove a one-shot's jobs
    row like ``_settle_task`` and ``drop_interrupted_occurrence`` do: once settled, the
    occurrence no longer counts as D7-parked, so a leftover row would read as an
    outstanding continuation and wedge the owner's other fires."""
    sched, claim = _held_claim(tmp_path, "s7-release", "job-release", _past_iso())
    assert sched.release_interrupted_occurrence(
        "job-release", claim["next_fire"], claim["created_at"],
        state="settled", task_state="failed", diagnostic="resume could not act (test)",
    )
    settled = _occurrence(sched.path, "job-release")
    assert settled["state"] == "settled"
    assert settled["task_state"] == "failed"
    assert sched.get_job("job-release") is None  # the one-shot's row is finished


def test_settled_release_keeps_a_recurring_jobs_row(tmp_path):
    """A recurring job's next slot is untouched by a settled release: the jobs row stays,
    with ``last_error`` saying why this occurrence ran nothing."""
    sched, claim = _held_claim(tmp_path, "s7-release-cron", "job-cron-release", "0 9 * * *")
    assert sched.release_interrupted_occurrence(
        "job-cron-release", claim["next_fire"], claim["created_at"],
        state="settled", task_state="failed", diagnostic="resume could not act (test)",
    )
    assert _occurrence(sched.path, "job-cron-release")["state"] == "settled"
    kept = sched.get_job("job-cron-release")
    assert kept is not None
    assert kept.last_error == "resume could not act (test)"


# ── r3: no checkpoint → Re-run and Drop only ────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_checkpoint_offers_rerun_and_drop_only(tmp_path, monkeypatch):
    """A fire killed before anything reached the checkpointer: the Needs-you item offers
    Re-run and Drop only — there is nothing to resume — and 'resume' is not answerable."""
    monkeypatch.setattr(scheduler_local, "_POLL_INTERVAL_S", _TEST_POLL_S)
    coord, saver = _Coordinator(), _KillableSaver()
    saver.dead = True  # nothing of the turn ever persists — the no-checkpoint case
    agent = "s7-nock"
    gen1 = _boot_generation(tmp_path, monkeypatch, coord, saver, agent)
    await gen1.sched.start()
    gen1.sched.add_job("ONESHOT-TOOL archive the exports", _past_iso(), job_id="job-nock")
    await _wait_until(lambda: coord.tool_runs.get("risky_step") == 1, message="the fire reached its tool call")
    claim = await _accepted_unresolved_claim(gen1.sched, "job-nock")
    await _sigterm(coord, saver, gen1)
    await _let_zombies_unwind()

    gen2 = await _restart(tmp_path, monkeypatch, coord, saver, agent, release=True)
    monkeypatch.setattr(STATE, "interrupted_recovery", InterruptedTurnRecovery(), raising=False)
    try:
        owner = json.loads(claim["job_snapshot"])["context_id"]
        rec = await _wait_until(
            lambda: next((r for r in STATE.decision_store.list(session_id=owner) if r.pending), None),
            message="the Needs-you recovery decision was raised",
        )
        assert [o["id"] for o in rec.options] == ["rerun", "drop"]
        assert "was interrupted by a restart" in rec.question
        assert "Last action" not in rec.question

        api = _api_app()
        refused = await _answer(api, rec.id, "resume")
        assert refused.status_code == 400  # not an option on a checkpoint-less run
        answered = await _answer(api, rec.id, "drop")
        assert answered.status_code == 200
        dropped = await _wait_until(
            lambda: (lambda r: r if r and r["state"] == "settled" else None)(
                _occurrence(gen2.sched.path, "job-nock")
            ),
            message="the occurrence settled as dropped",
        )
        assert dropped["task_state"] == "dropped"
        assert coord.tool_runs["risky_step"] == 1  # nothing ever re-ran
    finally:
        await _shutdown(coord, gen2)


# ── failure safety: an answer whose action cannot land gives the card back ──────────


@pytest.mark.asyncio
async def test_drop_failure_restores_the_card_for_another_answer(tmp_path, monkeypatch):
    """A store error after the answer CAS (the occurrence CAS returns False) must not
    leave the run held behind an answered record with no card: the decision reopens,
    and a later answer performs the action normally."""
    coord, saver, claim, gen2, owner, rec = await _held_one_shot(tmp_path, monkeypatch, "s7-dropfail")
    try:
        gen2.sched.drop_interrupted_occurrence = lambda *a: False  # the simulated store error
        assert (await _answer(_api_app(), rec.id, "drop")).status_code == 200
        reopened = await _wait_until(
            lambda: (lambda r: r if r and r.pending else None)(STATE.decision_store.get(rec.id)),
            message="the failed answer reopened the card",
        )
        assert reopened.answer is None
        assert _occurrence(gen2.sched.path, "job-oneshot")["state"] == "interrupted_needs_operator"

        del gen2.sched.drop_interrupted_occurrence  # the store recovered
        assert (await _answer(_api_app(), rec.id, "drop")).status_code == 200
        await _wait_until(
            lambda: (_occurrence(gen2.sched.path, "job-oneshot") or {"state": ""})["state"] == "settled",
            message="the retried drop settled the occurrence",
        )
        assert _occurrence(gen2.sched.path, "job-oneshot")["task_state"] == "dropped"
        assert STATE.decision_store.get(rec.id).answer == "drop"
    finally:
        await _shutdown(coord, gen2)


@pytest.mark.asyncio
async def test_crash_between_answer_and_action_reraises_the_card_at_boot(tmp_path, monkeypatch):
    """A restart in the answered-but-unacted window (e.g. mid operator Resume) leaves an
    answered record over a still-held occurrence. The next boot's recovery pass reopens
    the card — and its Resume still drives the turn onto the ORIGINAL handle."""
    coord, saver, claim, gen2, owner, rec = await _held_one_shot(tmp_path, monkeypatch, "s7-crash")
    STATE.decision_store.answer(rec.id, "resume")  # the CAS landed; the crash ate the action
    await _sigterm(coord, saver, gen2)
    await _let_zombies_unwind()

    gen3 = await _restart(tmp_path, monkeypatch, coord, saver, "s7-crash", release=True)
    try:
        reopened = await _wait_until(
            lambda: (lambda r: r if r and r.pending else None)(STATE.decision_store.get(rec.id)),
            message="boot repair reopened the answered-but-unacted card",
        )
        assert [o["id"] for o in reopened.options] == ["resume", "rerun", "drop"]
        assert len([r for r in STATE.decision_store.list(session_id=owner) if r.pending]) == 1

        assert (await _answer(_api_app(), rec.id, "resume")).status_code == 200
        await _wait_until(
            lambda: (_occurrence(gen3.sched.path, "job-oneshot") or {"state": ""})["state"] == "settled",
            message="the resumed occurrence settled",
        )
        settled = _occurrence(gen3.sched.path, "job-oneshot")
        assert settled["task_state"] == "completed"
        assert settled["task_id"] == claim["task_id"]  # the ORIGINAL handle
        assert coord.tool_runs["risky_step"] == 1  # the dangling call was never re-run
    finally:
        await _shutdown(coord, gen3)


@pytest.mark.asyncio
async def test_dismissing_the_card_never_resumes_and_stays_dismissed(tmp_path, monkeypatch):
    """Dismissing a recovery card over the real cancel route only withdraws the record —
    no clearing resume runs on the job session (only Re-run may start a turn) — and the
    boot repair leaves the dismissal standing (only MISSING or ANSWERED records come
    back): the held occurrence stays the durable trace."""
    coord, saver, claim, gen2, owner, rec = await _held_one_shot(tmp_path, monkeypatch, "s7-dismiss")
    api = _api_app()
    async with _REAL_ASYNC_CLIENT(transport=httpx.ASGITransport(app=api), base_url="http://api") as client:
        response = await client.post(f"/api/decisions/{rec.id}/cancel", json={"reason": "not needed"})
    assert response.status_code == 200
    await _quiesce_polls(gen2.sched)
    assert len(coord.views["ONESHOT-TOOL"]) == 1  # no clearing resume drove the session
    assert coord.tool_runs["risky_step"] == 1
    await _sigterm(coord, saver, gen2)
    await _let_zombies_unwind()

    gen3 = await _restart(tmp_path, monkeypatch, coord, saver, "s7-dismiss", release=True)
    try:
        await _quiesce_polls(gen3.sched)
        assert STATE.decision_store.get(rec.id).state == "withdrawn"
        assert [r for r in STATE.decision_store.list(session_id=owner) if r.pending] == []
        assert _occurrence(gen3.sched.path, "job-oneshot")["state"] == "interrupted_needs_operator"
        assert len(coord.views["ONESHOT-TOOL"]) == 1  # still nothing ran
    finally:
        await _shutdown(coord, gen3)


# ── r4: an S6 auto-resume emits one Activity entry ──────────────────────────────────


@pytest.mark.asyncio
async def test_auto_resume_emits_one_activity_entry(tmp_path, monkeypatch):
    import server as server_pkg
    from activity import ActivityLog

    feed = ActivityLog(str(tmp_path / "activity.db"))
    monkeypatch.setattr(STATE, "activity_log", feed, raising=False)
    # The console's Activity surfaces refresh on the LIVE event, not by re-reading the
    # saved log: the entry must also be published on the bus.
    events = []
    monkeypatch.setattr(
        server_pkg._event_bus, "publish", lambda topic, data=None, **kw: events.append((topic, data))
    )
    agent = "s7-feed"
    coord, saver, claim = await _fire_and_kill(
        tmp_path, monkeypatch, agent, prompt="SLOW-MODEL digest the overnight queue",
        job_id="job-slow", ready=lambda c: len(c.views.get("SLOW-MODEL") or []) >= 2,
        # step_one + its result committed, nothing dangling: the clean step-1 shape.
        checkpointed=lambda msgs: len(msgs) >= 3 and not _dangling_tool_calls(msgs),
    )
    gen2 = await _restart(tmp_path, monkeypatch, coord, saver, agent, release=True)
    try:
        await _wait_until(
            lambda: (_occurrence(gen2.sched.path, "job-slow") or {"state": ""})["state"] == "settled",
            message="the auto-resumed occurrence settled",
        )
        entries = [e for e in feed.recent() if e["text"].startswith("Resumed")]
        assert len(entries) == 1  # one auto-resume, ONE entry
        assert entries[0]["text"] == "Resumed SLOW-MODEL digest the overnight queue after a restart (step 1)"
        assert entries[0]["context_id"] == json.loads(claim["job_snapshot"])["context_id"]
        assert entries[0]["origin"] == "scheduler"
        assert entries[0]["task_id"] == claim["task_id"]
        live = [d for t, d in events if t == "activity.message" and (d or {}).get("text", "").startswith("Resumed")]
        assert len(live) == 1  # the saved entry also went out live, once
        assert live[0]["text"] == entries[0]["text"]
        assert live[0]["context_id"] == entries[0]["context_id"]
        assert live[0]["task_id"] == claim["task_id"]
        assert live[0]["state"] == "completed"
    finally:
        await _shutdown(coord, gen2)
