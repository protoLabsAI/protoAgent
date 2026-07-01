"""Deterministic background work jobs (ADR 0050 — ``BackgroundManager.spawn_work``).

``spawn_work`` runs a plain coroutine (NOT an LLM subagent turn) through the same
durable store + concurrency cap + event stream + drain-on-next-turn notification as
``spawn``. These tests cover the completion, failure, cancel, and notification paths
without any A2A endpoint (a work job never self-POSTs).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from background.manager import BackgroundManager
from background.store import BackgroundStore


def _mgr(tmp_path: Path, *, events=None, terminals=None) -> BackgroundManager:
    store = BackgroundStore(str(tmp_path / "background" / "jobs.db"))
    return BackgroundManager(
        agent_name="a",
        invoke_url="http://127.0.0.1:0",
        store=store,
        event_publish=(lambda topic, data: events.append((topic, data))) if events is not None else None,
        on_terminal=(lambda job: terminals.append(job.id)) if terminals is not None else None,
    )


async def _settle(store: BackgroundStore, job_id: str, tries: int = 400):
    for _ in range(tries):
        j = store.get(job_id)
        if j is not None and j.status != "running":
            return j
        await asyncio.sleep(0.005)
    raise AssertionError(f"job {job_id} never settled")


async def test_spawn_work_completes_and_notifies_once(tmp_path):
    events: list = []
    terminals: list = []
    mgr = _mgr(tmp_path, events=events, terminals=terminals)

    async def work():
        return "ingested 3 chunks"

    job_id = await mgr.spawn_work(
        origin_session="s1", kind="ingest", description="Ingest foo", detail="foo", work=work
    )
    assert job_id.startswith("bg-")

    job = await _settle(mgr.store, job_id)
    assert job.status == "completed"
    assert job.result == "ingested 3 chunks"
    assert job.subagent_type == "ingest"

    topics = [t for t, _ in events]
    assert "background.started" in topics and "background.completed" in topics
    assert terminals == [job_id]  # idle-wake hook fired exactly once

    # drained into the originating session exactly once
    drained = mgr.store.drain_pending("s1")
    assert [j.id for j in drained] == [job_id]
    assert mgr.store.drain_pending("s1") == []


async def test_spawn_work_failure_marks_failed(tmp_path):
    events: list = []
    mgr = _mgr(tmp_path, events=events)

    async def work():
        raise ValueError("gateway 500")

    job_id = await mgr.spawn_work(origin_session="s1", kind="ingest", description="Ingest bar", work=work)
    job = await _settle(mgr.store, job_id)
    assert job.status == "failed"
    assert "gateway 500" in job.result
    completed = [d for t, d in events if t == "background.completed"]
    assert completed and completed[0]["status"] == "failed"


async def test_spawn_work_cancel(tmp_path):
    mgr = _mgr(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()

    async def work():
        started.set()
        await release.wait()  # never released — the test cancels instead
        return "unreachable"

    job_id = await mgr.spawn_work(origin_session="s1", kind="ingest", description="slow", work=work)
    await asyncio.wait_for(started.wait(), timeout=2)

    res = await mgr.cancel(job_id)
    assert res["ok"] is True and res["status"] == "canceled"
    assert mgr.store.get(job_id).status == "canceled"
    await asyncio.gather(*list(mgr._fire_tasks), return_exceptions=True)  # let the cancelled task unwind


async def test_reconcile_fails_a_running_work_job(tmp_path):
    # A work coroutine can't survive a restart — reconcile marks it failed (parity with turns).
    mgr = _mgr(tmp_path)
    hold = asyncio.Event()

    async def work():
        await hold.wait()
        return "x"

    job_id = await mgr.spawn_work(origin_session="s1", kind="ingest", description="held", work=work)
    # simulate restart reconciliation while it's still running
    assert mgr.store.reconcile_interrupted() >= 1
    assert mgr.store.get(job_id).status == "failed"
    hold.set()
    await asyncio.gather(*list(mgr._fire_tasks), return_exceptions=True)
    # the late completion is a no-op — reconcile's terminal state stands
    assert mgr.store.get(job_id).status == "failed"
