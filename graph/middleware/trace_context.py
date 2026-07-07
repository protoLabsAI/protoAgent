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
import time

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

    def _emit_fleet_generation(self, response, duration_ms: int) -> None:
        """Emit a lightweight generation into the AGENT's own Langfuse project.

        The gateway logs the full-detail generation into ITS project (via the
        ``_with_trace`` metadata above). When the agent runs in a *different*
        project — a dedicated fleet project — that generation is absent from
        the agent's own trace, leaving a hole where its model call should be.
        This lands a model + usage + cost node (no prompt/completion payload)
        in the agent's project so its trace is whole. Best-effort; never raises.
        """
        try:
            from observability import pricing, tracing

            if not tracing.is_enabled():
                return
            # Per the wrap_model_call contract the return is a ModelResponse
            # (``.result``: list[BaseMessage]) or an AIMessage directly.
            msg = None
            result = getattr(response, "result", None)
            if result:
                from langchain_core.messages import AIMessage

                msg = next(
                    (m for m in reversed(result) if isinstance(m, AIMessage)),
                    result[-1],
                )
            elif hasattr(response, "usage_metadata") or hasattr(response, "response_metadata"):
                msg = response
            if msg is None:
                return
            usage = getattr(msg, "usage_metadata", None) or {}
            model = (getattr(msg, "response_metadata", None) or {}).get("model_name", "") or ""
            cost = pricing.cost_usd(model, usage) if usage else 0.0
            name = f"{os.environ.get('AGENT_NAME', 'protoagent')}-turn"
            tracing.trace_generation(
                name=name, model=model, usage=usage, cost_usd=cost, duration_ms=duration_ms
            )
        except Exception:  # noqa: BLE001 — tracing must never break a model call
            log.debug("[trace-context] could not emit fleet generation", exc_info=True)

    def wrap_model_call(self, request, handler):
        t0 = time.monotonic()
        response = handler(self._with_trace(request))
        self._emit_fleet_generation(response, int((time.monotonic() - t0) * 1000))
        return response

    async def awrap_model_call(self, request, handler):
        t0 = time.monotonic()
        response = await handler(self._with_trace(request))
        self._emit_fleet_generation(response, int((time.monotonic() - t0) * 1000))
        return response
