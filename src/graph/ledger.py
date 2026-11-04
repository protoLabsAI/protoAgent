"""The single writer for delegation edges — who handed what work to whom.

Mirrors ``server/turn_telemetry.py::record_turn`` deliberately, including the rule that
makes it work: **a new dispatch surface routes through this function or its edge is never
recorded.** That seam exists because CLI coding-agent runs were structurally invisible to
turn telemetry for months (#3015) — dispatched outside any turn, so the one place that
measured turns could not see them.

It lives in ``graph/`` rather than ``server/`` so every producer can reach it without
crossing the import-layering contract: ``graph/agent.py`` calls it directly, and the
delegates plugin reaches it through ``graph.sdk`` (a plugin may never import ``server``).

**Every call is best-effort.** A ledger failure must never break a dispatch — the work
matters more than the record of it.

The three producers today, each an already-existing single funnel:

- ``graph/agent.py::_run_subagent`` — foreground ``task`` / ``task_batch`` /
  ``sdk.run_subagent``.
- ``plugins/delegates/registry.py::dispatch`` — every external delegate (a2a peers, acp
  coders, openai endpoints), and therefore ``delegate_to``, the coder ladder, and the
  board loop.
- ``background/manager.py::spawn`` — background subagent jobs, which do NOT pass through
  ``_run_subagent``: they are fired as a self-directed A2A turn, which is exactly the kind
  of side path that goes unmeasured when a writer is bolted onto one funnel only.

**It is also the live record of work in flight.** The same funnels that write an edge open
it here first: an in-memory registry of open edges, announced on the bus as
``delegation.started`` → ``delegation.progress`` (a coder's plan / current tool / narration,
from ``graph.delegate_progress``) → ``delegation.finished``. All three are live-only
(``retain=False``) — a replayed "started" for work that ended long ago is worse than none —
so a view opened mid-run catches up from ``live_edges()`` (``GET /api/ledger/live``)
instead. An edge opened inside another (a subagent that delegates to a coder) names it as
``parent_id``, so the chain of who-asked-whom survives into the view.

**And it is what makes a finished run rebuildable (#4726).** The edge id and its parent are
written to the durable row as ``edge_uuid`` / ``parent_uuid``, with a ``run_id`` naming the
root of the tree: the A2A task id of the turn the work started in, or, for work started
outside any turn (a board review, a scheduled workflow), the root edge's own id. Without
them the nesting existed only in the live registry and was gone the moment an edge closed.

A turn (or any other entry point) declares what it is with ``run_scope``; every edge opened
inside it inherits the session, the turn's task id as ``run_id``, and an optional parent
edge. A background job's turn arrives over HTTP, so it cannot inherit the spawning turn's
context; its executor looks the job's edge up with ``link_for_task`` and scopes itself
under it, which keeps the job's subagents in the run that spawned the job.
"""

from __future__ import annotations

import contextvars
import logging
import time
import uuid
from contextlib import contextmanager

log = logging.getLogger("protoagent.ledger")

#: Open edges, by edge id. Bounded: an edge whose close never arrives (a process that lost a
#: background job's settle) must not grow it forever — the oldest goes first.
_LIVE: dict[str, dict] = {}
_LIVE_MAX = 256
#: Edge id by the task id it was recorded with — a background job is settled by job id.
_LIVE_BY_TASK: dict[str, str] = {}
#: The open edge the current task is working inside, for nesting and for progress.
_CURRENT: contextvars.ContextVar[str | None] = contextvars.ContextVar("ledger_current_edge", default=None)
#: The run the current task belongs to (#4726): ``session_id``, ``run_id`` and an optional
#: ``parent_uuid``, set by ``run_scope`` at a turn or another entry point.
_SCOPE: contextvars.ContextVar[dict | None] = contextvars.ContextVar("ledger_run_scope", default=None)
#: What the caller ASKED, when the text a funnel dispatches wraps it (``describing``).
_DESCRIBING: contextvars.ContextVar[str | None] = contextvars.ContextVar("ledger_describing", default=None)
#: Bus payload caps — every frame is fanned out to every open console.
_WHAT_MAX = 240
_ERROR_MAX = 300


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _publish(topic: str, data: dict) -> None:
    """Live-only publish on the host bus; a no-op without a host (tests, tooling)."""
    try:
        from graph.plugins.host import HOST

        if HOST.publish is not None:
            HOST.publish(topic, data, retain=False)
    except Exception:  # noqa: BLE001 — the live view must never cost a dispatch
        log.debug("[ledger] publish %s failed", topic, exc_info=True)


@contextmanager
def describing(text: str):
    """Record ``text`` as the ``what`` of the next edge opened inside this block, instead of
    the prompt the funnel dispatches. For a caller that wraps the ask before it reaches the
    funnel (the room's catch-up preamble): the record should say what was asked."""
    token = _DESCRIBING.set(text)
    try:
        yield
    finally:
        _DESCRIBING.reset(token)


def current_edge_id() -> str | None:
    """The open edge the calling task is dispatching inside, or None."""
    return _CURRENT.get()


def begin_run_scope(*, session_id: str = "", run_id: str = "", parent_uuid: str = "") -> contextvars.Token:
    """Declare the run the calling task belongs to, for every edge opened inside it.

    An empty field inherits from an enclosing scope, so a workflow scoped inside a chat turn
    keeps the turn's session and run. Pair with ``end_run_scope``; ``run_scope`` is the
    context-manager form."""
    outer = _SCOPE.get() or {}
    scope = {
        "session_id": str(session_id or outer.get("session_id") or ""),
        "run_id": str(run_id or outer.get("run_id") or ""),
        "parent_uuid": str(parent_uuid or outer.get("parent_uuid") or ""),
    }
    return _SCOPE.set(scope)


def end_run_scope(token: contextvars.Token) -> None:
    """Undo ``begin_run_scope``. Safe when torn down in another context (an async generator
    closed by a different task): the contextvar resets itself when that context exits."""
    try:
        _SCOPE.reset(token)
    except (ValueError, RuntimeError):
        pass


@contextmanager
def run_scope(*, session_id: str = "", run_id: str = "", parent_uuid: str = ""):
    """Context-manager form of ``begin_run_scope`` / ``end_run_scope``."""
    token = begin_run_scope(session_id=session_id, run_id=run_id, parent_uuid=parent_uuid)
    try:
        yield
    finally:
        end_run_scope(token)


def current_run_id() -> str:
    """The run the calling task belongs to: the open edge's run, else the scope's, else ""."""
    edge = _LIVE.get(_CURRENT.get() or "")
    if edge and edge.get("run_id"):
        return str(edge["run_id"])
    return str((_SCOPE.get() or {}).get("run_id") or "")


def link_for_task(task_id: str) -> dict:
    """``{"edge_uuid", "run_id"}`` of the edge recorded under ``task_id`` (a background job
    id), from the live registry or, after a restart, the durable row. Empty when unknown.

    How a background job's own turn, which arrives over HTTP and so inherits nothing, finds
    the edge that spawned it."""
    if not task_id:
        return {}
    edge = _LIVE.get(_LIVE_BY_TASK.get(task_id) or "")
    if edge:
        return {"edge_uuid": edge["id"], "run_id": str(edge.get("run_id") or "")}
    store = _store()
    if store is None:
        return {}
    try:
        row = store.by_task(task_id)
    except Exception:  # noqa: BLE001 — a missing link must never break the turn
        log.debug("[ledger] link_for_task failed", exc_info=True)
        return {}
    if not row or not row.get("edge_uuid"):
        return {}
    return {"edge_uuid": str(row["edge_uuid"]), "run_id": str(row.get("run_id") or "")}


def open_edge(
    *,
    to_kind: str,
    to_name: str,
    to_instance: str = "",
    what: str = "",
    session_id: str = "",
    parent_task_id: str = "",
    task_id: str = "",
    origin: str = "",
) -> str:
    """Register a delegation as in flight and announce ``delegation.started``. Never raises;
    returns the edge id (the key ``delegation.progress`` / ``.finished`` carry).

    The parent is the edge the caller is dispatching inside, else the scope's parent. The
    run is the parent's run, else the scope's, else this edge's own id (it is a root). An
    empty ``session_id`` is taken from the scope, which is how a ``task`` call made inside a
    tool body, where the session isn't in reach, still records the turn's session."""
    edge_id = uuid.uuid4().hex[:16]
    try:
        scope = _SCOPE.get() or {}
        parent_id = _CURRENT.get() or scope.get("parent_uuid") or ""
        parent = _LIVE.get(parent_id) if parent_id else None
        run_id = str((parent or {}).get("run_id") or scope.get("run_id") or edge_id)
        session_id = session_id or (parent or {}).get("session_id") or scope.get("session_id") or ""
        try:
            from graph.middleware.redaction import redact

            what = str(redact(what or ""))
        except Exception:  # noqa: BLE001 — fail closed on the free text
            what = ""
        edge = {
            "id": edge_id,
            "parent_id": parent_id,
            "run_id": run_id,
            "from": _agent_name(),
            "to_kind": to_kind,
            "to_name": to_name,
            "to_instance": to_instance,
            "what": _clip(what, _WHAT_MAX),
            "session_id": session_id,
            "parent_task_id": parent_task_id,
            "task_id": task_id,
            "origin": origin,
            "started_at": time.time(),
        }
        while len(_LIVE) >= _LIVE_MAX:
            # Out of room: drop the oldest — and say so, or a view keeps drawing it running.
            close_edge(next(iter(_LIVE)), outcome="cancelled", error="no longer tracked (live registry full)")
        _LIVE[edge_id] = edge
        if task_id:
            _LIVE_BY_TASK[task_id] = edge_id
        _publish("delegation.started", dict(edge))
    except Exception:  # noqa: BLE001
        log.exception("[ledger] open_edge failed")
    return edge_id


def _forget(edge_id: str) -> dict | None:
    edge = _LIVE.pop(edge_id, None)
    if edge and edge.get("task_id") and _LIVE_BY_TASK.get(edge["task_id"]) == edge_id:
        _LIVE_BY_TASK.pop(edge["task_id"], None)
    return edge


def close_edge(
    edge_id: str,
    *,
    outcome: str,
    duration_ms: int | None = None,
    error: str = "",
    cost_usd: float | None = None,
) -> None:
    """Take an edge out of flight and announce ``delegation.finished``. Never raises; an
    unknown id (already closed, or opened before a restart) is a no-op."""
    try:
        edge = _forget(edge_id)
        if edge is None:
            return
        if duration_ms is None:
            duration_ms = int((time.time() - float(edge.get("started_at") or time.time())) * 1000)
        _publish(
            "delegation.finished",
            {
                "id": edge_id,
                "parent_id": edge.get("parent_id", ""),
                "run_id": edge.get("run_id", ""),
                "completion": edge.get("completion", ""),
                "nudges": int(edge.get("nudges") or 0),
                "from": edge.get("from", ""),
                "to_kind": edge.get("to_kind", ""),
                "to_name": edge.get("to_name", ""),
                "to_instance": edge.get("to_instance", ""),
                "outcome": outcome,
                "duration_ms": int(duration_ms),
                "cost_usd": cost_usd,
                "error": _clip(error, _ERROR_MAX),
            },
        )
    except Exception:  # noqa: BLE001
        log.exception("[ledger] close_edge failed")


def note_progress(edge_id: str, snapshot: dict) -> None:
    """Attach a delegate's latest progress snapshot (``graph.delegate_progress``, already
    bounded and throttled) to its open edge and announce ``delegation.progress``."""
    try:
        edge = _LIVE.get(edge_id)
        if edge is None:
            return
        progress = {k: v for k, v in snapshot.items() if k not in ("timeline", "id")}
        edge["progress"] = progress
        _publish("delegation.progress", {"id": edge_id, "to_name": edge.get("to_name", ""), "progress": progress})
    except Exception:  # noqa: BLE001
        log.debug("[ledger] note_progress failed", exc_info=True)


def edge_link(edge_id: str) -> dict:
    """The durable link fields of an open edge, for a producer that records its own row
    (``record_delegation``) instead of using ``dispatch``. Empty when the edge is unknown."""
    edge = _LIVE.get(edge_id or "")
    if not edge:
        return {}
    return {
        "edge_uuid": edge["id"],
        "parent_uuid": str(edge.get("parent_id") or ""),
        "run_id": str(edge.get("run_id") or ""),
        "session_id": str(edge.get("session_id") or ""),
    }


#: A subagent's completion-check verdict (#4726). ``complete``: its deliverable arrived
#: (``nudges`` > 0 means the guard had to ask for it). ``incomplete``: the nudges didn't
#: rescue it. ``partial``: it hard-stopped at max_turns. "" when no contract was checked.
COMPLETIONS = ("complete", "incomplete", "partial")


def note_completion(edge_id: str, verdict: str, *, nudges: int = 0) -> None:
    """Record a subagent's completion-check verdict and nudge count on its open edge.

    The edge's ``outcome`` stays what the dispatch did (``ok`` / ``failed`` /
    ``cancelled``); this says whether the work it returned was the deliverable. A lane that
    ran fine and came back without its findings is ``ok`` and ``incomplete``."""
    edge = _LIVE.get(edge_id or "")
    if edge is None:
        return
    edge["completion"] = verdict if verdict in COMPLETIONS else ""
    edge["nudges"] = max(0, int(nudges or 0))


def note_task_id(edge_id: str, task_id: str) -> None:
    """Attach the task id a delegate's work was recorded under, once the producer knows it
    (an ACP coder run's telemetry key), so the edge's row joins that ``turns`` row."""
    edge = _LIVE.get(edge_id or "")
    if edge is None or not task_id:
        return
    edge["task_id"] = str(task_id)
    _LIVE_BY_TASK[str(task_id)] = edge_id


def live_edges() -> list[dict]:
    """Every edge in flight right now, oldest first — the catch-up for a view that opens
    mid-run, since the bus frames are live-only."""
    return [dict(e) for e in list(_LIVE.values())]


def _store():
    """The instance's ledger store, or None when the host hasn't wired one.

    Resolved per call rather than captured: the store is created during boot, and a
    module-level handle taken at import time would pin None for the process lifetime.
    """
    try:
        from runtime.state import STATE

        return getattr(STATE, "ledger_store", None)
    except Exception:  # noqa: BLE001 — no host (tests, host-free tooling)
        return None


def _agent_name() -> str:
    try:
        from runtime.state import STATE

        cfg = getattr(STATE, "graph_config", None)
        return str(getattr(cfg, "identity_name", "") or "")
    except Exception:  # noqa: BLE001
        return ""


def record_delegation(
    *,
    to_kind: str,
    to_name: str,
    to_instance: str = "",
    what: str = "",
    session_id: str = "",
    parent_task_id: str = "",
    task_id: str = "",
    outcome: str = "ok",
    error: str = "",
    duration_ms: int = 0,
    cost_usd: float | None = None,
    origin: str = "",
    from_agent: str = "",
    edge_uuid: str = "",
    parent_uuid: str = "",
    run_id: str = "",
    completion: str = "",
    nudges: int = 0,
) -> int | None:
    """Record one delegation edge. Never raises.

    ``cost_usd=None`` means *unknown*, not free. Only some paths carry a real number, and
    a confident zero would make an unmeasured coder look free while silently understating
    every rollup built on the column.
    """
    store = _store()
    if store is None:
        return None
    try:
        return store.record(
            from_agent=from_agent or _agent_name(),
            to_kind=to_kind,
            to_name=to_name,
            to_instance=to_instance,
            what=what,
            session_id=session_id,
            parent_task_id=parent_task_id,
            task_id=task_id,
            outcome=outcome,
            error=error,
            duration_ms=duration_ms,
            cost_usd=cost_usd,
            origin=origin,
            edge_uuid=edge_uuid,
            parent_uuid=parent_uuid,
            run_id=run_id,
            completion=completion,
            nudges=nudges,
        )
    except Exception:  # noqa: BLE001 — the record must never break the dispatch
        log.exception("[ledger] record_delegation failed")
        return None


def settle_delegation(
    task_id: str,
    *,
    outcome: str,
    duration_ms: int | None = None,
    error: str = "",
    cost_usd: float | None = None,
) -> bool:
    """Close out an edge recorded at dispatch. Never raises.

    Detached work — a background subagent job — is recorded when it is handed off, so
    in-flight delegations are visible, and closed out here when it lands. Without this
    every background edge reads ``ok`` with a zero duration forever, which is a claim
    about an outcome nobody observed.
    """
    edge_id = _LIVE_BY_TASK.get(task_id)
    if edge_id:
        close_edge(edge_id, outcome=outcome, duration_ms=duration_ms, error=error, cost_usd=cost_usd)
    store = _store()
    if store is None:
        return False
    try:
        return store.settle(task_id, outcome=outcome, duration_ms=duration_ms, error=error, cost_usd=cost_usd)
    except Exception:  # noqa: BLE001 — settling must never break a job's completion
        log.exception("[ledger] settle_delegation failed")
        return False


@contextmanager
def dispatch(
    *,
    to_kind: str,
    to_name: str,
    to_instance: str = "",
    what: str = "",
    session_id: str = "",
    parent_task_id: str = "",
    task_id: str = "",
    origin: str = "",
):
    """Wrap a dispatch and record its edge with the outcome and wall clock.

    Use this at a funnel rather than two bare ``record_delegation`` calls: it guarantees
    an edge is written on **every** exit path, including the ones that are easy to forget.
    A dispatch that raises still happened and still cost something, so a ledger that only
    records successes answers "what did this fleet do" with a survivor-biased yes.

    ``CancelledError`` is recorded as ``cancelled``, never ``failed`` — an operator
    stopping a turn says nothing about the delegate, and collapsing the two would put a red
    mark on a healthy coder every time someone hit stop.

    The yielded handle carries fields the caller only learns *during* the dispatch
    (``cost_usd``, ``task_id``, ``to_instance``, and ``outcome`` / ``error`` for work that
    reports failure without raising):

        with ledger.dispatch(to_kind="acp", to_name="protoCoder") as edge:
            reply = await adapter.dispatch(...)
            edge.cost_usd = adapter.last_usage_cost()
    """
    import asyncio
    from types import SimpleNamespace

    described = _DESCRIBING.get()
    if described is not None:
        what = described
        _DESCRIBING.set(None)  # one edge — a nested dispatch describes its own ask
    started = time.monotonic()
    # A SimpleNamespace, not a class body: a class body does not close over the enclosing
    # function's locals, so `task_id = task_id` inside one raises NameError rather than
    # seeding the default from the argument.
    edge = SimpleNamespace(cost_usd=None, task_id=task_id, to_instance=to_instance, outcome=None, error="")
    outcome, error = "ok", ""
    edge.id = open_edge(
        to_kind=to_kind,
        to_name=to_name,
        to_instance=to_instance,
        what=what,
        session_id=session_id,
        parent_task_id=parent_task_id,
        task_id=task_id,
        origin=origin,
    )
    link = edge_link(edge.id)
    session_id = link.get("session_id") or session_id
    token = _CURRENT.set(edge.id)
    try:
        yield edge
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except Exception as exc:  # noqa: BLE001 — re-raised below; we only observe it
        outcome, error = "failed", str(exc) or type(exc).__name__
        raise
    else:
        # The block finished without raising, but the caller may know better: a workflow
        # whose run failed returns its envelope rather than raising (#4726).
        if edge.outcome in ("ok", "failed", "cancelled"):
            outcome = edge.outcome
        error = str(edge.error or "")
    finally:
        _CURRENT.reset(token)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        # A producer may have learned the delegate's task id mid-dispatch (note_task_id).
        live = _LIVE.get(edge.id) or {}
        late_task = str(live.get("task_id") or "")
        if late_task and not edge.task_id:
            edge.task_id = late_task
        completion, nudges = str(live.get("completion") or ""), int(live.get("nudges") or 0)
        close_edge(edge.id, outcome=outcome, duration_ms=elapsed_ms, error=error, cost_usd=edge.cost_usd)
        record_delegation(
            to_kind=to_kind,
            to_name=to_name,
            to_instance=edge.to_instance,
            what=what,
            session_id=session_id,
            parent_task_id=parent_task_id,
            task_id=edge.task_id,
            outcome=outcome,
            error=error,
            duration_ms=elapsed_ms,
            cost_usd=edge.cost_usd,
            origin=origin,
            edge_uuid=link.get("edge_uuid", ""),
            parent_uuid=link.get("parent_uuid", ""),
            run_id=link.get("run_id", ""),
            completion=completion,
            nudges=nudges,
        )
