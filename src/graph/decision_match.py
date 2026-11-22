"""Conservative whole-message matching for typed no-pause decision answers."""

import unicodedata
from collections.abc import Sequence

from a2a_impl.auth import is_human_operator, open_mode
from graph.decisions import Decision, is_unpaused_decision


def _normalized(text: str) -> str:
    return " ".join("".join(
        char for char in text.casefold() if not unicodedata.category(char).startswith("P")
    ).split())


def match_decision(text: str, decisions: Sequence[Decision], *, principal: str | None,
                   server_started: bool = False) -> tuple[Decision, str] | None:
    """Return the sole matching (decision, canonical option id) pair, or None.

    ``decisions`` contains the chat's open records, including paused decisions. Identity
    must come from authentication, never message metadata. Ambiguous labels fail closed.
    """
    operator = is_human_operator(principal) or (principal == "open/unknown" and open_mode())
    if server_started or not operator or len(decisions) != 1:
        return None
    rec = decisions[0]
    if not rec.pending or not is_unpaused_decision(rec) or rec.requires_pick:
        return None
    normalized = _normalized(text)
    if not normalized:
        return None
    matches = [option["id"] for option in rec.options
               if normalized in {_normalized(option["id"]), _normalized(option["label"])}]
    return (rec, matches[0]) if len(matches) == 1 else None
