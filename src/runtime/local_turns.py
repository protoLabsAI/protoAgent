"""Live, cancelable non-A2A chat turns (#4205).

Own references only for the driver's lifetime. These are HTTP/plugin invocations,
not A2A tasks: expose them separately and never invent durable A2A history.
"""
from __future__ import annotations

import asyncio
import functools
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class LocalTurn:
    id: str
    session_id: str
    origin: str
    task: asyncio.Task
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    stopped: asyncio.Event = field(default_factory=asyncio.Event)
    cancel_requested: bool = False


# All accesses happen on the serving event loop; no executor-thread mutations.
_ACTIVE: dict[str, LocalTurn] = {}


def snapshot(session_id: str) -> list[dict]:
    """Metadata only — never copy prompts or incognito content into a new store."""
    return [
        {"id": turn.id, "origin": turn.origin, "started_at": turn.started_at,
         "cancel_requested": turn.cancel_requested}
        for turn in _ACTIVE.values() if turn.session_id == session_id
    ]


async def cancel(session_id: str, turn_id: str, *, timeout: float = 5) -> bool:
    """Cancel one invocation and wait for its driver/lock cleanup.

    A timeout is not a successful stop. Keep the entry reachable and avoid a second
    cancel interrupting cleanup; the caller can poll or retry the wait.
    """
    turn = _ACTIVE.get(turn_id)
    if turn is None or turn.session_id != session_id:
        return False
    if not turn.cancel_requested:
        turn.cancel_requested = True
        turn.task.cancel()
    await asyncio.wait_for(turn.stopped.wait(), timeout)
    return True


def cancellation_requested() -> bool:
    """Whether this driver's cancellation was an explicit operator Stop."""
    task = asyncio.current_task()
    return any(turn.task is task and turn.cancel_requested for turn in _ACTIVE.values())


def tracked(driver):
    """Register the entire driver, including its wait for the session lock."""
    @functools.wraps(driver)
    async def run(message, session_id, **kwargs):
        task = asyncio.current_task()
        assert task is not None
        turn = LocalTurn(uuid.uuid4().hex, session_id, kwargs.get("origin", "local"), task)
        _ACTIVE[turn.id] = turn
        try:
            return await driver(message, session_id, **kwargs)
        except asyncio.CancelledError:
            if not turn.cancel_requested:
                raise  # disconnect/shutdown cancellation keeps its normal semantics
            return [{"role": "assistant", "content": "Turn stopped. You can choose another model and continue this conversation.",
                     "error": {"type": "turn_cancelled", "message": "Turn stopped.", "upstream_status": None}}]
        finally:
            _ACTIVE.pop(turn.id, None)
            turn.stopped.set()
    return run
