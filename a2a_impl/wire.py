"""Readers for the A2A wire shapes a CLIENT of our own ``/a2a`` sees.

The server-initiated turns (a scheduled fire, a background push-resume) work by
self-POSTing ``SendMessage`` and holding the connection for the whole turn. What comes
back is a JSON-RPC envelope carrying the durable Task — which is the only place the
caller can learn the **task id** the turn ran under, and that id is what makes the
turn-lifecycle events it publishes addressable (a `turn.finished` without one cannot say
WHICH turn finished, so a console holding a second live turn's control clears the wrong
thing).

Deliberately shape-tolerant: A2A 1.0 puts the task flat on ``result``, while some
responses nest it under ``result.task``. Anything unreadable yields ``""`` — a missing id
degrades to the old unscoped behavior, it never raises into a fire.
"""

from __future__ import annotations

from typing import Any


def task_id_from_response(payload: Any) -> str:
    """The durable task id in a ``SendMessage`` response, or ``""``."""
    if not isinstance(payload, dict):
        return ""
    result = payload.get("result")
    if not isinstance(result, dict):
        return ""
    task = result.get("task")
    if not isinstance(task, dict):
        task = result
    return str(task.get("id") or "")
