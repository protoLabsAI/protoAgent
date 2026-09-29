"""Sleep-aware timeouts (#3724): asyncio's clock stops while a Mac sleeps, so an 1800s
``asyncio.wait_for`` bound held a board coder for hours of wall time."""

from __future__ import annotations

import asyncio
import sys

import pytest

from infra import clock

pytestmark = pytest.mark.platform_sensitive


class _SleepingClock:
    """A sleep-counting clock that jumps ``gap`` seconds after its first reading, the way
    CLOCK_MONOTONIC does across a sleep that asyncio's own clock never saw."""

    def __init__(self, gap: float):
        self.t, self.gap, self.reads = 0.0, gap, 0

    def __call__(self) -> float:
        self.reads += 1
        if self.reads == 2:
            self.t += self.gap
        return self.t


async def test_a_result_before_the_deadline_is_returned():
    assert await clock.wait_for(asyncio.sleep(0.01, result="ok"), 5) == "ok"


async def test_time_asleep_counts_toward_the_deadline(monkeypatch):
    monkeypatch.setattr(clock, "now", _SleepingClock(gap=3601))
    never = asyncio.get_running_loop().create_future()
    loop_start = asyncio.get_running_loop().time()

    with pytest.raises(TimeoutError):
        await clock.wait_for(never, 1800, slice_s=0.01)

    assert asyncio.get_running_loop().time() - loop_start < 1  # not 1800s of awake time
    assert never.cancelled()


async def test_awake_it_times_out_like_asyncio():
    with pytest.raises(TimeoutError):
        await clock.wait_for(asyncio.sleep(10), 0.05)


async def test_cancelling_the_caller_cancels_the_awaited(monkeypatch):
    inner = asyncio.get_running_loop().create_future()
    outer = asyncio.ensure_future(clock.wait_for(inner, 30))
    await asyncio.sleep(0.01)
    outer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outer
    assert inner.cancelled()


async def test_a_result_landing_at_the_deadline_is_kept():
    """A lock acquire that completes as it is being cancelled must not be discarded: the
    lock would be held by nobody."""

    async def finishes_when_cancelled():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            return "acquired"

    assert await clock.wait_for(finishes_when_cancelled(), 0.02) == "acquired"


async def test_no_timeout_waits_as_usual():
    assert await clock.wait_for(asyncio.sleep(0.01, result=1), None) == 1


def test_the_clock_counts_sleep_on_this_platform():
    import time

    if sys.platform == "darwin":
        # time.monotonic() is mach_absolute_time (stops asleep); the chosen clock is not it.
        assert abs(clock.now() - time.clock_gettime(time.CLOCK_MONOTONIC)) < 1
    elif hasattr(time, "CLOCK_BOOTTIME"):
        assert abs(clock.now() - time.clock_gettime(time.CLOCK_BOOTTIME)) < 1


# ─── the ACP client uses it ────────────────────────────────────────────────────────────

_SILENT_AGENT = r"""
import sys, json
for line in sys.stdin:
    msg = json.loads(line)
    m, mid = msg.get("method"), msg.get("id")
    if m == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": 1}}), flush=True)
    elif m == "session/new":
        print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "s1"}}), flush=True)
    # session/prompt: never answered, like a coder whose connection died across a sleep
"""


async def test_a_ready_task_gets_its_step_even_past_the_deadline(monkeypatch):
    monkeypatch.setattr(clock, "now", _SleepingClock(gap=3601))  # the deadline passes at once
    lock = asyncio.Lock()
    assert await clock.wait_for(lock.acquire(), 1800, slice_s=0.01) is True
    assert lock.locked()


async def test_a_coder_prompt_times_out_after_the_machine_slept(tmp_path, monkeypatch):
    from plugins.coding_agent.acp_client import AcpClient, AcpError

    script = tmp_path / "silent.py"
    script.write_text(_SILENT_AGENT, encoding="utf-8")
    client = AcpClient(sys.executable, [str(script)], cwd=str(tmp_path), name="opus", record_runs=False)
    slept = {"gap": 0.0}
    real_now = clock.now
    monkeypatch.setattr(clock, "now", lambda: real_now() + slept["gap"])
    monkeypatch.setattr(clock, "DEFAULT_SLICE_S", 0.05)
    # Once the prompt is in flight, the machine "sleeps" for two hours.
    asyncio.get_running_loop().call_later(1.5, lambda: slept.update(gap=7200.0))
    try:
        with pytest.raises(AcpError, match="timed out after 1800"):
            await asyncio.wait_for(client.prompt("go", timeout=1800), 20)
    finally:
        await client.close()
