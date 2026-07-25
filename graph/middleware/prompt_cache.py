"""PromptCacheMiddleware — Anthropic prompt caching + knowledge-context delivery.

Two coupled jobs, both at the `wrap_model_call` boundary (the only place that
sees the final ModelRequest):

1. **Deliver the volatile context** that `KnowledgeMiddleware.before_model`
   writes to ``state["context"]`` (retrieved knowledge, learned skills, hot
   memory). ``create_agent`` builds a *static* system prompt and does **not**
   read a ``context`` state key — so without this hook that context never
   reaches the model. We append it to the system message **after** the stable
   prefix.

2. **Cache the stable prefix.** We set ``cache_control`` on the stable
   system-prompt block (the big, turn-stable prefix: persona + tool guidance).
   The volatile context block sits *after* that breakpoint, so it's delivered
   to the model but never invalidates the cached prefix — the cache-ordering
   discipline every reference agent uses.

**Attempt-by-default, fail loud (#2255).** Caching used to be gated on an
Anthropic-looking model NAME — which silently disabled it for every gateway
alias (``protolabs/fast`` burned 1.5M uncached input tokens in one field turn).
Now ``enabled`` attaches ``cache_control`` for every model and the middleware
watches the outcome instead:

- a provider that REJECTS the blocks (error naming ``cache_control``) gets one
  retry without them, and that model falls back to plain delivery for the rest
  of the session (WARNING, once);
- a provider that silently IGNORES them (usage shows zero cache activity on
  consecutive calls with a cacheable-sized prefix) draws a WARNING naming the
  model, so "caching isn't working" is never invisible again;
- ``force=True`` keeps meaning "the operator knows best": always attach, never
  auto-fall back (a rejection propagates instead of degrading silently).

Context **delivery happens regardless** of any of this.
"""

from __future__ import annotations

import logging

from langchain.agents.middleware import AgentMiddleware

log = logging.getLogger(__name__)

# Anthropic's floor is ~1024 tokens per cache breakpoint — a stable prefix under
# that (~4 chars/token) can NEVER produce cache activity, so the zero-hit
# detector stays silent for small prompts instead of crying wolf.
MIN_CACHEABLE_CHARS = 4096

# Consecutive zero-cache-activity calls (with blocks attached and a cacheable
# prefix) before the once-per-model "caching isn't engaging" warning fires.
ZERO_HIT_WARN_AFTER = 3


def _message_text(msg) -> str:
    """Flatten a message's content to text (handles str or content-block list)."""
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return str(content)


class PromptCacheMiddleware(AgentMiddleware):
    def __init__(self, *, enabled: bool = True, ttl: str = "5m", force: bool = False):
        super().__init__()
        self._enabled = enabled
        self._ttl = ttl
        self._force = force
        # Models whose provider rejected cache_control this session — they fall
        # back to plain string delivery (never populated under force=True).
        self._blocks_disabled: set[str] = set()
        # Zero-cache-activity streaks + the once-per-model warning latch.
        self._zero_hit_streak: dict[str, int] = {}
        self._warned: set[str] = set()

    def _model_name(self, request) -> str:
        m = getattr(request, "model", None)
        return getattr(m, "model_name", None) or getattr(m, "model", "") or ""

    def _should_cache(self, request) -> bool:
        if not self._enabled:
            return False
        if self._force:
            return True
        return self._model_name(request) not in self._blocks_disabled

    def _cache_control(self) -> dict:
        cc = {"type": "ephemeral"}
        if self._ttl and self._ttl != "5m":
            cc["ttl"] = self._ttl  # e.g. "1h" — persistent tier
        return cc

    def _transform(self, request):
        """The (possibly rewritten) request plus whether cache blocks were attached."""
        sysmsg = getattr(request, "system_message", None)
        if sysmsg is None:
            return request, False
        ctx = (getattr(request, "state", None) or {}).get("context")
        cache = self._should_cache(request)
        if not ctx and not cache:
            return request, False  # nothing to do — safe no-op

        stable = _message_text(sysmsg)
        if cache:
            # Block list: stable prefix (cached) + volatile context (uncached).
            blocks = [{"type": "text", "text": stable, "cache_control": self._cache_control()}]
            if ctx:
                blocks.append({"type": "text", "text": f"\n\n# Context\n\n{ctx}"})
            new_sys = sysmsg.model_copy(update={"content": blocks})
        else:
            # No caching (disabled, or this model rejected blocks): deliver
            # context as plain appended text — universally safe.
            new_sys = sysmsg.model_copy(update={"content": f"{stable}\n\n# Context\n\n{ctx}"})
        return request.override(system_message=new_sys), cache

    # ── outcome watching (#2255) ─────────────────────────────────────────────

    @staticmethod
    def _is_blocks_rejection(exc: Exception) -> bool:
        """A provider error that names cache_control is the blocks being refused —
        precise on purpose: anything else propagates untouched."""
        return "cache_control" in str(exc)

    @staticmethod
    def _notify(text: str) -> None:
        """Mirror an operator-relevant warning into the console Activity feed
        (#2262) — best-effort, a no-op until the server binds the feed."""
        try:
            from activity import emit

            emit(text, origin="system", trigger="prompt-cache")
        except Exception:  # noqa: BLE001 — a notice must never break the turn
            log.debug("[prompt-cache] activity emit failed", exc_info=True)

    def _fall_back(self, request, exc: Exception):
        """Disable blocks for this model for the session and return the plain
        (string-delivery) retry request."""
        model = self._model_name(request)
        self._blocks_disabled.add(model)
        log.warning(
            "[prompt-cache] %s rejected cache_control (%s) — retrying without cache "
            "blocks and delivering plain text for this model for the rest of the session",
            model,
            str(exc)[:200],
        )
        self._notify(
            f"Prompt caching disabled for {model} this session — the provider rejected "
            f"cache_control blocks (the call was retried without them)."
        )
        retry, _cached = self._transform(request)
        return retry

    def _observe(self, request, response) -> None:
        """Blocks were attached — check the response's usage actually shows cache
        activity, and warn (once per model) when it repeatedly doesn't. Best-effort:
        a shape we don't recognize just counts nothing."""
        try:
            stable_chars = len(_message_text(getattr(request, "system_message", None)))
            if stable_chars < MIN_CACHEABLE_CHARS:
                return  # below the provider floor — zero activity is EXPECTED
            usage = None
            for msg in getattr(response, "result", None) or []:
                um = getattr(msg, "usage_metadata", None)
                if isinstance(um, dict) and um:
                    usage = um
                    break
            if usage is None:
                return  # no usage reported — can't judge this call
            details = usage.get("input_token_details") or {}
            model = self._model_name(request)
            if int(details.get("cache_read") or 0) or int(details.get("cache_creation") or 0):
                self._zero_hit_streak[model] = 0
                return
            streak = self._zero_hit_streak.get(model, 0) + 1
            self._zero_hit_streak[model] = streak
            if streak >= ZERO_HIT_WARN_AFTER and model not in self._warned:
                self._warned.add(model)
                log.warning(
                    "[prompt-cache] %s: %d consecutive calls with cache_control attached and "
                    "ZERO cache activity — the provider behind this model is likely ignoring "
                    "prompt caching, so you are paying full input price on every call. Set "
                    "prompt_cache.enabled: false to stop attaching blocks, or check the "
                    "gateway's model mapping.",
                    model,
                    streak,
                )
                self._notify(
                    f"Prompt caching is not engaging for {model} — {streak} consecutive "
                    f"calls show zero cache activity, so every call bills full input price. "
                    f"Check the gateway's model mapping, or set prompt_cache.enabled: false."
                )
        except Exception:  # noqa: BLE001 — watching must never touch the turn
            log.debug("[prompt-cache] usage observation failed", exc_info=True)

    # ── hooks ────────────────────────────────────────────────────────────────

    def wrap_model_call(self, request, handler):
        transformed, cached = self._transform(request)
        try:
            response = handler(transformed)
        except Exception as exc:
            if cached and not self._force and self._is_blocks_rejection(exc):
                return handler(self._fall_back(request, exc))
            raise
        if cached:
            self._observe(request, response)
        return response

    async def awrap_model_call(self, request, handler):
        transformed, cached = self._transform(request)
        try:
            response = await handler(transformed)
        except Exception as exc:
            if cached and not self._force and self._is_blocks_rejection(exc):
                return await handler(self._fall_back(request, exc))
            raise
        if cached:
            self._observe(request, response)
        return response
