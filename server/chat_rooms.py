"""@-delegate rooms — the operator-`@` exchange behind the chat turn (#3838, epic #3804).

Extracted from ``server/chat.py``. A message that OPENS with ``@<delegate>`` (or a run
of them) short-circuits the LLM turn: this module parses the addressed cast
(``_parse_at_delegate`` / ``_parse_at_delegates``), drives the room
(``_at_delegate_exchange`` — fan-out, multi-round, late collection, the #3126 lead
fall-through) and composes the answer plus the note frame no bubble carries
(``_covered_by_a_bubble`` / ``_room_note`` / ``_with_room_notes``).

**The call site is in ``server.chat_dispatch``** (#3861): ``_pre_turn_dispatch`` takes the per-thread
lock and calls ``_chat_rooms._at_delegate_exchange`` through this module at call time,
so a test patch HERE intercepts it.

**The exchange's collaborator lives in ``server.turn_control``** (#3847). The
thread-id resolver (``_resolve_thread_id``) is reached through that module
(``_turn_control._resolve_thread_id``) at CALL time, never bound at import — a test's
patch on ``server.turn_control`` is what runs, and ``import server.chat_rooms`` works
standalone with no import-time edge back into ``server.chat``.

``server.chat`` re-exports every name here so ``from server.chat import
_at_delegate_reply`` keeps resolving. Patch these names HERE, not on ``server.chat``: a
re-export is a copy of the binding, so a ``setattr`` there intercepts nothing
(``tests/test_chat_rooms_seam.py`` enforces that).
"""

from __future__ import annotations

import contextlib
import logging

# Parsing + resolution live in ``graph.mentions`` (see the section comment below).
from graph.mentions import (
    mention_target as _mention_target,
    parse_mention as _leading_at_token,
)
from runtime.state import STATE
from server import turn_control as _turn_control

# Same logger as server.chat, so the moved log lines keep their channel.
log = logging.getLogger("protoagent.server")


# ── @-delegate dispatch (S1) ─────────────────────────────────────────────────
# A message that OPENS with `@<delegate-name>` is routed straight to that delegate
# over DelegateRegistry.dispatch() — the chat analogue of the `delegate_to` tool —
# short-circuiting the LLM turn entirely. The `@` must be the first non-whitespace
# character (no mid-message mentions in v1). The registry is published on STATE by
# the delegates plugin at register() time; with the plugin absent there is no
# registry and every `@` falls through to a normal turn.
# Parsing + resolution live in ``graph.mentions`` — ONE source shared with the console
# composer's `@` popover, the way ``graph.slash_commands`` is shared for `/`. A composer
# that autocompletes a name this dispatcher won't route sends the operator's message to
# the wrong participant, so the two must read the same roster through the same code.


def _parse_at_delegate(text: str) -> tuple[str, str] | None:
    """``(canonical_delegate_name, rest)`` when ``text`` opens with ``@<known-delegate>``,
    else ``None``.

    Case-insensitive against the live roster, returning the delegate's REGISTERED name —
    ``@Proto`` resolves to ``proto`` (``dispatch``/``get`` are case-sensitive). ``None``
    when the delegates plugin isn't loaded, the ``@`` isn't leading, or the token names no
    configured delegate. A bare ``@name`` still returns ``("name", "")`` — the empty-query
    case is an error the dispatcher surfaces, not a parse failure."""
    if not getattr(STATE, "delegate_registry", None):
        return None
    tok = _leading_at_token(text)
    if tok is None:
        return None
    canonical = _mention_target(tok[0])
    return None if canonical is None else (canonical, tok[1])


def _delegate_unavailable_msg(name: str) -> str:
    """The reply for an ``@name`` that names no reachable delegate — names the roster so
    the operator can correct the mention without opening the Delegates panel (r2)."""
    reg = getattr(STATE, "delegate_registry", None)
    avail = ", ".join(reg.names()) if reg else ""
    return f"Unknown or unreachable delegate: @{name}. Available: {avail or '(none)'}."


def _parse_at_delegates(text: str) -> tuple[list[str], str] | None:
    """The leading RUN of resolvable mentions and the message after it, else ``None``.

    ``@proto @reviewer what do you think?`` addresses BOTH: the run extends token by
    token while each next ``@token`` resolves to a delegate, and the first thing that
    is not a resolvable mention begins the message. So ``@proto @nope hi`` addresses
    proto with the message ``@nope hi`` — for tokens past the first, "doesn't resolve"
    means "is prose", exactly as a mid-message ``@`` is prose.

    The FIRST token keeps ``_parse_at_delegate``'s stricter contract: unknown ⇒ the
    caller answers with the roster rather than running the text as a prompt. Duplicates
    de-duplicate (addressing someone twice is one address).
    """
    first = _parse_at_delegate(text)
    if first is None:
        return None
    targets, rest = [first[0]], first[1]
    while True:
        nxt = _leading_at_token(rest)
        if nxt is None:
            break
        canonical = _mention_target(nxt[0])
        if canonical is None:
            break
        if canonical not in targets:
            targets.append(canonical)
        rest = nxt[1]
    return targets, rest


async def _at_delegate_exchange(
    message: str, session_id: str = "", request_metadata: dict | None = None
) -> tuple[str | None, list[dict] | None]:
    """The complete @-delegate short-circuit (S1), shared by the streaming and
    non-streaming turn drivers so both dispatch identically.

    Returns ``(reply, outcomes)``. ``reply`` is the assistant text to emit — the
    delegate's answer (attributed per participant when several were addressed), a usage
    hint for a bare ``@name``, or an "unknown delegate" roster — or ``None`` to fall
    through to a normal turn. ``outcomes`` is one structured record per exchange, for the
    authorship frames; ``None`` for the fall-through and error replies, which no
    participant authored.

    **A leading run of mentions fans out** (`_parse_at_delegates`): ``@proto @reviewer
    <msg>`` sends the message to each, SEQUENTIALLY and in the order written — so the
    second addressee's catch-up already contains the first one's reply. The room makes
    that true for free: each exchange is on the thread before the next one reads it.

    **And it can run for more than one round** (``graph/room_rounds.py``). The addressed
    set IS the cast, resolved before any dispatch; ``room.max_rounds`` bounds how many
    times it goes round. At the default ``1`` this loop is exactly the old single pass.
    Above 1 the same cast re-runs in the same order — each participant now catching up
    on what the others just said — until a round in which nobody speaks (the settle) or
    the cap. A participant with nothing to add replies with a bare ``pass`` — which the
    prompt explicitly offers it, because a participant that was never told it may decline
    never does — and that is silence: it neither lands on the thread nor keeps the room
    alive, and an all-silent round is how a room ends early. A target whose
    address FAILED is dropped from later rounds rather than retried into the operator's
    wait. Nothing here reads a delegate's reply for intent (#3067): who speaks next comes
    from the operator's addressed set and the room's structure, never from what was said.

    ``message`` is NOT mutated: the original ``@name rest`` is what the caller logs to
    session history, and only the stripped ``rest`` reaches the delegate.

    **The exchange is recorded on the session's checkpointer thread** (``run_mention``).
    That matters because a bare follow-up message goes to the LEAD agent, not to the
    delegate — without the record the lead cannot answer "what did proto say?" one message
    later, having never seen it. Recording it is also what lets the delegate be caught up
    on the room next time it is addressed.
    """
    reg = getattr(STATE, "delegate_registry", None)
    if not reg:
        return None, None
    tok = _leading_at_token(message)
    if tok is None:
        return None, None
    parsed = _parse_at_delegates(message)
    # A leading `@token` that resolves to no delegate → answer with the roster instead of
    # running the raw text as a prompt (r2). The get() re-check keeps step 2 honest if the
    # roster mutated between names() and here.
    if parsed is None or any(reg.get(name) is None for name in parsed[0]):
        bad = tok[0] if parsed is None else next(n for n in parsed[0] if reg.get(n) is None)
        return _delegate_unavailable_msg(bad), None
    targets, rest = parsed
    if not rest:
        listing = " ".join(f"@{n}" for n in targets)
        return f"Usage: `{listing} <message>` — add a message to send.", None

    from graph.mention_op import catchup_caps, round_cap, run_mention
    from graph.room_rounds import plan_round

    cfg = getattr(STATE, "graph_config", None)
    max_rounds = round_cap(cfg)
    caps = catchup_caps(cfg)

    tid = _turn_control._resolve_thread_id(request_metadata, session_id)
    # `rounds` is the whole state the driver needs — one list per round, in order. The
    # flat `outcomes` the caller wants is derived from it at the end rather than kept in
    # parallel, so there is only one place a round can be recorded.
    rounds: list[list[dict]] = []
    # Bind the originating chat session for the whole exchange so the delegates plugin
    # records every a2a continuity this room mints against it (#3362) — the session then
    # scopes a later session-DELETE cleanup. It rides a ContextVar the registry reads
    # (`recording_session`), because run_mention reaches DelegateRegistry.dispatch through
    # host-free `graph/mention_op`, which can't carry a new argument. Reached duck-typed
    # through the roster like `forget_delegate_conversations`, and gated on a real session,
    # so a fork without the plugin (or a session-less caller) is exactly today's behaviour.
    _record_session = getattr(reg, "recording_session", None)
    session_scope = (
        _record_session(session_id) if _record_session is not None and session_id else contextlib.nullcontext()
    )
    with session_scope:
        plan = plan_round(targets, rounds, max_rounds=max_rounds)
        while not plan.done:
            this_round: list[dict] = []
            for name in plan.speakers:
                outcome = await run_mention(
                    STATE.graph,
                    reg,
                    tid,
                    name,
                    rest,
                    session_id=session_id,
                    # Both levers come off the PLAN, not from re-deriving them here: they are
                    # the room's policy (`RoundPlan.record_address` / `.drop_silence`), and
                    # `drop_silence` in particular is what makes `room.max_rounds: 1` byte-
                    # identical to the single pass this used to be.
                    record_address=plan.record_address,
                    drop_silence=plan.drop_silence,
                    **caps,
                )
                outcome["round"] = plan.round_index
                this_round.append(outcome)
            rounds.append(this_round)
            plan = plan_round(targets, rounds, max_rounds=max_rounds)
    outcomes: list[dict] = [outcome for one_round in rounds for outcome in one_round]

    # A stopped member can already be started, with consent, by the lead agent's
    # delegate_to tool (#3126). Direct @ dispatch runs outside the graph and therefore
    # cannot interrupt for that consent. Bounce the original, untouched message into a
    # normal lead turn only when EVERY address failed for exactly that recoverable
    # reason. A mixed result stays here: replaying the whole message through the lead
    # would dispatch again to participants who already answered.
    if _all_mentions_are_startable_unreachable(reg, targets, outcomes):
        return None, None

    # A member whose address gave up while its peer was still working is not retried — the
    # round driver dropped it, and a second SendMessage would be a duplicate task (#3359).
    # But its task is still running, so the delegates seam can COLLECT it: poll that one task
    # read-only and post the answer as the member's own late room message when it settles
    # (#3360b). The seam decides whether there is anything to collect (a pending task the
    # adapter kept), so every other failure is `False` here and changes nothing. Duck-typed
    # like `recording_session`; a fork without the plugin, or a session-less caller that has
    # nowhere to deliver to, is exactly the old behaviour.
    collect_late = getattr(reg, "collect_late", None)
    if collect_late is not None and session_id:
        # An incognito origin still gets the answer in its session, but no lead turn is pushed
        # for it — an on-time `@` answer runs no lead turn either (ADR 0069 D3b).
        incognito = bool((request_metadata or {}).get("incognito"))
        for o in outcomes:
            if not o.get("ok") and o.get("author"):
                try:
                    o["collecting"] = bool(
                        collect_late(tid, str(o["author"]), session_id=session_id, incognito=incognito)
                    )
                except Exception:  # noqa: BLE001 — a courtesy must never fail the room
                    log.exception("[room] starting late collection for @%s failed", o.get("author"))

    def _line(o: dict) -> str:
        who = str(o.get("author") or "")
        if o.get("ok"):
            return str(o.get("reply") or "").strip() or f"@{who} replied with nothing."
        # Keep S1's wording — a delegate failure is an answer to the operator, not a 500.
        return f"Delegate @{who} failed: {o.get('error') or 'unknown error'}"

    # A silence (a `pass`) is not a message: it went onto no thread and it gets no line
    # here either. Only ever set when multi-round is on — see dispatch_into_room.
    spoken = [o for o in outcomes if not o.get("silent")]

    if not spoken:
        # Multi-round only: everyone passed on the first round, so the room settled with
        # nothing said. Silence is a real answer to the operator, so say it plainly.
        body = "_Nobody had anything to add._"
    elif len(targets) == 1 and len(spoken) == 1:
        body = _line(spoken[0])
    else:
        # Several participants answered one message: attribute each reply, because an
        # unattributed join would read as one voice — the exact collapse the room exists
        # to avoid. Consoles that render per-exchange frames show the parts; this text is
        # the whole for everyone else.
        #
        # Gated on how many were ADDRESSED, not on how many happened to speak. Multi-
        # round makes those differ: `@a @b` where b passes every round and a answers once
        # leaves exactly one non-silent outcome, and the bare answer would then reach an
        # A2A or /v1 consumer — which gets no `room_reply` frames — with no byline at all,
        # from a message the operator sent to two participants.
        body = "\n\n".join(f"**@{o.get('author')}** — {_line(o)}" for o in spoken)
    answer = _with_room_notes(body, outcomes, plan)
    # Split the answer into the part the participants' own bubbles carry and the part
    # only the answer carries, and say which is which (#3449).
    #
    # The address short-circuits the lead, so this answer text is composed out of these
    # very replies — purely as the whole for consumers that get no `room_reply` frames
    # (A2A, `/v1`). A console that DOES render those frames has to be told, or it draws
    # each reply once under its byline and the lot again as the lead's answer: the
    # doubled answer Josh hit on v0.164.0.
    #
    # PER EXCHANGE, because the rendering is per exchange. `in_answer` marks every
    # exchange the console will draw as a bubble whose text IS what the answer says for
    # it (`_covered_by_a_bubble`) — the attribution a multi-address join adds is the
    # byline that bubble already renders, so it changes nothing.
    #
    # What no bubble carries goes out as the ROOM's own note frame: a line composed for
    # an exchange with no reply text of its own (a failed address, an empty reply) and
    # the room's bound notes (a clipped catch-up, the round cap). Sending it rather than
    # dropping the claim for the whole turn is the difference between fixing the report
    # and not: on DEFAULT catch-up caps (40 messages / 8000 chars) an ordinary long chat
    # truncates, appends a note, and would otherwise keep doubling — which is exactly
    # the session Josh hit it in.
    #
    # The note is emitted only when something was claimed. With nothing claimed the
    # console lands the answer whole, and a note frame would then be the duplicate.
    claimed = [o for o in spoken if _covered_by_a_bubble(o)]
    for o in claimed:
        o["in_answer"] = True
    if claimed:
        note = _room_note(
            [o for o in spoken if not _covered_by_a_bubble(o)], spoken, targets, _line, outcomes, plan
        )
        # A TURN-level field, carried on the first exchange rather than as an extra list
        # entry: this function returns `(answer, outcomes)` and the note belongs to
        # neither, while a synthetic outcome would be counted as a participant by every
        # consumer that sums `ok` over the list. The driver reads it once, after the
        # per-exchange loop.
        if note:
            claimed[0]["room_note"] = note
    return answer, outcomes


def _covered_by_a_bubble(outcome: dict) -> bool:
    """Will the console render this exchange as a bubble whose text is what the turn's
    answer says for it?

    Both halves are required. No reply text ⇒ no bubble at all, and the answer's line for
    it ("@x replied with nothing.") lives only in the answer. Not ``ok`` ⇒ the answer's
    line is the failure ("Delegate @x failed: …"), not the reply, so even a reply that
    somehow arrived alongside an error would not be what the answer restates.
    """
    return bool(outcome.get("ok")) and bool(str(outcome.get("reply") or "").strip())


def _room_note(uncovered: list[dict], spoken: list[dict], targets: list[str], line, outcomes: list[dict], plan) -> str:
    """The part of the answer no participant's bubble carries, or ``""``.

    Composed out of the same ``_line`` / ``_with_room_notes`` pieces the answer is, and
    attributed exactly as the answer attributes them, so this text is a subset of the
    answer rather than a second rendering of it in different words.
    """
    from graph.room_rounds import cap_note, catchup_note, collecting_note

    attribute = not (len(targets) == 1 and len(spoken) == 1)
    lines = [f"**@{o.get('author')}** — {line(o)}" if attribute else line(o) for o in uncovered]
    lines += [note for note in (catchup_note(outcomes), collecting_note(outcomes), cap_note(plan)) if note]
    return "\n\n".join(lines)


def _with_room_notes(body: str, outcomes: list[dict], plan) -> str:
    """The room's reply plus the bounds it hit, if any — otherwise ``body`` untouched.

    Two bounds are worth an operator's attention and neither had ANY surface before: a
    catch-up window that truncated (``catchup_note``) and the round cap ending the room
    instead of a settle (``cap_note``, silent while ``room.max_rounds`` is 1 — i.e.
    always, until an operator opts in). Both notes are the ROOM's copy about the room's
    own bounds, so both live in ``graph/room_rounds.py``; this is only the composition.

    They are appended prose on the existing reply, deliberately: the room has no chrome
    of its own, and inventing a frame the console doesn't render would be a bound the
    operator still can't see.
    """
    from graph.room_rounds import cap_note, catchup_note, collecting_note

    notes = [note for note in (catchup_note(outcomes), collecting_note(outcomes), cap_note(plan)) if note]
    return "\n\n".join([body, *notes]) if notes else body


def _all_mentions_are_startable_unreachable(reg, targets: list[str], outcomes: list[dict]) -> bool:
    """Whether every failed address can be recovered by ``delegate_to`` startup.

    Classification comes from the adapter exception, while member identity comes from
    the fleet roster. Both are required: an HTTP/timeout failure must not restart a live
    process, and an unreachable remote peer is not ours to spawn.

    Judged on ROUND ONE only, and explicitly so. Round one is the only round that
    dispatches every target (later rounds drop the failed and can be a subset), and "the
    whole addressed set was unreachable at the first attempt" is the question the #3126
    fall-through actually asks. Reading the flat list instead happens to give the same
    answer today — an all-failed round one exhausts the room immediately, so there is no
    round two — but that is an invariant of the driver, not of this loop, and a future
    change to the exhaustion rule would break the fall-through silently.
    """
    first_round = [outcome for outcome in outcomes if (outcome.get("round") or 1) == 1]
    if not first_round or len(first_round) != len(targets):
        return False
    from plugins.delegates.adapters import KIND_UNREACHABLE
    from plugins.delegates.autostart import startable_member

    for name, outcome in zip(targets, first_round, strict=True):
        if outcome.get("ok") or outcome.get("error_kind") != KIND_UNREACHABLE:
            return False
        delegate = reg.get(name)
        if delegate is None or startable_member(getattr(delegate, "url", "") or "") is None:
            return False
    return True


async def _at_delegate_reply(message: str) -> str | None:
    """``_at_delegate_exchange``'s reply text alone — the S1 entry point."""
    reply, _outcomes = await _at_delegate_exchange(message)
    return reply
