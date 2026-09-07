r"""Bounded multi-round rooms — the speaking order, and when the room is over (#3042).

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

    cap, rounds = round_cap(config), []
    plan = plan_round(targets, rounds, max_rounds=cap)
    while not plan.done:
        rounds.append([
            await run_mention(..., record_address=plan.record_address, drop_silence=plan.drop_silence)
            for name in plan.speakers
        ])
        plan = plan_round(targets, rounds, max_rounds=cap)
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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# Decoration a model wraps a one-word answer in, plus the punctuation it ends a line
# with: markdown emphasis and bullets, code ticks, quotes (straight and smart), parens,
# sentence punctuation. The prompt that ASKS for a pass writes the token as `pass`, so a
# model echoing that formatting back is the common shape, not the exotic one — and it
# routinely COMBINES the two (``**pass**.``, ``- `pass` ``, ``pass!!``). Which is why
# this is one class stripped from BOTH ends rather than a wrapper with the punctuation
# nested inside it: nesting made `**pass**` silence and `**pass**.` an answer, an
# asymmetry no model knows about. Stripping only at the ENDS is what keeps `pass` inside
# a sentence ("I'd pass on that approach") an answer.
_PASS_DECORATION = "*_`\"'\u2018\u2019\u201c\u201d()[]{}<>.,;:!?-\u2013\u2014 \t\n\r"

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
    return not stripped or stripped.strip(_PASS_DECORATION).casefold() == "pass"


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

    ``max_rounds`` is the EFFECTIVE cap for this room, which is the operator's
    ``room.max_rounds`` narrowed by what the cast can actually do — a room with one
    participant left is one round however high the knob is set (see ``plan_round``).
    Reading the effective value is what keeps ``drop_silence`` and ``cap_note`` honest:
    a solo cast is offered no pass it could not act on, and is told about no cap it did
    not really hit.

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

    Deliberately blind to ``error_kind``, including the "still running after Ns — the
    peer may still be working" timeout, even though the outcome carries the class and
    ``server/chat.py`` reads it for a different decision. Re-dispatching a
    still-working peer cannot rejoin its work: the room passes no resume handle, and the
    ``conversation_key`` an ``a2a`` member now gets (#3360) only GROUPS the retry with the
    first task under one ``contextId`` — it does not join the running task — so the retry
    opens a SECOND ``SendMessage`` task on a peer already busy with the first, waits the
    same ``poll_timeout_s`` again, and still returns nothing. Continuity makes that
    strictly worse rather than better: sharing a context is sharing the peer's THREAD, and
    a protoAgent peer serializes turns on one thread, so the retry does not even start
    until the turn it was meant to chase has finished. The operator would pay N timeouts
    and N duplicate tasks to be told the same thing N times. The member is not
    silently declared dead either way — its failure is written onto the thread as a
    ``(could not be reached: …)`` room message and the adapter's own "the peer may still
    be working; raise its poll timeout" text is quoted straight to the operator — and
    the cast guard in ``plan_round`` means a room that loses a participant this way ends
    at once rather than spending further rounds on the survivors.
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

    ``max_rounds`` is the operator's ceiling, not the room's: a cast that cannot hold a
    conversation is capped at one round regardless (see below). Junk (``None``, a
    string, ``inf``) floors at one round rather than raising — this is a public seam a
    second host calls directly, and a config typo must not take an operator's `@` down.
    """
    try:
        cap = max(1, int(max_rounds or 1))
    except (TypeError, ValueError, OverflowError):
        cap = 1
    order = list(dict.fromkeys(str(name) for name in addressed))  # de-dup, keep order
    dropped = _dropped(rounds)
    remaining = tuple(name for name in order if name not in dropped)
    index = len(rounds)

    # A round needs two participants to BE a round. With one speaker left there is
    # nobody to react to: `record_address` is False after round 1, so nothing is written
    # between that participant's own reply and its next turn — `catchup_window` returns
    # an EMPTY window and `_prompt` re-sends the operator's original words verbatim. So
    # rounds 2..N of a solo cast are provably a re-ask of a question already answered,
    # in the same ACP session, with the delegate's file writes and bill attached, and
    # the only brake is the model choosing the soft `pass`. `room.max_rounds` is one
    # global knob and `@one-agent do X` is the common address; multiplying THAT by the
    # cap is not what the operator opted into. Applied to the SURVIVORS, not to the
    # addressed set, so a room that loses a participant mid-way stops instead of
    # spending its remaining rounds on somebody talking to a failure envelope.
    if len(remaining) < 2:
        cap = 1

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

    Named only when that participant actually ANSWERED. ``truncated`` is computed before
    the dispatch and survives the failure path unchanged, so a delegate that refused the
    connection comes back ``{ok: False, truncated: True}`` — and telling the operator to
    widen a catch-up window underneath ``Delegate @proto failed: connection refused``
    points them at a knob that has nothing to do with why it failed. A silence is
    excluded for the same reason the reply body excludes it: a pass is not an answer, so
    there is no answer for the clipping to have shaped.
    """
    clipped = list(
        dict.fromkeys(
            str(o.get("author") or "")
            for o in outcomes
            if o.get("ok") and not o.get("silent") and o.get("truncated") and o.get("author")
        )
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
