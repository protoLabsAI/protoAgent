"""Timeouts that count the time a machine spends asleep (#3724).

asyncio schedules on ``time.monotonic()``. On macOS that is ``mach_absolute_time()``, which
STOPS while the machine sleeps, so ``asyncio.wait_for(x, 1800)`` means 1800 seconds of
*awake* time. A board coder dispatched during a DarkWake (the brief background wakes a
sleeping Mac takes every ~16 minutes) held its 1800s bound for hours of real time: on one
night, 2,089s to 12,802s.

``wait_for`` here keeps its deadline on a clock that does count sleep and waits in short
slices, so after a wake the first awake slice sees the deadline has passed and times out
as usual. While the machine is awake it behaves like ``asyncio.wait_for``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable
from typing import TypeVar

T = TypeVar("T")

#: How often the deadline is re-checked. After a wake, a timeout fires at most this late.
DEFAULT_SLICE_S = 15.0


def _pick_clock():
    # Linux: CLOCK_BOOTTIME counts suspend (CLOCK_MONOTONIC does not). macOS: CLOCK_MONOTONIC
    # counts sleep (it is time.monotonic() that doesn't). Anything else: the wall clock, which
    # counts sleep but can be stepped by NTP; still better than a bound that ignores sleep.
    for name in ("CLOCK_BOOTTIME", "CLOCK_MONOTONIC"):
        clock_id = getattr(time, name, None)
        if clock_id is None:
            continue
        if name == "CLOCK_MONOTONIC" and not _is_macos():
            continue
        try:
            time.clock_gettime(clock_id)
        except OSError:
            continue
        return lambda: time.clock_gettime(clock_id)
    return time.time


def _is_macos() -> bool:
    import sys

    return sys.platform == "darwin"


#: Seconds on a clock that keeps counting while the machine sleeps.
now = _pick_clock()


async def wait_for(aw: Awaitable[T], timeout: float | None, *, slice_s: float | None = None) -> T:
    """Like ``asyncio.wait_for``, but the deadline counts time spent asleep.

    Raises ``TimeoutError`` when ``timeout`` seconds pass on the sleep-counting clock;
    ``None`` waits forever. Cancelling the caller cancels ``aw``. As with
    ``asyncio.wait_for``, a result that lands at the deadline is returned, not discarded:
    for a lock acquire, discarding it would leave the lock held by nobody.
    """
    if timeout is None:
        return await aw
    slice_s = DEFAULT_SLICE_S if slice_s is None else slice_s
    task = asyncio.ensure_future(aw)
    deadline = now() + timeout
    try:
        while True:
            # Wait first, check the deadline after: a task that is ready at once (a free lock)
            # always gets its step, even if the deadline has already passed.
            remaining = max(0.0, deadline - now())
            done, _ = await asyncio.wait({task}, timeout=min(slice_s, remaining))
            if done:
                return task.result()
            if deadline - now() <= 0:
                break
    except asyncio.CancelledError:
        task.cancel()
        raise
    if task.done():
        return task.result()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        raise TimeoutError from None
    return task.result()  # it completed while being cancelled: keep the result
