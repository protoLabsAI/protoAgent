"""Make a runtime toolset change observable to the AGENT, not just the process (#2640).

protoAgent's premise is that capabilities appear at runtime — ``scaffold_plugin`` +
``reload_plugins``, ``enable_plugin``, a plugin update, an operator settings change all
rebuild the graph with a different toolset (ADR 0096's spine ends at *use*). Nothing told
the running agent, so an agent mid-session kept operating on a stale belief about what it
could do.

The observed failure is worse than a missed opportunity: the agent **refuses work it is
capable of**, politely and with reasoning, which reads to an operator as a missing
feature rather than a stale toolset. Live 2026-08-12: ``board_register_project`` was
deployed and bound, with a description matching the need almost word for word, and the
agent still reported "there's no config-write tool" — it had already concluded that
before the deploy, and had written the conclusion into its friction log, so the stale
belief was sitting in its own context reinforcing itself. It took an operator saying
"you have a tool for this" to dislodge.

So: record the bound toolset at every graph build, and when it CHANGES, leave a one-shot
note for the next turn. Deliberately narrow —

- **Silent on the first build.** No previous set means boot, not a change.
- **Silent when nothing changed.** The common case costs a set comparison.
- **One-shot.** Consumed by the next turn, not repeated — a standing banner would just
  become part of the wallpaper.
- **Both directions.** A tool that VANISHED matters as much: an agent that plans around
  a tool it no longer has fails at call time instead of planning differently.

Module-level state is per-process, which is the right scope: a fleet member is its own
process, and its toolset is its own.
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)

_lock = threading.Lock()
_known: frozenset[str] | None = None
_pending: dict[str, list[str]] | None = None

#: Cap on names listed per direction — a bundle install can add dozens, and the point is
#: to prompt a re-check, not to reproduce the tool list the model already has.
_MAX_LISTED = 12


def record_toolset(names) -> dict[str, list[str]] | None:
    """Record the currently-bound tool names; return the delta if this is a CHANGE.

    Returns ``None`` on the first call (boot) and whenever the set is unchanged, so the
    caller can't accidentally announce a non-event."""
    global _known, _pending
    current = frozenset(str(n) for n in names if n)
    with _lock:
        previous, _known = _known, current
        if previous is None or previous == current:
            return None
        delta = {
            "added": sorted(current - previous),
            "removed": sorted(previous - current),
        }
        _pending = delta
    log.info(
        "[tools] toolset changed: +%d -%d (%s)",
        len(delta["added"]),
        len(delta["removed"]),
        ", ".join(delta["added"][:5]) or "none added",
    )
    return delta


def take_pending_delta() -> dict[str, list[str]] | None:
    """Return the un-announced delta and clear it. One-shot by construction."""
    global _pending
    with _lock:
        delta, _pending = _pending, None
    return delta


def format_delta(delta: dict[str, list[str]]) -> str:
    """The note the model reads. Phrased as an instruction to re-check rather than a
    bare list, because the failure being fixed is a *stale conclusion* — the agent had
    the tool and didn't look. Naming the tools is what makes looking cheap."""
    lines: list[str] = []
    added, removed = delta.get("added") or [], delta.get("removed") or []
    if added:
        shown = ", ".join(added[:_MAX_LISTED])
        more = f" (+{len(added) - _MAX_LISTED} more)" if len(added) > _MAX_LISTED else ""
        lines.append(f"Now available: {shown}{more}.")
    if removed:
        shown = ", ".join(removed[:_MAX_LISTED])
        more = f" (+{len(removed) - _MAX_LISTED} more)" if len(removed) > _MAX_LISTED else ""
        lines.append(f"No longer available: {shown}{more}.")
    if not lines:
        return ""
    lines.append(
        "Your tools changed since your last turn. If you previously concluded you "
        "could not do something for lack of a tool, re-check before repeating that — "
        "it may be available now."
    )
    return "<tools_changed>\n" + "\n".join(lines) + "\n</tools_changed>"


def reset_for_tests() -> None:
    """Clear both the known set and any pending delta."""
    global _known, _pending
    with _lock:
        _known, _pending = None, None
