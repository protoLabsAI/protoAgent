"""ModelOverrideMiddleware — per-turn model + reasoning-effort selection (per chat tab).

The graph is compiled once with a single lead model, but the console lets each
chat tab pick its own model AND its reasoning effort (the /effort command). Both
ride on the turn as ``state["model"]`` / ``state["reasoning_effort"]`` (stamped by
the chat layer from the request metadata); this middleware reads them at the
``wrap_model_call`` boundary and swaps ``request.model`` to a client built via
``create_llm(config, model_name=…, reasoning_effort=…)`` and cached per
(model, effort). Unset → the configured default, unchanged.

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
    """Swap the turn's model to the tab-selected model + reasoning effort."""

    def __init__(self, config):
        super().__init__()
        self._config = config
        self._cache: dict[tuple[str, str], object] = {}  # (model_name, effort) → ChatOpenAI

    def _llm_for(self, want: str, effort: str):
        key = (want, effort)
        llm = self._cache.get(key)
        if llm is None:
            from graph.llm import create_llm

            llm = create_llm(
                self._config,
                model_name=want or None,
                reasoning_effort=effort or None,
            )
            self._cache[key] = llm
        return llm

    def _override(self, request):
        state = getattr(request, "state", None) or {}
        want = (state.get("model") or "").strip()
        effort = (state.get("reasoning_effort") or "").strip()
        if not want and not effort:
            return request  # nothing selected — use the compiled default
        cur = _model_name_of(getattr(request, "model", None))
        # With no per-tab model the override still targets the current model — but only
        # when an effort is set (otherwise there's nothing to change). Same model + no
        # effort is a no-op.
        target = want or cur
        if not effort and target == cur:
            return request
        try:
            return request.override(model=self._llm_for(target, effort))
        except Exception:  # noqa: BLE001 — never break a turn over model selection
            log.exception("[model-override] could not switch to %r (effort=%r); using default", target, effort)
            return request

    def wrap_model_call(self, request, handler):
        return handler(self._override(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._override(request))
