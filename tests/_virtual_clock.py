"""A virtual clock for the A2A stall guard (``a2a_impl.executor._stall_guarded``).

A "keeps working outlives the stall window" test needs two things at once: every gap
between frames under the window, and the whole run longer than it. On the wall clock
that means a sub-second window racing real sleeps, and a loaded CI box (xdist, a GC
pause, a busy executor thread) stretches one healthy gap past it — a false stall that
says nothing about the product (#4167's CI run).

Here time moves only when the work under test says it does: ``await clock.sleep(s)``
stands in for a step that takes ``s`` seconds, and the guard's window is measured on the
same clock (``install`` swaps ``executor._frame_within``). Scheduling delay costs zero
virtual time, so a healthy gap can never stretch, and the run takes no real time at all.
The guard still trips the moment virtual time passes a window with no frame — a step
that stops reporting progress is still caught, deterministically.
"""

from __future__ import annotations

import asyncio

import a2a_impl.executor as executor

# A real-time backstop on each guarded wait, so a broken test fails instead of hanging.
# Generous: virtual time is what the test is about; this only bounds a wedge.
_REAL_CAP_S = 60.0


class _Window:
    __slots__ = ("closes_at", "step", "expired", "fired")

    def __init__(self, closes_at: float, step: asyncio.Task) -> None:
        self.closes_at = closes_at
        self.step = step
        self.expired = False  # virtual time passed `closes_at` with no frame yet
        self.fired = asyncio.get_running_loop().create_future()


class VirtualClock:
    def __init__(self) -> None:
        self.now = 0.0
        self._open: list[_Window] = []

    async def sleep(self, seconds: float) -> None:
        """The work under test taking ``seconds``: advance, close every window that has
        now run out with no frame, and yield once so the loop can act on it."""
        self.now += seconds
        for window in self._open:
            if window.closes_at < self.now and not window.step.done() and not window.fired.done():
                # Decided HERE, at the virtual instant the window runs out: a frame that
                # arrives after it is late, however soon after in real time.
                window.expired = True
                window.fired.set_result(None)
        await asyncio.sleep(0)

    async def frame_within(self, step: asyncio.Task, seconds: float) -> bool:
        """``executor._frame_within`` on this clock: True if ``step`` finishes before
        ``seconds`` of VIRTUAL time pass, False if they pass first."""
        window = _Window(self.now + seconds, step)
        self._open.append(window)
        try:
            await asyncio.wait({step, window.fired}, timeout=_REAL_CAP_S, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self._open.remove(window)
            window.fired.cancel()
        return step.done() and not window.expired

    def install(self, monkeypatch) -> VirtualClock:
        # No `raising=False`: a renamed seam must fail loudly, not quietly put these tests
        # back on the wall clock.
        monkeypatch.setattr(executor, "_frame_within", self.frame_within)
        return self
