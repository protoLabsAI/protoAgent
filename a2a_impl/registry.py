"""Owned-lifetime ActiveTaskRegistry — retires producer/consumer tasks at turn teardown (#1713).

Upstream bug (a2a-sdk 1.1.0, reported as a2aproject/a2a-python#1121 / #1123): at the end
of a turn the SDK's ``ActiveTask`` teardown drops the last external strong reference to
the still-*pending* ``producer:<task_id>`` asyncio task without cancelling or awaiting it:

1. On the terminal event the consumer sets ``_is_finished`` and shuts the request queue
   while the producer is still pending (``active_task.py:299-306``).
2. The producer wakes into its ``finally`` and parks at
   ``await self._event_queue_subscribers.close(immediate=False)`` (``active_task.py:566``
   — the exact line in the production tracebacks), an await the SDK itself documents as
   a deadlock risk when unconsumed events remain (``event_queue_v2.py:214-217``).
3. The consumer's ``finally`` drops the refcount to 0 and fires ``_on_cleanup`` →
   ``ActiveTaskRegistry._remove_task`` pops the ``ActiveTask`` from the registry dict —
   the only external strong reference — *without* touching ``_producer_task``.
4. The pending producer is now reachable only through the reference island
   ``ActiveTask._producer_task ↔ producer-coro.self``; cyclic GC collects it →
   ``ERROR asyncio Task was destroyed but it is pending!`` (103× in production logs,
   clustering at turn completions).

The real fix belongs upstream (strong-ref + cancel/await the producer in the SDK's own
teardown). Until that ships, this module contains it from the host side: a registry
subclass whose cleanup path *owns* the ActiveTask's background tasks for their full
lifetime — it holds a strong reference, gives the producer a short grace period to flush
naturally, then cancels and awaits it, so pending work either completes or fails loudly
(a logged warning) instead of being silently destroyed by the garbage collector.

This intentionally reaches into a2a-sdk private attributes (``_producer_task``,
``_consumer_task``, ``_active_task_registry``) — there is no public seam: protoAgent
supplies only the ``AgentExecutor``, and the producer task is created inside
``ActiveTask.start()``. Every access is guarded so an SDK upgrade that moves the
internals degrades to a logged warning + stock behavior, never a crash. Re-verify (and
ideally delete this module) when bumping past a2a-sdk 1.1.0 — see the upstream issues.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from a2a.server.agent_execution.active_task_registry import ActiveTaskRegistry

if TYPE_CHECKING:
    from a2a.server.agent_execution.active_task import ActiveTask

log = logging.getLogger(__name__)

# How long a pending producer/consumer gets to finish naturally before we cancel it.
# The common case (task merely not yet resumed when the last reference dropped) resolves
# in one loop iteration; the grace only matters when the final queue-join genuinely
# hangs, and then we *want* the deterministic cancel.
FLUSH_GRACE_S = 0.5
# Upper bound on waiting for a cancelled task to unwind before we warn and move on.
CANCEL_WAIT_S = 5.0


class OwnedProducerActiveTaskRegistry(ActiveTaskRegistry):
    """ActiveTaskRegistry that retires an ActiveTask's background tasks before
    dropping the last strong reference to them (#1713).

    The stock ``_on_active_task_cleanup`` schedules only ``_remove_task(task_id)``;
    this override schedules retire-then-remove, strong-ref'd in the registry's own
    ``_cleanup_tasks`` set (the same pattern the SDK already uses for the cleanup
    tasks themselves), with the ActiveTask kept alive by the coroutine's closure
    until its producer/consumer are done.
    """

    def _on_active_task_cleanup(self, active_task: ActiveTask) -> None:
        task = asyncio.create_task(
            self._retire_and_remove(active_task),
            name=f"a2a-retire:{active_task.task_id}",
        )
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    async def _retire_and_remove(self, active_task: ActiveTask) -> None:
        """Retire the producer/consumer tasks, then remove the ActiveTask.

        Removal always happens (even if retirement itself errors) so a defect here
        can never leak ActiveTasks in the registry — worst case we degrade to the
        stock drop-without-await behavior for that one task, with a logged error.
        """
        try:
            for attr in ("_producer_task", "_consumer_task"):
                bg = getattr(active_task, attr, None)
                if isinstance(bg, asyncio.Task):
                    await self._retire(bg, active_task.task_id)
        except Exception:  # pragma: no cover — defensive: never block removal
            log.exception(
                "[a2a] retiring background tasks for task %s failed", active_task.task_id
            )
        await self._remove_task(active_task.task_id)

    @staticmethod
    async def _retire(bg: asyncio.Task, task_id: str) -> None:
        """Await ``bg`` briefly so in-flight frames can flush; cancel+await if stuck."""
        if not bg.done():
            _, pending = await asyncio.wait({bg}, timeout=FLUSH_GRACE_S)
            if pending:
                log.debug(
                    "[a2a] %s still pending %.1fs after task %s teardown; cancelling",
                    bg.get_name(),
                    FLUSH_GRACE_S,
                    task_id,
                )
                bg.cancel()
                _, pending = await asyncio.wait({bg}, timeout=CANCEL_WAIT_S)
                if pending:  # pragma: no cover — nothing in the SDK shields cancel
                    log.warning(
                        "[a2a] %s for task %s did not stop within %.1fs of cancel; "
                        "leaving it referenced in the cleanup task",
                        bg.get_name(),
                        task_id,
                        CANCEL_WAIT_S,
                    )
                    # Keep a strong reference until it does finish, so it still
                    # can't be GC'd pending.
                    await asyncio.wait({bg})
                    return
        if bg.cancelled():
            return
        exc = bg.exception()
        if exc is not None:
            # The SDK's producer/consumer normally swallow their own errors; anything
            # surfacing here would previously have been silently destroyed with the
            # task. Fail loudly instead.
            log.warning(
                "[a2a] %s for task %s finished with an unhandled error at teardown: %r",
                bg.get_name(),
                task_id,
                exc,
            )


def harden_active_task_registry(handler: object) -> bool:
    """Swap ``handler._active_task_registry`` for the owned-producer subclass.

    Called at mount time, before the handler serves any request, so the stock
    registry being replaced is guaranteed empty. Returns ``True`` on success;
    on any surprise (an a2a-sdk upgrade moving the internals) it logs a warning
    and leaves the stock registry in place — never raises.
    """
    try:
        current = getattr(handler, "_active_task_registry", None)
        if not isinstance(current, ActiveTaskRegistry):
            raise TypeError(
                f"handler._active_task_registry is {type(current).__name__}, "
                "expected ActiveTaskRegistry"
            )
        handler._active_task_registry = OwnedProducerActiveTaskRegistry(  # type: ignore[attr-defined]
            agent_executor=current._agent_executor,
            task_store=current._task_store,
            push_sender=current._push_sender,
        )
    except Exception:
        log.warning(
            "[a2a] could not install the owned-producer task registry; falling back to "
            "the stock a2a-sdk registry (producer tasks may be GC'd while pending at "
            "turn end — #1713). Did an a2a-sdk upgrade move the internals?",
            exc_info=True,
        )
        return False
    return True
