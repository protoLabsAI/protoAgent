"""ToolDeferralMiddleware — progressive tool disclosure (ADR 0005 #3).

When the agent is wired with many tools (notably a large MCP catalog), every
bound tool's name + description + JSON schema is sent to the model on *every*
turn. Past ~10–15 tools that burns context and degrades tool selection ("tool
pollution"). This middleware withholds most tool *schemas* from the model while
keeping every tool *callable*:

- ``create_agent`` still receives the full tool list, so the ToolNode can
  execute any tool — deferral never breaks execution.
- At the ``wrap_model_call`` boundary (the only place that sees the final
  ModelRequest), we trim ``request.tools`` to a small **base** set plus whatever
  the agent has *loaded* by calling ``search_tools``.

"Loaded" is read straight from the message history: ``search_tools`` returns its
matches as a backticked bulleted list, so the names it surfaced are recoverable
from its ToolMessages — no extra state channel, and it survives summarization as
long as those messages do. Once surfaced, a tool stays available for the rest of
the thread.

OFF by default (the middleware is only added when ``tools.deferred.enabled``);
when off, the full tool set reaches the model exactly as before.

A FENCED pass is never deferred (#4472). A background subagent runs this graph under a
``subagent_fence`` naming its declared allowlist, and ``SubagentFenceMiddleware`` (outer)
already trims the request to it. Deferring on top hid every fenced tool outside the base
``keep`` set, and ``search_tools`` (the only way to load one) is outside the fence, so
a background ``campaign_producer`` ran with ``current_time``/``load_skill`` and none of
its plugin tools. The fence is the subagent's tool budget, exactly as an in-graph
``task`` run binds its allowlist on a graph with no deferral at all.
"""

from __future__ import annotations

import logging
import re

from langchain.agents.middleware import AgentMiddleware

from graph.middleware.subagent_fence import FENCE_DENY_ALL, _bound_tool_name, _state_fence
from tools.lg_tools import SEARCH_TOOLS_NAME

log = logging.getLogger(__name__)

# Names inside backticks on the search_tools result lines.
_BACKTICKED = re.compile(r"`([^`]+)`")


def _message_text(msg) -> str:
    """Flatten a message's content to text (str or content-block list)."""
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return str(content)


def _tool_name(t) -> str | None:
    """Name of a request tool entry — a BaseTool or a provider tool-spec dict."""
    name = getattr(t, "name", None)
    if name:
        return name
    if isinstance(t, dict):
        return t.get("name") or (t.get("function") or {}).get("name")
    return None


def activated_tool_names(messages) -> set[str]:
    """Tool names the agent has surfaced via ``search_tools`` so far.

    Scans ToolMessages emitted by ``search_tools`` for the backticked names in
    its rendered result. Tolerant: a non-search ToolMessage or odd content
    contributes nothing.
    """
    names: set[str] = set()
    for m in messages or []:
        if getattr(m, "type", None) != "tool":
            continue
        if getattr(m, "name", None) != SEARCH_TOOLS_NAME:
            continue
        names.update(_BACKTICKED.findall(_message_text(m)))
    return names


def _fence_sizes_request(request) -> bool:
    """Whether a subagent fence, not deferral, sizes this request's tools.

    True when the pass carries a fence with real names and at least one bound tool is
    inside it: the case ``fence_tools`` trims to. A deny-all fence, or one naming no
    bound tool, leaves the request untrimmed, so deferral still applies there."""
    allowed = {t for t in _state_fence(request) if t != FENCE_DENY_ALL}
    if not allowed:
        return False
    return any(_bound_tool_name(t) in allowed for t in getattr(request, "tools", None) or ())


class ToolDeferralMiddleware(AgentMiddleware):
    """Trim the per-call tool set to base + agent-loaded tools."""

    def __init__(self, keep_names):
        super().__init__()
        self._keep = set(keep_names)

    def _transform(self, request):
        tools = getattr(request, "tools", None)
        if not tools:
            return request
        if _fence_sizes_request(request):
            return request  # the fence already bound the subagent's allowlist (#4472)
        state = getattr(request, "state", None) or {}
        messages = state.get("messages") if isinstance(state, dict) else getattr(state, "messages", None)
        allowed = self._keep | activated_tool_names(messages)
        # Keep base + loaded tools; an unidentifiable entry (no name) is kept.
        kept = [t for t in tools if (_tool_name(t) or "") in allowed or _tool_name(t) is None]
        if not kept or len(kept) == len(tools):
            return request  # nothing deferred this turn — safe no-op
        deferred = len(tools) - len(kept)
        log.debug("[tool-deferral] exposing %d/%d tools this turn", len(kept), len(tools))
        # Prove the lever: count withheld tool schemas (ADR 0006 Slice 4b).
        try:
            from observability import metrics

            metrics.record_tools_deferred(deferred)
        except Exception:  # noqa: BLE001 — telemetry must never break a model call
            pass
        return request.override(tools=kept)

    def wrap_model_call(self, request, handler):
        return handler(self._transform(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._transform(request))
