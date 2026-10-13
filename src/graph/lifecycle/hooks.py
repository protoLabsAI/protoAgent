"""System lifecycle hooks (ADR 0074, extends ADR 0039).

A plugin can react when the *system* changes state — the app finished booting
(``app.loaded``), the agent went from idle to active (``agent.active``), or the
desktop shell woke from sleep/focus (``system.wake``, reserved). This mirrors the
goal-hook seam (``graph/goals/hooks.py``): hooks are module-level, populated by the
loader at graph build via ``set_lifecycle_hooks`` and re-set on config reload, and
fired from the lifecycle dispatcher (``graph/lifecycle/dispatch.py``).

A hook fn takes the event ``payload`` (a plain dict — ``ts`` + ``previous_state`` +
event-specific keys); it may be sync or async. A hook that raises is logged and
swallowed — a bad hook must never break boot or a turn.
"""

from __future__ import annotations

import inspect
import logging

log = logging.getLogger(__name__)

# Config-event name → the hook-dict callback key it fires. The three system events
# (kept in one place so the emit sites, the config, and the hooks agree on names).
_CALLBACK_KEY = {
    "app_loaded": "on_app_loaded",
    "agent_active": "on_agent_active",
    "system_wake": "on_system_wake",
}

# Each entry: {"plugin_id", "on_app_loaded": fn|None, "on_agent_active": fn|None,
# "on_system_wake": fn|None}.
_LIFECYCLE_HOOKS: list[dict] = []


def set_lifecycle_hooks(hooks: list[dict] | None) -> None:
    """Replace the registered lifecycle hooks (called at build + reload)."""
    _LIFECYCLE_HOOKS[:] = list(hooks or [])


def lifecycle_hooks() -> list[dict]:
    """The live registered lifecycle hooks (read-only view for /lifecycle + tests)."""
    return list(_LIFECYCLE_HOOKS)


async def fire_lifecycle_hook(event: str, payload: dict) -> None:
    """Fire the matching plugin hook for a system lifecycle ``event`` (``app_loaded`` →
    ``on_app_loaded``, ``agent_active`` → ``on_agent_active``, ``system_wake`` →
    ``on_system_wake``). An unknown event is a no-op; a hook may be sync or async; a
    raising hook is logged and swallowed."""
    key = _CALLBACK_KEY.get(event)
    if key is None:
        return
    for hook in _LIFECYCLE_HOOKS:
        fn = hook.get(key)
        if fn is None:
            continue
        try:
            result = fn(payload)
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 — a bad hook must not break the lifecycle path
            log.exception("[lifecycle] %s hook (plugin %s) failed", key, hook.get("plugin_id"))
