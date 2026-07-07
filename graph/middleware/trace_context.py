"""TraceContextMiddleware — join gateway LLM generations to the active Langfuse trace.

The LiteLLM gateway runs its own Langfuse success/failure callback in the SAME
Langfuse project, so every model call already produces a generation — but in a
SEPARATE trace with no link back to the agent turn that made it. LiteLLM's
Langfuse integration honors request ``metadata`` keys for exactly this:

- ``existing_trace_id``       → the generation lands in that trace (verified in
  litellm 1.83.10 ``integrations/langfuse/langfuse.py`` — popped from clean
  metadata and used as the trace id)
- ``parent_observation_id``   → nests it under that observation
- ``generation_name``         → names the generation

The proxy reads ``metadata`` from the request body (``add_litellm_data_to_request``
merges it into litellm_params for any key; only ``user_api_key_*``-prefixed keys,
``tags``, and ``_pipeline_managed_guardrails`` are stripped), so an
OpenAI-compatible client delivers it via ``extra_body``.

Trace ids are PER-TURN (and the current span is per-CALL), so a static
``extra_body`` on the compiled model can't carry them. This middleware sits at
the ``wrap_model_call`` boundary — the only seam that sees every model call,
including per-tab overridden models — and stamps a fresh trace context onto a
cheap ``model_copy`` of the request's model right before the call.

No-op (one function call returning None) when tracing is disabled or no trace
is active, and for models without an ``extra_body`` slot (e.g. ACP aux models).
Never raises — a tracing failure must not break a turn.
"""

from __future__ import annotations

import logging
import os

from langchain.agents.middleware import AgentMiddleware

log = logging.getLogger(__name__)


class TraceContextMiddleware(AgentMiddleware):
    """Stamp the current Langfuse trace context onto each gateway LLM call."""

    def _with_trace(self, request):
        try:
            from observability import tracing

            ctx = tracing.current_trace_context()
            if not ctx:
                return request
            model = getattr(request, "model", None)
            # Only OpenAI-compatible clients carry extra_body; anything else
            # (ACP aux models, fakes) is left untouched.
            if model is None or not hasattr(model, "extra_body"):
                return request
            meta = {
                "existing_trace_id": ctx["trace_id"],
                "generation_name": f"{os.environ.get('AGENT_NAME', 'protoagent')}-turn",
            }
            if ctx.get("span_id"):
                meta["parent_observation_id"] = ctx["span_id"]
            extra_body = dict(getattr(model, "extra_body", None) or {})
            merged_meta = dict(extra_body.get("metadata") or {})
            merged_meta.update(meta)
            extra_body["metadata"] = merged_meta
            return request.override(model=model.model_copy(update={"extra_body": extra_body}))
        except Exception:  # noqa: BLE001 — tracing must never break a model call
            log.debug("[trace-context] could not stamp trace context", exc_info=True)
            return request

    def wrap_model_call(self, request, handler):
        return handler(self._with_trace(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._with_trace(request))
