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


def current_edge_id() -> str | None:
    """The open edge the calling task is dispatching inside, or None."""
    return _CURRENT.get()


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
    returns the edge id (the key ``delegation.progress`` / ``.finished`` carry)."""
    edge_id = uuid.uuid4().hex[:16]
    try:
        try:
            from graph.middleware.redaction import redact

            what = str(redact(what or ""))
        except Exception:  # noqa: BLE001 — fail closed on the free text
            what = ""
        edge = {
            "id": edge_id,
            "parent_id": _CURRENT.get() or "",
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
            stale = next(iter(_LIVE))
            _forget(stale)
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
        return store.settle(
            task_id, outcome=outcome, duration_ms=duration_ms, error=error, cost_usd=cost_usd
        )
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

    The yielded handle carries fields the caller only learns *during* the dispatch:

        with ledger.dispatch(to_kind="acp", to_name="protoCoder") as edge:
            reply = await adapter.dispatch(...)
            edge.cost_usd = adapter.last_usage_cost()
    """
    import asyncio
    from types import SimpleNamespace

    started = time.monotonic()
    # A SimpleNamespace, not a class body: a class body does not close over the enclosing
    # function's locals, so `task_id = task_id` inside one raises NameError rather than
    # seeding the default from the argument.
    edge = SimpleNamespace(cost_usd=None, task_id=task_id, to_instance=to_instance)
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
    token = _CURRENT.set(edge.id)
    try:
        yield edge
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except Exception as exc:  # noqa: BLE001 — re-raised below; we only observe it
        outcome, error = "failed", str(exc) or type(exc).__name__
        raise
    finally:
        _CURRENT.reset(token)
        elapsed_ms = int((time.monotonic() - started) * 1000)
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
        )
