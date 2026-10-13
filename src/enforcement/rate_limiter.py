"""Sliding-window rate limiter for tool-call frequency.

In-memory only — resets on process restart (rate limits are per-process, not
persistent). Backported from the protoLabs fleet (pwnDeck), generalised to
neutral tool names.
"""

from __future__ import annotations

import time
from collections import defaultdict


class RateLimiter:
    """Sliding-window rate limiter keyed by action (tool) name.

    Args:
        limits: maps an action name to ``{"max": int, "window_seconds": int}``.
            Actions not present are unlimited.
            e.g. ``{"web_search": {"max": 20, "window_seconds": 60}}``
    """

    def __init__(self, limits: dict | None = None):
        self._limits = dict(limits or {})
        self._windows: dict[str, list[float]] = defaultdict(list)

    def check(self, action: str) -> tuple[bool, str | None]:
        """Return ``(allowed, reason)``. Records the call when allowed."""
        cfg = self._limits.get(action)
        if cfg is None:
            return True, None

        max_calls = int(cfg["max"])
        window = float(cfg["window_seconds"])
        now = time.monotonic()
        cutoff = now - window

        recent = [t for t in self._windows[action] if t > cutoff]
        self._windows[action] = recent

        if len(recent) >= max_calls:
            return False, (f"Rate limit exceeded for '{action}': {max_calls} calls per {window:g}s window.")
        recent.append(now)
        return True, None

    def reset(self, action: str | None = None) -> None:
        """Reset counters — one action, or all when ``action`` is None."""
        if action:
            self._windows.pop(action, None)
        else:
            self._windows.clear()
