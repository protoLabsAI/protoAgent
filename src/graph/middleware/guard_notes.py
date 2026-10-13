"""Notes a guard middleware injects into a run — recognised by a tag, not by their text.

#3556. The completion guard, the round governor and the stall guard each append a
``HumanMessage`` to steer the model, then later need to find their own notes again: to
count nudges, to latch "already nudged this turn", to keep a note from breaking the run
it measures. They did that by matching a visible prefix (``[completion-guard]`` …) — but
a task prompt or an operator message is a ``HumanMessage`` too, so text that merely BEGAN
with the tag was counted as a note: a delegation whose prompt opened with
``[completion-guard]`` got one nudge instead of two; an operator message opening with
``[round-governor]`` suppressed that turn's re-grounding nudge. User-controlled text was
deciding control flow.

The prefix stays, for the model (providers never see ``additional_kwargs``). Recognition
reads the tag, the same lane as ``protoagent_injected_context``.
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage

# Marker on ``additional_kwargs``; the value names the guard that wrote the note.
GUARD_NOTE_KWARG = "protoagent_guard_note"


def guard_note(guard: str, text: str) -> HumanMessage:
    """A note from ``guard``, tagged so it can be told from anything a person typed."""
    return HumanMessage(content=text, additional_kwargs={GUARD_NOTE_KWARG: guard})


def is_guard_note(message, guard: str | None = None) -> bool:
    """Was this message written by a guard — by ``guard`` specifically, when given?"""
    if not isinstance(message, HumanMessage):
        return False
    tag = (getattr(message, "additional_kwargs", None) or {}).get(GUARD_NOTE_KWARG)
    return bool(tag) and (guard is None or tag == guard)
