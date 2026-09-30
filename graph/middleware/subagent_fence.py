"""SubagentFenceMiddleware — per-turn tool fence for detached subagent runs (#1639).

A detached background job runs the FULL lead graph (ADR 0050's self-POST substrate),
so the per-subagent tool scoping the in-graph ``task`` path enforces never applied —
the subagent's ``tools`` allowlist was role guidance only. The fire path now stamps
the resolved allowlist on the turn's state (``subagent_fence`` — carried A2A message
metadata → request metadata → state, the same per-turn channel ``model``/``incognito``
ride), and this gate blocks any tool call outside it with the enforcement-style
``ToolMessage`` block, so the model reads the denial and adapts. A turn without the
state key is untouched — ordinary chat turns pay one dict lookup.

Fence rules (one place, so the drivers and the steering fold agree):

* **Narrowest wins.** When two fences meet on one pass — a fenced RESUME of a parked
  turn that was itself fenced, or a fenced message held behind a parked interrupt that
  folds into the resumed pass — the pass runs under their INTERSECTION
  (:func:`intersect_fences`). An unfenced side adds no restriction. An empty
  intersection is :data:`FENCE_DENY_ALL` (every tool blocked), never ``[]`` — an empty
  list means "no fence" to this middleware, so it would fail OPEN.
* **The parked call finishes on its own resume.** The tool call that parked the turn
  (``ask_human``, an approval-gated tool) runs to completion on the pass that resumes it
  even when the resumer's fence excludes it — only that call (the LangGraph task that
  holds the resume value), only on that pass; every other call in the pass stays fenced.
  Otherwise a fenced resumer silently drops the operator's answer.
"""

from __future__ import annotations

import logging

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

logger = logging.getLogger(__name__)

# A fence that allows nothing: the intersection of two disjoint fences. Not a tool name
# (tool names are identifiers), so ``name in fence`` is False for every call.
FENCE_DENY_ALL = "<no tools>"

try:  # the per-task scratchpad LangGraph threads through a node's config
    from langgraph._internal._constants import CONFIG_KEY_SCRATCHPAD as _SCRATCHPAD_KEY
except Exception:  # noqa: BLE001 — private module; the key's value is stable
    _SCRATCHPAD_KEY = "__pregel_scratchpad"


def intersect_fences(current, incoming) -> list[str]:
    """The fence a pass runs under when ``current`` (already on the state) meets
    ``incoming`` (a resumer's fence, a held message's fence): narrowest wins. A falsy
    side is "no fence" and restricts nothing; two disjoint fences yield
    ``[FENCE_DENY_ALL]`` — blocked, not unfenced. Order follows ``incoming``."""
    cur = [str(t) for t in (current or [])]
    inc = [str(t) for t in (incoming or [])]
    if not cur:
        return inc
    if not inc:
        return cur
    keep = set(cur)
    both = [t for t in inc if t in keep and t != FENCE_DENY_ALL]
    return both or [FENCE_DENY_ALL]


def is_resumed_parked_call(request) -> bool:
    """Is this tool call the one that PARKED the turn, now being resumed?

    The tool node runs each call as its own LangGraph task (``Send`` per call), and a
    ``Command(resume={interrupt_id: value})`` binds the value to exactly the task whose
    ``interrupt()`` it answers — it reaches the task's scratchpad as ``resume``. So a
    non-empty task-level ``resume`` identifies the parked call on the pass that resumes
    it, and nothing else: a sibling call, a later call, a later pass all see ``[]``.
    The global (id-less) resume value is deliberately NOT honoured — it isn't bound to
    one task. Any unreadable shape → False (fenced)."""
    try:
        runtime = getattr(request, "runtime", None)
        config = getattr(runtime, "config", None) or {}
        scratchpad = (config.get("configurable") or {}).get(_SCRATCHPAD_KEY)
        return bool(getattr(scratchpad, "resume", None))
    except Exception:  # noqa: BLE001 — fail closed
        return False


class SubagentFenceMiddleware(AgentMiddleware):
    """Block tool calls outside the turn's stamped subagent allowlist."""

    def _deny_reason(self, request) -> str | None:
        state = getattr(request, "state", None) or {}
        fence = state.get("subagent_fence")
        if not fence:
            return None
        name = request.tool_call.get("name", "")
        if name in fence:
            return None
        if is_resumed_parked_call(request):
            # The call that parked this turn completes on its own resume (see module doc).
            logger.info("[subagent-fence] allowed the resumed parked call %s outside the fence", name)
            return None
        allowed = [t for t in fence if t != FENCE_DENY_ALL]
        if not allowed:
            return f"tool '{name}' is blocked: this turn allows no tools — answer without calling any."
        # Turn-neutral wording: the fence also rides peer-channel turns (#2972),
        # not only background subagent runs — the model reads this to adapt.
        return (
            f"tool '{name}' is outside this turn's tool allowlist "
            f"({', '.join(sorted(allowed))}) — work within the allowed tools."
        )

    def _blocked(self, request, reason: str) -> ToolMessage:
        logger.info("[subagent-fence] blocked %s: %s", request.tool_call.get("name", "?"), reason)
        return ToolMessage(
            content=f"Blocked by policy: {reason}",
            tool_call_id=request.tool_call.get("id", ""),
            status="error",  # render as a failure card, matching the enforcement gate
        )

    def wrap_tool_call(self, request, handler):
        reason = self._deny_reason(request)
        if reason:
            return self._blocked(request, reason)
        return handler(request)

    async def awrap_tool_call(self, request, handler):
        reason = self._deny_reason(request)
        if reason:
            return self._blocked(request, reason)
        return await handler(request)
