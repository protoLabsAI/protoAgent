"""Goal lifecycle hooks (ADR 0028 D4).

A plugin can react when a goal reaches a terminal state — push a notification,
record a finding, or set the next goal — turning the goal system into a building
block for a self-improving loop instead of a dead-end status.

Hooks are module-level (populated by the loader at graph build via ``set_goal_hooks``,
re-set on config reload) and fired from ``GoalController._finish``. A hook fn takes
the terminal ``GoalState``; it may be sync or async. A hook that raises is logged
and swallowed — a bad hook must never break the goal loop.
"""

from __future__ import annotations

import inspect
import logging

log = logging.getLogger(__name__)

# Each entry: {"plugin_id", "on_achieved": fn|None, "on_failed": fn|None, "on_stalled": fn|None}.
_GOAL_HOOKS: list[dict] = []


def set_goal_hooks(hooks: list[dict] | None) -> None:
    """Replace the registered goal hooks (called at build + reload)."""
    _GOAL_HOOKS[:] = list(hooks or [])


async def fire_goal_hooks(status: str, state) -> None:
    """Fire the matching hook for a terminal goal. ``status`` is ``achieved`` (→
    ``on_achieved``) or anything else — ``exhausted``/``unachievable`` (→ ``on_failed``)."""
    key = "on_achieved" if status == "achieved" else "on_failed"
    for hook in _GOAL_HOOKS:
        fn = hook.get(key)
        if fn is None:
            continue
        try:
            result = fn(state)
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 — a bad hook must not break the goal loop
            log.exception("[goal] %s hook (plugin %s) failed", key, hook.get("plugin_id"))


async def fire_stall_hook(state) -> None:
    """Fire the ``on_stalled`` hook for a monitor goal that stopped moving (ADR 0030 D5).

    Unlike :func:`fire_goal_hooks`, this does **not** end the goal — it's a signal that the
    external engine stopped earning (the verifier evidence hasn't changed for ``stall_after``
    checks), fired once per stall episode so a plugin can notify / record a finding / set a
    remediation goal while the objective stays alive. A hook that raises is logged and swallowed."""
    for hook in _GOAL_HOOKS:
        fn = hook.get("on_stalled")
        if fn is None:
            continue
        try:
            result = fn(state)
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 — a bad hook must not break the goal loop
            log.exception("[goal] on_stalled hook (plugin %s) failed", hook.get("plugin_id"))
