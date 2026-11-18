"""Durable one-audience-per-segment scheduling over the P1b.1 partition (ADR 0131 P1c).

A conversation's original thread runs ONE serialized runner. Every accepted input is persisted
as a *pending turn* on the existing task records (a
``public_visible=False`` shell, operator-only), carrying a monotonic acceptance ``seq``, its
fixed ``audience`` (peer or private), the verified ``principal``, the message-id ``dedupe`` key,
the ``checkpoint`` binding, the ``interrupt_id`` it resumes, and its ``state``
(queued/running/parked/terminal). No second scheduler and no binding database: the queue is a
projection of durable task state, and the ACTIVE SEGMENT is one more record keyed by thread,
carrying a monotonic ``generation``.

**One transactional ordering.** Every segment transition — claim, fold-in, complete, park,
resume — is a compare-and-set on that ``generation``, not a
process lock. "Accept-and-queue wins or completion-and-next-start wins" is that one CAS: a steer
arriving as a turn completes either folds into the running segment (its fold bumps the
generation, so the completion's CAS loses and the turn keeps running) or does not (the
completion's CAS wins first, so the steer stays queued for the next segment). Exactly one, never
both, never neither.

**One audience per segment.** The audience is fixed when a segment is claimed and never changes
mid-turn. A fold attaches an accepted input to the running segment ONLY when the audiences match,
so a model call never mixes audiences; the other audience's inputs queue behind, ordered by
``seq``, with no cross-audience batching. A parked segment reserves the runner until its MATCHING
guarded resume (the CAS guards on the parked ``interrupt_id``); ordinary steers do not satisfy an
arbitrary parked decision. The matching resume may pass queued fresh inputs of its own audience.

**Recovery.** After a restart, :func:`recover_undrained_segments` reloads the durable queue,
audience and order from the reopened store BEFORE a runner is claimed. A turn left ``running`` by
the crash is reported suspended for reconciliation, never re-dispatched — its tool effects cannot
be replayed. A parked segment stays reserved. A private turn keeps its opaque ``input_ref``, so it
is resumed through the private path and never falls back to a public input.

**Live private writer.** The private steer route persists pending turns when enabled; the live reader is the
server-start recovery (``server.chat.recover_private_pending_turns``, P1h.4, behind the
private-steering flag). It depends only on
the private partition/intake and ``graph.thread_ids`` — never ``server`` — so the import layering holds.
"""

from __future__ import annotations

from a2a_impl.private_intake import ref_task_id
from a2a_impl.stores import PRIVATE_SCHEMA_VERSION
from graph import thread_ids

KIND_PENDING_TURN = "pending-turn"
KIND_SEGMENT = "active-segment"

AUDIENCE_PEER = "peer"
AUDIENCE_PRIVATE = "private"

STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_PARKED = "parked"
STATE_TERMINAL = "terminal"

#: An idle segment: no audience owns the runner, no pending turn is active.
SEGMENT_IDLE = "idle"

_NON_TERMINAL = (STATE_QUEUED, STATE_RUNNING, STATE_PARKED)


def segment_task_id(session_id: str) -> str:
    """The private-only task id holding ``session_id``'s active-segment record."""
    return f"segment:{session_id}"


def pending_task_id(session_id: str, seq: int) -> str:
    """The private-only task id holding one pending turn (ordered by ``seq``)."""
    return f"pending:{session_id}:{int(seq):012d}"


def _is_kind(record, kind: str) -> bool:
    payload = getattr(record, "private_payload", None)
    return (
        isinstance(payload, dict)
        and payload.get("version") == PRIVATE_SCHEMA_VERSION
        and payload.get("kind") == kind
    )


# ── the durable pending-turn queue ──────────────────────────────────────────────


async def pending_turns(task_store, session_id: str, context, *, states=None) -> list[dict]:
    """The session's pending turns, oldest acceptance first (by ``seq``).

    ``states`` filters by state (default: every non-terminal turn). Reads the operator-only
    partition timeline; raises on an unreadable partition so the caller can fail closed."""
    wanted = set(states) if states is not None else set(_NON_TERMINAL)
    out: list[dict] = []
    for payload in (await task_store.scheduling_payloads(session_id, context)).values():
        if payload.get("kind") == KIND_PENDING_TURN and payload.get("state") in wanted:
            out.append(dict(payload))
    out.sort(key=lambda p: p.get("seq", 0))
    return out


async def sessions_with_pending_turns(task_store, context) -> list[str]:
    """Every session holding a non-terminal pending turn (queued, running or parked).

    What a restart must look at before any runner is claimed; raises on an unreadable
    partition so the caller can fail closed."""
    return await task_store.scheduling_contexts(context, kind=KIND_PENDING_TURN, states=_NON_TERMINAL)


async def accept_pending_turn(
    task_store,
    *,
    session_id: str,
    audience: str,
    principal: str,
    dedupe: str,
    checkpoint: str = "",
    interrupt_id: str = "",
    input_ref: str = "",
    context,
) -> dict:
    """Persist an accepted input as the next ordered pending turn, and return its record.

    Idempotent on the message-id ``dedupe`` key: a retried input with a key already recorded
    reuses the one pending turn (one turn per message id) instead of minting a second. The
    acceptance ``seq`` is monotonic; sequence allocation and dedupe run in one database
    transaction. The record is a ``public_visible=False`` shell — its
    audience, principal, checkpoint and interrupt binding live only in the private payload, so no
    peer-readable projection is ever exposed. Raises on a write failure; the caller fails closed."""
    payload = {
        "version": PRIVATE_SCHEMA_VERSION,
        "kind": KIND_PENDING_TURN,
        "audience": audience,
        "principal": str(principal or ""),
        "dedupe": str(dedupe or ""),
        "checkpoint": str(checkpoint or ""),
        "interrupt_id": str(interrupt_id or ""),
        "input_ref": str(input_ref or ""),
        "state": STATE_QUEUED,
    }

    if audience == AUDIENCE_PRIVATE:
        payload["resolver_metadata"] = thread_ids.resolver_metadata(session_id)

    def accept(records):
        turns = [p for p in records.values() if p.get("kind") == KIND_PENDING_TURN]
        for turn in turns:
            if dedupe and turn.get("dedupe") == dedupe:
                return dict(turn)
        # Intake may precede a restart or a later ordinary turn. Prefer the
        # referenced input's snapshot over the current process cache.
        original = records.get(ref_task_id(input_ref), {})
        if audience == AUDIENCE_PRIVATE and isinstance(original.get("resolver_metadata"), dict):
            payload["resolver_metadata"] = dict(original["resolver_metadata"])
        payload["seq"] = max((int(p["seq"]) for p in turns), default=0) + 1
        records[pending_task_id(session_id, payload["seq"])] = payload
        return dict(payload)

    return await task_store.update_scheduling_payloads(session_id, context, accept)


async def set_turn_state(task_store, session_id: str, seq: int, state: str, context, **extra) -> bool:
    """Advance one pending turn to ``state`` (and merge any ``extra`` payload fields).

    Driven by the serialized runner, so an unconditional guarded write is correct; returns False
    when the record is gone (never widens anything)."""
    updates = {"state": state, **extra}
    return await task_store.cas_private_payload(
        pending_task_id(session_id, seq), guard={}, updates=updates, context=context
    )


# ── the active segment (the serialized runner) ──────────────────────────────────


async def read_segment(task_store, session_id: str, context) -> dict:
    """The active-segment record, or a default idle segment (``generation`` 0) when none exists."""
    record = await task_store.get_record(segment_task_id(session_id), context)
    if record is None or not _is_kind(record, KIND_SEGMENT):
        return {
            "version": PRIVATE_SCHEMA_VERSION,
            "kind": KIND_SEGMENT,
            "generation": 0,
            "state": SEGMENT_IDLE,
            "audience": "",
            "seq": None,
            "interrupt_id": "",
            "exists": False,
        }
    seg = dict(record.private_payload)
    seg["exists"] = True
    return seg


async def _ensure_segment(task_store, session_id: str, context) -> dict:
    """Create once under the database writer reservation; never reset a live claim."""
    default = await read_segment(task_store, session_id, context)

    def ensure(records):
        payload = records.setdefault(segment_task_id(session_id), {k: v for k, v in default.items() if k != "exists"})
        return {**payload, "exists": True}

    return await task_store.update_scheduling_payloads(session_id, context, ensure)


async def claim_segment(
    task_store, session_id: str, *, audience: str, seq: int, interrupt_id: str = "", context
) -> int | None:
    """Claim the idle runner for ``audience``/``seq``; return the new generation, or None if busy.

    A transactional compare-and-set on the segment generation: an idle segment becomes
    ``running`` under this audience, fixing the audience for the whole segment. A segment already
    running or parked (reserved) returns None — the caller's input stays queued.
    The pending turn becomes running in the same transaction as the claim."""
    seg = await _ensure_segment(task_store, session_id, context)
    if seg["state"] != SEGMENT_IDLE:
        return None
    generation = int(seg["generation"])

    def claim(records):
        current = records[segment_task_id(session_id)]
        turn = records.get(pending_task_id(session_id, seq), {})
        if current.get("generation") != generation or turn.get("state") != STATE_QUEUED:
            return None
        current.update(generation=generation + 1, state=STATE_RUNNING, audience=audience,
                       seq=int(seq), interrupt_id=str(interrupt_id or ""))
        turn["state"] = STATE_RUNNING
        return generation + 1

    return await task_store.update_scheduling_payloads(session_id, context, claim)


async def try_fold(task_store, session_id: str, *, generation: int, audience: str, seq: int, context) -> int | None:
    """Attach a newly-accepted input to the RUNNING segment, or decline. One ordering decision.

    Folds only when the segment is still running at ``generation`` under the SAME ``audience`` —
    so a model call never mixes audiences. The fold bumps the generation, so a completion racing
    on the old generation loses and the turn keeps running to drain the fold. Returns the new
    generation on a fold, or None when the segment already moved on (completed, or a different
    audience) — then the input stays queued for its own segment."""
    def fold(records):
        seg = records.get(segment_task_id(session_id), {})
        turn = records.get(pending_task_id(session_id, seq), {})
        if (seg.get("state") != STATE_RUNNING or seg.get("audience") != audience
                or seg.get("generation") != generation or turn.get("state") != STATE_QUEUED
                or turn.get("audience") != audience):
            return None
        seg["generation"] = generation + 1
        # The accepted input belongs to this execution from this commit onward. A crash
        # exposes it as suspended, never as fresh work to dispatch again.
        turn.update(state=STATE_RUNNING, segment_seq=seg["seq"])
        return generation + 1

    return await task_store.update_scheduling_payloads(session_id, context, fold)


async def complete_segment(task_store, session_id: str, *, generation: int, context) -> bool:
    """Release the runner to idle — the other half of the one ordering decision.

    CAS on the segment generation: commits only when nothing raced in. If a fold bumped the
    generation first (accept-and-queue won), this matches no row and returns False — the caller
    must drain the folded input and keep running, then complete at the new generation.
    The head and all folded turns become terminal in the SAME transaction as the release,
    so recovery cannot mistake completed inputs for interrupted execution."""
    def complete(records):
        seg = records.get(segment_task_id(session_id), {})
        if seg.get("generation") != generation:
            return False
        head_seq = seg.get("seq")
        if head_seq is not None:
            for turn in records.values():
                if turn.get("kind") == KIND_PENDING_TURN and (
                    turn.get("seq") == head_seq or turn.get("segment_seq") == head_seq
                ):
                    turn["state"] = STATE_TERMINAL
        seg.update(generation=generation + 1, state=SEGMENT_IDLE,
                   audience="", seq=None, interrupt_id="")
        return True

    return await task_store.update_scheduling_payloads(session_id, context, complete)


async def park_segment(task_store, session_id: str, *, generation: int, interrupt_id: str, context) -> bool:
    """Park the running segment at an interrupt — it RESERVES the runner (not idle) until its
    matching guarded resume. CAS on the generation; audience and seq are preserved."""
    def park(records):
        seg = records.get(segment_task_id(session_id), {})
        if seg.get("generation") != generation:
            return False
        for turn in records.values():
            if turn.get("kind") == KIND_PENDING_TURN and (
                turn.get("seq") == seg.get("seq") or turn.get("segment_seq") == seg.get("seq")
            ):
                turn.update(state=STATE_PARKED, interrupt_id=str(interrupt_id))
        seg.update(generation=generation + 1, state=STATE_PARKED, interrupt_id=str(interrupt_id))
        return True

    return await task_store.update_scheduling_payloads(session_id, context, park)


async def resume_segment(task_store, session_id: str, *, generation: int, interrupt_id: str, context) -> int | None:
    """Resume a parked segment — ONLY its matching guarded resume. Returns the new generation, or
    None when this is not the matching resume.

    The CAS guards on both the generation and the parked ``interrupt_id``, so an ordinary steer
    (or a resume for a different interrupt) can never continue the segment: it is a continuation
    of that segment, not a later queue item. The resume may then pass queued fresh inputs of the
    SAME audience to unblock the interrupt (:func:`pending_turns`)."""
    def resume(records):
        seg = records.get(segment_task_id(session_id), {})
        if (seg.get("state") != STATE_PARKED or seg.get("generation") != generation
                or str(seg.get("interrupt_id")) != str(interrupt_id)):
            return None
        for turn in records.values():
            if turn.get("kind") == KIND_PENDING_TURN and (
                turn.get("seq") == seg.get("seq") or turn.get("segment_seq") == seg.get("seq")
            ):
                turn["state"] = STATE_RUNNING
        seg.update(generation=generation + 1, state=STATE_RUNNING)
        return generation + 1

    return await task_store.update_scheduling_payloads(session_id, context, resume)


# ── scheduling + recovery ───────────────────────────────────────────────────────


async def next_dispatch(task_store, session_id: str, context) -> dict | None:
    """The queued pending turn to run next, or None when the runner is busy or reserved.

    Honors "no cross-audience batching" (it returns ONE head turn; the runner claims the segment
    at that turn's audience, and only same-audience folds attach) and "a parked segment reserves
    the runner" (a running or parked segment yields None)."""
    seg = await read_segment(task_store, session_id, context)
    if seg["state"] in (STATE_RUNNING, STATE_PARKED):
        return None
    queued = await pending_turns(task_store, session_id, context, states=(STATE_QUEUED,))
    return queued[0] if queued else None


async def recover_undrained_segments(task_store, session_id: str, context) -> dict:
    """Reload the durable queue, audience and order after a restart, BEFORE claiming a runner.

    Returns the reopened-store view: the active ``segment``, the ``queued`` turns in acceptance
    order, any ``running`` turn the crash interrupted (``suspended`` — surfaced for reconciliation,
    NEVER re-dispatched, so no tool effect is replayed), any ``parked`` turn still reserving the
    runner, and the recovered ``audience``. A turn's ``input_ref`` is preserved for every private
    turn, so recovery resumes it through the private path and never substitutes a public input.

    Raises on an unreadable partition — the caller leaves the private error recoverable rather
    than restarting publicly."""
    turns = await pending_turns(task_store, session_id, context)
    segment = await read_segment(task_store, session_id, context)
    queued = [t for t in turns if t["state"] == STATE_QUEUED]
    suspended = [t for t in turns if t["state"] == STATE_RUNNING]
    parked = [t for t in turns if t["state"] == STATE_PARKED]
    reserved = segment["state"] in (STATE_RUNNING, STATE_PARKED)
    # A reserved runner keeps its own fixed audience (the suspended/parked segment owns it until
    # reconciliation or its matching resume); only an idle runner takes the head queued turn's.
    if reserved:
        audience = segment.get("audience") or ""
    elif queued:
        audience = queued[0]["audience"]
    else:
        audience = ""
    # Restore only the turn that owns (or is next to claim) the runner. Metadata
    # lives in the same private JSON as acceptance, so retries keep the original
    # snapshot and a crash cannot leave an accepted turn without its resolver inputs.
    active = next((t for t in turns if t["seq"] == segment.get("seq")), None) if reserved else (
        queued[0] if queued else None
    )
    if active and active["audience"] == AUDIENCE_PRIVATE:
        metadata = active.get("resolver_metadata")
        if isinstance(metadata, dict):
            thread_ids.remember_resolver_metadata(session_id, metadata)
    return {
        "segment": segment,
        "queued": queued,
        "suspended": suspended,
        "parked": parked,
        "audience": audience,
        "reserved": reserved,
    }


async def dispatch_pending_turn(task_store, session_id: str, context, send) -> str | None:
    """Recovery runner seam. ``send`` returns a task id or None for a PROVEN refusal.

    Exceptions/ambiguous delivery leave the execution reserved for reconciliation, never
    retry automatically. The caller must not translate a timeout into a proven refusal.

    A fold may attach a fresh same-audience input to the claimed segment while ``send`` is in
    flight (bumping the generation). On a PROVEN refusal nothing this dispatch owned ever ran —
    not the head, not a folded input — so the head AND every folded turn return to the queue and
    the runner is released to idle. The segment is never left ``running`` with no completer.
    """
    def claim(records):
        seg = records[segment_task_id(session_id)]
        queued = sorted((p for p in records.values() if p.get("kind") == KIND_PENDING_TURN
                         and p.get("state") == STATE_QUEUED), key=lambda p: p["seq"])
        if seg["state"] != SEGMENT_IDLE or not queued:
            return None
        turn = queued[0]
        seg.update(generation=seg["generation"] + 1, state=STATE_RUNNING,
                   audience=turn["audience"], seq=turn["seq"], interrupt_id=turn["interrupt_id"])
        turn["state"] = STATE_RUNNING
        return dict(turn)

    await _ensure_segment(task_store, session_id, context)
    turn = await task_store.update_scheduling_payloads(session_id, context, claim)
    if turn is None:
        return None
    # Recovery may have selected a peer ahead of this turn, and intervening
    # turns may have replaced the session cache. Restore the claimed snapshot
    # immediately before send resolves the checkpoint thread.
    if turn["audience"] == AUDIENCE_PRIVATE:
        metadata = turn.get("resolver_metadata")
        if isinstance(metadata, dict):
            thread_ids.remember_resolver_metadata(session_id, metadata)
    task_id = await send(turn)
    if task_id is None:
        def requeue(records):
            seg = records[segment_task_id(session_id)]
            # A proven refusal: this dispatch never started, so nothing it owned ran. A fold that
            # landed while send was in flight bumped the generation, so the claim is identified by
            # the head turn's seq (a fold never changes the segment's seq), NOT the now-stale
            # generation. If a different claim has taken the runner, leave it alone.
            head_seq = turn["seq"]
            if seg["state"] != STATE_RUNNING or seg.get("seq") != head_seq:
                return
            # Requeue the refused head AND every input folded into this segment: none executed, so
            # none may be left running with no completer. Then release the runner to idle for a
            # clean re-dispatch in the recovered acceptance order.
            for pending in records.values():
                if pending.get("kind") != KIND_PENDING_TURN or pending.get("state") != STATE_RUNNING:
                    continue
                if pending.get("seq") == head_seq or pending.get("segment_seq") == head_seq:
                    pending["state"] = STATE_QUEUED
                    pending.pop("segment_seq", None)
            seg.update(generation=seg["generation"] + 1, state=SEGMENT_IDLE,
                       audience="", seq=None, interrupt_id="")

        await task_store.update_scheduling_payloads(session_id, context, requeue)
    return task_id
