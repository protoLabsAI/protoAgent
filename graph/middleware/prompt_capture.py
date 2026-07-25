"""PromptCaptureMiddleware — snapshot the EXACT system prompt per model call (#2243).

Sits directly after ``PromptCacheMiddleware`` in ``_build_middleware`` — the one
seam where the final ``request.system_message`` exists (TraceContext only
touches ``extra_body``; ToolDeferral only trims ``tools``; nothing downstream
mutates the prompt). The cache boundary already marks the stable/dynamic split:
``blocks[0]`` is the stable ``build_system_prompt`` blob (hash-deduped by the
store), ``blocks[1]`` the volatile ``state["context"]`` tail. On the
non-Anthropic path PromptCache appends that tail as plain text instead — this
recovers the split by matching the exact appended suffix.

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
    when the request carries no system message (nothing to capture)."""
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
        ctx = (getattr(request, "state", None) or {}).get("context") or ""
        if ctx:
            # The exact string PromptCache appended on the no-blocks path.
            suffix = f"\n\n# Context\n\n{ctx}"
            if content.endswith(suffix):
                return content[: -len(suffix)], suffix
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
    def __init__(self, *, retention_days: int = 30, stable_sections: list | None = None):
        super().__init__()
        self._retention_days = int(retention_days)
        # Labeled section boundaries of the stable prefix (#2243 P2) —
        # [{label, chars}], computed at graph build from the SAME parts list the
        # prompt is joined from, persisted once per blob hash. None = unsegmented
        # (a caller that built its prompt without parts).
        self._stable_sections = list(stable_sections) if stable_sections else None

    def _store(self):
        # Lazy — the store's path resolution must not run at graph build
        # (injection-log precedent: env identity finalizes late). The singleton
        # is shared with the read routes; retention applies in-write, so
        # stamping it here keeps the config knob authoritative.
        from observability.prompt_snapshots import prompt_snapshots

        store = prompt_snapshots()
        store.retention_days = self._retention_days
        return store

    def _capture(self, request, response) -> None:
        try:
            # Incognito threads leave NO durable trail (ADR 0069 D3b) — a
            # persisted prompt snapshot would be one. Skip entirely.
            if (getattr(request, "state", None) or {}).get("incognito"):
                return
            split = _split_system(request)
            if split is None:
                return
            stable, context_tail = split

            from graph.middleware.request_context import current_request_metadata
            from observability import tracing

            # Dynamic-tail sections ride the state beside `context` (written as a
            # pair by KnowledgeMiddleware) — but a lingering pair from an earlier
            # call only applies when THIS request still carries a tail.
            context_sections = None
            if context_tail:
                context_sections = (getattr(request, "state", None) or {}).get("context_sections")

            self._store().record(
                task_id=str(current_request_metadata().get("a2a.task_id") or ""),
                session_id=tracing.current_session_id() or "",
                trace_id=tracing.current_trace_id() or "",
                stable_text=stable,
                context_text=context_tail,
                model=_model_name(request),
                stable_sections=self._stable_sections,
                context_sections=context_sections,
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
