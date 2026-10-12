"""Small text helpers shared across layers (no imports beyond the stdlib)."""

from __future__ import annotations

import re

_SENTENCE_END = re.compile(r"(?<=[.!?])\s|\n")


def first_sentence(text: str, max_len: int = 120) -> str:
    """The opening sentence (or line) of `text`, whitespace-flattened and clipped.

    The operator-facing stand-in for a long body of text: a delegation's brief restates
    everything the delegate needs, so its first sentence is the most that belongs in a chat
    row or a job title. One home so the row and the job it tracks can't disagree.
    """
    first = _SENTENCE_END.split(str(text or "").strip(), maxsplit=1)[0]
    line = " ".join(first.split())
    return line if len(line) <= max_len else f"{line[: max_len - 1].rstrip()}…"
