"""Collect the model usage of delegations that run OUTSIDE the lead graph (#3957).

A ``/<subagent>`` or ``/<workflow>`` chat command short-circuits the turn: the work runs in
``server.chat_dispatch``'s pre-turn chain, not in the lead graph, so neither turn driver's
usage accounting sees it — the streaming driver sums ``on_chat_model_end`` events from the
LEAD graph's event stream, and the non-streaming one a callback attached to the LEAD
graph's config. Their telemetry rows read "0 LLM calls, 0 tokens" on the configured
default model while the work ran — and billed — on the turn's model.

Every in-process delegation already funnels through one place that extracts per-call
usage rows (``graph.agent._run_subagent``, #2872 / #3565). This module lets a caller that
has no graph event stream to read them from bind a collector for a block: every
delegation that settles inside the block appends its rows here. A ``ContextVar``, so a
workflow's runner task (``asyncio.create_task`` copies the context) reaches the same list.

Bound ONLY around work that no other accounting sees: a ``task`` call inside the lead
graph is already billed through its custom ``usage`` events, so binding a collector
around a lead-graph run would count it twice.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager

_rows_ctx: contextvars.ContextVar[list[dict] | None] = contextvars.ContextVar(
    "protoagent_delegation_usage", default=None
)


@contextmanager
def collect() -> Iterator[list[dict]]:
    """Bind a fresh collector for the block; yields the list the rows land in."""
    rows: list[dict] = []
    token = _rows_ctx.set(rows)
    try:
        yield rows
    finally:
        try:
            _rows_ctx.reset(token)
        except ValueError:  # reset from another context (a generator closed elsewhere)
            _rows_ctx.set(None)


def note(rows: list[dict]) -> None:
    """Append a settled delegation's usage rows to the bound collector, if any. Never raises."""
    sink = _rows_ctx.get()
    if sink is None or not rows:
        return
    try:
        sink.extend(dict(r) for r in rows if isinstance(r, dict))
    except Exception:  # noqa: BLE001 — telemetry must never break a delegation
        pass
