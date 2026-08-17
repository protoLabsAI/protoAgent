"""Announce a runtime toolset change to the agent, once (#2640).

The delta itself is recorded in :mod:`graph.tool_delta` at graph-build time; this is the
half that gets it in front of the model. Unconditional — it must NOT ride the knowledge
middleware, which is switchable (``middleware.knowledge: false``) and would silently take
capability-awareness with it. (It briefly did ride it, as a stated coupling; #2776 ended
that — this middleware owns the notice end-to-end again.)

**Why a message frame, not the ``context`` channel** (ADR 0101 D2, #2776). The old
delivery staged the note on ``state["context"]`` for PromptCacheMiddleware to append as a
second system block. That worked only because KnowledgeMiddleware rewrote the channel
every model call — once knowledge moved to per-turn frame delivery, a one-shot note left
on a last-write-wins channel would be re-sent forever. And a per-call system block is
exactly what ADR 0101 D2 removes: it sits between the cached stable prefix and the
history, invalidating history caching. A tagged ``HumanMessage`` in the turn's input
frame is one-shot by construction (it's appended once, to the log), and mid-thread
HUMAN messages are handled consistently by every provider — the earlier objection here
was to mid-thread *system* messages, which this is not.

Delivered at ``before_agent`` (turn entry): a mid-turn change can't reach the running
graph anyway (hot reload rebuilds the graph; the old one keeps executing), so the next
turn was always the earliest a delta could land.
"""

from __future__ import annotations

import logging

from langchain.agents.middleware import AgentMiddleware

from graph.context_frame import context_frame_message
from graph.tool_delta import format_delta, take_pending_delta

log = logging.getLogger(__name__)


def _compose(state) -> dict | None:
    """Append the one-shot notice as its own tagged frame message."""
    delta = take_pending_delta()
    if delta is None:
        return None  # the overwhelmingly common case — no allocation, no injection
    note = format_delta(delta)
    if not note:
        return None
    return {"messages": [context_frame_message(note)]}


class ToolDeltaMiddleware(AgentMiddleware):
    """Inject a one-shot notice when the bound toolset changed since the last turn."""

    def before_agent(self, state, runtime) -> dict | None:  # type: ignore[override]
        return _compose(state)

    async def abefore_agent(self, state, runtime) -> dict | None:  # type: ignore[override]
        return _compose(state)
