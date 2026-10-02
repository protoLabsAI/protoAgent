"""The A2A delegate adapter and its wire helpers (ADR 0025).

Split out of ``adapters.py`` (#3831): the A2A request headers, JSON-RPC error/progress
helpers, the peer's cost-v1 billing (#3016/#3038), the short-reply smell (#3085), the
401/403 auth hint, and ``A2aAdapter`` itself. ``adapters`` re-exports every name here and
still holds the ``ADAPTERS`` registry. Module-level state (``_clamp_warned``,
``_DETACHED_DELEGATION``, ``_SHORT_REPLY_MIN_ELAPSED_S``) lives HERE — patch it on this
module, not on ``adapters``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import uuid
from contextvars import ContextVar

from .base import (
    KIND_STILL_RUNNING,
    KIND_TIMEOUT,
    KIND_UNREACHABLE,
    Adapter,
    Delegate,
    DelegateError,
    FieldSpec,
    _env_fields,
    _parse_env,
    _secret,
    _timed,
)

logger = logging.getLogger("protoagent.plugins.delegates")


# How much of a JSON-RPC error's free-form ``data`` member survives into the message the
# delegating agent reads. Generous enough for a peer's traceback, capped so a peer that
# echoes a whole request body can't flood the caller's context.
_A2A_ERROR_DETAIL_LIMIT = 2000

# A2A 1.0 ``TaskNotFoundError``: the peer no longer knows a task id (it restarted, or its
# task retention expired). ``A2aAdapter.get_task`` reads it as "gone", not as a failure.
_A2A_TASK_NOT_FOUND = -32001


def _a2a_headers(d) -> dict:
    """The request headers every A2A call to ``d`` carries — dispatch and ``get_task`` alike."""
    # A2A-Version is mandatory for an a2a-sdk >=1.0 peer: a missing header defaults
    # to 0.3 on the receiver → -32009 VERSION_NOT_SUPPORTED (ADR 0051 audit). The
    # scheduler/inbox/background self-POSTs already set it; the delegate client must too.
    headers = {"Content-Type": "application/json", "A2A-Version": "1.0"}
    if d.auth_token:
        headers["Authorization"] = f"Bearer {d.auth_token}" if d.auth_scheme != "apiKey" else d.auth_token
        if d.auth_scheme == "apiKey":
            headers["X-API-Key"] = d.auth_token
    elif _is_loopback_url(d.url):
        # ADR 0089 D4: an in-instance delegate to a loopback member/board carries no
        # explicit credential (the "local board is tokenless" pattern, supervisor.py). Now
        # that members require a credential (D5), present the fleet service token so the
        # call still authenticates — the member accepts it as operator. Loopback ONLY: an
        # off-box delegate must configure its own token; the fleet token never leaves the box.
        try:
            from graph.fleet.service_token import resolve_service_token

            headers["Authorization"] = f"Bearer {resolve_service_token()}"
        except Exception:  # noqa: BLE001 — not in a fleet / no token: dispatch unauthenticated as before
            logger.debug("[delegates] no fleet service token for loopback delegate %r", d.name)
    return headers


def _continuity_credential(d) -> str:
    """The auth material that joins name+url in ``conversations``' keys: rotating a row's
    token IN PLACE must not hand the new principal what the old one was holding."""
    return f"{d.auth_scheme}:{d.auth_token}" if d.auth_token else ""


def _park_message(name: str, task_id: str, question: str) -> str:
    """What a delegation that PARKED on a question returns: the question plus the resume
    handle, phrased for the calling agent (the HITL delegation chain)."""
    return (
        f"⏸ delegate {name!r} needs input before it can continue.\n"
        + (f"Question: {str(question)[:1000]}\n" if question else "")
        + f"Parked task: {task_id}\n\n"
        f"To continue: answer with delegate_to(target={name!r}, "
        f"query='<your answer>', resume_task_id={str(task_id)!r}). "
        "If you can't answer it yourself, get the answer first — ask your own "
        "operator (ask_human) if you have one; your question bubbles up the same "
        "way — then resume with it."
    )


def _is_loopback_url(url: str) -> bool:
    """True when ``url`` targets this box's loopback interface — i.e. an in-instance
    member/board (its own workspace port, or the hub's own port for a ``host`` tenant
    path). Used to decide whether an otherwise-tokenless delegate may present the fleet
    service token (ADR 0089): loopback only — the token never rides off the box."""
    from urllib.parse import urlsplit

    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return host in ("localhost", "::1") or host == "127.0.0.1" or host.startswith("127.")


def _fleet_token_available() -> bool:
    """Would ``_a2a_headers`` have presented the fleet service token? (Same lookup, same
    failure mode — it degrades to "no credential" rather than raising.)"""
    try:
        from graph.fleet.service_token import resolve_service_token

        return bool(resolve_service_token())
    except Exception:  # noqa: BLE001 — not in a fleet / unreadable token: nothing was sent
        return False


def _hub_proxied_slug(url: str) -> str:
    """The member slug when ``url`` is a hub's loopback fleet proxy to another agent —
    ``http://127.0.0.1:<port>/agents/<slug>/a2a`` (ADR 0113 D4, what ``supervisor.status()``
    advertises for a remote member) — else ``""``. ``host`` is the hub itself, not a proxied
    member, so it doesn't count."""
    from urllib.parse import unquote, urlsplit

    if not _is_loopback_url(url):
        return ""
    try:
        parts = [p for p in urlsplit(url).path.split("/") if p]
    except ValueError:
        return ""
    if len(parts) == 3 and parts[0] == "agents" and parts[2] == "a2a" and parts[1] != "host":
        return unquote(parts[1])
    return ""


def _a2a_error_detail(d: Delegate, err: object) -> str:
    """Turn a JSON-RPC error payload into an operator-legible cause — especially the
    version-skew case, which otherwise surfaces as an opaque ``-32009``.

    Every other code keeps its ``code`` **and** its ``data`` member. ``-32603 "Internal
    error"`` is the generic JSON-RPC code: reading only ``message`` reduced a peer's real
    failure to the two least informative words it could have sent, with the cause sitting
    unread in ``data``."""
    code = err.get("code") if isinstance(err, dict) else None
    msg = str(err.get("message") or "").strip() if isinstance(err, dict) else str(err)
    if code == -32009 or "VERSION_NOT_SUPPORTED" in str(err).upper():
        return (
            f"delegate {d.name!r}: peer rejected A2A-Version 1.0 (VERSION_NOT_SUPPORTED) — it speaks "
            "an older A2A dialect. Upgrade the peer, or point its url at a 1.0 /a2a endpoint."
        )
    if not isinstance(err, dict):
        return f"delegate {d.name!r}: {err}"
    head = msg or "(no message)"
    if code is not None:
        head = f"{head} (JSON-RPC {code})"
    data = err.get("data")
    if data in (None, "", {}, []):
        return f"delegate {d.name!r}: {head}"
    detail = data if isinstance(data, str) else json.dumps(data, default=str)
    return f"delegate {d.name!r}: {head}: {' '.join(str(detail).split())[:_A2A_ERROR_DETAIL_LIMIT]}"


def _a2a_progress_fingerprint(result: object) -> str:
    """A stable fingerprint of the task fields this side can observe as progress.

    A2A peers are allowed to report a long-running task as ``TASK_STATE_WORKING`` many
    times. That heartbeat is not progress by itself: an identical task observation must
    still trip ``poll_timeout_s``. The fields below are deliberately the material,
    operator-visible shape the adapter can act on — task identity, context, state,
    status-message content and artifact content — not incidental server metadata such as
    timestamps that would make a stuck peer look alive forever.
    """

    def _compact(value):
        if isinstance(value, dict):
            return {
                str(k): _compact(v)
                for k, v in sorted(value.items(), key=lambda item: str(item[0]))
                if v not in (None, "", [], {})
            }
        if isinstance(value, list):
            return [_compact(v) for v in value]
        return value

    def _parts(message: object) -> list:
        if not isinstance(message, dict):
            return []
        return [
            _compact(
                {
                    "kind": part.get("kind") or part.get("type"),
                    "text": part.get("text"),
                    "data": part.get("data"),
                    "file": part.get("file"),
                }
            )
            for part in message.get("parts") or []
            if isinstance(part, dict)
        ]

    if not isinstance(result, dict):
        return json.dumps({"raw": _compact(result)}, sort_keys=True, separators=(",", ":"), default=str)
    task = result.get("task", result)
    if not isinstance(task, dict):
        return json.dumps({"raw": _compact(result)}, sort_keys=True, separators=(",", ":"), default=str)
    status = task.get("status") if isinstance(task.get("status"), dict) else {}
    observation = {
        "id": task.get("id"),
        "contextId": task.get("contextId"),
        "state": status.get("state"),
        "statusMessage": _parts(status.get("message")),
        "artifacts": [
            _compact(
                {
                    "name": art.get("name"),
                    "description": art.get("description"),
                    "parts": _parts(art),
                }
            )
            for art in task.get("artifacts") or []
            if isinstance(art, dict)
        ],
    }
    return json.dumps(_compact(observation), sort_keys=True, separators=(",", ":"), default=str)


def _status_message_text(result: object) -> str:
    """The peer task's STATUS-message text (its progress narration), or ``""``.

    Deliberately the status message ONLY — not ``_extract_text``, which also reads
    artifacts: a still-working task's artifacts are partial output, and a stateless task
    envelope carries text that is not an answer at all. When a poll deadline expires this is
    the "last status message" the result carries back to the caller (#3700), so it must be
    the peer's own account of where it is, nothing more.
    """
    if not isinstance(result, dict):
        return ""
    task = result.get("task", result)
    status = task.get("status") if isinstance(task, dict) else None
    message = status.get("message") if isinstance(status, dict) else None
    parts = message.get("parts") if isinstance(message, dict) else None
    if not isinstance(parts, list):
        return ""
    return " ".join(str(p.get("text") or "") for p in parts if isinstance(p, dict)).strip()


def _still_running_message(
    d: Delegate,
    task_id: str,
    state: object,
    status_text: str,
    *,
    poll_timeout: float,
    send_timeout: float | None = None,
    auto_delivered: bool = False,
) -> str:
    """The message a delegation returns when its poll deadline expires with the peer STILL
    working (#3700).

    Never a bare failure, and never "retry": it names the peer task id, the last observed
    state and status message, and says the work may still finish and can be resumed or
    collected with that id. ``poll_timeout_s`` is per-delegate configurable — raising it is
    the fix for a peer that legitimately runs longer than the no-progress bound, not
    re-sending the work (which double-boards it on a peer still busy with the first task).

    ``auto_delivered`` decides the ONE promise this message must not get wrong: whether
    something is still polling the task behind it. It is ``True`` only when a collection is
    left running after this message — a room address that stashed a pending handle for
    ``late.collect`` to bring the answer back on its own. A detached background delegation
    that has ALREADY exhausted its own ``late.collect_task`` window (or any caller with no
    conversation to collect into) leaves NOTHING polling, so the message must not promise
    auto-delivery — it points at ``resume_task_id`` to pick the finished reply up instead.
    Promising "delivered automatically" when nothing collects is the #3700 lost-reply failure
    re-told as a false reassurance.
    """
    status = " ".join(str(status_text or "").split())[:_A2A_ERROR_DETAIL_LIMIT]
    last_status = f'; last status "{status}"' if status else ""
    if send_timeout is not None:
        head = f"delegate {d.name!r} still running after {send_timeout:g}s (this call's timeout)"
        raise_hint = "raise this call's timeout"
    else:
        head = (
            f"delegate {d.name!r} still running after {int(poll_timeout)}s without observable progress "
            "(its configurable poll_timeout_s)"
        )
        raise_hint = "raise this delegate's poll_timeout_s"
    # The exact call that collects it (#3775): a ``resume_task_id`` on a task that is still
    # WORKING waits on that same task with ``GetTask`` only — it never re-sends the work.
    collect = f"delegate_to(target={d.name!r}, query='collect the result', resume_task_id={task_id!r})"
    if auto_delivered:
        delivery = (
            "Its answer will be delivered automatically on a later turn if it finishes; do NOT "
            f"re-send this work (that double-boards it). To collect it sooner, call {collect} — that "
            "waits on the SAME task (re-sending nothing) and returns its answer once it lands"
        )
    else:
        delivery = (
            "Nothing is polling it now, so its answer will NOT arrive on its own — collect it with "
            f"{collect}, which waits on the SAME task (re-sending nothing) and returns its answer once "
            "it lands; do NOT re-send this work (that double-boards it on a peer still busy with the "
            "first task)"
        )
    return (
        f"{head} — the peer may still be working on task {task_id} (state={state}{last_status}). "
        f"{delivery}, or {raise_hint} for a job that legitimately runs this long."
    )


# The A2A protocol version(s) our delegate client can speak (it sends the
# ``A2A-Version: 1.0`` header + the 1.0 SendMessage/GetTask dialect). Used to
# pre-check a peer's advertised version and fail fast on a clear mismatch.
_A2A_SUPPORTED_VERSIONS = ("1.0",)


def _advertised_a2a_versions(card: dict) -> list[str]:
    """Every A2A protocol version a peer's agent-card advertises (de-duped, in
    first-seen order), or ``[]`` if the card says nothing about it.

    Reads the native proto field (``supportedInterfaces[].protocolVersion``) AND
    the proto-free top-level hint (``protocolVersion`` / ``supportedVersions``)
    that protoLabs agents also expose — so an older or non-protoLabs peer is still
    understood when it advertises its version in either shape. ``[]`` means
    *don't know* (older peers, partial cards): callers must treat that as
    best-effort and NOT block."""
    if not isinstance(card, dict):
        return []
    seen: list[str] = []

    def _add(v: object) -> None:
        s = str(v or "").strip()
        if s and s not in seen:
            seen.append(s)

    for iface in card.get("supportedInterfaces") or []:
        if isinstance(iface, dict):
            _add(iface.get("protocolVersion"))
    _add(card.get("protocolVersion"))
    versions = card.get("supportedVersions")
    if isinstance(versions, (list, tuple)):
        for v in versions:
            _add(v)
    return seen


# ── the peer's own cost-v1 telemetry (#3016) ──────────────────────────────────
#
# A protoAgent peer measures the turn it just ran for us and ships the numbers back on
# the wire (``tools/a2a_parse._extract_cost`` reads them off the terminal artifact).
# This is the one delegation whose spend we never have to estimate — the peer already
# computed it, with its own pricing, for the work we caused — so the adapter takes it
# verbatim and bills it to the calling turn instead of dropping it on the floor.


# Bounds on the numbers a peer reports about itself (#3038).
#
# This is the first path in the codebase where a REMOTE party's number reaches local
# storage, so magnitude is a trust boundary and not tidiness. A peer answering with
# ``{"usage": {"input_tokens": 1e308}}`` becomes a 309-digit Python int, which the
# executor accumulates, ``record_turn`` folds into ``total_tokens``, and SQLite then
# refuses: ``OverflowError: Python int too large to convert to SQLite INTEGER``.
# ``record_turn`` swallows that because telemetry is best-effort — correct in isolation,
# and it handed a peer a way to delete the CALLING turn's whole row, the lead agent's
# own genuine spend with it.
#
# SQLite's signed-64-bit INTEGER is the hard wall; these ceilings sit far below it,
# because a per-turn count that large is not a real turn and a bound you can reason
# about beats one that merely avoids the crash. Negatives floor at zero: no peer gets to
# issue a token credit, and a negative would drive ``record_turn``'s disjoint prompt
# split (#3003) somewhere no consumer expects.
_MAX_WIRE_TOKENS = 1_000_000_000_000  # 1e12 tokens in one turn is not a turn
_MAX_WIRE_COST_USD = 1_000_000.0  # …nor is a million dollars of it

#: Delegates already warned about in this process. See :func:`_note_clamped`.
_clamp_warned: set[str] = set()


def _note_clamped(delegate: str, fields: list[str]) -> None:
    """Say ONCE PER PEER that its reported numbers were out of range (#3038).

    The dropped row was never truly silent — ``record_turn`` logs the ``OverflowError``
    at ERROR with a traceback naming the task. What it cannot say is WHICH remote party
    caused it, because by the time SQLite refuses the row the peer's number is one
    addend inside ``total_tokens``. Attribution is what this line adds.

    Once per peer, not once per delegation: the clamp sits on the hot path of every
    delegation and a peer that reports garbage reports it on every reply, so an
    unguarded warning is a log flood a hostile party sets the volume of. The budget is
    keyed on the delegate name, which bounds it by the delegate REGISTRY — an operator's
    list, not a remote party's — and, unlike a single process-wide token, one peer's
    clamp cannot spend the warning the next peer's clamp needs.
    """
    detail = ", ".join(fields)
    if delegate in _clamp_warned:
        logger.debug("[delegates] clamped out-of-range cost-v1 from %r: %s", delegate, detail)
        return
    _clamp_warned.add(delegate)
    logger.warning(
        "[delegates] peer %r reported out-of-range cost-v1 values (%s) — clamped to sane bounds (#3038). "
        "Later clamps from this peer are logged at DEBUG.",
        delegate,
        detail,
    )


def _wire_number(
    value,
    ceiling: float = _MAX_WIRE_COST_USD,
    *,
    field: str = "",
    clamped: list | None = None,
    unknown: list | None = None,
) -> float:
    """A wire number as a finite float bounded to ``[0, ceiling]``.

    Proto-JSON round-trips numbers as floats and a foreign peer may send them as
    strings, so every field is coerced rather than trusted. The two ways a value can
    fail are kept apart, because they say different things about the peer:

    * **out of range** — a real magnitude that is absurd. Bounded to ``0`` or
      ``ceiling``, and ``field`` is appended to ``clamped`` so the caller — the one that
      knows WHICH peer sent it — can report it.
    * **non-finite** — ``Infinity`` (JSON parses it, and ``1e400`` overflows to it) or
      ``NaN``. That says the peer lost track, not that it spent a lot, so it bills
      nothing, which is #3016's settled contract; ``field`` goes to ``unknown``, never
      to ``clamped``. Rejecting it here is also what keeps ``int(inf)`` — an
      ``OverflowError``, not the ``ValueError`` a coercion guard expects — out of the
      caller, and an infinite ``cost_usd`` out of the turn's sums.
    """
    oversized = False
    try:
        n = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    except OverflowError:
        # The natural wire spelling of an absurd number, and the one spelling the first
        # cut of this bound missed entirely (#3038): ``json.loads`` decodes a 400-digit
        # integer LITERAL to a Python int, and ``float()`` REFUSES an int wider than a
        # double rather than overflowing to inf the way the string ``"1e400"`` does.
        # Uncaught it raised out of :func:`_peer_usage_row` into ``_bill_peer_usage``'s
        # catch-all, which erased the peer's whole cost-v1 — the opposite of bounding
        # it. It is out of range, not unknown, so it takes the ceiling like any other
        # oversized magnitude.
        oversized = True
        n = -math.inf if isinstance(value, int) and value < 0 else math.inf
    if not oversized and not math.isfinite(n):
        if unknown is not None and field:
            unknown.append(field)
        return 0.0
    if n < 0:
        bounded = 0.0
    elif n > ceiling:
        bounded = ceiling
    else:
        return n
    if clamped is not None and field:
        clamped.append(field)
    return bounded


def _wire_int(
    value,
    ceiling: int = _MAX_WIRE_TOKENS,
    *,
    field: str = "",
    clamped: list | None = None,
    unknown: list | None = None,
) -> int:
    """A wire number as an int in ``[0, ceiling]`` — :func:`_wire_number` truncated
    toward zero. The default ceiling is exactly representable as a float, so the round
    trip through :func:`_wire_number` cannot land the clamp above the bound."""
    return int(_wire_number(value, float(ceiling), field=field, clamped=clamped, unknown=unknown))


def _peer_usage_row(result, delegate: str) -> dict | None:
    """A peer's cost-v1 payload as a turn-accumulator usage row (#3016), or ``None``.

    The shape is the one the A2A executor's ``usage`` lane already sums —
    ``{input_tokens, output_tokens, cache_read_input_tokens,
    cache_creation_input_tokens, cost_usd, model}`` — plus a ``peer`` tag: the #2872
    precedent applied to a peer, so delegated spend bills to the PARENT turn while
    staying identifiable, and the tag keeps the peer's prompt size out of the lead
    thread's context-window fill (a peer's context is not ours).

    ``input_tokens`` is carried through cache-INCLUSIVE, the LangChain
    ``usage_metadata`` convention the peer accumulated it under and the one this side's
    accumulator and ``record_turn`` expect (#3003) — so the stored row's disjoint
    prompt split stays correct with a peer's tokens in it.

    ``cost_usd`` is the peer's OWN ``costUsd``, never re-derived here: the peer knows
    which models it actually routed to and we don't, so re-pricing would be a guess
    dressed as a measurement. A peer that reports tokens but no ``costUsd`` therefore
    contributes tokens and no cost — a visible undercount rather than an invented number.

    Every field is BOUNDED on the way in (#3038): a peer is a remote party, and an
    unbounded number of its choosing used to take the calling turn's whole telemetry
    row down with it. See :data:`_MAX_WIRE_TOKENS`.

    ``None`` when the peer emitted no cost-v1 (any non-protoAgent A2A agent) or when the
    payload carries neither tokens nor a cost: the caller then behaves exactly as before.
    """
    from tools.a2a_parse import PEER_MODEL_PREFIX, _extract_cost

    payload = _extract_cost(result)
    if not payload:
        return None
    usage = payload.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    clamped: list[str] = []
    unknown: list[str] = []

    def _tokens(name: str) -> int:
        return _wire_int(usage.get(name), field=name, clamped=clamped, unknown=unknown)

    row = {
        "input_tokens": _tokens("input_tokens"),
        "output_tokens": _tokens("output_tokens"),
        "cache_read_input_tokens": _tokens("cache_read_input_tokens"),
        "cache_creation_input_tokens": _tokens("cache_creation_input_tokens"),
        "cost_usd": _wire_number(payload.get("costUsd"), field="costUsd", clamped=clamped, unknown=unknown),
    }
    if not any(row.values()):
        return None
    # Reported only once the row is one we will actually BILL, and only for values that
    # were really out of range (#3038). A payload that contributes nothing is dropped
    # above exactly as it was before, and spending the once-per-peer warning on a row no
    # consumer ever sees would leave a later, real clamp with nothing left to say it
    # with. Non-finite is the other half: an ordinary upstream condition with a settled
    # contract (#3016 — bill nothing, the peer lost track), so it goes to debug and
    # never names the peer in a line that claims its numbers were clamped to a bound.
    if clamped:
        _note_clamped(delegate, clamped)
    if unknown:
        logger.debug("[delegates] non-finite cost-v1 from %r billed as zero: %s", delegate, ", ".join(unknown))
    # A marker, not a model name: cost-v1 carries no model, and inventing one would break
    # the promise that `models` proves which model actually ran (ADR 0006 Slice 4b). The
    # prefix is what makes "what did this turn spend on peers" answerable from the stored
    # row without a second telemetry row per delegation — it is the only durable trace a
    # peer leaves, since the `peer` tag below is a stream-only routing hint. Every reader
    # that picks the model that RAN filters markers out with `a2a_parse.drop_peer_markers`.
    row["model"] = f"{PEER_MODEL_PREFIX}{delegate}"
    row["peer"] = delegate
    return row


# A detached background delegation must bill NOTHING, and saying so takes a flag (#3016).
#
# ``asyncio.create_task`` COPIES the spawning context, so the job that
# ``_spawn_background_delegation`` fires from inside the ``delegate_to`` tool body inherits
# that body's LangChain run context — the callback manager ``adispatch_custom_event`` reads.
# Left alone, a detached delegation therefore bills the spawning turn *if the peer answers
# while that turn is still streaming* and is silently dropped if it answers after: the same
# delegation producing two different telemetry rows depending on how fast the peer was.
# A detached job is not the spawning turn's work — it is delivered to a LATER turn — so it
# is excluded deterministically instead, which is also what ADR 0006 documents.
_DETACHED_DELEGATION: ContextVar[bool] = ContextVar("protoagent_detached_delegation", default=False)


def mark_delegation_detached() -> None:
    """Mark this coroutine's context as a detached background delegation (ADR 0050).

    Called at the top of the background job's own coroutine, so the flag is set in the
    context ``asyncio.create_task`` copied for it and never leaks back to the spawning
    turn (a contextvar set inside a task is that task's alone)."""
    _DETACHED_DELEGATION.set(True)


async def _bill_peer_usage(result, delegate: str) -> None:
    """Surface a peer's reported spend onto the CALLING turn's stream (#3016).

    Dispatched as a custom ``usage`` event from inside the ``delegate_to`` tool body's
    call stack — the lane #2872 built for subagents: ``server/chat.py`` forwards each one
    verbatim as a ``("usage", …)`` frame and the A2A executor's accumulator sums it into
    the turn's tokens, cost, and models. Reusing that lane is what lets
    ``Adapter.dispatch`` keep its ``-> str`` contract (stable across three adapters), and
    is why nothing is stashed on the adapter: ``ADAPTERS`` holds one instance per type for
    the whole process, so last-call state on it would race across a concurrent fan-out.

    Best-effort **end to end** — reading the payload included. A peer with no cost-v1
    emits nothing, having no run context at all (a unit test, the CLI runner) is not an
    error, and neither is a payload this cannot parse: telemetry must never turn a
    delegation that succeeded into one that failed. That is not hypothetical here — a
    raise would propagate through ``registry.dispatch``, which records the delegate as
    failing before re-raising, so a malformed number would discard an answer already in
    hand *and* put a red mark on a healthy peer.
    """
    if _DETACHED_DELEGATION.get():
        return
    try:
        row = _peer_usage_row(result, delegate)
        if not row:
            return
        from langchain_core.callbacks import adispatch_custom_event

        await adispatch_custom_event("usage", dict(row))
    except Exception:  # noqa: BLE001 — telemetry must never break a delegation
        logger.debug("[delegates] peer usage row not billed to the turn stream", exc_info=True)


# ── truncated / partial-reply smell (#3085) ───────────────────────────────────
#
# The observed failure: a background delegation to protoEngineer returned a 416-char
# reply that ended mid-sentence — narration of what the delegate was ABOUT to do, not
# the answer, which never arrived. The primary cause (streamed text parts newline-joined
# into mid-word breaks, and content beyond the first artifact dropped) is fixed in
# ``a2a_parse._extract_text``; this is the belt-and-braces diagnostic on the dispatch
# side. A reply this short after a dispatch that ran a non-trivial while reads as
# work-about-to-happen rather than the work itself, so the adapter says so — once, at
# WARNING, on the way past. It is a HEURISTIC (a genuinely terse answer trips it too), so
# it only logs and never alters the returned text.
_SHORT_REPLY_CHARS = 500
#: Below this dispatch duration a short reply is unremarkable — a fast, terse answer is
#: normal. The smell is a SHORT reply after a LONG wait, so gate on elapsed too.
_SHORT_REPLY_MIN_ELAPSED_S = 2.0


def _warn_if_suspiciously_short(delegate: str, text: str, elapsed_s: float) -> None:
    """Log a warning when a delegate's reply is suspiciously short relative to how long
    the dispatch ran (#3085) — a post-hoc aid for diagnosing truncated / partial replies,
    never a behavior change. Fires only once the dispatch has run past
    :data:`_SHORT_REPLY_MIN_ELAPSED_S`, so an instant terse answer stays quiet."""
    if len(text) >= _SHORT_REPLY_CHARS or elapsed_s < _SHORT_REPLY_MIN_ELAPSED_S:
        return
    logger.warning(
        "[delegates] delegate %r returned only %d chars after %.1fs — the reply may be truncated "
        "or a partial narration rather than the answer (#3085). If the peer streamed its reply, "
        "check that it consolidated its text parts before completing the task.",
        delegate,
        len(text),
        elapsed_s,
    )


def _a2a_auth_hint(d: Delegate, status_code: int) -> str:
    """The actionable half of a 401/403 from an A2A peer, or ``""``.

    A bare passthrough ("HTTP 401: Unauthorized: expected 'Authorization: Bearer '")
    tells the operator that something is unauthorized, not what to do — and the most
    likely cause is a deliberate design decision they have no way to know about: a
    tokenless delegate presents the fleet service token on **loopback only** (ADR 0089),
    because that token must never leave the box. Point an off-box delegate at a peer and
    it sends no credential at all, exactly as intended, and 401s.

    Names the least-privilege option too: a peer's ``auth.federation_token`` (ADR 0066)
    reaches only ``/a2a`` + ``/v1``, where its operator token opens the whole API.
    """
    if status_code not in (401, 403) or d.auth_token:
        return ""
    slug = _hub_proxied_slug(d.url)
    if slug:
        # A hub-proxied member (ADR 0113 D4 — what a remote's roster ``a2a`` points at). From
        # here we can't tell a REMOTE slug from a LOCAL one (the registry is the hub's), nor
        # which hop refused, so the wording stays neutral and names both fixes. The one thing
        # we DO know: whether a fleet token went out at all.
        if not _fleet_token_available():
            return (
                f" — no fleet service token could be resolved, so no credential reached the hub"
                f" proxy at {d.url}: the hub itself refused the call. Check that this instance is"
                " part of that hub's fleet, or set an explicit Auth token on the delegate."
            )
        return (
            f" — the hub proxy at {d.url} refused the call. If {slug!r} is a remote fleet"
            " member, the hub has no working token for it: pair it (Settings ▸ Agents ▸ Pair…)"
            " or update its token on the hub's fleet row — don't set a token on this delegate,"
            " the hub supplies the remote's credential. Otherwise check that the hub accepts"
            " this instance's fleet service token."
        )
    if _is_loopback_url(d.url):
        # Loopback AND no credential reached the peer — the service token lookup failed
        # rather than being withheld. Different problem, different fix.
        return (
            " — this is a loopback delegate, so the fleet service token should have been"
            " presented; it could not be resolved. Check that this instance is part of a fleet,"
            " or set an explicit Auth token on the delegate."
        )
    return (
        f" — {d.name!r} has no Auth token and {d.url} is not loopback, so no credential was sent:"
        " the fleet service token is presented for loopback peers only and never leaves the box"
        " (ADR 0089). Set the delegate's Auth token to the peer's `auth.federation_token` if it"
        " has one (ADR 0066 — it reaches only /a2a and /v1), otherwise the peer's `auth.token`."
    )


class A2aAdapter(Adapter):
    type = "a2a"
    label = "A2A agent"
    blurb = "A fleet peer over the A2A JSON-RPC protocol."
    secret_field = "auth.token"

    def config_schema(self) -> list[FieldSpec]:
        return [
            FieldSpec(
                "url",
                "URL",
                "text",
                required=True,
                placeholder="https://peer.example/a2a",
                help="The peer's A2A endpoint (usually ends in /a2a).",
            ),
            FieldSpec(
                "auth.scheme",
                "Auth scheme",
                "select",
                options=["", "bearer", "apiKey"],
                help="How the peer expects credentials, if any.",
            ),
            FieldSpec(
                "auth.token",
                "Auth token",
                "secret",
                help="Stored in secrets.yaml (gitignored), never in tracked config. If the "
                "peer has an auth.federation_token configured (ADR 0066), use THAT value "
                "here instead of their operator token — it reaches only /a2a + /v1 on "
                "their side, not their full operator surface.",
            ),
            FieldSpec(
                "poll_timeout_s",
                "Task poll timeout (s)",
                "number",
                default=300,
                advanced=True,
                help="Max seconds to wait without observed progress on a long-running delegated "
                "task before giving up locally — the peer keeps working. Also caps the initial "
                "synchronous SendMessage read, so a peer that answers inline (protoAgent's own "
                "server does) may legitimately take up to this long to reply — the old flat 60s "
                "hard-failed every member turn beyond it (#1778). Raise it for slow agents (e.g. "
                "a code build).",
            ),
            *_env_fields(),
        ]

    def parse(self, raw: dict) -> Delegate:
        d = Delegate(**self._base(raw))
        d.url = str(raw.get("url", "")).strip()
        if not d.url:
            raise DelegateError(f"a2a delegate {d.name!r} needs a url")
        auth = raw.get("auth") or {}
        d.auth_scheme = str(auth.get("scheme", "")).strip()
        d.auth_token = _secret(auth, "token", "credentialsEnv")
        try:
            d.poll_timeout_s = float(raw.get("poll_timeout_s") or 300.0)
        except (TypeError, ValueError):
            d.poll_timeout_s = 300.0
        _parse_env(raw, d)
        return d

    async def dispatch(
        self,
        d: Delegate,
        query: str,
        *,
        timeout: float | None = None,
        item_id: str | None = None,
        resume_task_id: str | None = None,
    ) -> str:
        import time

        import httpx  # noqa: F401 — used by the pre-flight probe below

        from observability import tracing
        from security import policy

        # An explicit per-call ``timeout`` counts from HERE, before the pre-flight probe, so
        # the probe's own time comes out of the caller's budget rather than on top of it.
        started = time.monotonic()
        blocked = policy.check_url(d.url)
        if blocked:
            raise DelegateError(blocked.replace("destination", f"delegate {d.name!r}", 1))
        # Pre-flight protocol-version check (best-effort). Fetch the peer's card and, if
        # it CLEARLY advertises an A2A protocol version we can't speak, fail fast with a
        # legible mismatch instead of sending and waiting for the opaque -32009
        # VERSION_NOT_SUPPORTED mid-dispatch. A silent/unreachable card never blocks — we
        # fall through to dispatch, whose -32009 mapping (``_a2a_error_detail``) still applies.
        info = await self.probe(d)
        advertised = info.get("supported_versions") or []
        if advertised and not any(v in _A2A_SUPPORTED_VERSIONS for v in advertised):
            raise DelegateError(
                f"delegate {d.name!r} advertises A2A protocol {'/'.join(advertised)} but this agent "
                f"speaks {'/'.join(_A2A_SUPPORTED_VERSIONS)} — refusing to dispatch (an older peer "
                "would reject the call with -32009 VERSION_NOT_SUPPORTED). Upgrade the peer, or point "
                "its url at a 1.0 /a2a endpoint."
            )
        headers = _a2a_headers(d)

        async def _rpc(client, method, params):
            body = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}
            # Map transport failures to a legible CAUSE — a delegating agent (and the
            # operator) needs "unreachable" vs "timed out" vs "version-incompatible",
            # not an opaque stack trace or a bare connection error.
            try:
                r = await client.post(d.url, json=body, headers=headers)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                raise DelegateError(
                    f"delegate {d.name!r} unreachable at {d.url} ({type(exc).__name__})",
                    kind=KIND_UNREACHABLE,
                ) from exc
            except httpx.PoolTimeout as exc:
                # NOT a peer timeout: we never got a connection out of the pool, so nothing
                # was sent and the peer saw nothing. Deliberately separated from the read /
                # write timeouts below — tagging this KIND_TIMEOUT would drop a conversation
                # the peer is still perfectly able to continue, throwing continuity away over
                # a purely local resource stall.
                raise DelegateError(
                    f"delegate {d.name!r} unreachable at {d.url} (PoolTimeout: no connection available)",
                    kind=KIND_UNREACHABLE,
                ) from exc
            except httpx.TimeoutException as exc:
                # A READ or WRITE timeout (connect and pool timeouts are caught above): bytes
                # went out, so the peer may well have accepted the request and be working on
                # it. Tagged so ``_dispatch_traced`` can drop this conversation's continuity —
                # the peer's answer lands in a context this side will never see, and a
                # protoAgent peer serializes turns per thread, so the next address would queue
                # behind the turn we just gave up on. Uncertainty resolves toward dropping:
                # losing continuity costs a re-send, reusing a context whose state we cannot
                # reason about costs the room.
                raise DelegateError(
                    f"delegate {d.name!r} timed out contacting {d.url}", kind=KIND_TIMEOUT
                ) from exc
            except httpx.HTTPError as exc:
                raise DelegateError(f"delegate {d.name!r} transport error: {str(exc)[:160]}") from exc
            if r.status_code >= 400:
                raise DelegateError(
                    f"delegate {d.name!r} HTTP {r.status_code}: {r.text[:200]}{_a2a_auth_hint(d, r.status_code)}"
                )
            data = r.json()
            if data.get("error"):
                raise DelegateError(_a2a_error_detail(d, data["error"]))
            return data.get("result") or {}

        poll_timeout = d.poll_timeout_s if d.poll_timeout_s and d.poll_timeout_s > 0 else 300.0

        # Boundary span for the OUTBOUND hop. Without it, a delegation is invisible on
        # the caller's side except as the enclosing `tool:delegate_to` observation, so
        # you cannot separate "the peer was slow" from "we were slow calling it" — the
        # first question anyone asks of a multi-agent trace. This span's duration IS the
        # cross-agent latency: dispatch → peer terminal, including the GetTask poll.
        #
        # Opened BEFORE the trace context is read below, deliberately: the peer then
        # nests under *this* span rather than the enclosing turn, so a delegation chain
        # reads as a real call tree instead of a flat list of sibling agents.
        with tracing.trace_span(
            f"a2a:{d.name}",
            metadata={"url": d.url, "delegate": d.name, "poll_timeout_s": poll_timeout},
            as_type="agent",
        ):
            # Live progress for the delegation card (#3979): only when whoever owns the card
            # bound a sink AND the peer's card advertises streaming — a non-streaming peer
            # keeps the spinner and the final reply, exactly as before.
            from graph.delegate_progress import DelegateProgress, current_sink

            from .a2a_progress import LiveView

            sink = current_sink()
            live = LiveView(DelegateProgress(d.name, sink)) if sink is not None and info.get("streaming") else None
            try:
                reply = await self._dispatch_traced(
                    d,
                    query,
                    send_timeout=timeout,
                    poll_timeout=poll_timeout,
                    _rpc=_rpc,
                    resume_task_id=resume_task_id,
                    started=started,
                    live=live,
                    headers=headers,
                )
            except asyncio.CancelledError:
                if live is not None:
                    live.abort()
                raise
            except BaseException:
                if live is not None:
                    await live.close(ok=False)
                raise
            if live is not None:
                await live.close(ok=True)
            return reply

    async def _dispatch_traced(
        self, d, query, *, send_timeout, poll_timeout, _rpc, resume_task_id=None, started=None, live=None, headers=None
    ) -> str:
        """The wire half of ``dispatch``, inside the outbound span (see caller)."""
        import time

        import httpx

        from tools.a2a_parse import (
            _extract_context_id,
            _extract_text,
            _is_input_required,
            _is_terminal,
            classify_answer,
            state_name,
        )

        from . import conversations

        # Mark protoAgent-originated peer delegation independently of tracing. The
        # receiver uses this only as operator-visible provenance; authorization is
        # still decided by the authenticated transport before the executor runs.
        # Stamp both levels because older SDK peers may preserve only one.
        provenance = {"origin": "a2a", "trigger": "delegate_to"}

        # Fleet tracing: when this dispatch runs inside a traced turn, hand the
        # peer our Langfuse trace context as ``a2a.trace`` metadata — the shape
        # a2a_impl/executor._extract_caller_trace reads on the receiving side
        # (camelCase traceId/spanId, threaded into trace_session as
        # caller_trace_id/caller_span_id → the peer JOINS our trace). Attached at
        # BOTH request level (preferred by the receiver's metadata merge) and
        # message level (fallback for peers whose SDK drops request metadata).
        send_params: dict = {
            "metadata": dict(provenance),
            "message": {
                "role": "ROLE_USER",
                "parts": [{"text": query}],
                "messageId": str(uuid.uuid4()),
                "metadata": dict(provenance),
            },
            # Ask for the task back NOW instead of holding this request open for the whole
            # turn (#3360; A2A 1.0 ``SendMessageConfiguration.returnImmediately``). A peer
            # that holds the connection — protoAgent's own server does, by default — hands
            # over no task id until it has finished, so a turn that outruns the read budget
            # ends as a bare transport timeout with nothing left to observe: no progress to
            # reset ``poll_timeout_s`` on (it degrades into a flat wall-clock cap) and no
            # handle for anything to come back to. Returned at once, the task goes through
            # the GetTask loop below, which already knows progress, parks and terminal
            # states. A peer that ignores the flag answers inline exactly as before, and
            # the read budget below still covers it.
            "configuration": {"returnImmediately": True},
        }
        try:
            from observability import tracing

            tctx = tracing.current_trace_context()
        except Exception:  # noqa: BLE001 — tracing must never break a dispatch
            tctx = None
        if tctx:
            wire = {"traceId": tctx["trace_id"]}
            if tctx.get("span_id"):
                wire["spanId"] = tctx["span_id"]
            send_params["metadata"]["a2a.trace"] = wire
            send_params["message"]["metadata"]["a2a.trace"] = wire
        # A2A 1.0 (a2a-sdk >=1.0): JSON-RPC `SendMessage` / `GetTask`, the ROLE_USER
        # enum, and a `result.task` envelope. (`message/send` + lowercase `user` is
        # the v0.3 legacy dialect, which 1.0 servers reject with -32601.)
        #
        # Resume of a PARKED task (the HITL delegation chain): the caller answers a
        # question the peer raised mid-task. Wire shape proven by the handler tests:
        # SendMessage carrying the parked task's taskId + contextId re-enters
        # execute() with resume=True. We GetTask first for the contextId and to be
        # honest about state (already-terminal → return its text; gone → say so).
        if resume_task_id:
            send_params["message"]["taskId"] = resume_task_id

        # Conversation continuity (#3360): when the caller passed a ``conversation_key``
        # — the room does, keyed to the thread — re-send the ``contextId`` this peer
        # assigned the LAST time this conversation addressed it, so the peer answers into
        # an ongoing conversation instead of a brand-new one on every address. Nothing is
        # sent until the peer has minted one for us, so a peer that assigns no contextId
        # sees the byte-identical request it always did. A parked task's own contextId
        # OVERRIDES this below: that is the HITL chain answering one specific task, and
        # its context is the one the peer is waiting on.
        #
        # The credential joins the name+url in the map's key (see ``conversations``):
        # rotating a row's token IN PLACE must not hand the new principal the conversation
        # the old one was having.
        credential = _continuity_credential(d)
        room_context = conversations.remembered(d.conversation_key, d.name, d.url, credential)
        if room_context:
            send_params["message"]["contextId"] = room_context

        def _learn(envelope) -> None:
            """The other half of the map: note the context this exchange ran in, for the
            next address on this conversation. Read off the peer's OWN envelope — never
            assumed to be what we sent, because the peer assigns it (and on a first
            address there was nothing to send). Empty ⇒ nothing remembered, so the next
            address sends nothing either. A RESUME is excluded: it answers one parked task
            in the context that task parked in, which says nothing about the context this
            conversation continues in.

            Called ONLY where this dispatch is about to return an ANSWER. A remembered
            context has to name a conversation that is idle and whose last exchange is on
            this side's thread; every other way out of the wire block below is ``_drop()``
            instead.

            ``session_id`` is the chat session this dispatch originated from (the room set it
            via ``DelegateRegistry.dispatch``), stored BESIDE the resolved key so a delete
            that knows only the session can forget this context later (#3362). ``""`` when the
            caller didn't know it — the entry is still keyed and remembered, just unreachable
            by the route's session-scoped cleanup."""
            if not resume_task_id:
                conversations.remember(
                    d.conversation_key,
                    d.name,
                    d.url,
                    _extract_context_id(envelope),
                    credential,
                    session_id=d.origin_session_id,
                )

        def _drop() -> None:
            """The exchange ended leaving the peer holding something this side does not
            have — a park it cannot be sent a resume for, or a turn still running past the
            deadline we quit on. Drop the pointer so the next address opens a clean context
            (the pre-#3360 wire) instead of walking into it; ``conversations.forget_one``
            carries the full argument. Not on a RESUME, which is not the room's pointer to
            drop."""
            if not resume_task_id:
                conversations.forget_one(d.conversation_key, d.name, d.url, credential)

        async def _rpc_tracked(client, method, params):
            """``_rpc`` plus the one transport failure that invalidates continuity: a READ
            timeout means the peer accepted the message and is still working on it, so the
            conversation we remember has moved on without us (KIND_TIMEOUT, set in
            ``_rpc``). Unreachable / HTTP / JSON-RPC failures are NOT that — the peer never
            took the work, so the pointer still describes its side correctly and the room's
            next address belongs in the same conversation as the last answered one."""
            try:
                return await _rpc(client, method, params)
            except DelegateError as exc:
                if getattr(exc, "kind", "") == KIND_TIMEOUT:
                    _drop()
                raise

        # A *synchronous* peer — one that ignores ``returnImmediately`` above and answers
        # SendMessage INLINE, holding the connection open for the whole delegated turn before
        # returning the final Message — needs the initial SendMessage READ to be allowed to
        # run as long as the task legitimately might (poll_timeout), NOT the flat 60s that
        # hard-failed every member turn >60s (#1778: hub→member delegation silently fell back
        # on any non-trivial turn). Connect stays short so an unreachable peer still fails
        # fast; the same client serves the GetTask poll loop, which returns quickly regardless.
        # An explicit ``timeout`` still overrides.
        read_budget = send_timeout if send_timeout is not None else poll_timeout
        # Wall-clock start of the wire round-trips, used to flag a suspiciously short reply
        # relative to how long the dispatch took (#3085) and to anchor the per-call cap below.
        t0 = time.monotonic()
        # An explicit per-call ``timeout`` is the caller's "max seconds to wait for the reply",
        # and it OVERRIDES the configured bound for this call (``delegate_to(timeout=…)``) —
        # which is how a caller runs a known-long job. Against a peer that answers inline it
        # was the one held read's budget; against one that hands the task back at once it
        # has to be the POLL's bound instead, and a wall clock in both directions: it stops
        # a task that keeps progressing at N, and it also outlasts a quiet stretch longer
        # than ``poll_timeout_s`` (one long tool call streams nothing between its start and
        # end frames). Without one, the no-progress ``poll_timeout`` is the bound.
        hard_deadline = (started if started is not None else t0) + send_timeout if send_timeout is not None else None
        # A resume_task_id naming a task that is still WORKING collects it instead of resuming.
        collecting = False
        async with httpx.AsyncClient(timeout=httpx.Timeout(read_budget, connect=10.0)) as client:
            if resume_task_id:
                parked = await _rpc_tracked(client, "GetTask", {"id": resume_task_id})
                ptask = parked.get("task", parked) or {}
                pstate = (ptask.get("status") or {}).get("state")
                if _is_terminal(pstate):
                    # Collected here, so a room collection of the same task stands down (#3775).
                    conversations.forget_pending_task(d.name, d.url, str(resume_task_id), credential)
                    done_text = _extract_text(parked)
                    return (
                        f"(task {resume_task_id} had already finished — state {pstate}; nothing to resume)"
                        + (f"\n\n{done_text}" if done_text else "")
                    )
                if not _is_input_required(pstate) and not pstate:
                    raise DelegateError(
                        f"delegate {d.name!r}: task {resume_task_id} is in an unknown state, not parked "
                        "for input — it can't be resumed with an answer."
                    )
                if not _is_input_required(pstate):
                    # Still WORKING (or SUBMITTED): this is a COLLECTION of a task an earlier
                    # call stopped waiting on (#3775), not an answer to a park. Send nothing —
                    # a SendMessage would hand the peer the "answer" as new input on a task
                    # still busy — and just wait on the SAME task with GetTask, under this
                    # call's bounds, through the ordinary poll / classification below.
                    collecting = True
                elif ptask.get("contextId"):
                    # Wins over any remembered room context (set above): a resume answers
                    # THIS parked task, and the peer resumes it only under the context it
                    # parked in. Sending the room's context here would open a new task in
                    # a different conversation and leave the park waiting forever.
                    send_params["message"]["contextId"] = ptask["contextId"]
            if collecting:
                result = parked
            else:
                result = await _rpc_tracked(client, "SendMessage", send_params)
            task = result.get("task", result) or {}
            task_id = task.get("id")
            state = (task.get("status") or {}).get("state")
            # Answer eligibility is decided by STATE, not by "is there text" (#3362). A
            # synchronous peer answers SendMessage INLINE, and its result can be a terminal
            # COMPLETED task (the answer), a FAILED task (a diagnostic), an inline
            # INPUT_REQUIRED park (its question — which _extract_text must NOT hand back as
            # the answer, losing the task id and the resume protocol with it, caught live
            # 2026-08-20), or a still-WORKING task to poll. A terminal / parked / bare
            # result skips the poll loop below (its guard already stops on those) and is
            # resolved by the classification block AFTER it; only a non-terminal task polls.
            progress_fingerprint = _a2a_progress_fingerprint(result)
            deadline = time.monotonic() + poll_timeout
            poll_interval = 1.0
            if live is not None and task_id and not _is_terminal(state) and not _is_input_required(state):
                # Follow the task's own SSE stream for the card (an observer only — this
                # poll still decides the answer; see a2a_progress).
                live.start(d.url, headers or {}, str(task_id))

            def _within_bounds() -> bool:
                now = time.monotonic()
                return now < hard_deadline if hard_deadline is not None else now < deadline

            while task_id and not _is_terminal(state) and not _is_input_required(state) and _within_bounds():
                # Never sleep past the caller's explicit timeout — the interval grows to 5s.
                nap = poll_interval if hard_deadline is None else min(poll_interval, hard_deadline - time.monotonic())
                if live is not None:
                    # Same nap, but the stream seeing the task settle wakes it at once.
                    await live.wait_settled(max(nap, 0.0))
                else:
                    await asyncio.sleep(max(nap, 0.0))
                # Back off toward 5s. Every GetTask makes a protoAgent peer load and parse the
                # task's whole stored history — ``historyLength`` trims only the reply — on
                # the event loop its turn is running on, so a long turn must not be polled
                # at 1s for its whole length.
                poll_interval = min(poll_interval * 1.5, 5.0)
                # A2A 1.0 GetTaskRequest is {tenant, id, history_length} — `id`, not
                # the v0.3 legacy `name` (proto: a2a.types.a2a_pb2.GetTaskRequest).
                # The old {"name": …} shape only ever worked against 0.3 peers; a 1.0
                # peer rejects/ignores it, so the poll loop could never converge for
                # an async-style peer (latent: protoAgent peers answer SendMessage
                # inline, so this path rarely ran).
                #
                # ``historyLength: 0``: nothing below reads a task's history — state, the
                # status message and artifacts are the whole of what the adapter acts on —
                # while a long protoAgent turn appends to it on every streamed status update,
                # so without this each 1s poll re-ships the entire turn so far.
                result = await _rpc_tracked(client, "GetTask", {"id": task_id, "historyLength": 0})
                task = result.get("task", result) or {}
                observed_task_id = task.get("id")
                state = (task.get("status") or {}).get("state")
                next_fingerprint = _a2a_progress_fingerprint(result)
                if (not observed_task_id or observed_task_id == task_id) and next_fingerprint != progress_fingerprint:
                    progress_fingerprint = next_fingerprint
                    deadline = time.monotonic() + poll_timeout
            if collecting and (_is_terminal(state) or _is_input_required(state)):
                # The lead collected this task itself, so a room collection still polling it
                # must not deliver the same outcome a second time (#3775).
                conversations.forget_pending_task(d.name, d.url, str(resume_task_id), credential)
            if _is_input_required(state):
                # The peer parked on an input interrupt. The HITL delegation chain
                # (operator decision, 2026-08-20): the QUESTION comes back to the
                # CALLING agent as a normal tool result with a resume handle — the
                # caller answers it (delegate_to(..., resume_task_id=…)); a caller
                # that can't answer escalates the same way to ITS caller (its own
                # ask_human parks ITS task, which bubbles another hop), so a chain
                # x→y→z ends at whichever console has a human. Never poll a park to
                # the deadline, and never bury the question in an error.
                #
                # And the ROOM's continuity ends here (#3360). A park leaves the peer's
                # thread holding a pending interrupt, and only the LEAD can answer it
                # (``delegate_to(..., resume_task_id=…)``, which bypasses the room). A room
                # address re-sent into that context is not a resume: a protoAgent peer
                # queues it as steering and re-yields the SAME interrupt, so the room would
                # get the identical question back once per address, forever, each one
                # parking another task. Dropping the pointer restores the pre-#3360
                # outcome — the next address opens a clean context and is answered.
                _drop()
                question = _extract_text(result) or ""
                if not task_id:
                    raise DelegateError(
                        f"delegate {d.name!r} asked for input but returned no task id to resume"
                        + (f": {str(question)[:300]}" if question else "")
                    )
                return _park_message(d.name, task_id, question)
            # STATE decides whether this result is an ANSWER, a diagnostic, or nothing for
            # the room — never "is there text" (#3362). ``classify_answer`` keeps that
            # decision apart from ``_is_terminal`` (the poll-stop predicate): a terminal
            # COMPLETED task and a genuine bare Message are answers; FAILED/CANCELED/REJECTED
            # is a diagnostic; a still-working or stateless task is neither.
            verdict = classify_answer(result)
            if verdict.answerable:
                text = _extract_text(result)
                if text:
                    # Learn continuity only on an ANSWER the caller will actually receive
                    # (#3360/#3362). For task envelopes, ``answerable`` means COMPLETED; a
                    # WORKING / FAILED / parked / malformed task still cannot pin the room.
                    # A genuine bare Message is also an answer by compatibility, and may
                    # carry the peer's contextId directly, so it must teach the next address
                    # too.
                    _learn(result)
                    # Bill the peer's own cost-v1 telemetry to this turn before returning
                    # (#3016) — the terminal artifact carries it on both the inline and the
                    # polled path; a bare Message has none, so this is a no-op there.
                    await _bill_peer_usage(result, d.name)
                    _warn_if_suspiciously_short(d.name, text, time.monotonic() - t0)
                    return text
                # A COMPLETED (or bare) terminus we could read NO answer text out of: an
                # exchange the peer has and this thread does not (#3360/#3362). Drop the
                # pointer, bill the terminal telemetry once if it rode the result, and raise
                # a state-bearing error rather than returning empty.
                _drop()
                await _bill_peer_usage(result, d.name)
                raise DelegateError(f"delegate {d.name!r} returned no text (state={state})")
            if verdict.failed:
                # A FAILED / CANCELED / REJECTED task is the peer's DIAGNOSTIC, never an
                # answer (#3362) — its status message is the error, not the reply. Drop
                # continuity (the peer moved the conversation somewhere this side has no
                # answer in), bill the terminal telemetry once if present, and surface the
                # normalized state plus a bounded slice of the diagnostic.
                _drop()
                await _bill_peer_usage(result, d.name)
                diag = " ".join((_extract_text(result) or "").split())[:_A2A_ERROR_DETAIL_LIMIT]
                raise DelegateError(
                    f"delegate {d.name!r} {state_name(state)} its task without an answer (state={state})"
                    + (f": {diag}" if diag else "")
                )
            # PENDING: a non-terminal task we stopped polling (deadline), or a task envelope
            # with no usable state. Neither is an answer for the room, so neither may pin
            # this conversation to the peer's context (#3360): "still running" leaves the
            # peer mid-turn in it — the room records the address as FAILED and drops the
            # member, so whatever the peer writes next is history this side never sees, and a
            # protoAgent peer would make the next address queue behind that same turn. Drop
            # to the pre-#3360 wire: a fresh context next time.
            _drop()
            if task_id and not _is_terminal(state):
                # The last thing the peer told us about where it is — carried back to the
                # caller so a deadline is never a bare failure (#3700).
                last_status_text = _status_message_text(result)
                # Retain the TASK for collection while continuity stays dropped (#3360b).
                # The peer took the work and is still doing it; the room will not address
                # this member again (``room_rounds._dropped``), but ``late.collect`` can poll
                # this one task with GetTask and bring its answer back when it settles. A
                # separate slot from the context, deliberately: restoring the contextId would
                # queue the next address behind the very turn we just gave up on. No-op
                # without a conversation key; never for a resume, which is the lead's.
                #
                # Whether it actually registered (a real conversation key, not a resume) is
                # also the one thing the deadline message must not get wrong: only then is a
                # collection left running for ``late.collect`` to deliver on its own (#3700).
                left_pending_for_room = bool(d.conversation_key) and not resume_task_id
                if collecting and not left_pending_for_room:
                    # A collection that ran out of time again: a room collection may still be
                    # polling this same task, and then the answer DOES arrive on its own.
                    left_pending_for_room = conversations.pending_task_held(
                        d.name, d.url, str(resume_task_id), credential
                    )
                if not resume_task_id:
                    conversations.remember_pending(
                        d.conversation_key,
                        d.name,
                        d.url,
                        str(task_id),
                        context_id=str(task.get("contextId") or ""),
                        credential=credential,
                        session_id=d.origin_session_id,
                    )
                # A background (detached) delegation carries no conversation key, so the room's
                # ``late.collect`` never runs for it — yet its caller isn't holding a turn open
                # either, so it can afford to keep waiting. Reuse the same read-only GetTask poll
                # (``late.collect_task``) here, up to the same bounded window, and deliver the
                # peer's REAL reply as the job result if it finishes rather than losing it to a
                # FAILED job with no task id to resume (#3700).
                #
                # ONLY when the no-progress ``poll_timeout`` tripped (``hard_deadline is None``):
                # an explicit ``delegate_to(timeout=N)`` is the caller's HARD cap on how long to
                # wait, and extending past it — by up to ``_COLLECT_MAX_S`` — would silently
                # blow that cap. When the caller set N, the deadline stands at N.
                if _DETACHED_DELEGATION.get() and hard_deadline is None:
                    from . import late

                    outcome, late_text, ext_state, ext_status = await late.collect_task(d, str(task_id))
                    if outcome in (late.ANSWERED, late.PARKED) and late_text:
                        return late_text
                    if outcome == late.FAILED and _is_terminal(ext_state):
                        # The task actually settled as a failure while we waited — surface the
                        # peer's own diagnostic, not a "still running" note.
                        raise DelegateError(f"delegate {d.name!r}: {late_text}")
                    # Still unfinished after the extra window, which this path has now used up —
                    # nothing polls the task after this, so the message must NOT promise
                    # auto-delivery. Fall through carrying the freshest state/status observed.
                    state = ext_state or state
                    last_status_text = ext_status or last_status_text
                # The deadline stands — but the result names the peer task id, the last state
                # and status message, and how to resume/collect the work, and never says to
                # retry (#3700). ``poll_timeout_s`` remains per-delegate configurable; the text
                # says so. ``auto_delivered`` is the room case only: a detached delegation has
                # exhausted its own poll above and left no conversation handle to collect into.
                if hard_deadline is not None and time.monotonic() >= hard_deadline:
                    raise DelegateError(
                        _still_running_message(
                            d,
                            str(task_id),
                            state,
                            last_status_text,
                            poll_timeout=poll_timeout,
                            send_timeout=send_timeout,
                            auto_delivered=left_pending_for_room,
                        ),
                        kind=KIND_STILL_RUNNING,
                    )
                raise DelegateError(
                    _still_running_message(
                        d,
                        str(task_id),
                        state,
                        last_status_text,
                        poll_timeout=poll_timeout,
                        auto_delivered=left_pending_for_room,
                    ),
                    kind=KIND_STILL_RUNNING,
                )
            raise DelegateError(f"delegate {d.name!r} returned no text (state={state})")

    async def get_task(self, d: Delegate, task_id: str, *, timeout: float = 30.0) -> dict | None:
        """ONE read-only ``GetTask`` for a task this side stopped waiting on (#3360b).

        The whole of what collecting a late answer may send: there is no ``SendMessage``
        here, so no argument, state or peer reply can turn a collection into a dispatch.
        Returns the JSON-RPC ``result`` (the task envelope), or ``None`` when the peer no
        longer knows the task — a restart or its retention expiring, which is normal rather
        than an error. Raises ``DelegateError`` on a transport or protocol failure, so a
        collector can retry it. Asks for no history, like the dispatch poll.
        """
        import httpx

        from security import policy

        blocked = policy.check_url(d.url)
        if blocked:
            raise DelegateError(blocked.replace("destination", f"delegate {d.name!r}", 1))
        body = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "GetTask",
            "params": {"id": str(task_id), "historyLength": 0},
        }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0)) as client:
                r = await client.post(d.url, json=body, headers=_a2a_headers(d))
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise DelegateError(
                f"delegate {d.name!r} unreachable at {d.url} ({type(exc).__name__})", kind=KIND_UNREACHABLE
            ) from exc
        except httpx.HTTPError as exc:
            raise DelegateError(f"delegate {d.name!r} transport error: {str(exc)[:160]}") from exc
        if r.status_code >= 400:
            raise DelegateError(f"delegate {d.name!r} HTTP {r.status_code}: {r.text[:200]}")
        # A malformed reply is a protocol failure the collector retries — never read as a
        # settled task (an empty ``{}`` would classify as a bare Message, i.e. "finished").
        try:
            data = r.json()
        except ValueError as exc:
            raise DelegateError(f"delegate {d.name!r} sent a GetTask reply that is not JSON") from exc
        if not isinstance(data, dict):
            raise DelegateError(f"delegate {d.name!r} sent a malformed GetTask reply")
        error = data.get("error")
        if error:
            if isinstance(error, dict) and error.get("code") == _A2A_TASK_NOT_FOUND:
                return None
            raise DelegateError(_a2a_error_detail(d, error))
        result = data.get("result")
        if not isinstance(result, dict) or not result:
            raise DelegateError(f"delegate {d.name!r} sent a GetTask reply with no task")
        return result

    async def probe(self, d: Delegate) -> dict:
        import httpx

        from security import policy

        origin = d.url.split("/a2a")[0].rstrip("/") if "/a2a" in d.url else d.url.rstrip("/")
        card = f"{origin}/.well-known/agent-card.json"
        blocked = policy.check_url(card)
        if blocked:
            return {"ok": False, "error": blocked}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r, ms = await _timed(client.get(card))
            if r.status_code >= 400:
                return {"ok": False, "latency_ms": ms, "error": f"HTTP {r.status_code}"}
            body = r.json() or {}
            name = body.get("name", "")
            # Capture the peer's advertised A2A protocol version(s) so a caller (and
            # dispatch's pre-check) can fail fast on a version mismatch. ``version`` is
            # the peer's APP version (distinct); ``protocol_version`` is the primary
            # advertised protocol, "" when the card is silent (older peers).
            advertised = _advertised_a2a_versions(body)
            pv = advertised[0] if advertised else ""
            detail = f"agent-card OK ({name})" + (f", A2A {pv}" if pv else "")
            from .a2a_progress import peer_streams

            return {
                "ok": True,
                "latency_ms": ms,
                "protocol_version": pv,
                "supported_versions": advertised,
                "version": body.get("version", ""),
                # Whether the peer serves A2A SSE streams — what the live delegation view
                # (#3979) follows a task over. False for a silent card.
                "streaming": peer_streams(body),
                "detail": detail,
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)[:200]}
