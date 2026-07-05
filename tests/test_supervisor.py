"""The supervised background-task helper (graph.supervisor).

Generalizes the lifecycle the SpaceTraders fleet engine hand-rolled (run windows
back-to-back; a watchdog that re-kicks a crash, restarts a stall, recovers a known fault).
Pure asyncio — tested directly with tiny intervals. ``asyncio_mode = auto`` runs the async
tests.
"""

from __future__ import annotations

import asyncio

from graph.sdk import supervise as sdk_supervise  # re-exported on the plugin SDK surface
from graph.supervisor import RetryAfter, Supervisor, supervise


def test_supervise_is_exported_on_the_sdk():
    assert sdk_supervise is supervise


async def test_loops_back_to_back_then_stops():
    n = 0

    async def work():
        nonlocal n
        n += 1
        return n

    sv = supervise(work, interval=10, breath=0.0)  # watchdog idle; we only test the loop
    sv.start()
    assert sv.start().endswith("already running")  # idempotent
    await asyncio.sleep(0.1)
    looped = n
    assert looped >= 3, looped
    assert sv.running() and sv.status()["result"] == looped
    await sv.aclose()
    after = n
    await asyncio.sleep(0.05)
    assert n == after and not sv.running()  # stopped — no more work


async def test_run_once_completes_and_is_not_rekicked():
    runs = 0

    async def work():
        nonlocal runs
        runs += 1
        return "done"

    sv = supervise(work, loop=False, interval=0.02)
    sv.start()
    await asyncio.sleep(0.15)  # several watchdog ticks — a one-shot must NOT be re-run
    assert runs == 1
    assert not sv.running()
    assert sv.status()["result"] == "done"
    assert sv.status()["want_running"] is False  # watchdog marked it completed
    await sv.aclose()


async def test_crash_is_rekicked_after_on_crash_recovery():
    calls = 0
    recovered = []

    async def work():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("boom")
        await asyncio.sleep(0.5)  # second run stays alive
        return "ok"

    def on_crash(result):
        recovered.append(result)
        return True  # handled → re-kick

    sv = supervise(work, interval=0.02, breath=0.0, on_crash=on_crash)
    sv.start()
    await asyncio.sleep(0.15)
    assert calls >= 2  # crashed once, watchdog re-kicked, ran again
    assert len(recovered) == 1  # on_crash fired exactly once for the down-streak
    assert "error" in recovered[0]  # got the crash result
    await sv.aclose()


async def test_unrecoverable_crash_clears_want_running():
    async def work():
        raise RuntimeError("always")

    def on_crash(_result):
        return False  # cannot recover → stop the storm

    sv = supervise(work, interval=0.02, on_crash=on_crash)
    sv.start()
    await asyncio.sleep(0.12)
    assert sv.status()["want_running"] is False
    assert not sv.running()
    await sv.aclose()


# ── on_crash bounded retry + RetryAfter (#1823) ───────────────────────────────


async def test_default_one_shot_preserved_on_repeated_crashes():
    """Backward-compat: with the default (max_attempts=1), on_crash still fires at most once
    per down-streak even when the runner keeps re-crashing fast — then blind-re-kicks."""
    on_crash_calls = 0

    async def work():
        raise ValueError("always boom")

    def on_crash(_r):
        nonlocal on_crash_calls
        on_crash_calls += 1
        return True  # handled → re-kick (but the runner keeps crashing)

    sv = supervise(work, interval=0.02, breath=0.0, on_crash=on_crash)  # default max_attempts=1
    sv.start()
    await asyncio.sleep(0.2)  # many crash / re-kick cycles
    assert on_crash_calls == 1  # one-shot latch preserved
    assert sv.status()["want_running"] is True  # blind re-kick continues (historical behavior)
    await sv.aclose()


async def test_bounded_retry_reinvokes_on_crash_then_stops():
    """max_attempts>1 re-invokes on_crash on each fast re-crash, up to the bound, then STOPS
    (instead of the old unbounded blind re-kick)."""
    on_crash_calls = 0

    async def work():
        raise ValueError("always boom")

    def on_crash(_r):
        nonlocal on_crash_calls
        on_crash_calls += 1
        return True

    sv = supervise(work, interval=0.02, breath=0.0, on_crash=on_crash, on_crash_max_attempts=3)
    sv.start()
    await asyncio.sleep(0.3)
    assert on_crash_calls == 3  # re-invoked on each re-crash, up to the bound
    assert sv.status()["want_running"] is False  # exhausted → stopped, not a blind loop
    assert not sv.running()
    await sv.aclose()


async def test_retry_after_polls_without_rekick_then_recovers():
    """The SpaceTraders case: on_crash returns RetryAfter to poll a slow recovery — the
    supervisor waits + re-invokes WITHOUT re-kicking, then re-kicks once on_crash returns True."""
    work_calls = 0
    seen = []  # (result, work_calls at the time) per on_crash invocation

    async def work():
        nonlocal work_calls
        work_calls += 1
        if work_calls == 1:
            raise ValueError("boom")
        await asyncio.sleep(0.5)  # the recovered run stays alive
        return "ok"

    def on_crash(result):
        seen.append((result, work_calls))
        if len(seen) < 3:
            return RetryAfter(0.01)  # not fixed yet — poll again, no re-kick
        return True  # recovered → re-kick

    sv = supervise(work, interval=0.02, breath=0.0, on_crash=on_crash, on_crash_max_attempts=5)
    sv.start()
    await asyncio.sleep(0.25)
    assert len(seen) == 3  # polled 3× (2× RetryAfter + 1× True)
    assert [wc for (_r, wc) in seen] == [1, 1, 1]  # runner NOT re-kicked during polling
    assert work_calls >= 2  # re-kicked only after True → work ran again
    assert sv.running()  # recovered and alive
    await sv.aclose()


async def test_retry_after_exhausted_stops():
    """RetryAfter forever, bounded by max_attempts → stop when the budget is spent."""
    calls = 0

    async def work():
        raise ValueError("boom")

    def on_crash(_r):
        nonlocal calls
        calls += 1
        return RetryAfter(0.01)  # never recovers

    sv = supervise(work, interval=0.02, breath=0.0, on_crash=on_crash, on_crash_max_attempts=2)
    sv.start()
    await asyncio.sleep(0.2)
    assert calls == 2  # polled up to the bound
    assert sv.status()["want_running"] is False  # exhausted → stop
    await sv.aclose()


def test_retry_after_exported_on_sdk():
    from graph.sdk import RetryAfter as sdk_ra

    assert sdk_ra is RetryAfter
    assert RetryAfter(1.5).seconds == 1.5


async def test_stall_is_detected_and_restarted():
    async def work():
        await asyncio.sleep(10)  # hangs — running but making no progress

    sv = supervise(
        work,
        interval=0.02,
        stall_ticks=2,
        progress=lambda: 0,  # constant token → frozen progress
        stall_check=lambda: True,  # confirmed stalled
    )
    sv.start()
    await asyncio.sleep(0.2)
    assert sv.status()["restarts"] >= 1
    await sv.aclose()


async def test_stall_check_prevents_false_trip():
    async def work():
        await asyncio.sleep(10)

    sv = supervise(
        work,
        interval=0.02,
        stall_ticks=2,
        progress=lambda: 0,  # frozen…
        stall_check=lambda: False,  # …but NOT a real stall (e.g. legit long work)
    )
    sv.start()
    await asyncio.sleep(0.15)
    assert sv.status()["restarts"] == 0  # never restarted
    await sv.aclose()


async def test_request_stop_is_graceful():
    windows = 0

    async def work():
        nonlocal windows
        windows += 1
        await asyncio.sleep(0.02)
        return windows

    sv = supervise(work, interval=10, breath=0.0)
    sv.start()
    await asyncio.sleep(0.05)
    sv.request_stop()  # finish the current window, then stop — no re-kick
    await asyncio.sleep(0.1)
    assert not sv.running()
    assert sv.status()["want_running"] is False
    settled = windows
    await asyncio.sleep(0.05)
    assert windows == settled
    await sv.aclose()


async def test_status_shape():
    async def work():
        await asyncio.sleep(0.01)

    sv = Supervisor(work, name="probe", interval=10)
    s = sv.status()
    assert s["name"] == "probe" and s["running"] is False and s["restarts"] == 0
    sv.start()
    await asyncio.sleep(0.02)
    s = sv.status()
    assert s["running"] and s["want_running"] and s["watchdog"]
    assert "started" in s["events"]
    await sv.aclose()
