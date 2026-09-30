"""Turn control — who may run a turn on a thread, and under what attendance.

Extracted from ``server/chat.py`` (#3847). This module is the ONE home of the
per-turn control state the two turn drivers in ``server/chat.py`` consult:

- the per-thread lock registry (``_THREAD_LOCKS`` / ``_thread_lock``) and the
  thread-id resolver (``_resolve_thread_id``) every turn / session gesture keys on;
- origin classification (``is_autonomous_origin``, ``is_interactive_origin``,
  ``_is_autonomous``, ``_interactive_turn_priority``) and the autonomous HITL
  auto-answer constants;
- session attendance (#3110: ``mark_session_attended`` / ``attendance_stream``);
- the live server-turn control plane (#3092: ``register_live_server_turn`` …);
- the HITL hold (#1560: ``_hold_if_hitl_pending``);
- the idle beacon (#1720: ``_ACTIVE_TURNS`` / ``active_turns`` /
  ``seconds_since_last_turn``).

``server/chat.py`` re-exports the names (the same objects), EXCEPT the rebound
idle-beacon ints ``_ACTIVE_TURNS`` / ``_LAST_TURN_MONOTONIC`` — a re-export of a
rebound int is a stale copy, so they are read only here. Patch THIS module, never
``server.chat`` (``tests/test_turn_control_seam.py`` guards it); the drivers and the
sibling ``chat_*`` modules call the patched helpers through this module at call time.

It imports nothing from ``server`` at import time; the one collaborator still in
``server.chat`` (``_pending_interrupt_value``, used by the HITL hold) is reached
through :func:`_chat` at CALL time, so a patch there keeps intercepting.
"""

import asyncio
import contextlib
import importlib
import logging
import time
import weakref
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

# Tool bodies need the same thread-id rule without importing ``server`` (which would
# violate the graph/plugin import boundary). Keep this private alias for existing callers.
from graph.thread_ids import resolve_thread_id as _resolve_thread_id  # noqa: F401 — the resolver's owner
from runtime import turn_activity as _turn_activity

log = logging.getLogger("protoagent.server")


def _chat() -> ModuleType:
    """``server.chat``, resolved at call time (``importlib`` rather than ``from server
    import chat``: the ``server`` package re-exports the ``chat`` FUNCTION under that
    name, shadowing the submodule)."""
    return importlib.import_module("server.chat")


# Per-thread_id locks (WeakValueDictionary so a lock is GC'd once no turn holds it,
# bounding memory). See _thread_lock.
_THREAD_LOCKS: weakref.WeakValueDictionary = weakref.WeakValueDictionary()


def _thread_lock(thread_id: str) -> asyncio.Lock:
    """Per-thread_id async lock — serializes turns on the SAME checkpointer thread so
    two concurrent A2A message/send on one context_id can't lost-update each other's
    history. Auto-evicted once no turn references the lock."""
    lock = _THREAD_LOCKS.get(thread_id)
    if lock is None:
        lock = asyncio.Lock()
        _THREAD_LOCKS[thread_id] = lock
    return lock


# Origins (ADR 0022) whose turns run with NO operator watching the chat: a HITL pause
# (ask_human / request_user_input) on one of these would park the task in input-required
# FOREVER — and that state is deliberately exempt from the TTL sweep, so it never settles.
# Live operator turns carry an empty origin (they keep parking — a human is watching);
# inbound `a2a` calls are excluded too, because the remote caller can itself resume the
# input-required task. For everything here, we auto-answer the interrupt instead of parking.
# "background-resume" is the ADR 0070 push-resume nudge — server-fired like the rest
# (the manager discards the A2A response). It stays in this set so an UNATTENDED nudge
# still auto-answers, but it is no longer conclusive on its own (#3110): when a live
# operator is attending the origin session (see the attendance registry below), the
# manager stamps the nudge ``attended`` and the report-delivery turn becomes eligible to
# park for a human, exactly like an ordinary operator turn.
_AUTONOMOUS_ORIGINS = frozenset(
    {"scheduler", "watch", "inbox", "webhook", "background", "background-resume", "delegate-result"}
)

# The immutable provenance of a result-delivery nudge (kept in _AUTONOMOUS_ORIGINS above)
# is distinct from whether a human is *currently* attending its origin session (#3110).
# These are the origins whose autonomy is conditional on live attendance rather than fixed.
_ATTENDANCE_CONDITIONAL_ORIGINS = frozenset({"background-resume", "delegate-result"})

# ── session attendance (SSE presence) — #3110 ────────────────────────────────
# Which origin chat sessions currently have a LIVE operator SSE connection (the console
# opens GET /api/chat/attend while a session is on screen; see ``attendance_stream``).
# Refcounted, because N tabs / a reconnect mid-drop can attend one session at once, and
# an unbalanced release must not evict a session another connection still holds. Bounded
# so a leaked registration can never grow it without limit. This is a live, in-process
# snapshot the manager reads at push-resume time to decide attended vs. unattended — it
# is NOT durable and, by design, fails CLOSED (an unknown/blank/over-cap session reads
# unattended), so ambiguity can never let a server-fired turn park with nobody to answer.
_ATTENDED_SESSIONS: dict[str, int] = {}
_ATTENDED_SESSIONS_MAX = 1024


def mark_session_attended(session_id: str) -> bool:
    """Register one live operator SSE connection to ``session_id`` (refcount++). Returns
    whether the session is now recorded attended. Bounded + fail-closed: a blank id, or a
    NEW session once the registry is at its cap, is a no-op (that session reads unattended)
    — a presence leak degrades to the pre-#3110 auto-answer behavior, never to unbounded
    growth or a spurious park."""
    sid = str(session_id or "").strip()
    if not sid:
        return False
    if sid not in _ATTENDED_SESSIONS and len(_ATTENDED_SESSIONS) >= _ATTENDED_SESSIONS_MAX:
        log.warning("[attendance] registry at cap (%d) — dropping presence for %s", _ATTENDED_SESSIONS_MAX, sid)
        return False
    _ATTENDED_SESSIONS[sid] = _ATTENDED_SESSIONS.get(sid, 0) + 1
    return True


def release_session_attended(session_id: str) -> None:
    """Drop one live SSE connection to ``session_id`` (refcount--); the entry is removed at
    zero. Idempotent and never raises — safe to call from an SSE teardown ``finally`` even
    for a session that was never (or is no longer) registered."""
    sid = str(session_id or "").strip()
    if not sid:
        return
    n = _ATTENDED_SESSIONS.get(sid, 0) - 1
    if n > 0:
        _ATTENDED_SESSIONS[sid] = n
    else:
        _ATTENDED_SESSIONS.pop(sid, None)


def is_session_attended(session_id: str) -> bool:
    """Whether a live operator is connected to ``session_id`` right now. Fails CLOSED: a
    blank id or any lookup error reads as unattended (#3110 — an ambiguous attendance
    signal must never make an otherwise-autonomous turn eligible to park indefinitely)."""
    try:
        sid = str(session_id or "").strip()
        return bool(sid) and _ATTENDED_SESSIONS.get(sid, 0) > 0
    except Exception:  # noqa: BLE001 — ambiguity fails closed to unattended
        return False


# ── server-turn control plane (#3092) ───────────────────────────────────────

_CONTROL_ORIGINS = frozenset({"background-resume", "scheduler", "watch", "inbox", "delegate-result"})


@dataclass
class _LiveServerTurn:
    session_id: str
    task_id: str
    origin: str
    trigger: str = ""
    controllable: bool = False
    accepted_ids: set[str] = field(default_factory=set)


# Deregistered on park / terminal / the executor's guaranteed ``turn_ended`` frame
# (server/a2a.py). Bounded anyway (#3933), like ``_ATTENDED_SESSIONS``: an entry a crash
# path still manages to strand can never grow it without limit. At the cap the OLDEST
# entry is evicted (insertion order) — a stranded registration is by nature an old one,
# and the turn starting now is the one an operator is about to address.
_LIVE_SERVER_TURNS: dict[str, _LiveServerTurn] = {}
_LIVE_SERVER_TURNS_MAX = 1024


def _server_turn_key(session_id: str, task_id: str) -> str:
    return f"{session_id}\0{task_id}"


def _control_origin(origin: object) -> str:
    return str(origin or "").strip().lower()


def server_turn_control_payload(
    session_id: str,
    task_id: str,
    *,
    origin: object = "",
    trigger: object = "",
    attended: object = None,
) -> dict[str, Any] | None:
    """Session-scoped control descriptor for a server-originated chat turn.

    The payload is deliberately small and durable-task keyed: the UI can address the
    in-flight turn by ``task_id``, but the server will only honor it for the SAME
    ``session_id`` and while that exact task remains live in this registry.
    """
    sid = str(session_id or "").strip()
    tid = str(task_id or "").strip()
    org = _control_origin(origin)
    if not sid or not tid or org not in _CONTROL_ORIGINS:
        return None
    is_attended = _truthy(attended) if attended is not None else is_session_attended(sid)
    controllable = bool(is_attended)
    return {
        "session_id": sid,
        "task_id": tid,
        "origin": org,
        "trigger": str(trigger or ""),
        "controllable": controllable,
        "operator_controllable": controllable,
    }


def register_live_server_turn(
    session_id: str,
    task_id: str,
    *,
    origin: object = "",
    trigger: object = "",
    attended: object = None,
) -> dict[str, Any] | None:
    """Record an attended-capable server-originated turn as addressable by task id."""
    payload = server_turn_control_payload(
        session_id,
        task_id,
        origin=origin,
        trigger=trigger,
        attended=attended,
    )
    if payload is None:
        return None
    key = _server_turn_key(payload["session_id"], payload["task_id"])
    if key not in _LIVE_SERVER_TURNS:
        while len(_LIVE_SERVER_TURNS) >= _LIVE_SERVER_TURNS_MAX:
            stale = _LIVE_SERVER_TURNS.pop(next(iter(_LIVE_SERVER_TURNS)))
            log.warning(
                "[server-turn] control registry at cap (%d) — evicting the oldest entry "
                "(session %s, task %s); it never deregistered",
                _LIVE_SERVER_TURNS_MAX,
                stale.session_id,
                stale.task_id,
            )
    _LIVE_SERVER_TURNS[key] = _LiveServerTurn(
        session_id=payload["session_id"],
        task_id=payload["task_id"],
        origin=payload["origin"],
        trigger=payload["trigger"],
        controllable=bool(payload["controllable"]),
    )
    return payload


def finish_live_server_turn(session_id: str, task_id: str) -> None:
    """Forget a live server turn once it parks or reaches a terminal state."""
    sid = str(session_id or "").strip()
    tid = str(task_id or "").strip()
    if sid and tid:
        _LIVE_SERVER_TURNS.pop(_server_turn_key(sid, tid), None)


def live_server_turn_control(session_id: str, task_id: str) -> dict[str, Any] | None:
    """The recorded control payload for this exact live session/task, if any."""
    sid = str(session_id or "").strip()
    tid = str(task_id or "").strip()
    turn = _LIVE_SERVER_TURNS.get(_server_turn_key(sid, tid)) if sid and tid else None
    if turn is None:
        return None
    currently_controllable = bool(turn.controllable and is_session_attended(turn.session_id))
    return {
        "session_id": turn.session_id,
        "task_id": turn.task_id,
        "origin": turn.origin,
        "trigger": turn.trigger,
        "controllable": currently_controllable,
        "operator_controllable": currently_controllable,
    }


def submit_server_turn_interjection(
    session_id: str,
    task_id: str,
    text: str,
    *,
    msg_id: str | None = None,
) -> dict[str, Any]:
    """Queue an operator interjection for a live, attended server-originated turn.

    This intentionally does not acquire the per-thread graph lock or start a graph
    turn. It only enqueues into the same steering queue that an already-running turn
    drains at the model-call boundary. Stale, terminal, wrong-session, unattended, and
    duplicate-id submissions are rejected without touching another session's queue.
    """
    sid = str(session_id or "").strip()
    tid = str(task_id or "").strip()
    body = str(text or "").strip()
    if not body:
        return {"ok": False, "reason": "empty", "pending": 0}
    turn = _LIVE_SERVER_TURNS.get(_server_turn_key(sid, tid)) if sid and tid else None
    if turn is None:
        return {"ok": False, "reason": "not_live", "pending": 0}
    if not turn.controllable or not is_session_attended(sid):
        return {"ok": False, "reason": "uncontrollable", "pending": 0}
    mid = str(msg_id or "").strip() or f"server-steer:{tid}:{len(turn.accepted_ids) + 1}"
    if mid in turn.accepted_ids:
        from graph import steering

        return {"ok": False, "reason": "duplicate", "id": mid, "pending": steering.pending(sid)}
    from graph import steering

    queued = steering.enqueue(sid, body, msg_id=mid)
    if queued is None:
        return {"ok": False, "reason": "empty", "pending": steering.pending(sid)}
    turn.accepted_ids.add(mid)
    return {"ok": True, "id": queued, "pending": steering.pending(sid)}


async def attendance_stream(session_id: str, *, keepalive_s: float = 15.0, is_disconnected=None):
    """SSE body for ``GET /api/chat/attend`` (#3110). Marks ``session_id`` attended for the
    whole life of the connection and RELEASES it in a ``finally`` — so a tab close, a route
    change, or a dropped socket always cleans up and presence fails back to unattended.

    Yields a ``: attending`` comment up front (fires the client's ``onopen``) then periodic
    ``: keepalive`` comments to hold the connection open through idle stretches, mirroring
    ``operator_api.routes._sse_event_stream``. ``is_disconnected`` (the request's disconnect
    probe) lets the loop exit promptly; when omitted the generator relies on being cancelled
    on client disconnect, which still runs the ``finally``."""
    sid = str(session_id or "").strip()
    attended = mark_session_attended(sid)
    try:
        yield ": attending\n\n"
        while True:
            if is_disconnected is not None:
                try:
                    if await is_disconnected():
                        break
                except Exception:  # noqa: BLE001 — a probe failure ends the stream (cleanup in finally)
                    break
            await asyncio.sleep(keepalive_s)
            yield ": keepalive\n\n"
    finally:
        if attended:
            release_session_attended(sid)

# What we resume an autonomous turn's HITL interrupt with, so the agent stops waiting and
# finishes the turn instead of deadlocking. Bounded by the cap below so a model that keeps
# re-asking can't auto-answer in an infinite loop; past the cap we force the turn to complete
# (clearing the stray interrupt) rather than parking — an autonomous turn must never park.
_AUTONOMOUS_HITL_SENTINEL = (
    "[no interactive operator available] This turn is running autonomously "
    "(scheduled / inbox / background), so no human can answer right now. Do not wait for "
    "input — proceed using your best judgment and explicitly state any assumption you made."
)
_MAX_AUTONOMOUS_AUTOANSWERS = 3


def _truthy(value) -> bool:
    """Coerce a metadata value to bool. JSON-RPC callers may send the flag as a real bool,
    a string ("true"/"1"), or an int — bare bool("false") is a footgun, so treat strings
    by their content."""
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def is_autonomous_origin(origin: object) -> bool:
    """Whether ``origin`` names a SERVER-FIRED turn — one nobody is watching or holding a
    stream for (see ``_AUTONOMOUS_ORIGINS``).

    Public because two decisions now hinge on the same set and must not drift: this
    module's HITL auto-answer (an unattended turn must not park for a human), and
    ``server.a2a``'s live-progress republish (#2361 — a turn the browser is streaming
    itself must NOT also come over the bus, or every tool card renders twice)."""
    return str(origin or "").strip().lower() in _AUTONOMOUS_ORIGINS


# ADR 0115 D6 — origins whose turns an operator is actively watching. These run under the
# model in-flight limiter's `interactive` class (graph/llm_limiter.py), so they (and the
# subagent tasks they spawn) jump the queue ahead of `bulk` fan-outs and hold the reserved
# slot on a saturated lane. The console's own turns carry `api-chat` (the non-streaming
# `/api/chat` route) or an empty origin (the streaming A2A path — a live operator is holding
# the stream; see the empty-origin note on `_AUTONOMOUS_ORIGINS` above). Everything else stays
# `default` (tagging is best-effort — the ADR tags nothing else): inbound `a2a` (the REMOTE
# caller is watching, not us), the server-fired autonomous origins, and the programmatic
# `v1` / `plugin` API surfaces.
_INTERACTIVE_ORIGINS = frozenset({"", "local", "api-chat", "console"})


def is_interactive_origin(origin: object) -> bool:
    """Whether ``origin`` names an operator-watched chat/console turn (ADR 0115 D6 —
    ``interactive`` on the model in-flight limiter). See ``_INTERACTIVE_ORIGINS``."""
    return str(origin or "").strip().lower() in _INTERACTIVE_ORIGINS


@contextlib.contextmanager
def _interactive_turn_priority(origin: object):
    """Run an operator-watched turn under the ADR 0115 D6 ``interactive`` model-limiter class,
    so it and the subagent tasks it spawns queue ahead of ``bulk`` fan-outs on a saturated
    lane. A no-op for every other origin, which stays ``default`` — A2A, background and
    scheduled turns are deliberately left untagged (best-effort tagging; untagged is
    ``default``).

    Mirrors ``goal_turn``: the ContextVar reset can raise if this scope is torn down in a
    different context than it was entered (an SSE consumer's early ``GeneratorExit`` closing
    the streaming generator), and the var resets on context exit regardless, so the raise is
    swallowed."""
    if not is_interactive_origin(origin):
        yield
        return
    from graph.llm_limiter import INTERACTIVE, reset_priority, set_priority

    token = set_priority(INTERACTIVE)
    try:
        yield
    finally:
        try:
            reset_priority(token)
        except ValueError:
            pass


def _background_resume_attended(request_metadata: dict | None) -> bool:
    """Whether a ``background-resume`` nudge was stamped ATTENDED at push-resume time — a
    live operator was connected to the origin session when the manager fired it (#3110).

    Read from the request metadata (the manager snapshots the SSE-boundary presence once,
    at resume time, so singleton and coalesced-batch nudges make the SAME decision for a
    given origin session and the turn honors it deterministically instead of re-racing a
    flapping signal). Fail-closed: anything but an explicit truthy ``attended`` reads as
    unattended, so an unstamped nudge keeps the pre-#3110 autonomous behavior."""
    return _truthy((request_metadata or {}).get("attended"))


def _is_autonomous(request_metadata: dict | None) -> bool:
    """Whether this turn runs with no operator watching (see _AUTONOMOUS_ORIGINS).

    Headless-first (#1911): a fleet-to-fleet A2A caller with no human in the loop can also
    DECLARE the turn unattended by setting ``unattended: true`` in the request metadata,
    which takes the same no-deadlock path as the internal autonomous origins. Undeclared
    plain A2A stays operator-attended (an approval interrupt still parks for a human).

    Attended background-resume (#3110): a ``background-resume`` nudge is autonomous ONLY
    while its origin session has no live operator connection. When the manager stamped it
    ``attended`` (an operator was on the wire at resume time), the report-delivery turn is
    NOT autonomous — it may park for ``ask_human`` / ``request_user_input`` so the human
    can answer, exactly like an ordinary operator turn. Every other autonomous origin
    (scheduler / watch / inbox / webhook / detached background) is unconditional, and an
    explicit ``unattended: true`` always wins."""
    md = request_metadata or {}
    if _truthy(md.get("unattended")):
        return True
    origin = md.get("origin")
    if (
        str(origin or "").strip().lower() in _ATTENDANCE_CONDITIONAL_ORIGINS
        and _background_resume_attended(md)
    ):
        return False
    return is_autonomous_origin(origin)


def _is_hitl_resume(request_metadata: dict | None) -> bool:
    """Whether this message IS the operator's answer to the pending HITL pause.

    The console stamps ``hitl_resume`` on the message metadata when it submits or
    dismisses a form / question / approval card — that message must resume the parked
    interrupt (``Command(resume=…)``), not run a fresh graph turn. A2A callers that
    resume properly (message/send on the parked taskId) never need this marker — the
    executor already flips ``resume`` for them."""
    return bool((request_metadata or {}).get("hitl_resume"))


# _hold_if_hitl_pending's "this message answers the pending interrupt" signal — a
# sentinel (not a string) so it can never collide with an interrupt VALUE.
_HITL_RESUME = object()


async def _hold_if_hitl_pending(
    message: str, session_id: str, config: dict, *, request_metadata: dict | None, fence=None
):
    """The HITL hold (#1560): decide what a FRESH message may do while this thread is
    parked at a ``request_user_input`` / ``ask_human`` / approval ``interrupt()``.

    LangGraph treats fresh input on an interrupted thread as "abandon the interrupt and
    continue" — the un-answered tool_call is left dangling (later stripped by
    ToolCallRepairMiddleware) and the model sees the new message BEFORE (and instead of)
    the form answer, while the parked task can never resolve. So while a HITL interrupt
    is pending:

    - the operator's actual answer (``hitl_resume`` metadata) → resume the graph
      properly (returns the ``_HITL_RESUME`` sentinel);
    - any other operator message → HOLD it: park it in the per-session steering queue
      (returns the interrupt payload). It stays queued while the form is open — the
      queue only drains at a model call, and the parked graph makes none — and folds in
      via ``SteeringMiddleware`` at the FIRST model call after the form resolves
      (submitted OR dismissed), i.e. immediately after the form response, in arrival
      order. A dismissal is also a resume, so held messages can never deadlock; the
      pending-form state itself lives in the durable LangGraph checkpoint (re-read
      here every time), so a restart can't strand the hold.

    A held message from a FENCED turn (``fence``, #2972) is queued WITH its fence, and the
    pass that folds it in is narrowed to it (``SteeringMiddleware``; narrowest wins) — so it
    can't be acted on by the wider toolset of an unfenced resumed pass. Carrying the fence
    (rather than holding the message back for a narrow-enough pass) keeps it in arrival
    order and never strands it: no such pass may ever come.

    Autonomous turns are exempt (they must never park — unchanged clobber semantics),
    and with no pending interrupt this returns ``None`` and the turn is untouched.
    Callers must hold the per-thread lock (the check must not race a parking turn)."""
    if _is_autonomous(request_metadata):
        return None
    pending_val = await _chat()._pending_interrupt_value(config)
    if pending_val is None:
        return None
    if _is_hitl_resume(request_metadata):
        return _HITL_RESUME
    from graph import steering

    steering.enqueue(session_id, message, fence=fence)
    log.info("[hitl] holding operator message for session %s — form pending", session_id)
    return pending_val


# ── Idle beacon (#1720) ──────────────────────────────────────────────────────
# The plugin auto-update loop honors a plugin's ``when: idle`` policy by checking
# BOTH signals below: the count of chat turns currently in flight (so a turn that
# runs longer than the idle window is never mistaken for idle — a start-only
# timestamp had that bug) AND a monotonic timestamp of the last turn boundary (so
# we also wait out a quiet cooldown after the last turn ends). A hot-reload
# rebuilds tools/routers — safe between turns, disruptive during one. Both entry
# points (streaming = A2A + console-stream, non-streaming = console + OpenAI-compat)
# bracket their turn with ``_turn_started()`` / ``_turn_ended()`` via a thin
# wrapper so the count is always balanced, even on early generator close or error.
_ACTIVE_TURNS: int = 0
_LAST_TURN_MONOTONIC: float = 0.0


def _turn_started(session_id: str = "") -> None:
    global _ACTIVE_TURNS, _LAST_TURN_MONOTONIC
    _ACTIVE_TURNS += 1
    _LAST_TURN_MONOTONIC = time.monotonic()
    # Per-session half of the same bracket: the busy signal `GET /api/chat/sessions/{id}`
    # reports as `active` (the Zed shim waits on it instead of interleaving two turns).
    _turn_activity.begin(session_id)


def _turn_ended(session_id: str = "") -> None:
    global _ACTIVE_TURNS, _LAST_TURN_MONOTONIC
    _ACTIVE_TURNS = max(0, _ACTIVE_TURNS - 1)
    _LAST_TURN_MONOTONIC = time.monotonic()
    _turn_activity.end(session_id)


def active_turns() -> int:
    """Chat turns currently in flight (0 ⇒ nothing running)."""
    return _ACTIVE_TURNS


def seconds_since_last_turn() -> float:
    """Seconds since the last chat turn boundary (start or end), or ``inf`` if none
    yet this process. Paired with ``active_turns()`` for the auto-update idle gate."""
    if _LAST_TURN_MONOTONIC <= 0:
        return float("inf")
    return max(0.0, time.monotonic() - _LAST_TURN_MONOTONIC)
