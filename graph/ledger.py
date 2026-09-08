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
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager

log = logging.getLogger("protoagent.ledger")


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
    try:
        yield edge
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except Exception as exc:  # noqa: BLE001 — re-raised below; we only observe it
        outcome, error = "failed", str(exc) or type(exc).__name__
        raise
    finally:
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
            duration_ms=int((time.monotonic() - started) * 1000),
            cost_usd=edge.cost_usd,
            origin=origin,
        )
