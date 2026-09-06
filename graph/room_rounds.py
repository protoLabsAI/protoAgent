"""Bounded multi-round rooms — the speaking order, and when the room is over (#3042).

`@proto @reviewer <question>` addresses two participants. Today each answers once and
the exchange ends, which is fine for "ask two people the same thing" and useless for
"let them work it out": neither ever sees the other's answer as something to respond
to. This module is the policy that generalizes one round into N — and, just as
importantly, the policy that STOPS.

It answers exactly one question, for one addressed run:

    given the addressed set, the rounds already dispatched, and each target's outcome
    in them — who (if anyone) speaks next, and is the room over?

**Pure.** No I/O, no model, no registry, no host imports: it never dispatches anything
and never learns what a delegate *meant*. The single thing it reads out of a reply is
whether that reply was silence (see ``is_silence``), and that check is a regex over the
whole string, not comprehension. Reply-text routing — a delegate's words deciding who
speaks next — shipped once and was reverted (#3067) as a capability leak: only the
operator (`@`) and the orchestrator (``delegate_to``) choose participants. The driver
keeps that property by construction, because the speaker set it returns is always a
subset of the set the operator addressed, in the order the operator wrote it.

Which makes the whole host side of a room this, and nothing else — ``server/chat.py``
runs exactly this loop, and a second host (a console room view, a headless driver) is
the same handful of lines::

    rounds: list[list[dict]] = []
    plan = plan_round(targets, rounds, max_rounds=round_cap(config))
    while not plan.done:
        rounds.append([
            await run_mention(..., record_address=plan.record_address, drop_silence=plan.drop_silence)
            for name in plan.speakers
        ])
        plan = plan_round(targets, rounds, max_rounds=plan.max_rounds)
    outcomes = [o for one_round in rounds for o in one_round]
    reply = "\n\n".join(x for x in (reply, catchup_note(outcomes), cap_note(plan)) if x)

The two levers ride on the plan deliberately: ``drop_silence`` is what keeps a
single-round address byte-identical to the behavior that shipped before rounds existed,
and a host re-deriving it by hand is how that invariant would quietly be lost.

The `pass` token is a CONTRACT WITH THE MODEL, and the other half of it lives in
``mention_op._PASS_OFFER``: a participant is told, in the prompt, that it may decline and
exactly how to spell it — but only when ``drop_silence`` says the decline will be
honored. Recognizing a token nobody was asked for is how a "settle" that never fires
looks from the inside.

The shape mirrors what two independent third parties converged on after hitting the
same wall in a comparable runtime — a deterministic round-robin over a FIXED cast, a
`pass` token for "nothing to add", and an all-pass round meaning the conversation
settled. Two things are deliberately *not* here:

* **No wall-clock cap.** A turn-length cap declares work dead while a participant is
  still doing it (44% of measured turns, in one such runtime's own numbers). Rounds are
  bounded; the time a round takes is not.
* **No silent truncation.** When the cap — rather than a settle — ends the room, the
  caller is handed a note to show the operator. A bound the operator cannot see is
  indistinguishable from the feature not working.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# Decoration a model wraps a one-word answer in: markdown emphasis, code ticks, quotes
# (straight and smart). The prompt that ASKS for a pass writes the token as `pass`, so a
# model echoing that formatting back is the common case, not the exotic one.
_PASS_WRAP = r"[*_`\"'\u2018\u2019\u201c\u201d\s]*"

# "I have nothing to add." Anchored to the WHOLE reply, so `pass` inside a sentence
# ("I'd pass on that approach") is an answer and counts as speaking — and so is a
# QUALIFIED one ("Pass, but note the auth change"), which carries something the room
# needs. Tolerant only of decoration around the bare word: wrapping parens, a trailing
# period, emphasis, ticks, quotes.
_PASS_RE = re.compile(rf"^{_PASS_WRAP}\(?\s*pass\s*\)?\s*[.!]?{_PASS_WRAP}$", re.IGNORECASE)

# Reasons a room ended. Plain strings rather than an enum, to match the dict-shaped
# outcomes the rest of the room already speaks in.
SETTLED = "settled"  # a whole round in which nobody spoke — the normal, good ending
CAPPED = "capped"  # `max_rounds` reached with the conversation still moving
EXHAUSTED = "exhausted"  # every addressed participant failed; nobody is left to ask


def is_silence(text: Any) -> bool:
    """Whether a reply says nothing — empty, whitespace, or a bare `pass` token.

    Silence is not a message: it is neither written to the room nor counted as speaking,
    so a participant with nothing to add can decline without either polluting the
    transcript or keeping the room alive for another round.
    """
    stripped = str(text or "").strip()
    return not stripped or bool(_PASS_RE.match(stripped))


def spoke(outcome: Mapping) -> bool:
    """Whether one dispatch outcome counts as someone having SPOKEN this round.

    A failed address is not speech — the delegate never received anything — and neither
    is silence. Everything else is.
    """
    return bool(outcome.get("ok")) and not is_silence(outcome.get("reply"))


@dataclass(frozen=True)
class RoundPlan:
    """The next round, or the end of the room.

    ``speakers`` is empty exactly when ``done`` is True. ``round_index`` is 1-based and
    names the round ``speakers`` would run (or, once done, how many rounds ran).
    ``reason`` is one of `SETTLED` / `CAPPED` / `EXHAUSTED` when done, else ``""``.

    ``record_address`` / ``drop_silence`` are the two levers a host hands straight to
    ``mention_op.dispatch_into_room`` for this round. They live here rather than in the
    host because they ARE the room's policy — in particular ``drop_silence`` is what
    keeps a single-round address byte-identical to the pre-multi-round behavior, and a
    second host (a console room view, a headless driver) re-deriving it by hand is
    exactly how that invariant gets lost.
    """

    speakers: tuple[str, ...]
    round_index: int
    done: bool
    reason: str
    max_rounds: int

    @property
    def record_address(self) -> bool:
        """Whether this round writes the operator's own message onto the thread.

        Only the first one does. Rounds 2..N re-address the SAME message; re-writing that
        envelope per round would read, in everyone's catch-up, as the operator repeating
        themselves. Meaningful on an open plan — a finished one dispatches nothing.
        """
        return self.round_index <= 1

    @property
    def drop_silence(self) -> bool:
        """Whether a `pass` is HONORED as silence in this run (see ``is_silence``).

        False whenever multi-round is off, and that is deliberate: at ``max_rounds: 1``
        a delegate that literally replies "pass" is quoted like any other answer, on the
        thread and to the operator, exactly as it always has been. Silence only becomes a
        move once there is a next round for it to decline. It is also what tells
        ``dispatch_into_room`` to OFFER the pass in the prompt — a participant that was
        never told it may decline never does, and the room then always runs to its cap.
        """
        return self.max_rounds > 1


def _dropped(rounds: Sequence[Sequence[Mapping]]) -> set[str]:
    """Targets that have failed an address and must not be dispatched again.

    A dead delegate answers no faster the third time. Re-dispatching it once per round
    is how a bounded room turns into N times the timeout the operator waits through.
    """
    return {
        str(outcome.get("author") or "")
        for one_round in rounds
        for outcome in one_round
        if not outcome.get("ok")
    }


def plan_round(
    addressed: Sequence[str],
    rounds: Sequence[Sequence[Mapping]],
    *,
    max_rounds: int = 1,
) -> RoundPlan:
    """Who speaks next — deterministic, given only what has already happened.

    ``addressed`` is the leading `@` run, in the order the operator wrote it. It is
    resolved BEFORE any dispatch and never grows, so nobody is ever dispatched only to
    decide they had nothing to say. ``rounds`` holds the rounds already dispatched, one
    list per round, each item a ``dispatch_into_room``-shaped mapping
    (``{"author", "ok", "reply", …}``).

    The room is over when any of these holds, checked in this order:

    1. **Exhausted** — every addressed participant has failed an address.
    2. **Settled** — the last round completed and nobody in it spoke.
    3. **Capped** — ``max_rounds`` rounds have run.
    """
    cap = max(1, int(max_rounds or 1))
    order = list(dict.fromkeys(str(name) for name in addressed))  # de-dup, keep order
    dropped = _dropped(rounds)
    remaining = tuple(name for name in order if name not in dropped)
    index = len(rounds)

    if not remaining:
        return RoundPlan((), index, True, EXHAUSTED if order else SETTLED, cap)
    if rounds and not any(spoke(outcome) for outcome in rounds[-1]):
        return RoundPlan((), index, True, SETTLED, cap)
    if index >= cap:
        return RoundPlan((), index, True, CAPPED, cap)
    return RoundPlan(remaining, index + 1, False, "", cap)


def catchup_note(outcomes: Sequence[Mapping]) -> str:
    """The operator-facing line for a room whose CATCH-UP truncated, else ``""``.

    ``dispatch_into_room`` has always returned ``truncated``, and it has always gone only
    into the delegate's own prompt preface ("earlier messages omitted") — so the operator
    read a confident answer given on a partial view of the room with no way to know, and
    no way to learn which knob widens it. The workaround people reach for is to
    re-mention, which fragments the very conversation the room is holding.

    Here rather than in the host for the same reason as ``cap_note``: it is the room's
    copy about the room's own bounds, and a second host that forgot to render it would
    reintroduce exactly the silence this fixes. Names every clipped participant once, in
    the order they were addressed.
    """
    clipped = list(
        dict.fromkeys(str(o.get("author") or "") for o in outcomes if o.get("truncated") and o.get("author"))
    )
    if not clipped:
        return ""
    who = ", ".join(f"@{name}" for name in clipped)
    return (
        f"_Older messages were left out of the catch-up for {who} — the room since they "
        f"last spoke is longer than the window. Raise `room.catchup_max_messages` / "
        f"`room.catchup_max_chars` to widen it._"
    )


def cap_note(plan: RoundPlan) -> str:
    """The operator-facing line for a room the CAP ended, else ``""``.

    Empty for a settle (the good ending needs no announcement), and empty whenever
    multi-round is off — a single-round room is the shipped behavior, and saying
    "stopped at the 1-round cap" after every `@` would be noise, not information.
    """
    if not plan.done or plan.reason != CAPPED or plan.max_rounds <= 1:
        return ""
    return (
        f"_The room stopped at its {plan.max_rounds}-round cap — the conversation had not "
        f"settled. Ask again to continue it, or raise `room.max_rounds`._"
    )
