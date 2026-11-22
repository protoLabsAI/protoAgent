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

# Origins (#4144) whose client receives only the reply TEXT: the OpenAI-compatible ``/v1`` endpoint,
# ``POST /api/chat``, and ``plugin`` — the ADR 0018 ``HOST.invoke`` seam
# (``plugin_wiring._plugin_agent_invoke``), which hands a relay plugin (Discord and the other
# ``register_chat_surface`` adapters) only the joined assistant text, so no caller of it can
# receive a component. Nothing in their response can show an artifact, so their turns stamp
# ``renders_ui: False`` and the artifact tools answer honestly ("saved; this client can't display
# it", "no viewer to check") instead of pointing at a chat that isn't there.
_TEXT_ONLY_ORIGINS = frozenset({"v1", "api-chat", "plugin"})


def renders_ui(origin: str | None) -> bool:
    """Whether a turn from ``origin`` reaches a client that can display rendered UI (#4144)."""
    return str(origin or "").strip().lower() not in _TEXT_ONLY_ORIGINS


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
    settings: dict | None = None,
) -> dict[str, Any]:
    """Queue an operator interjection for a live, attended server-originated turn.

    This intentionally does not acquire the per-thread graph lock or start a graph
    turn. It only enqueues into the same steering queue that an already-running turn
    drains at the model-call boundary. Stale, terminal, wrong-session, unattended, and
    duplicate-id submissions are rejected without touching another session's queue.
    ``settings`` (the sender's per-message model/effort/bypass) ride the queued item so
    an interjection left undrained when the turn ends is restarted under them, not under
    the configured defaults (bd-0ad5v); the live fold itself ignores them.
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

    # Attended server-turn interjections remain text-only.
    queued = steering.enqueue(sid, body, msg_id=mid, settings=settings)
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


# What an autonomous turn's HITL interrupt is refused with when the unattended approval
# policy (ADR 0124 D5, graph/unattended_policy.py) does not allow it: the agent stops waiting
# and finishes the turn instead of deadlocking. The policy decides approvals per tool (allow /
# ask / never); this notice is what is left for a pause that carries no policy, such as a
# question. The bound below is a loop guard, not a tier: a model that keeps re-asking can't be
# refused in an infinite loop, and past it we force the turn to complete (clearing the stray
# interrupt) rather than parking — an autonomous turn must never park.
_AUTONOMOUS_HITL_SENTINEL = (
    "[no interactive operator available] This turn is running autonomously "
    "(scheduled / inbox / background), so no human can answer right now. Do not wait for "
    "input — proceed using your best judgment and explicitly state any assumption you made."
)
_MAX_UNATTENDED_REFUSALS = 3


def _truthy(value) -> bool:
    """Coerce a metadata value to bool. JSON-RPC callers may send the flag as a real bool,
    a string ("true"/"1"), or an int — bare bool("false") is a footgun, so treat strings
    by their content."""
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def spend_cap_status() -> dict:
    """Read-only instance meter and durable pause/approval state; never creates a decision."""
    from datetime import UTC, datetime

    from graph.decisions import decision_store
    from runtime.state import STATE

    config = STATE.graph_config
    enabled = bool(getattr(config, "spend_cap_enabled", False))
    store = STATE.telemetry_store
    spend = store.daily_autonomous_spend(_AUTONOMOUS_ORIGINS) if store else {
        "day": datetime.now(UTC).date().isoformat(), "cost_usd": None, "total_tokens": None,
    }
    rec = decision_store().spend_cap_decision(spend["day"]) if enabled else None
    approved = rec is not None and rec.state == "answered"
    cost_limit = getattr(config, "spend_cap_daily_cost_hard_limit", 10.0)
    token_limit = getattr(config, "spend_cap_daily_token_hard_limit", 1_000_000)
    reached = store is not None and (spend["cost_usd"] >= cost_limit or spend["total_tokens"] >= token_limit)
    # Notice (soft) limits ride along for the console; 0 means "no notice limit". They never
    # affect ``paused`` — ``_notify_spend_soft_limit`` owns the once-a-day notice.
    cost_soft = float(getattr(config, "spend_cap_daily_cost_soft_limit", 0) or 0)
    token_soft = int(getattr(config, "spend_cap_daily_token_soft_limit", 0) or 0)
    return {**spend, "enabled": enabled, "meter_available": store is not None,
            "daily_cost_hard_limit": cost_limit, "daily_token_hard_limit": token_limit,
            "daily_cost_soft_limit": cost_soft, "daily_token_soft_limit": token_soft,
            "approved_today": approved, "decision_id": rec.id if rec else None,
            "paused": enabled and (bool(rec and rec.pending) or (not approved and (reached or store is None)))}


# ADR 0135 D11: importance belongs to the notice type, fixed here by the raising code path.
_SPEND_SOFT_NOTICE_IMPORTANCE = "normal"
_SPEND_SOFT_NOTICE_KIND = "spend-cap-soft"


def _notify_spend_soft_limit(status: dict) -> None:
    """Warn the operator once per UTC day that unattended spend passed a soft limit.

    Called only from the admission gate on an ADMITTED turn with the cap enabled, off
    the same meter reading (``status``) the hard limit uses — no second meter. The
    once-a-day marker is claimed in the meter's store BEFORE sending, so a restart or
    a concurrent admission cannot repeat it. Silent once the operator has answered the
    day's hard-limit decision: they already know. Best-effort: a failure here never
    changes admission.
    """
    try:
        from runtime.state import STATE

        config, store = STATE.graph_config, STATE.telemetry_store
        cost_soft = float(getattr(config, "spend_cap_daily_cost_soft_limit", 0) or 0)
        token_soft = int(getattr(config, "spend_cap_daily_token_soft_limit", 0) or 0)
        if store is None or status.get("approved_today"):
            return
        cost, tokens = status["cost_usd"] or 0, status["total_tokens"] or 0
        over_cost = cost_soft > 0 and cost >= cost_soft
        over_tokens = token_soft > 0 and tokens >= token_soft
        if not (over_cost or over_tokens) or not store.claim_daily_notice(status["day"], _SPEND_SOFT_NOTICE_KIND):
            return

        from graph.plugins.chat_surface import notify_chat_surfaces
        from graph.sdk import console_url
        from security.public_console import public_origin

        try:
            base = (public_origin() or console_url() or "").rstrip("/")
        except Exception:  # noqa: BLE001 — a link lookup must never suppress the text
            log.exception("[spend-cap] could not resolve the console origin for the soft-limit notice")
            base = ""
        passed = (f"${cost_soft:g} cost" if over_cost else f"{token_soft:,} token")
        notify_chat_surfaces(
            f"Unattended work has passed today's {passed} soft limit and is still running. "
            f"Spent so far today (UTC): ${cost:.2f} and {tokens:,} tokens. It will pause at "
            f"${status['daily_cost_hard_limit']:g} or {status['daily_token_hard_limit']:,} tokens. "
            "To change that, open Settings > Spend cap in the console and adjust the limits.",
            f"{base}/app/" if base else "",
            kind="notify", importance=_SPEND_SOFT_NOTICE_IMPORTANCE, source="spend-cap",
        )
    except Exception:  # noqa: BLE001 — the soft notice is advisory; admission must not depend on it
        log.exception("[spend-cap] could not send the soft-limit notice")


def autonomous_admission_error(origin: object, session_id: str) -> str | None:
    """One instance admission gate for both drivers, before any model/dispatch work.

    No awaits between checking and creating: admissions on the server event loop
    cannot raise duplicate decisions. The ledger deduplicates the UTC-day identity
    and survives restart. Missing/unreadable accounting fails closed when enabled.
    """
    from graph.decisions import SPEND_CAP_PREFIX, decision_store
    from runtime.state import STATE

    if not is_autonomous_origin(origin) or not getattr(STATE.graph_config, "spend_cap_enabled", False):
        return None
    try:
        status = spend_cap_status()
        if not status["paused"]:
            _notify_spend_soft_limit(status)
            return None
        if not status["decision_id"]:
            rec = decision_store().create(
                session_id or "system:activity",
                "Unattended work is paused at the daily spend cap. Resume for the rest of today (UTC)?",
                [{"id": "resume", "label": "Resume unattended work for today"}],
                agent=_current_agent_name(),
                interrupt_id=SPEND_CAP_PREFIX + status["day"],
                context=f"Autonomous spend: ${status['cost_usd']}; tokens: {status['total_tokens']}. "
                        f"Daily limits: ${status['daily_cost_hard_limit']}; {status['daily_token_hard_limit']} tokens. "
                        "An answer permits all unattended work for the current UTC day. Job settings are unchanged.",
            )
            status["decision_id"] = rec.id
        return f"Unattended work paused by spend cap; answer decision {status['decision_id']} to resume."
    except Exception:
        log.exception("[spend-cap] admission accounting unavailable")
        return "Unattended work paused: spend-cap accounting is unavailable."


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


def turn_caller_may_approve() -> bool:
    """Whether the caller driving this turn may answer an approval card or ask to skip one —
    from the tier the auth middleware classified, never from request metadata. One place, so
    both turn drivers bind the same answer. False only for the federation tier (ADR 0066)."""
    from observability.tracing import current_trust_tier

    return current_trust_tier() != "federation"


def operator_chat_turn(origin: object) -> bool:
    """Authenticate and classify a turn before carrying its provenance into tool state.

    Empty origin is the console streaming protocol; None is unclassified. Never infer
    authority from a session id or caller-supplied metadata's trust label.
    """
    from observability.tracing import current_trust_tier

    return isinstance(origin, str) and is_interactive_origin(origin) and current_trust_tier() == "operator"


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
    if str(origin or "").strip().lower() in _ATTENDANCE_CONDITIONAL_ORIGINS and _background_resume_attended(md):
        return False
    return is_autonomous_origin(origin)


# ── request_decision: the one HITL interrupt that parks even on an autonomous turn ──
# ADR 0070 (this card's amendment): every HITL interrupt on an autonomous turn auto-answers
# so the turn can't deadlock — EXCEPT a ``kind: "decision"`` one (tools.lg_tools.
# request_decision). That is the agent deliberately stopping at a choice it is not allowed to
# make on its own, not one it could guess at: so the turn parks exactly as an attended one
# does, and the server writes a durable decisions-ledger record (graph/decisions.py) that
# outlives the input-required TTL sweep. The operator answers it over /api/decisions, which
# resumes the parked turn. The streaming and non-streaming drivers consult these at call time.
#
# The park is gated on the LEDGER RECORD, not merely the checkpoint interrupt: an autonomous
# turn holds only while the decision is STILL pending (``_decision_interrupt_pending``). Once
# the decision is cancelled / answered / expired, its interrupt is stale and the hold lets
# the turn through — so a dismissed decision can never wedge the session's later scheduled
# fires, even if its checkpoint interrupt lingers.


def is_decision_interrupt(payload: object) -> bool:
    """Whether an interrupt payload is a ``request_decision`` pause (``kind == "decision"``)."""
    return isinstance(payload, dict) and str(payload.get("kind") or "").strip().lower() == "decision"


async def _decision_interrupt_pending(config: dict) -> bool:
    """Whether a STILL-PENDING decisions-ledger record backs this thread's checkpoint interrupt.

    The signal that an autonomous turn must PARK (wait for a person) on a decision rather
    than auto-answer it. A decision interrupt whose record is no longer pending — the
    operator cancelled it (the cancel route marks the record but the checkpoint interrupt
    can linger), it was answered, it expired (card 2), or it was never recorded — is STALE:
    left to park, it would wedge EVERY later autonomous (scheduler / inbox / webhook) turn
    on the session. So a stale decision interrupt is treated like any other autonomous
    interrupt (auto-answered by the caller), never parked.

    Reached only on the narrow autonomous-decision branch of the hold, so the extra snapshot
    read it does (to key the record by interrupt id) never touches the common hold path.
    Fails OPEN to "not pending" (⇒ don't park, auto-answer): an autonomous turn must never
    deadlock, so an unresolvable record never holds one."""
    try:
        from graph.decisions import decision_store

        found = await _chat()._pending_interrupt(config)
        interrupt_id = str((found[0] if found else "") or "")
        if not interrupt_id:
            # An empty id would match a no-pause record (#5678 9a, ``decisions.never_block``):
            # it has no pause to hold for, so it never parks an autonomous turn.
            return False
        thread_id = str((config.get("configurable") or {}).get("thread_id") or "")
        rec = decision_store().for_interrupt(thread_id, interrupt_id)
        return rec is not None and rec.pending
    except Exception:  # noqa: BLE001 — an unresolved record fails open to no-park (no deadlock)
        return False


def _current_agent_name() -> str:
    """The active agent's identity for decision ownership, blank only when unknown.

    Keep the default identity too: dropping it prevents another session of this instance
    from withdrawing its own decision. The ledger is scoped to this instance.
    """
    try:
        from runtime.state import STATE

        return str(getattr(getattr(STATE, "graph_config", None), "identity_name", "") or "")
    except Exception:  # noqa: BLE001 — a missing name must never break a park
        return ""


async def record_decision_pause(session_id: str, payload: object, config: dict) -> object | None:
    """Write a durable decisions-ledger record for a parked ``request_decision`` pause, so the
    park shows in the operator inbox and survives the checkpoint TTL.

    Best-effort by contract: the record is the durable HANDLE, not a gate — a ledger failure
    is logged and the park still stands (an autonomous turn must never un-park because a write
    failed). Returns the created record, or ``None`` when nothing was written."""
    if not is_decision_interrupt(payload):
        return None
    try:
        from graph.decisions import decision_store

        found = await _chat()._pending_interrupt(config)
        return decision_store().create(
            str(session_id or ""),
            str(payload.get("question") or ""),  # type: ignore[union-attr]
            payload.get("options") or [],  # type: ignore[union-attr]
            agent=_current_agent_name(),
            thread_id=config["configurable"]["thread_id"],
            interrupt_id=str(found[0] or "") if found else "",
            context=str(payload.get("context") or ""),
            expires_in_minutes=payload.get("expires_in_minutes"),
            default_option=payload.get("default_option"),
            default_action=payload.get("default_action"),
            requires_pick=bool(payload.get("requires_pick", False)),
        )
    except Exception:  # noqa: BLE001 — the ledger is a durable handle, never a turn gate
        log.warning("[decision] could not record a parked decision for session %s", session_id, exc_info=True)
        return None


async def decision_pause_payload(session_id: str, payload: object, config: dict) -> object:
    """Attach the durable ledger identity without mutating the checkpoint payload.

    Call under the thread lock, including when echoing a held pause. Recording is
    best-effort: failure preserves the pause and logs the error. Generic HITL passes
    through unchanged.
    """
    record = await record_decision_pause(session_id, payload, config)
    if record is not None:
        return {**payload, "decision_id": record.id}
    return payload


def _is_hitl_resume(request_metadata: dict | None) -> bool:
    """Whether this message IS the operator's answer to the pending HITL pause.

    The console stamps ``hitl_resume`` on the message metadata when it submits or
    dismisses a form / question / approval card — that message must resume the parked
    interrupt (``Command(resume=…)``), not run a fresh graph turn. A2A callers that
    resume properly (message/send on the parked taskId) never need this marker — the
    executor already flips ``resume`` for them."""
    return bool((request_metadata or {}).get("hitl_resume"))


#: The ``answered_by`` every decision answered through a turn records (#5678 5b). Only a
#: verified human-operator credential (``caller_may_answer_decision``) can resume a paused
#: decision, and every such credential — the console bearer, a paired phone, a hub delegation —
#: is the operator; the answer route records the same actor. Never read from message data.
DECISION_ANSWERER = "operator"


def caller_may_answer_decision() -> bool:
    """Whether the caller driving this turn may ANSWER a paused ``request_decision`` (#5678 5b).

    Reads the principal kind authentication stamped for the request (``verified_principal_scope``
    — the HTTP middleware, upgraded by the executor for this process's own resume grant), never
    request metadata: ``hitl_resume``, a named task id or ``decision_resolution`` data are all
    caller-supplied. A human operator passes (shared operator bearer, paired phone, a verified
    hub delegation); a federation caller, the fleet service token and a paired agent device do
    not, and their message is handled as an ordinary message while the decision stays pending.
    Open mode (no credential enforced) rates every caller the operator, as the answer route
    does; no bound principal is an in-process turn (a test, the SDK), not a remote caller."""
    from a2a_impl.auth import is_human_operator, open_mode
    from graph.middleware.request_context import current_verified_principal

    kind = current_verified_principal()
    if kind is None:
        return True
    return is_human_operator(kind) or (kind == "open/unknown" and open_mode())


# _hold_if_hitl_pending's "this message answers the pending interrupt" signal — a
# sentinel (not a string) so it can never collide with an interrupt VALUE.
_HITL_RESUME = object()


async def _hold_if_hitl_pending(
    message: str,
    session_id: str,
    config: dict,
    *,
    request_metadata: dict | None,
    fence=None,
    images: list[tuple[str, str]] | None = None,
    incognito: bool | None = None,
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

    A held message keeps its ``images``, the same way the steer route queues them (#4654):
    each one is saved as a bridged attachment, unless the message is incognito, and the
    fold hands it to a vision model. Holding only the text dropped a screenshot the
    operator sent while a decision was open. The resumed turn then saw the words, not the
    picture, and had to ask for it again (2026-11-17). ``incognito`` is the turn's own flag
    when the caller has it (the non-streaming driver takes it as a parameter, not in its
    metadata); otherwise it is read from ``request_metadata``.

    Autonomous turns are exempt EXCEPT when a decision is still awaiting an answer, and
    with no pending interrupt this returns ``None`` and the turn is untouched. Callers must
    hold the per-thread lock (the check must not race a parking turn)."""
    pending_val = await _chat()._pending_interrupt_value(config)
    if _is_autonomous(request_metadata):
        # An autonomous turn never parks for a human — EXCEPT on a request_decision pause
        # that STILL has a pending ledger record (the agent chose to stop at a choice it may
        # not make on its own). A decision interrupt with no pending record — cancelled,
        # answered, expired, or never recorded — is STALE and must NOT hold the turn, or a
        # dismissed decision would wedge every later scheduled fire on the session.
        if not (is_decision_interrupt(pending_val) and await _decision_interrupt_pending(config)):
            return None
    if pending_val is None:
        return None
    if _is_hitl_resume(request_metadata):
        if not is_decision_interrupt(pending_val) or caller_may_answer_decision():
            return _HITL_RESUME
        # Only the operator answers a decision (#5678 5b): a peer's ``hitl_resume`` is an
        # ordinary message, held below like any other while the decision stays pending.
        log.info("[decision] non-operator hitl_resume on session %s held — decision stays pending", session_id)
    from graph import steering

    if images:
        from runtime.state import STATE

        if incognito is None:
            incognito = bool((request_metadata or {}).get("incognito"))
        steering.enqueue(
            session_id,
            message,
            fence=fence,
            images=images,
            refs="" if incognito else _chat()._bridge_attachment_ids(images, session_id),
            vision=bool(getattr(STATE.graph_config, "model_vision", False)),
            incognito=incognito,
        )
    else:
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


def _session_state() -> ModuleType:
    """``server.session_state``, resolved at call time like :func:`_chat` — the bracket
    below is this module's only ``server`` collaborator besides the chat drivers."""
    return importlib.import_module("server.session_state")


def _turn_started(session_id: str = "") -> None:
    global _ACTIVE_TURNS, _LAST_TURN_MONOTONIC
    _ACTIVE_TURNS += 1
    _LAST_TURN_MONOTONIC = time.monotonic()
    # Per-session half of the same bracket: the busy signal `GET /api/chat/sessions/{id}`
    # reports as `active` (the Zed shim waits on it instead of interleaving two turns).
    # The 0→1 edge is the session's idle/paused→running transition (ADR 0130 S2):
    # `session.changed` publishes from the SAME bracket every driver passes through,
    # so a local, server-started, steer-restarted or resumed turn all announce alike.
    if _turn_activity.begin(session_id):
        try:
            _session_state().note_turn_started(session_id)
        except Exception:  # noqa: BLE001 — the live-sync event must never break the bracket
            log.exception("[session-state] turn-start publish failed for %s", session_id)


def _turn_ended(session_id: str = "") -> None:
    global _ACTIVE_TURNS, _LAST_TURN_MONOTONIC
    _ACTIVE_TURNS = max(0, _ACTIVE_TURNS - 1)
    _LAST_TURN_MONOTONIC = time.monotonic()
    # The →0 edge: the session's end state (idle, or paused when the leg parked) is
    # derived against the task store inside note_turn_ended, not guessed here.
    if _turn_activity.end(session_id):
        try:
            _session_state().note_turn_ended(session_id)
        except Exception:  # noqa: BLE001 — the live-sync event must never break the bracket
            log.exception("[session-state] turn-end publish failed for %s", session_id)
    # The last in-flight turn just settled — the moment a queued priority-now inbox page
    # OR a priority-next item can run (next means after the current work, not never). The
    # kick is a cheap no-op unless one may be pending (in-memory hint), and it must never
    # break the turn bracket.
    if _ACTIVE_TURNS == 0:
        try:
            from server import stores

            stores.kick_inbox_now_retry()
        except Exception:  # noqa: BLE001 — the sweep loop is the backstop
            log.debug("[inbox] turn-settle retry kick failed", exc_info=True)


def active_turns() -> int:
    """Chat turns currently in flight (0 ⇒ nothing running)."""
    return _ACTIVE_TURNS


def seconds_since_last_turn() -> float:
    """Seconds since the last chat turn boundary (start or end), or ``inf`` if none
    yet this process. Paired with ``active_turns()`` for the auto-update idle gate."""
    if _LAST_TURN_MONOTONIC <= 0:
        return float("inf")
    return max(0.0, time.monotonic() - _LAST_TURN_MONOTONIC)


async def validate_decision_resume(config: dict, decision_id: str, session_id: str = "") -> dict:
    """Reject stale decision delivery under the thread lock, before any graph input."""
    from graph.decisions import DecisionClosed, decision_store

    rec = decision_store().get(decision_id)
    if rec is not None and session_id:
        if rec.session_id != session_id:
            raise DecisionClosed("decision belongs to another session")
        # Fresh-context goal iterations park on a scoped thread. Keep the session lock,
        # but resume that exact checkpoint rather than the base chat's checkpoint.
        config = {**config, "configurable": {**config["configurable"], "thread_id": rec.thread_id}}
    found = await _chat()._pending_interrupt(config)
    if (
        rec is None or not rec.pending or not rec.interrupt_id
        or rec.thread_id != config["configurable"]["thread_id"]
        or found is None or found[0] != rec.interrupt_id
    ):
        # A pending record on THIS thread whose checkpoint provably no longer holds its
        # interrupt can never be answered: close it now (under the session lock), so the
        # answer route reports the real state (409) and the card stops offering it (#5107).
        # An unreadable checkpoint is "unknown" and closes nothing.
        if rec is not None and rec.pending and rec.thread_id == config["configurable"]["thread_id"]:
            from graph.decision_reconcile import close_stranded, interrupt_parked
            from runtime.state import STATE

            if await interrupt_parked(STATE.graph, rec) is False:
                close_stranded(decision_store(), rec)
        raise DecisionClosed("decision no longer owns this interrupt")
    return config


async def answer_typed_decision(config: dict, message: str, session_id: str,
                                request_metadata: dict | None = None) -> tuple[dict, str] | None:
    """Save an exact operator answer before delivering it in this already-locked turn."""
    from a2a_impl.auth import is_human_operator, open_mode
    from graph.decisions import decision_store
    from graph.middleware.request_context import current_verified_principal

    md = request_metadata or {}
    # An attended background result is still server-started. Unknown origins also
    # fail closed; only established incoming message surfaces can supply typed answers.
    origin = str(md.get("origin") or "").strip().lower()
    server_started = origin not in {"", "local", "console", "api-chat", "v1", "plugin", "a2a"}
    principal = current_verified_principal()
    operator = is_human_operator(principal) or (principal == "open/unknown" and open_mode())
    if (server_started or _truthy(md.get("unattended")) or md.get("decision_id")
            or not operator):
        return None
    # A parked form/question still owns the next input; never consume its answer here.
    if await _chat()._pending_interrupt_value(config) is not None:
        return None
    try:
        rec = decision_store().answer_typed(session_id, config["configurable"]["thread_id"], message,
                                            principal=principal)
    except Exception:
        log.warning("Could not save typed decision answer on %s", session_id, exc_info=True)
        return None
    if rec is None:
        return None
    return await claim_decision_delivery(config, rec.id, session_id)


async def claim_decision_delivery(config: dict, decision_id: str, session_id: str) -> tuple[dict, str] | None:
    """Accept a turn that delivers a no-pause decision's saved answer (#5678 2c), under the lock.

    ``None`` when the record is not a no-pause one (unknown, or it names a pause): the caller
    sends it through :func:`validate_decision_resume` exactly as before. For a no-pause record
    the turn is refused (:class:`DecisionClosed`, before any graph input) unless the record
    belongs to this session, has a saved answer or expired default, and was not delivered yet; the delivery is then
    claimed atomically, so a duplicate turn can never feed the answer twice. Returns the config
    on the record's thread and the message to run, which is built from the LEDGER — the
    incoming message text is never trusted to carry the answer."""
    from graph.decision_resume import decision_answer_message
    from graph.decisions import DecisionClosed, decision_store, is_unpaused_decision

    store = decision_store()
    rec = store.get(decision_id)
    if rec is None or not is_unpaused_decision(rec):
        return None
    if rec.session_id != session_id:
        raise DecisionClosed("decision belongs to another session")
    if rec.state != "answered" and not (rec.state == "expired" and rec.answer is not None):
        raise DecisionClosed("decision has no saved answer to deliver")
    if rec.delivered_at:
        raise DecisionClosed("decision answer was already delivered")
    message = decision_answer_message(decision_id, session_id)
    if not store.claim_delivery(decision_id):
        raise DecisionClosed("decision answer was already delivered")
    return {**config, "configurable": {**config["configurable"], "thread_id": rec.thread_id}}, message


@contextlib.asynccontextmanager
async def decision_resume(config: dict, answer):
    """Close the exact ledger entry only once its checkpoint interrupt is consumed.

    Runs on every resume surface, including console and taskId answers. A failed graph
    invocation that leaves the interrupt in place keeps the decision retryable.
    """
    from graph.decisions import ANSWER_KINDS, DecisionClosed, decision_store
    from runtime.state import STATE

    found = await _chat()._pending_interrupt(config) if answer is not None else None

    async def settle():
        if found and found[0] and is_decision_interrupt(found[1]):
            snapshot = await STATE.graph.aget_state(config)
            pending_ids = {
                i.id for t in (getattr(snapshot, "tasks", ()) or ())
                if getattr(t, "result", None) is None
                for i in (getattr(t, "interrupts", ()) or ())
            }
            if not getattr(snapshot, "tasks", ()):
                pending_ids.update(i.id for i in (getattr(snapshot, "interrupts", ()) or ()))
            if found[0] not in pending_ids:
                store = decision_store()
                rec = store.for_interrupt(config["configurable"]["thread_id"], found[0])
                if rec is not None and rec.pending:
                    try:
                        resolution = config.get("configurable", {}).get("decision_resolution") or {}
                        # A resumed turn can reach another pause; attribution belongs only
                        # to the decision addressed by this request.
                        if resolution.get("id", rec.id) != rec.id:
                            resolution = {}
                        if resolution.get("state") == "expired":
                            store.expire(rec.id)
                        else:
                            # No resolution = the operator typed the answer into the chat
                            # composer (hitl_resume): a text answer (#5668). The resolution
                            # rides request metadata, so an unknown kind falls back to text
                            # rather than failing the settle. ``answered_by`` is the verified
                            # credential's actor, never the resolution's (#5678 5b).
                            answer_kind = resolution.get("answer_kind", "text")
                            if answer_kind not in ANSWER_KINDS:
                                answer_kind = "text"
                            store.answer(rec.id, resolution.get("answer", str(answer)),
                                         answered_by=DECISION_ANSWERER,
                                         answered_surface=resolution.get("answered_surface", "chat"),
                                         answer_kind=answer_kind)
                    except DecisionClosed:
                        pass
                return True
        return False

    async def watch():
        while True:
            try:
                if await settle():
                    return
            except Exception:
                log.exception("Could not read decision consumption checkpoint; retrying")
            await asyncio.sleep(0.02)

    # Both drivers hold the session lock. Observe persisted checkpoint consumption
    # independently of the rest of the resumed turn.
    watcher = asyncio.create_task(watch()) if found and is_decision_interrupt(found[1]) else None
    try:
        yield
    finally:
        if watcher is not None:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
        await settle()
