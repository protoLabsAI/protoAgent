"""Behavioral scheduling contracts for both tool and external board callers."""
import asyncio

import pytest

from plugins.coder.solve import Budget, Verdict, solve

# Verifier process cleanup exercises platform-specific subprocess handling.
pytestmark = pytest.mark.platform_sensitive


def verdict(passed=False, failed=1):
    return Verdict(passed, total=1, failed=0 if passed else failed, output="fix formatting")


@pytest.mark.parametrize("forced", [None, "best-of-k", "fusion"])
async def test_serial_verifies_before_next_launch_and_stops(forced):
    events = []
    feedbacks = []

    async def gen(task, *, feedback):
        n = len(feedbacks) + 1
        feedbacks.append(feedback)
        events.append(("generate", n))
        return str(n)

    async def verify(code):
        events.append(("verify", int(code)))
        return verdict(code == "2")

    result = await solve("t", generate=gen, fusion_generate=gen, verify=verify,
                         budget=Budget(6), k=3, fusion_k=3, force_rung=forced)
    assert events == [("generate", 1), ("verify", 1), ("generate", 2), ("verify", 2)]
    assert result.gens_spent == result.candidates_tried == 2
    assert "fix formatting" in feedbacks[1]


@pytest.mark.platform_sensitive
async def test_parallel_pass_drains_siblings_and_selects_start_order(tmp_path):
    second_verified = asyncio.Event()
    journal = []

    async def gen(task, *, feedback):
        index = len(journal)
        path = tmp_path / str(index)
        path.write_text(f"unique work {index}")
        if index == 1:
            await second_verified.wait()
        return str(path)

    async def verify(code):
        if code.endswith("2"):
            second_verified.set()
        return verdict(True)

    result = await solve("t", generate=gen, verify=verify, budget=Budget(8), k=8,
                         force_rung="best-of-k", generation_concurrency=2, candidates=journal)
    assert result.gens_spent == 2
    assert result.solution.endswith("1")
    assert len(result.candidates) == 2
    assert all(c.verdict.passed for c in journal)
    assert sorted(p.read_text() for p in tmp_path.iterdir()) == ["unique work 1", "unique work 2"]


@pytest.mark.parametrize("limit", [1, 2])
async def test_bounded_verification_budget_and_full_batch(limit):
    active = peak = count = 0

    async def gen(task, *, feedback):
        nonlocal count
        count += 1
        return str(count)

    async def verify(code):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return verdict(True)

    result = await solve("t", generate=gen, verify=verify, budget=Budget(5), k=10,
                         force_rung="best-of-k", generation_concurrency=3,
                         verification_concurrency=limit, full_batch=True)
    assert peak == limit
    assert count == result.gens_spent == 5
    assert result.solution == "1"


@pytest.mark.parametrize("phase", ["generate", "verify"])
async def test_exception_drains_started_work_and_charges_budget(phase):
    journal = []
    budget = Budget(9)
    sibling_done = asyncio.Event()

    async def gen(task, *, feedback):
        n = len(journal)
        if n == 1 and phase == "generate":
            raise ValueError("broken")
        await asyncio.sleep(0)
        return str(n)

    async def verify(code):
        if code == "1":
            raise ValueError("broken")
        sibling_done.set()
        return verdict()

    with pytest.raises(ValueError, match="broken"):
        await solve("t", generate=gen, verify=verify, budget=budget, k=9,
                    force_rung="best-of-k", generation_concurrency=2, candidates=journal)
    assert sibling_done.is_set()
    assert budget.spent == len(journal) == 2
    assert journal[1].solution == "2"
    assert journal[1].verdict is not None


@pytest.mark.parametrize("deadline", [False, True])
async def test_cancellation_joins_callbacks_and_preserves_handles(deadline):
    journal = []
    entered = asyncio.Event()
    cleaned = []
    budget = Budget(8)

    async def gen(task, *, feedback):
        return f"worktree-{len(journal)}"

    async def verify(code):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append(code)

    async def run():
        async with asyncio.timeout(0.02 if deadline else None):
            return await solve("t", generate=gen, verify=verify, budget=budget, k=8,
                               force_rung="best-of-k", generation_concurrency=2,
                               verification_concurrency=2, candidates=journal)

    task = asyncio.create_task(run())
    await entered.wait()
    if not deadline:
        task.cancel()
    with pytest.raises(TimeoutError if deadline else asyncio.CancelledError):
        await task
    assert budget.spent == len(journal) == len(cleaned) == 2
    assert all(c.solution and isinstance(c.error, asyncio.CancelledError) for c in journal)


async def test_shared_verification_limit_is_fair_across_solves():
    slots = asyncio.Semaphore(1)
    order = []
    active = 0

    async def gen(task, *, feedback):
        return task

    async def verify(code):
        nonlocal active
        active += 1
        assert active == 1
        order.append(code)
        await asyncio.sleep(0)
        active -= 1
        return verdict()

    await asyncio.gather(*(solve(name, generate=gen, verify=verify, budget=Budget(3),
                                force_rung="best-of-k", k=3, verification_semaphore=slots)
                           for name in ("a", "b")))
    assert order == ["a", "b"] * 3


@pytest.mark.platform_sensitive
@pytest.mark.parametrize("cancel", [False, True])
async def test_verifier_reaps_process_tree(monkeypatch, cancel):
    from plugins.coder import verify as module

    entered = asyncio.Event()
    calls = []

    class Process:
        async def communicate(self):
            entered.set()
            await asyncio.Event().wait()

        async def wait(self):
            calls.append("reaped")

    async def spawn(*args, **kwargs):
        assert all(kwargs[key] == value for key, value in module.group_kwargs().items())
        return Process()

    async def kill(proc):
        calls.append("tree killed")

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(module, "akill_tree", kill)
    task = asyncio.create_task(module.run_tests("", "", timeout=0.02 if not cancel else 30))
    await entered.wait()
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert not (await task).passed
    assert calls == ["tree killed", "reaped"]


async def test_failure_refills_free_pipeline_without_waiting_for_slow_sibling():
    third_started = asyncio.Event()
    journal = []
    feedbacks = []

    async def gen(task, *, feedback):
        n = len(journal)
        feedbacks.append(feedback)
        if n == 1:
            await third_started.wait()
        if n == 3:
            third_started.set()
        return str(n)

    async def verify(code):
        return verdict(code == "3")

    result = await solve("t", generate=gen, verify=verify, budget=Budget(3), k=3,
                         force_rung="best-of-k", generation_concurrency=2, candidates=journal)
    assert result.solution == "3"
    assert "fix formatting" in feedbacks[2]
    assert all(c.verdict is not None for c in journal)


@pytest.mark.platform_sensitive
async def test_cancel_generation_preserves_adapter_partial_work(tmp_path):
    entered = asyncio.Event()
    journal = []
    budget = Budget(3)
    path = tmp_path / "partial"
    stopped = asyncio.Event()

    async def gen(task, *, feedback):
        path.write_text("unique unfinished implementation")
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()  # adapter stops its process, retains the worktree

    async def verify(code):
        pytest.fail("unfinished generation must not be verified")

    task = asyncio.create_task(solve("t", generate=gen, verify=verify, budget=budget, candidates=journal))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()
    assert path.read_text() == "unique unfinished implementation"
    assert budget.spent == len(journal) == 1
    assert isinstance(journal[0].error, asyncio.CancelledError)


@pytest.mark.parametrize("option", ["generation_concurrency", "verification_concurrency"])
async def test_invalid_limits_start_no_work(option):
    async def gen(task, *, feedback):
        pytest.fail("invalid configuration must not start work")

    with pytest.raises(ValueError, match="positive"):
        await solve("t", generate=gen, verify=None, budget=Budget(3), **{option: 0})
