"""Announce a runtime toolset change to the agent, once (#2640).

The delta itself is recorded in :mod:`graph.tool_delta` at graph-build time; this is the
half that gets it in front of the model. Unconditional — it must NOT ride the knowledge
middleware, which is switchable (``middleware.knowledge: false``) and would silently take
capability-awareness with it.

**Why it composes instead of assigning.** ``context`` is a plain ``str`` in the state
schema with no reducer, so two writers clobber. Every ``before_model`` hook is its own
graph node, chained in order, and LangGraph applies each node's updates before the next
runs — so this middleware, registered AFTER KnowledgeMiddleware, sees whatever knowledge
just wrote and prepends to it. Registration order is therefore load-bearing, not
cosmetic; ``_build_middleware`` says so at the call site.

**Why ``context`` and not the system prompt.** ``PromptCacheMiddleware`` treats the
system message as the stable, cached prefix and ``state["context"]`` as the volatile
tail. A one-shot note appended to the system prompt would bust the cache for that call
and risk being baked into a cached prefix — the opposite of one-shot. The volatile tail
is exactly where a single-turn notice belongs.

**Why not a message.** Appending a ``SystemMessage`` to ``messages`` would be additive
and collision-free, but it persists in thread history and mid-thread system messages are
handled inconsistently across providers.
"""

from __future__ import annotations

import logging

from langchain.agents.middleware import AgentMiddleware

from graph.tool_delta import format_delta, take_pending_delta

log = logging.getLogger(__name__)

_LABEL = "Toolset changed"


def _compose(state) -> dict | None:
    """Prepend the one-shot notice to whatever context is already staged.

    First in the block on purpose: it's an instruction to re-check a conclusion, and it
    is worthless after the model has reasoned past it."""
    delta = take_pending_delta()
    if delta is None:
        return None  # the overwhelmingly common case — no allocation, no injection
    note = format_delta(delta)
    if not note:
        return None
    existing = (state or {}).get("context") or ""
    sections = list((state or {}).get("context_sections") or [])
    # Both keys move together, mirroring KnowledgeMiddleware's contract — sections that
    # describe one context paired with another is how the viewer's budget breakdown lies.
    return {
        "context": f"{note}\n\n{existing}" if existing else note,
        "context_sections": [{"label": _LABEL, "chars": len(note)}, *sections],
    }


class ToolDeltaMiddleware(AgentMiddleware):
    """Inject a one-shot notice when the bound toolset changed since the last turn."""

    def before_model(self, state, runtime) -> dict | None:  # type: ignore[override]
        return _compose(state)

    async def abefore_model(self, state, runtime) -> dict | None:  # type: ignore[override]
        return _compose(state)
