"""Console ↔ editor chat hand-off — the in-memory store behind "Continue in Zed".

Zed can't deep-link into an agent thread (``zed://agent`` only takes ``?prompt=``), so a
hand-off is OFFERED here — by the console's "Continue in Zed" action or by
``open_in_editor`` — and CLAIMED by the Zed ACP shim when the operator starts a new
thread: the shim posts the thread's ``cwd`` and, on a match, adopts the offered chat
session as its A2A contextId instead of minting a fresh one.

Rules (the shared console↔shim contract):

- Keyed by project ROOT; the latest offer per root wins. ``root=None`` is "any" — it
  matches every cwd (an offer made with no project in hand).
- TTL 120 s. Expired offers are dropped lazily on every read/write.
- One-shot: a claim removes the offer it returns.
- A cwd matches a root when it IS the root, is INSIDE it, or is a PARENT of it (Zed is
  often opened on a parent folder such as ``~/dev/nava``). The most recent unexpired
  match wins.

In-memory and per-process by design: a hand-off is a two-minute baton between two UIs
on the operator's machine, not state worth persisting across a restart. Lives in
``runtime/`` so both the tool layer (``tools/fs_tools.py``) and the HTTP layer
(``operator_api/``) can reach it without crossing the import layering.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePath

TTL_SECONDS = 120.0

# Key for an offer with no project root ("any cwd").
_ANY = "*"


@dataclass(frozen=True)
class Handoff:
    id: str
    session_id: str
    root: str | None  # absolute project root, or None = matches any cwd
    project: str | None
    path: str | None
    line: int | None
    title: str | None
    created: float  # wall clock (time.time()) — ordering + expiry
    expires_at: float

    def to_offer(self) -> dict:
        """The ``POST /api/editor/handoff`` response body."""
        return {"id": self.id, "expires_at": _iso(self.expires_at), "root": self.root}

    def to_claim(self) -> dict:
        """The ``POST /api/editor/handoff/claim`` 200 body."""
        return {
            "session_id": self.session_id,
            "project": self.project,
            "path": self.path,
            "line": self.line,
            "title": self.title,
        }


_LOCK = threading.Lock()
_STORE: dict[str, Handoff] = {}


def _iso(ts: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _norm(p: str) -> PurePath:
    """Absolute, symlink-resolved form of a path for comparison. The registry's roots are
    already ``Path.resolve()``d, so a cwd reached through a symlink must be too."""
    return Path(p).expanduser().resolve(strict=False)


def matches(root: str | None, cwd: str) -> bool:
    """Does an offer keyed on ``root`` match a thread started in ``cwd``?

    ``root=None`` matches anything. Otherwise ``cwd`` must be the root, inside it, or a
    parent of it. A blank cwd matches only the "any" offer."""
    if root is None:
        return True
    if not (cwd or "").strip():
        return False
    r, c = _norm(root), _norm(cwd)
    return c == r or c.is_relative_to(r) or r.is_relative_to(c)


def _purge(now: float) -> None:
    for key in [k for k, h in _STORE.items() if h.expires_at <= now]:
        del _STORE[key]


def offer(
    session_id: str,
    *,
    root: str | None = None,
    project: str | None = None,
    path: str | None = None,
    line: int | None = None,
    title: str | None = None,
    now: float | None = None,
) -> Handoff:
    """Record a hand-off of ``session_id`` for threads started under ``root`` (None = any).
    Replaces any earlier offer for the same root (latest write wins)."""
    now = time.time() if now is None else now
    h = Handoff(
        id=f"ho-{secrets.token_hex(6)}",
        session_id=session_id,
        root=str(_norm(root)) if root else None,
        project=project or None,
        path=path or None,
        line=line if isinstance(line, int) and line >= 1 else None,
        title=(title or "").strip() or None,
        created=now,
        expires_at=now + TTL_SECONDS,
    )
    with _LOCK:
        _purge(now)
        _STORE[h.root or _ANY] = h
    return h


def claim(cwd: str, *, now: float | None = None) -> Handoff | None:
    """Take (and remove) the most recent unexpired offer matching ``cwd``, or None."""
    now = time.time() if now is None else now
    with _LOCK:
        _purge(now)
        hits = [(key, h) for key, h in _STORE.items() if matches(h.root, cwd)]
        if not hits:
            return None
        key, best = max(hits, key=lambda kv: kv[1].created)
        del _STORE[key]
        return best


def pending() -> list[Handoff]:
    """Unexpired offers, newest first (diagnostics / tests)."""
    now = time.time()
    with _LOCK:
        _purge(now)
        return sorted(_STORE.values(), key=lambda h: h.created, reverse=True)


def clear() -> None:
    """Drop every offer (tests)."""
    with _LOCK:
        _STORE.clear()
