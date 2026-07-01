"""Watch lifecycle hooks (ADR 0067 D3).

A plugin reacts when a watch trips — ``on_met`` (verifier passed), ``on_expired`` (deadline
passed), ``on_stalled`` (unchanged evidence for ``stall_after`` checks; the watch stays
active). Populated by the loader at graph build via ``set_watch_hooks`` (re-set on reload),
fired from ``WatchController``. A hook fn takes the ``Watch``; sync or async. A hook that
raises is logged and swallowed — a bad hook must never break the tick.
"""

from __future__ import annotations

import inspect
import logging

log = logging.getLogger(__name__)

# Each entry: {"plugin_id", "on_met": fn|None, "on_expired": fn|None, "on_stalled": fn|None}.
_WATCH_HOOKS: list[dict] = []


def set_watch_hooks(hooks: list[dict] | None) -> None:
    """Replace the registered watch hooks (called at build + reload)."""
    _WATCH_HOOKS[:] = list(hooks or [])


async def fire_watch_hook(event: str, watch) -> None:
    """Fire the matching hook for ``event`` (``on_met`` | ``on_expired`` | ``on_stalled``)."""
    for hook in _WATCH_HOOKS:
        fn = hook.get(event)
        if fn is None:
            continue
        try:
            result = fn(watch)
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 — a bad hook must not break the tick
            log.exception("[watch] %s hook (plugin %s) failed", event, hook.get("plugin_id"))
