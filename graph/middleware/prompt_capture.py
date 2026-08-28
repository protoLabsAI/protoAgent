"""PromptCaptureMiddleware — snapshot the EXACT system prompt per model call (#2243).

Sits directly after ``PromptCacheMiddleware`` in ``_build_middleware`` — the
seam where the final COMPOSED ``request.system_message`` exists. Provider-shape
transforms (ADR 0097: the Claude identity prepend, the Codex instructions move)
run INSIDE this wrap, so what the wire carries can differ from what composes
here — ``WirePromptCaptureMiddleware`` (innermost, #2527) stashes the effective
wire text and ``_capture`` records it beside the composed prompt whenever the
two diverge. A row's ``wire_text`` is NULL when delivery was faithful. The
cache boundary marks the stable system prefix; dynamic context is delivered
ephemerally via the message stream (ADR 0108 D2) and captured via the
projected-context stash from KnowledgeMiddleware.

Because ``wrap_model_call`` wraps the handler, the response is in-hook too, so
the call's real ``usage_metadata`` (input/output/cache tokens) is stored with
the prompt for free.

Correlation: ``task_id`` arrives via the request-context metadata (stamped by
the A2A executor as ``a2a.task_id`` — it is not part of agent state);
``session_id``/``trace_id`` come from the tracing contextvars. Rows without a
task id (non-A2A callers) fall back to the ``(session_id, trace_id)`` key.

Best-effort like the injection log: a capture failure debug-logs and never
touches the turn.
"""

from __future__ import annotations

import logging

from langchain.agents.middleware import AgentMiddleware

log = logging.getLogger(__name__)


def _model_name(request) -> str:
    m = getattr(request, "model", None)
    return getattr(m, "model_name", None) or getattr(m, "model", "") or ""


def _split_system(request) -> tuple[str, str] | None:
    """The final system prompt as ``(stable, context_tail)`` — verbatim, so
    ``stable + context_tail`` is byte-for-byte what the model received. None
    when the request carries no system message (nothing to capture).

    Since ADR 0108 D2 the dynamic context rides the message stream, not the
    system prompt, so the context tail is always empty on the string path.
    The block-list path (Anthropic) still splits at block boundaries.
    """
    sysmsg = getattr(request, "system_message", None)
    if sysmsg is None:
        return None
    content = getattr(sysmsg, "content", "")
    if isinstance(content, list):
        texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        if not texts:
            return None
        return texts[0], "".join(texts[1:])
    if isinstance(content, str):
        return content, ""
    return None


def _usage_from(response) -> dict:
    """Real token usage off the response's AIMessage (``stream_usage=True`` is
    already guaranteed by ``graph/llm.py``). Zeros when absent — a snapshot
    without usage is still a snapshot."""
    for msg in getattr(response, "result", None) or []:
        um = getattr(msg, "usage_metadata", None)
        if not isinstance(um, dict) or not um:
            continue
        details = um.get("input_token_details") or {}
        return {
            "input_tokens": int(um.get("input_tokens") or 0),
            "output_tokens": int(um.get("output_tokens") or 0),
            "cache_read_tokens": int(details.get("cache_read") or 0),
            "cache_creation_tokens": int(details.get("cache_creation") or 0),
        }
    return {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_creation_tokens": 0}


class PromptCaptureMiddleware(AgentMiddleware):
    def __init__(
        self,
        *,
        retention_days: int = 30,
        max_calls: int = 5000,
        stable_sections: list | None = None,
        parent_task_id: str = "",
        subagent_type: str = "",
    ):
        super().__init__()
        self._retention_days = int(retention_days)
        # The store's OTHER cap (#3019). It travels with retention_days because
        # the two are one policy: whichever bites first is the real window, and
        # at real turn volume that is this one. Defaults mirror the store's.
        self._max_calls = int(max_calls)
        # Labeled section boundaries of the stable prefix (#2243 P2) —
        # [{label, chars}], computed at graph build from the SAME parts list the
        # prompt is joined from, persisted once per blob hash. None = unsegmented
        # (a caller that built its prompt without parts).
        self._stable_sections = list(stable_sections) if stable_sections else None
        # Subagent identity (#2388 P3): a subagent build passes the delegating
        # tool-call id + its type; rows then nest under the parent turn instead
        # of claiming a task_id/session of their own. Main-loop builds leave both "".
        self._parent_task_id = parent_task_id or ""
        self._subagent_type = subagent_type or ""

    def _store(self):
        # Lazy — the store's path resolution must not run at graph build
        # (injection-log precedent: env identity finalizes late). The singleton
        # is shared with the read routes; retention applies in-write, so
        # stamping it here keeps the config knob authoritative.
        from observability.prompt_snapshots import prompt_snapshots

        store = prompt_snapshots()
        store.retention_days = self._retention_days
        store.max_calls = self._max_calls
        return store

    def _capture(self, request, response) -> None:
        try:
            # Incognito threads leave NO durable trail (ADR 0069 D3b) — a
            # persisted prompt snapshot would be one. Skip entirely.
            if (getattr(request, "state", None) or {}).get("incognito"):
                from graph.context_frame import pop_projected_context

                pop_projected_context()
                return
            split = _split_system(request)
            if split is None:
                from graph.context_frame import pop_projected_context

                pop_projected_context()
                return
            stable, context_tail = split

            # Wire-vs-composed honesty (#2527): the innermost observer stashed what
            # the call actually carried (post provider transforms). Record it only
            # on divergence — NULL = faithful; "" = NOTHING reached the wire (the
            # #2519 alarm case); text = a transform changed it (e.g. the Claude
            # identity prepend). None = no observer under this call (subagents).
            from graph.middleware.wire_capture import pop_wire_system

            wire = pop_wire_system()
            wire_text = wire if (wire is not None and wire != stable + context_tail) else None

            from graph.middleware.request_context import current_request_metadata
            from observability import tracing

            # ADR 0108 D2 (#3191): the projected context (memory, skills, working
            # state, tool-delta notices) is stashed by the inner middleware during
            # wrap_model_call and popped here.
            from graph.context_frame import pop_projected_context

            projected_text, projected_sections = pop_projected_context()
            context_sections = None

            self._store().record(
                task_id=("" if self._parent_task_id else str(current_request_metadata().get("a2a.task_id") or "")),
                session_id=tracing.current_session_id() or "",
                trace_id=tracing.current_trace_id() or "",
                parent_task_id=self._parent_task_id,
                subagent_type=self._subagent_type,
                stable_text=stable,
                context_text=context_tail,
                model=_model_name(request),
                stable_sections=self._stable_sections,
                context_sections=context_sections,
                wire_text=wire_text,
                projected_context=projected_text,
                projected_sections=projected_sections,
                **_usage_from(response),
            )
        except Exception:  # noqa: BLE001 — capture must never touch the turn
            log.debug("[prompt-capture] snapshot failed", exc_info=True)

    def wrap_model_call(self, request, handler):
        response = handler(request)
        self._capture(request, response)
        return response

    async def awrap_model_call(self, request, handler):
        response = await handler(request)
        self._capture(request, response)
        return response
