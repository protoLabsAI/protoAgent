"""Activity provenance feed (ADR 0022).

Besides the :class:`ActivityLog` store, this package is the ONE seam through
which in-graph code surfaces operator-relevant notices into the console feed
(#2262): ``graph/`` must not import ``server/`` (import-linter contract), and
the feed's path derivation needs server-side identity — so the server BINDS the
feed it builds at boot (``set_default_feed``), and everything below the server
calls best-effort ``emit()``. Before binding (early boot, bare tests) emit is a
silent no-op; it never raises — a notice must never break the turn it's about.
"""

from __future__ import annotations

import logging

from activity.store import ActivityLog

log = logging.getLogger(__name__)

__all__ = ["ActivityLog", "emit", "set_default_feed"]

_default_feed: ActivityLog | None = None


def set_default_feed(feed: ActivityLog | None) -> None:
    """Bind the process-wide feed ``emit`` appends to. The server calls this right
    after building the per-instance ActivityLog (``server/agent_init``); passing
    None unbinds (tests)."""
    global _default_feed
    _default_feed = feed


def emit(text: str, *, origin: str = "system", trigger: str = "", context_id: str = "") -> None:
    """Append one notice row to the bound feed, best-effort.

    The lane for middleware/infra warnings the OPERATOR should see (a provider
    silently ignoring prompt caching, a store misbehaving) — the console feed
    renders unknown origins generically, so ``origin="system"`` needs no
    frontend support. No-op when no feed is bound; never raises.
    """
    feed = _default_feed
    if feed is None or not text:
        return
    try:
        feed.add(context_id=context_id, origin=origin, trigger=trigger, text=text)
    except Exception:  # noqa: BLE001 — a notice must never break what it narrates
        log.debug("[activity] emit failed", exc_info=True)
