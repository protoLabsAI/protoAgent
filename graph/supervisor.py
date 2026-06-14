"""Supervised background task — a self-perpetuating async loop with a watchdog.

The lifecycle pattern protoAgent plugins reinvent for any long-running background engine
(the SpaceTraders fleet autopilot, a board reconciler, an ingest worker, a poller): run a
unit of work back-to-back, with a watchdog that **re-kicks** it if it crashes, **restarts**
it if it stalls, and **recovers** from a known fault — so the loop survives unattended. The
deterministic watchdog is the heartbeat; an LLM agent (if any) is the exception handler, not
the reverse.

The plugin supplies only the *work* (one window) and the *predicates* (is it stalled? can a
crash be recovered?); the Supervisor owns the create / cancel / re-kick / restart / heartbeat
machinery and exposes a ``status()`` dict.

Pure asyncio + stdlib — no protoAgent host deps, so it unit-tests directly. Plugins reach it
via ``from graph.sdk import supervise`` (or ``Supervisor``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

log = logging.getLogger("protoagent.supervisor")

WorkFn = Callable[[], Awaitable[Any]]


async def _maybe_await(value: Any) -> Any:
    """Await ``value`` if it's a coroutine, else return it — so predicates/hooks may be
    sync or async."""
    if asyncio.iscoroutine(value):
        return await value
    return value


class Supervisor:
    """Runs ``work`` (an async no-arg callable) under a watchdog.

    Args:
        work: ``async () -> Any`` — one unit of work (e.g. one engine window). Its return is
            stored as ``status()['result']``.
        name: label for logs/status.
        loop: run ``work`` back-to-back (default) vs once.
        breath: seconds to pause between work units when looping.
        interval: watchdog check period (seconds).
        on_crash: ``(result) -> bool`` (sync/async) called when the runner crashed or stopped
            while still wanted-running. Return True if the fault was handled (the watchdog
            then re-kicks), False to clear want-running and stop the storm (an unrecoverable
            fault). Called at most once per down-streak. ``result`` is the last
            ``{"error": …}`` / ``{"stopped": …}``.
        progress: ``() -> Any`` (sync/async) — a token that CHANGES while the task makes
            progress (e.g. ``len(log)``). The watchdog flags a stall when it stops changing.
        stall_check: ``() -> bool`` (sync/async) — confirm a *real* stall (e.g. "no ship in
            transit"), so a legitimately-long unit of work isn't false-tripped. A stall needs
            BOTH frozen progress (if ``progress`` is set) AND ``stall_check`` truthy (if set).
        stall_ticks: consecutive stalled checks before the watchdog restarts the runner.
        rekicks_warn: emit a "persistently failing" event after this many consecutive re-kicks.
        event_cap: keep the last N lifecycle events for ``status()``.
    """

    def __init__(
        self,
        work: WorkFn,
        *,
        name: str = "task",
        loop: bool = True,
        breath: float = 3.0,
        interval: float = 90.0,
        on_crash: Callable[[Any], Any] | None = None,
        progress: Callable[[], Any] | None = None,
        stall_check: Callable[[], Any] | None = None,
        stall_ticks: int = 3,
        rekicks_warn: int = 5,
        event_cap: int = 30,
    ):
        self._work = work
        self.name = name
        self._loop = loop
        self._breath = breath
        self._interval = interval
        self._on_crash = on_crash
        self._progress = progress
        self._stall_check = stall_check
        self._stall_ticks = stall_ticks
        self._rekicks_warn = rekicks_warn
        self._event_cap = event_cap

        self._task: asyncio.Task | None = None
        self._watchdog: asyncio.Task | None = None
        self._want = False        # operator wants it running; the watchdog keeps it there
        self._stop = False        # graceful wind-down after the current unit
        self._crashed = False     # last runner exit was an exception (vs clean / cancelled)
        self._result: Any = None
        self._restarts = 0
        self._events: list[str] = []

    # ── public API ────────────────────────────────────────────────────────────────────
    def start(self) -> str:
        """Start (or ensure running). Idempotent; also (re)starts the watchdog."""
        self._want = True
        self._ensure_watchdog()
        if self.running():
            return f"{self.name}: already running"
        self._spawn_runner()
        self._event("started")
        return f"{self.name}: started"

    def stop(self) -> str:
        """Operator stop — cancel now; the watchdog will NOT re-kick."""
        self._want = False
        task = self._task
        if task is None or task.done():
            return f"{self.name}: not running"
        task.cancel()
        self._event("stopped (operator)")
        return f"{self.name}: stopping"

    def request_stop(self) -> None:
        """Graceful wind-down: finish the current unit of work, then stop (no re-kick)."""
        self._want = False
        self._stop = True

    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> dict:
        return {
            "name": self.name,
            "running": self.running(),
            "want_running": self._want,
            "watchdog": self._watchdog is not None and not self._watchdog.done(),
            "restarts": self._restarts,
            "result": self._result,
            "events": self._events[-8:],
        }

    # ── internals ─────────────────────────────────────────────────────────────────────
    def _event(self, msg: str) -> None:
        self._events.append(msg)
        del self._events[: -self._event_cap]
        log.debug("[supervisor:%s] %s", self.name, msg)

    def _spawn_runner(self) -> None:
        self._stop = False
        self._task = asyncio.create_task(self._runner(), name=f"{self.name}-runner")

    async def _runner(self) -> None:
        me = asyncio.current_task()
        self._crashed = False
        try:
            while not self._stop:
                self._result = await self._work()
                if not self._loop:
                    break
                await asyncio.sleep(self._breath)
            if self._loop:
                self._event("wound down")
        except asyncio.CancelledError:
            self._result = {"stopped": True}
            raise
        except Exception as e:  # noqa: BLE001 — surface, don't crash the supervisor
            self._crashed = True
            self._result = {"error": f"{type(e).__name__}: {e}"}
            self._event(f"crashed: {type(e).__name__}: {e}")
        finally:
            # Only detach if we're still the active runner — a stall-restart may have already
            # swapped in a new task, and an old cancelled task must not null it out.
            if self._task is me:
                self._task = None

    def _ensure_watchdog(self) -> None:
        if self._watchdog is None or self._watchdog.done():
            self._watchdog = asyncio.create_task(self._watch(), name=f"{self.name}-watchdog")

    async def _restart_runner(self) -> None:
        old = self._task
        self._task = None                 # detach first (the guarded finally won't fight us)
        if old is not None and not old.done():
            old.cancel()                  # fire-and-forget; its finally is guarded
        self._restarts += 1
        self._spawn_runner()

    async def _watch(self) -> None:
        frozen = 0
        rekicks = 0
        recovered = False
        last_token: Any = object()        # sentinel so the first compare is never "frozen"
        while True:
            try:
                await asyncio.sleep(self._interval)
                if not self._want:
                    frozen = 0
                    continue

                # (1) not running while wanted. A clean one-shot completion (loop=False, no
                #     crash) is DONE — clear want, don't re-run. Otherwise recover + re-kick.
                if not self.running():
                    if not self._loop and not self._crashed:
                        self._want = False
                        self._event("completed")
                        continue
                    if self._on_crash is not None and not recovered:
                        recovered = True
                        try:
                            handled = bool(await _maybe_await(self._on_crash(self._result)))
                        except Exception as e:  # noqa: BLE001
                            handled = False
                            self._event(f"on_crash errored: {e}")
                        if not handled:
                            self._want = False  # unrecoverable → don't spin
                            self._event("unrecoverable — want_running cleared")
                            continue
                        self._event("recovered via on_crash")
                    rekicks += 1
                    if rekicks == self._rekicks_warn:
                        self._event("persistently failing — may need attention")
                    self._event(f"re-kick #{rekicks}")
                    self._spawn_runner()
                    frozen = 0
                    continue
                rekicks = 0
                recovered = False

                # (2) running but stalled → restart. Needs frozen PROGRESS (if a progress fn
                #     is set) AND a truthy STALL_CHECK (if set); with neither, no stall detection.
                if self._progress is None and self._stall_check is None:
                    continue
                token = await _maybe_await(self._progress()) if self._progress else None
                progress_frozen = self._progress is None or token == last_token
                last_token = token
                confirmed = True
                if self._stall_check is not None:
                    confirmed = bool(await _maybe_await(self._stall_check()))
                frozen = frozen + 1 if (progress_frozen and confirmed) else 0
                if frozen >= self._stall_ticks:
                    self._event("stall detected — restarting")
                    await self._restart_runner()
                    frozen = 0
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — the watchdog must NEVER die
                await asyncio.sleep(self._interval)

    async def aclose(self) -> None:
        """Cancel the runner + watchdog and await them — for clean teardown (tests, shutdown)."""
        self._want = False
        for task in (self._task, self._watchdog):
            if task is not None and not task.done():
                task.cancel()
        for task in (self._task, self._watchdog):
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._task = self._watchdog = None


def supervise(work: WorkFn, **kwargs: Any) -> Supervisor:
    """Create a (not-yet-started) :class:`Supervisor` for ``work``; call ``.start()``.

    Example — a plugin's background engine, self-perpetuating + supervised::

        from graph.sdk import supervise

        engine = supervise(run_one_window, name="fleet", interval=90,
                           progress=lambda: len(LOG),
                           stall_check=lambda: not any_ship_in_transit(),
                           on_crash=recover_from_reset)
        engine.start()           # returns immediately; runs in the background
        ...
        engine.status()          # {running, want_running, restarts, result, events, …}
        engine.request_stop()    # graceful wind-down (e.g. from a goal on_achieved hook)
    """
    return Supervisor(work, **kwargs)
