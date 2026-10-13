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
* **A parked ANSWER completes on its own resume; a parked APPROVAL does not.** When the
  call that parked the turn is an answer-type HITL tool (``HITL_TOOL_NAMES`` —
  ``ask_human`` / ``request_user_input``), it runs to completion on the pass that resumes
  it even when the resumer's fence excludes it — only that call (the LangGraph task that
  holds the resume value), only on that pass; every other call stays fenced. Otherwise a
  fenced resumer silently drops the operator's answer. Any OTHER parked call is an
  approval-gated tool (``run_command``, ``delete_file``, …): a resumer whose fence
  excludes it cannot approve it — its resume is a DECLINE (the tool doesn't run; the
  model reads a declined result, not an error).
* **The model is only SHOWN the fence.** Blocking at call time alone left every bound
  tool's schema on every model call of a fenced run: a detached ``social_researcher``
  with a 6-tool allowlist carried all 120 of its lead's schemas (~42k prompt tokens,
  ~80% of each call's fixed floor), and spent rounds calling tools it could see but
  never use. ``wrap_model_call`` trims ``request.tools`` to the fence, the same way
  ``ToolDeferralMiddleware`` trims to its base set — the ToolNode still holds every
  tool, so execution is untouched. A deny-all fence, or a fence naming no bound tool,
  leaves the schemas as they were (a provider rejects a history with tool calls and no
  ``tools``); the call-time block still applies there.
"""

from __future__ import annotations

import logging

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from graph.fence_scope import fence_scope

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


def _state_fence(request) -> list:
    """The fence on the tool call's graph state (``[]`` when none)."""
    state = getattr(request, "state", None) or {}
    try:
        return list(state.get("subagent_fence") or [])
    except Exception:  # noqa: BLE001 — an unreadable state adds no scope
        return []


def _bound_tool_name(tool) -> str | None:
    """Name of a ``ModelRequest.tools`` entry — a BaseTool or a provider tool-spec dict."""
    name = getattr(tool, "name", None)
    if name:
        return str(name)
    if isinstance(tool, dict):
        return tool.get("name") or (tool.get("function") or {}).get("name")
    return None


def fence_tools(request):
    """``request`` with its bound tools trimmed to the turn's fence (or unchanged).

    Unfenced, deny-all, or no bound tool inside the fence → the request as-is. An entry
    whose name can't be read is kept (never drop what we can't identify)."""
    fence = _state_fence(request)
    allowed = {t for t in fence if t != FENCE_DENY_ALL}
    tools = getattr(request, "tools", None)
    if not allowed or not tools:
        return request
    kept = [t for t in tools if (name := _bound_tool_name(t)) is None or name in allowed]
    if len(kept) == len(tools) or not any(_bound_tool_name(t) in allowed for t in kept):
        return request
    logger.debug("[subagent-fence] binding %d/%d tools for this fenced call", len(kept), len(tools))
    return request.override(tools=kept)


def _answer_tools() -> frozenset[str]:
    """The answer-type HITL tools (their interrupt asks a question; the resume is an
    ANSWER, not an approval) — the registry the subagent HITL deny uses too."""
    from tools.lg_tools import HITL_TOOL_NAMES

    return HITL_TOOL_NAMES


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
        if is_resumed_parked_call(request) and name in _answer_tools():
            # A parked ANSWER completes on its own resume (see module doc).
            logger.info("[subagent-fence] allowed the resumed parked answer %s outside the fence", name)
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

    def _declined(self, request) -> ToolMessage | None:
        """A resumed parked APPROVAL outside the fence → the declined outcome, else None."""
        state = getattr(request, "state", None) or {}
        fence = state.get("subagent_fence")
        name = request.tool_call.get("name", "")
        if not fence or name in fence or name in _answer_tools() or not is_resumed_parked_call(request):
            return None
        logger.info("[subagent-fence] declined the parked approval of %s: the resumer's fence excludes it", name)
        # Not status="error": a decline is a normal outcome (as the gated tools return it).
        return ToolMessage(
            content=(
                f"Declined — not run: {name!r}. It was approved from a channel whose tool allowlist "
                "excludes this tool. Do not retry; wait for the operator's next instruction."
            ),
            tool_call_id=request.tool_call.get("id", ""),
        )

    def _blocked(self, request, reason: str) -> ToolMessage:
        logger.info("[subagent-fence] blocked %s: %s", request.tool_call.get("name", "?"), reason)
        return ToolMessage(
            content=f"Blocked by policy: {reason}",
            tool_call_id=request.tool_call.get("id", ""),
            status="error",  # render as a failure card, matching the enforcement gate
        )

    def wrap_model_call(self, request, handler):
        return handler(fence_tools(request))

    async def awrap_model_call(self, request, handler):
        return await handler(fence_tools(request))

    def wrap_tool_call(self, request, handler):
        declined = self._declined(request)
        if declined is not None:
            return declined
        reason = self._deny_reason(request)
        if reason:
            return self._blocked(request, reason)
        with fence_scope(_state_fence(request)):
            return handler(request)

    async def awrap_tool_call(self, request, handler):
        declined = self._declined(request)
        if declined is not None:
            return declined
        reason = self._deny_reason(request)
        if reason:
            return self._blocked(request, reason)
        # The tool body runs as code of THIS turn's fence: anything it leaves behind to
        # run later as its own turn (a background job's nudge, a scheduled resume, a
        # watch reaction, a goal's hooks) records it and stays fenced (graph/fence_scope).
        with fence_scope(_state_fence(request)):
            return await handler(request)
