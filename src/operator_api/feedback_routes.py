"""Per-turn user feedback (ADR 0126 E2 / D3) — ``POST`` + ``GET /api/feedback``.

A thumbs up/down on one assistant turn. The console's thumbs buttons (the next card)
POST a rating; this route stores it in the telemetry store's ``feedback`` table and
mirrors it into Langfuse as a ``user_feedback`` score on that turn's past trace (reusing
``observability.tracing.score_trace`` from the bd-1cky.1 foundation — this adds no second
scoring helper). The GET returns the session's turns with their trace_ids and current
ratings so the console can map a rendered assistant message back to its trace in one call.

Behind the standard operator-tier auth the rest of ``/api/*`` sits behind (ADR 0089 — the
default-deny bearer middleware guards every path), so registering here inherits it; no new
trust surface. Read side is read-only; write side stores at most one row per turn.

The trace_id join. Console messages carry no trace_id, but per-turn telemetry rows
(``observability.telemetry_store.turns``) do, alongside ``task_id``, ``session_id`` and
``created_at``. GET returns one entry per recorded turn LEG in the session —
``{task_id, trace_id, created_at, rating, note}`` — oldest first, so the console joins a
rendered assistant message to its turn by ``task_id`` when it has one, else by turn ORDER
within the session (the Nth entry is the Nth turn). A turn with no telemetry row (an
ACP/tool-only turn that recorded nothing) simply has no entry — the console shows no
thumbs for it rather than guessing.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import timezone, datetime

from fastapi import HTTPException
from pydantic import BaseModel

from runtime.state import STATE

#: A feedback note is one short line — collapse any newlines/whitespace runs and cap the
#: length so a pasted paragraph can't bloat the row (or the Langfuse score comment).
_NOTE_MAX = 500

#: The Langfuse score name this endpoint owns, one per rated turn (ADR 0126 D3).
_SCORE_NAME = "user_feedback"


def _clean_note(note: str | None) -> str:
    """One line, length-capped. ``split()``/join collapses every whitespace run (newlines
    included) to single spaces and strips the ends."""
    return " ".join((note or "").split())[:_NOTE_MAX]


def _score_id(trace_id: str) -> str:
    """The STABLE Langfuse score id for one turn's ``user_feedback`` rating, derived from
    its ``trace_id``. A turn carries exactly one user_feedback score, so pinning the id to
    the (trace_id, score-name) pair makes a re-rating UPSERT that single score in place —
    the second rating replaces the first in Langfuse rather than stacking beside it — and
    lets un-rating delete exactly it. SHA-1 hex: deterministic, ASCII, a valid score id."""
    return hashlib.sha1(f"{trace_id}:{_SCORE_NAME}".encode()).hexdigest()


class FeedbackRequest(BaseModel):
    # The turn being rated lives in (session_id, trace_id). trace_id is the turn's Langfuse
    # id — the console gets it from GET /api/feedback. Empty when tracing is off: the rating
    # is still stored locally, just not scored (no trace to score).
    session_id: str
    trace_id: str = ""
    # 1 = thumbs up, 0 = thumbs down, null = un-rate (delete the row). Any other value 422s.
    value: int | None = None
    note: str = ""
    # Incognito (ADR 0069 D3b): asserted by the console, which owns the chat's incognito
    # state. There is no server-side persistent incognito marker to read — that is the whole
    # point of incognito — so the client tells us, and we refuse rather than persist a
    # durable record of a non-persisted thread. Additive, default False.
    incognito: bool = False


def register_feedback_routes(app) -> None:
    """Register ``POST`` + ``GET /api/feedback`` on ``app``."""

    @app.post("/api/feedback")
    async def _api_feedback_post(req: FeedbackRequest):
        """Store (or replace / delete) one turn's rating and mirror it to Langfuse.

        A rating with the same (session_id, trace_id) replaces the previous one; a
        ``value`` of ``null`` un-rates (deletes the row). Incognito sessions are refused
        with a 409 and nothing is stored or scored. A request with no trace_id (tracing
        off) is stored locally and NOT scored.
        """
        session_id = (req.session_id or "").strip()
        if not session_id:
            raise HTTPException(status_code=400, detail="session_id is required")
        if req.incognito:
            # Refuse before any store/score — an incognito chat leaves no durable trace by
            # design (ADR 0069 D3b), and a rating would be exactly that.
            raise HTTPException(status_code=409, detail="incognito sessions cannot be rated")
        if req.value not in (None, 0, 1):
            raise HTTPException(status_code=422, detail="value must be 1, 0, or null")

        trace_id = (req.trace_id or "").strip()
        note = _clean_note(req.note)
        store = STATE.telemetry_store
        # Resolved once per request against the live package attribute so the test's
        # monkeypatch of tracing.{is_enabled,score_trace,delete_score} is seen, and so a
        # sibling test that reloads the tracing module can't leave us on a stale object
        # (protoAgent#176 — why every operator_api route imports observability inline).
        from observability import tracing

        # Un-rate: delete the local row (if the store is on) AND drop the mirrored
        # Langfuse score, so the trace stops carrying a stale user_feedback rating —
        # clearing both halves, not just the local one. The Langfuse delete is keyed by
        # the SAME deterministic score id the rating was written under, is a no-op when
        # the turn was never traced (no trace_id) or tracing is off, and never raises.
        if req.value is None:
            removed = 0
            if store is not None:
                removed = await asyncio.to_thread(
                    store.delete_feedback, session_id=session_id, trace_id=trace_id
                )
            unscored = False
            if trace_id and tracing.is_enabled():
                await asyncio.to_thread(tracing.delete_score, _score_id(trace_id))
                unscored = True
            return {
                "ok": True,
                "session_id": session_id,
                "trace_id": trace_id or None,
                "rating": None,
                "removed": bool(removed),
                "unscored": unscored,
            }

        # Store/replace the local row (best-effort — the store can be off on an instance
        # with telemetry disabled; scoring below is independent of it).
        created_at = datetime.now(timezone.utc).isoformat()
        if store is not None:
            await asyncio.to_thread(
                store.set_feedback,
                session_id=session_id,
                trace_id=trace_id,
                value=int(req.value),
                note=note,
                created_at=created_at,
            )

        # Mirror into Langfuse as a user_feedback score on the turn's PAST trace — the
        # bd-1cky.1 helper, reused (no second scoring helper), PINNED to a deterministic
        # score id so re-rating the same turn REPLACES the one score in place instead of
        # adding a second, contradictory one. Only when a trace_id is present AND tracing
        # is live: a turn with no trace (tracing off) has nothing to score, and reporting
        # `scored` true when score_trace is a no-op would wrongly claim the rating reached
        # Langfuse.
        scored = False
        if trace_id and tracing.is_enabled():
            await asyncio.to_thread(
                tracing.score_trace,
                trace_id,
                _SCORE_NAME,
                float(req.value),
                note,
                score_id=_score_id(trace_id),
            )
            scored = True

        return {
            "ok": True,
            "session_id": session_id,
            "trace_id": trace_id or None,
            "rating": int(req.value),
            "note": note,
            "scored": scored,
        }

    @app.get("/api/feedback")
    async def _api_feedback_get(session_id: str):
        """The session's per-turn trace_ids and current ratings, oldest first.

        One entry per recorded turn leg: ``{task_id, trace_id, created_at, rating, note}``
        (rating/note are null for an unrated turn). ``{enabled: false}`` with no turns when
        the telemetry store is off, mirroring the telemetry routes' degrade contract.
        """
        store = STATE.telemetry_store
        if store is None:
            return {"enabled": False, "turns": [], "tracing_enabled": _tracing_enabled()}
        sid = (session_id or "").strip()
        if not sid:
            return {"enabled": True, "turns": [], "tracing_enabled": _tracing_enabled()}
        turns = await asyncio.to_thread(store.session_feedback, sid)
        return {"enabled": True, "turns": turns, "tracing_enabled": _tracing_enabled()}


def _tracing_enabled() -> bool:
    """Whether Langfuse tracing is live — so the console can tell "this turn wasn't traced"
    from "tracing is off" and decide whether a thumbs rating will reach Langfuse at all."""
    from observability import tracing

    return tracing.is_enabled()
