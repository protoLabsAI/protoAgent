"""PromptCacheMiddleware — Anthropic prompt caching + knowledge-context delivery.

Two coupled jobs, both at the `wrap_model_call` boundary (the only place that
sees the final ModelRequest):

1. **Deliver the volatile context** that `KnowledgeMiddleware.before_model`
   writes to ``state["context"]`` (retrieved knowledge, learned skills, hot
   memory). ``create_agent`` builds a *static* system prompt and does **not**
   read a ``context`` state key — so without this hook that context never
   reaches the model. We append it to the system message **after** the stable
   prefix.

2. **Cache the stable prefix.** For Anthropic-family models we set
   ``cache_control`` on the stable system-prompt block (the big, turn-stable
   prefix: persona + tool guidance). The volatile context block sits *after*
   that breakpoint, so it's delivered to the model but never invalidates the
   cached prefix — the cache-ordering discipline every reference agent uses.

Caching is gated (model must look Anthropic, or ``force=True``) so it's a
safe no-op on non-Anthropic gateways; context **delivery happens regardless**.
"""

from __future__ import annotations

import logging
import re

from langchain.agents.middleware import AgentMiddleware

log = logging.getLogger(__name__)

_ANTHROPIC_RE = re.compile(r"(claude|anthropic|sonnet|opus|haiku)", re.IGNORECASE)


def _message_text(msg) -> str:
    """Flatten a message's content to text (handles str or content-block list)."""
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return str(content)


class PromptCacheMiddleware(AgentMiddleware):
    def __init__(self, *, enabled: bool = True, ttl: str = "5m", force: bool = False):
        super().__init__()
        self._enabled = enabled
        self._ttl = ttl
        self._force = force

    def _model_name(self, request) -> str:
        m = getattr(request, "model", None)
        return getattr(m, "model_name", None) or getattr(m, "model", "") or ""

    def _should_cache(self, request) -> bool:
        if not self._enabled:
            return False
        return self._force or bool(_ANTHROPIC_RE.search(self._model_name(request)))

    def _cache_control(self) -> dict:
        cc = {"type": "ephemeral"}
        if self._ttl and self._ttl != "5m":
            cc["ttl"] = self._ttl  # e.g. "1h" — persistent tier
        return cc

    def _transform(self, request):
        sysmsg = getattr(request, "system_message", None)
        if sysmsg is None:
            return request
        ctx = (getattr(request, "state", None) or {}).get("context")
        cache = self._should_cache(request)
        if not ctx and not cache:
            return request  # nothing to do — safe no-op

        stable = _message_text(sysmsg)
        if cache:
            # Block list: stable prefix (cached) + volatile context (uncached).
            blocks = [{"type": "text", "text": stable, "cache_control": self._cache_control()}]
            if ctx:
                blocks.append({"type": "text", "text": f"\n\n# Context\n\n{ctx}"})
            new_sys = sysmsg.model_copy(update={"content": blocks})
        else:
            # No caching (non-Anthropic / disabled): deliver context as plain
            # appended text — universally safe across providers.
            new_sys = sysmsg.model_copy(update={"content": f"{stable}\n\n# Context\n\n{ctx}"})
        return request.override(system_message=new_sys)

    def wrap_model_call(self, request, handler):
        return handler(self._transform(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._transform(request))
