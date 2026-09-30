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

The note is carried in the RUN's state (a private ``UntrackedValue`` channel,
:data:`TOOL_DELTA_NOTE_KEY`), not on this instance: one compiled graph serves every
concurrent turn, so an instance attribute would hand one turn's notice to whichever
turn's model calls ran next (and drop it from the turn that took it). An untracked
channel is never checkpointed, so ADR 0108 D2 holds.
"""

from __future__ import annotations

import logging
from typing import Annotated, NotRequired

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.agents.middleware.types import PrivateStateAttr
from langgraph.channels.untracked_value import UntrackedValue

from graph.context_frame import context_frame_message
from graph.tool_delta import format_delta, take_pending_delta

log = logging.getLogger(__name__)

# Run-scoped channel carrying this turn's one-shot toolset notice (a str).
TOOL_DELTA_NOTE_KEY = "protoagent_tool_delta_note"


class ToolDeltaState(AgentState):
    """Private, never-checkpointed, one-run channel for the toolset notice."""

    protoagent_tool_delta_note: NotRequired[Annotated[str | None, UntrackedValue, PrivateStateAttr]]


def _note_of(request) -> str:
    try:
        return str((getattr(request, "state", None) or {}).get(TOOL_DELTA_NOTE_KEY) or "")
    except AttributeError:  # state not a mapping
        return ""


class ToolDeltaMiddleware(AgentMiddleware):
    """Inject a one-shot notice when the bound toolset changed since the last turn.

    ADR 0108 D2 (#3188): the notice is composed in ``before_agent`` (once per
    turn) and delivered ephemerally via ``wrap_model_call`` so it never enters
    the checkpointer. It rides the run's own state (turn-scoped), never this
    shared instance.
    """

    state_schema = ToolDeltaState

    def before_agent(self, state, runtime) -> dict | None:  # type: ignore[override]
        delta = take_pending_delta()
        if delta is None:
            return None
        note = format_delta(delta)
        return {TOOL_DELTA_NOTE_KEY: note} if note else None

    async def abefore_agent(self, state, runtime) -> dict | None:  # type: ignore[override]
        return self.before_agent(state, runtime)

    @staticmethod
    def _with_note(request):
        note = _note_of(request)
        if not note:
            return request
        from graph.context_frame import stash_projected_context

        msgs = list(getattr(request, "messages", None) or [])
        msgs.append(context_frame_message(note))
        stash_projected_context(note)
        return request.override(messages=msgs)

    def wrap_model_call(self, request, handler):
        return handler(self._with_note(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._with_note(request))
