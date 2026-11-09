"""Process-local undo ledger for console deletes (#4955 Q10).

The Work view's delete actions — clear a goal, clear a watch — show a short "Undo"
toast (#4955 §4.5). Undo is done on the SERVER so it still works after the originating
tab is closed: a delete stashes a snapshot of the record here and hands back an opaque
token; ``POST /api/undo/{token}`` within the window replays the stashed restore, which
writes the record back through the store's own ``set`` (re-firing its
``goal.changed`` / ``watch.changed`` event, so every open console refreshes).

Entries expire after ``UNDO_WINDOW_S`` so a stale token can't resurrect a record much
later. Thread-safe and stdlib only — the console clear handlers run off the event loop
(``to_thread``), so the ledger is touched from worker threads.
"""

from __future__ import annotations

import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic

# How long a stashed record stays restorable, in seconds: the 8 s the Work view shows the
# Undo toast (#4955 §4.5) plus slack for the click-to-redeem round-trip, so a click at the
# very end of the toast still lands inside the window.
UNDO_WINDOW_S = 10.0


@dataclass
class _Entry:
    kind: str
    record: dict
    restore: Callable[[dict], None]
    expires_at: float


class UndoLedger:
    """A bounded, process-local map of undo token → stashed delete.

    ``stash`` snapshots a record and returns an opaque token; ``restore`` replays the
    stashed restore callable exactly once and drops the entry. An expired, unknown, or
    already-used token raises :class:`operator_api.routes.NotFoundError` (a 404 at the
    route), so a stale undo changes nothing.
    """

    def __init__(self, window_s: float = UNDO_WINDOW_S, clock: Callable[[], float] = monotonic):
        self._window_s = window_s
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}

    def stash(self, kind: str, record: dict, restore: Callable[[dict], None]) -> str:
        """Snapshot ``record`` and return a token that restores it for ``UNDO_WINDOW_S``.

        ``record`` is copied so a later mutation of the caller's dict can't change what
        undo writes back; on redeem ``restore`` is called with that snapshot.
        """
        token = secrets.token_urlsafe(24)
        now = self._clock()
        with self._lock:
            self._prune(now)
            self._entries[token] = _Entry(
                kind=kind,
                record=dict(record),
                restore=restore,
                expires_at=now + self._window_s,
            )
        return token

    def restore(self, token: str) -> str:
        """Replay the stashed restore for ``token`` once, drop it, and return its kind.

        Raises ``NotFoundError`` when the token is unknown, already used, or past the
        window — the route maps that to a 404. The entry is popped UNDER the lock (so two
        concurrent redeems can't both run it), but the restore itself runs OUTSIDE the
        lock — it writes through the store (file IO plus a bus publish) and must not block
        another thread's stash/restore.
        """
        now = self._clock()
        with self._lock:
            self._prune(now)
            entry = self._entries.pop(token, None)
        if entry is None:
            # Imported here (not at module scope) to avoid a routes ↔ undo import cycle —
            # routes.py reaches the ledger lazily too.
            from operator_api.routes import NotFoundError

            raise NotFoundError("This undo has expired or was already used.")
        entry.restore(entry.record)
        return entry.kind

    def _prune(self, now: float) -> None:
        """Drop expired entries. Caller holds the lock."""
        for token in [t for t, e in self._entries.items() if e.expires_at <= now]:
            del self._entries[token]


# Process-local singleton — the clear handlers stash here and POST /api/undo/{token}
# redeems here. One per process (a token is only meaningful within the process that
# minted it); never rebound.
LEDGER = UndoLedger()
