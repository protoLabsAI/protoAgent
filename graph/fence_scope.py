"""The tool fence of the turn whose code is running right now (#1639/#2972).

A turn's tool fence rides its graph state (``subagent_fence``) and every fresh pass
stamps it explicitly — an unfenced pass stamps ``[]``. So work a turn LEAVES BEHIND to
run later as a turn of its own — a background job's push-resume nudge, a ``wait``
resume, a scheduled one-shot, a watch reaction, a goal's completion hooks — must record
the fence it was created under and carry it onto that later turn; otherwise the later
turn would run unfenced (it can no longer inherit the fence off the thread).

``SubagentFenceMiddleware`` opens a :func:`fence_scope` around every tool call it lets
through, so the tool body — and anything it calls, synchronously or via
``asyncio.to_thread`` / a task it starts (contextvars are copied) — reads the calling
turn's effective fence from :func:`current_fence`. Scopes nest by intersection
(narrowest wins): a nested subagent's tool never widens its parent's fence. Outside
any scope (a plugin route, a config lifecycle hook, the scheduler's own loop) the fence
is ``[]`` — no turn, no fence.
"""

from __future__ import annotations

import contextlib
from contextvars import ContextVar

_FENCE: ContextVar[tuple[str, ...]] = ContextVar("protoagent_turn_fence", default=())


def current_fence() -> list[str]:
    """The fence of the turn this code runs in (``[]`` = unfenced / not in a turn)."""
    return list(_FENCE.get())


def normalize_fence(fence) -> list[str]:
    """A stored/wire fence as a clean list of tool names. Falsy → ``[]`` (no fence); a
    truthy value that isn't a list fails CLOSED (deny-all), never open."""
    if not fence:
        return []
    if not isinstance(fence, (list, tuple)):
        from graph.middleware.subagent_fence import FENCE_DENY_ALL

        return [FENCE_DENY_ALL]
    return [str(t) for t in fence if str(t)]


@contextlib.contextmanager
def fence_scope(fence):
    """Run the block as code of a turn fenced by ``fence`` (intersected with any
    enclosing scope's — narrowest wins; a falsy fence adds no restriction)."""
    from graph.middleware.subagent_fence import intersect_fences

    token = _FENCE.set(tuple(intersect_fences(list(_FENCE.get()), normalize_fence(fence))))
    try:
        yield
    finally:
        _FENCE.reset(token)
