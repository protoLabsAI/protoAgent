"""ModelOverrideMiddleware — per-turn model selection (per chat tab).

The graph is compiled once with a single lead model, but the console lets each
chat tab pick its own model. The chosen model rides on the turn as
``state["model"]`` (stamped by the chat layer from the request); this middleware
reads it at the ``wrap_model_call`` boundary and swaps ``request.model`` to a
client for that model, built via ``create_llm(config, model_name=…)`` and cached
per model. Unset / unknown → the configured default, unchanged.

Added OUTERMOST among the wrap_model_call middleware so the actual (overridden)
model is what PromptCacheMiddleware sees when it decides caching.
"""

from __future__ import annotations

import logging

from langchain.agents.middleware import AgentMiddleware

log = logging.getLogger(__name__)


def _model_name_of(model) -> str:
    return getattr(model, "model_name", None) or getattr(model, "model", "") or ""


class ModelOverrideMiddleware(AgentMiddleware):
    """Swap the turn's model to the tab-selected one (``state["model"]``)."""

    def __init__(self, config):
        super().__init__()
        self._config = config
        self._cache: dict[str, object] = {}  # model_name → ChatOpenAI

    def _llm_for(self, want: str):
        llm = self._cache.get(want)
        if llm is None:
            from graph.llm import create_llm
            llm = create_llm(self._config, model_name=want)
            self._cache[want] = llm
        return llm

    def _override(self, request):
        want = ((getattr(request, "state", None) or {}).get("model") or "").strip()
        if not want or want == _model_name_of(getattr(request, "model", None)):
            return request  # unset or already on it — no-op
        try:
            return request.override(model=self._llm_for(want))
        except Exception:  # noqa: BLE001 — never break a turn over model selection
            log.exception("[model-override] could not switch to %r; using default", want)
            return request

    def wrap_model_call(self, request, handler):
        return handler(self._override(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._override(request))
