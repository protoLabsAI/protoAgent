"""Last-dispatch outcome per delegate — what the health prober can't tell you.

The prober answers "can I reach it?"; a dispatch answers "did the work go through?".
Those come apart, and the gap is where operators get stuck: an `acp` probe runs only the
ACP ``initialize`` handshake, so a coder whose binary launches fine but fails every
*session* shows a green dot while every `delegate_to` call fails. The console had no
record of the failure at all — it lived in the tool reply, in whichever chat happened to
trigger it.

So record each dispatch's outcome, keyed by delegate name, and let the panel show the last
one beside the health dot.

Deliberately in-memory and process-local, like ``health.py``'s cache: this is a debugging
aid for the running instance, not an audit log. It costs one dict entry per delegate and
is rebuilt by use.

**What counts as a failure.** Only a raised dispatch error. Not a cancellation — an
operator stopping a turn is not the delegate misbehaving, and recording it as one would
put a red mark on a healthy coder every time someone hit stop. Not a task-level
disappointment either: a coder that runs to completion and reports it couldn't do the job
dispatched *fine*. This tracks the transport, which is exactly the axis the health dot
also tracks and the one the two can disagree on.
"""

from __future__ import annotations

import time

# name -> {ok: bool, at: float, error: str}
_LAST: dict[str, dict] = {}

# Dispatch errors are already capped by their producers, but this is a second ceiling on
# what the API hands the panel — a row subtitle, not a log viewer.
_ERROR_LIMIT = 400


def record_success(name: str) -> None:
    """Note that a dispatch to ``name`` completed. Overwrites a prior failure — a stale
    error that never clears is worse than no error, because it reads as current."""
    _LAST[name] = {"ok": True, "at": time.time()}


def record_failure(name: str, error: str) -> None:
    """Note that a dispatch to ``name`` raised, with the operator-facing reason."""
    _LAST[name] = {"ok": False, "at": time.time(), "error": " ".join(str(error).split())[:_ERROR_LIMIT]}


def snapshot() -> dict[str, dict]:
    """Last-dispatch outcome per delegate name (copy)."""
    return {k: dict(v) for k, v in _LAST.items()}


def prune(keep: set[str]) -> None:
    """Drop entries for delegates that no longer exist, so a long-lived process doesn't
    accumulate rows for names the operator deleted. Called from the health sweep, which
    already walks the live roster for exactly this reason."""
    for gone in [n for n in _LAST if n not in keep]:
        _LAST.pop(gone, None)


def reset() -> None:
    """Clear everything (tests)."""
    _LAST.clear()
