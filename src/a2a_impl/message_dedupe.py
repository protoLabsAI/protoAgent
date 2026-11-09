"""One run per client message: a re-sent ``messageId`` joins its first task (#4975).

A2A has the sender mint a ``messageId`` per message; a client that sends the same message
again (a retry, a replayed request) is sending the SAME message, not a new one. a2a-sdk
does not check: every ``SendMessage`` / ``SendStreamingMessage`` without a ``taskId``
creates a fresh task and runs the agent again. The desktop console hit exactly that. Its
turn streams through a Tauri command, and when the page navigated away mid-turn (an agent
switch) WebKit cancelled the command's IPC request. Tauri's IPC script reads any failed
request as "the custom protocol is blocked" and replays the same command over
``postMessage``, so the shell POSTed the identical A2A body a second time and the agent
answered the one message twice.

The send wrappers (``hitl_routing.install_parked_task_routing``) now consult a
:class:`MessageLedger` first. A message whose ``(contextId, messageId)`` it has already
accepted does not start a run:

- if the first send has not named its task yet, the duplicate waits until it does; if the
  first send fails before a task exists, the duplicate is just a retry and runs normally;
- a unary duplicate answers with the first task as it stands;
- a streaming duplicate subscribes to the first task while it runs, or gets its final
  snapshot once it has ended.

The ledger is in memory and bounded (size and age): it covers the replay and retry
window, not a resend after a server restart. A message with no ``contextId`` or no
``messageId`` is never matched.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from a2a.types import Message, SubscribeToTaskRequest, Task, TaskState
from a2a.utils.errors import TaskNotFoundError, UnsupportedOperationError

log = logging.getLogger(__name__)

_TERMINAL = (
    TaskState.TASK_STATE_COMPLETED,
    TaskState.TASK_STATE_FAILED,
    TaskState.TASK_STATE_CANCELED,
    TaskState.TASK_STATE_REJECTED,
)

# How long an accepted message is remembered, and how many are kept. A replay lands within
# seconds; an hour covers a slow client retry loop with room to spare.
_TTL_S = 60 * 60
_MAX_ENTRIES = 4096


@dataclass
class _Accepted:
    key: tuple[str, str]
    accepted_at: float
    task_id: str = ""
    known: asyncio.Event = field(default_factory=asyncio.Event)


def _key(message: Message) -> tuple[str, str] | None:
    ctx = str(getattr(message, "context_id", "") or "")
    mid = str(getattr(message, "message_id", "") or "")
    return (ctx, mid) if ctx and mid else None


def event_task_id(event: Any) -> str:
    """The task id an SDK send event belongs to ("" if it names none)."""
    if isinstance(event, Task):
        return event.id
    return str(getattr(event, "task_id", "") or "")


class MessageLedger:
    """The client messages this handler has accepted, keyed by ``(contextId, messageId)``."""

    def __init__(self, *, ttl_s: float = _TTL_S, max_entries: int = _MAX_ENTRIES) -> None:
        self._ttl_s = ttl_s
        self._max = max_entries
        self._entries: OrderedDict[tuple[str, str], _Accepted] = OrderedDict()

    def _prune(self, now: float) -> None:
        while self._entries:
            key, entry = next(iter(self._entries.items()))
            if len(self._entries) <= self._max and now - entry.accepted_at <= self._ttl_s:
                return
            self._entries.pop(key)
            entry.known.set()  # nobody may wait on a forgotten entry

    def accept(self, message: Message) -> tuple[bool, _Accepted | None]:
        """Record ``message`` as accepted. Returns ``(fresh, entry)``: ``fresh`` is False when
        the same message was already accepted (``entry`` is then the first one's).
        Synchronous on purpose: two copies arriving together must not both read "fresh"."""
        key = _key(message)
        if key is None:
            return True, None
        now = time.monotonic()
        self._prune(now)
        prior = self._entries.get(key)
        if prior is not None:
            return False, prior
        entry = _Accepted(key=key, accepted_at=now)
        self._entries[key] = entry
        return True, entry

    @staticmethod
    def bind(entry: _Accepted | None, task_id: str) -> None:
        """The first send's task is known: wake any duplicate waiting on it."""
        if entry is None or entry.task_id or not task_id:
            return
        entry.task_id = task_id
        entry.known.set()

    def drop(self, entry: _Accepted | None) -> None:
        """The first send ended without ever naming a task (it failed before one existed):
        forget it, so a retry of the same message runs. A no-op once a task is bound."""
        if entry is None or entry.task_id:
            return
        if self._entries.get(entry.key) is entry:
            self._entries.pop(entry.key)
        entry.known.set()

    @staticmethod
    async def task_of(entry: _Accepted) -> str:
        """The first send's task id, waiting for the first send to name it. "" when it ended
        without one (it failed before a task existed), so the duplicate is a plain retry.

        No timeout: a unary first send names its task only when it returns, at the end of
        the turn, and a duplicate that gave up waiting would run the turn a second time."""
        if not entry.task_id:
            await entry.known.wait()
        return entry.task_id


async def answer_duplicate(handler: Any, message: Message, task_id: str, context: Any) -> Task | None:
    """The unary answer to a re-sent message: the first task as it stands now."""
    task = await handler.task_store.get(task_id, context)
    log.info(
        "[a2a] message %s in %s was already accepted as task %s; not running it again",
        message.message_id,
        message.context_id,
        task_id,
    )
    return task


async def stream_duplicate(handler: Any, message: Message, task_id: str, context: Any):
    """The streaming answer to a re-sent message: follow the first task while it runs, or
    its final snapshot once it has ended. Never runs the agent."""
    log.info(
        "[a2a] message %s in %s was already accepted as task %s; streaming that task instead of a new run",
        message.message_id,
        message.context_id,
        task_id,
    )
    task = await handler.task_store.get(task_id, context)
    if task is None:
        return
    if task.status.state not in _TERMINAL:
        try:
            async for event in handler.on_subscribe_to_task(SubscribeToTaskRequest(id=task_id), context):
                yield event
            return
        except (UnsupportedOperationError, TaskNotFoundError):
            # It ended between the read and the subscribe, or is no longer live here (a parked
            # task with no running producer): serve the stored snapshot.
            task = await handler.task_store.get(task_id, context)
            if task is None:
                return
    yield task
