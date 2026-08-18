"""PromptCacheMiddleware — Anthropic prompt caching + knowledge-context delivery.

Two coupled jobs, both at the `wrap_model_call` boundary (the only place that
sees the final ModelRequest):

1. **Deliver anything staged on the legacy ``context`` channel.** Since #2776
   (ADR 0101 D2) the dynamic context layer rides the message stream as a
   per-turn frame, so this channel is empty on a stock build — the delivery
   path stays for forks/plugins that write ``state["context"]`` directly, and
   for threads checkpointed by an older build. When non-empty it appends after
   the stable prefix, exactly as before.

2. **Cache the stable prefix.** We set ``cache_control`` on the stable
   system-prompt block (the big, turn-stable prefix: persona + tool guidance).
   With the context block gone (#2776), nothing sits between that breakpoint
   and the message history — the precondition for history caching (#2777).

**Attempt-by-default, fail loud (#2255).** Caching used to be gated on an
Anthropic-looking model NAME — which silently disabled it for every gateway
alias (``protolabs/fast`` burned 1.5M uncached input tokens in one field turn).
Now ``enabled`` attaches ``cache_control`` for every model and the middleware
watches the outcome instead:

- a provider that REJECTS the blocks (error naming ``cache_control``) gets one
  retry without them, and that model falls back to plain delivery for the rest
  of the session (WARNING, once);
- a provider that silently IGNORES them (usage REPORTS cache fields as zero on
  consecutive calls with a cacheable-sized prefix) draws a WARNING naming the
  model, so "caching isn't working" is never invisible again;
- a provider that doesn't REPORT cache usage at all (no cache fields in
  ``input_token_details`` — e.g. a vLLM lane started without
  ``--enable-prompt-tokens-details``) draws a DIFFERENT warning: caching may be
  working invisibly, and the fix is the lane's reporting, not our blocks
  (learned the hard way in homelab-iac#242, where "ignoring" was really
  "not reporting");
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

# Rolling history breakpoints (#2777, ADR 0101 D1): of Anthropic's four
# cache_control slots, one holds the stable system prefix and up to this many
# ride the newest markable messages — so call N+1 reads call N's history from
# cache instead of re-paying the whole thread every round. Two, not three: the
# second is the belt for a tail that changed between calls (steering, repair),
# and the spare slot stays free for a future tool-schema breakpoint.
HISTORY_BREAKPOINTS = 2


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
        # Models whose current streak contains at least one call that REPORTED
        # cache fields (explicit zeros) — distinguishes "provider shows zero
        # activity" from "provider doesn't report cache usage at all".
        self._streak_reported: set[str] = set()

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
        # Rolling history breakpoints (#2777) — view-only: request.override never
        # persists, so the checkpointer's stored messages stay clean strings.
        new_msgs = self._mark_history(request)
        if new_msgs is not None:
            return request.override(system_message=new_sys, messages=new_msgs), cache
        return request.override(system_message=new_sys), cache

    # ── rolling history breakpoints (#2777) ──────────────────────────────────

    @staticmethod
    def _tool_results_markable(request) -> bool:
        """Whether a ToolMessage can carry a breakpoint on this client.

        Verified empirically for both wire paths: langchain-anthropic lifts a
        content block's ``cache_control`` onto the tool_result envelope (legal,
        cache-effective), while langchain-openai's tool converter rebuilds the
        block list and silently DROPS extra keys — so on the gateway path a
        tool-message mark would be a no-op and waste a walk slot. Keyed on the
        client class (capability), never the model name (#2255's lesson).
        """
        try:
            from langchain_anthropic import ChatAnthropic

            return isinstance(getattr(request, "model", None), ChatAnthropic)
        except Exception:  # noqa: BLE001 — optional import, default to the safe path
            return False

    def _marked_copy(self, msg):
        """A copy of ``msg`` with ``cache_control`` on its final text block, or
        ``None`` when the message has no markable block (empty content, a
        tool-calls-only assistant step, a trailing thinking/image block)."""
        cc = self._cache_control()
        content = getattr(msg, "content", None)
        if isinstance(content, str) and content:
            return msg.model_copy(update={"content": [{"type": "text", "text": content, "cache_control": cc}]})
        if isinstance(content, list) and content:
            last = content[-1]
            if isinstance(last, dict) and last.get("type") == "text" and last.get("text"):
                return msg.model_copy(update={"content": [*content[:-1], {**last, "cache_control": cc}]})
        return None

    def _mark_history(self, request):
        """Mark the newest ``HISTORY_BREAKPOINTS`` markable messages, walking
        from the tail. Returns the new message list, or ``None`` when nothing
        was marked. Copies only — stored history is never mutated."""
        from langchain_core.messages import ToolMessage

        msgs = getattr(request, "messages", None)
        if not msgs:
            return None
        tools_ok = self._tool_results_markable(request)
        out = list(msgs)
        budget = HISTORY_BREAKPOINTS
        changed = False
        for i in range(len(out) - 1, -1, -1):
            if budget == 0:
                break
            if isinstance(out[i], ToolMessage) and not tools_ok:
                continue  # the mark would be silently dropped on this wire path
            marked = self._marked_copy(out[i])
            if marked is not None:
                out[i] = marked
                budget -= 1
                changed = True
        return out if changed else None

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
                self._streak_reported.discard(model)
                return
            # Explicit zeros mean the provider REPORTS cache usage and there
            # genuinely was none; absent keys mean it doesn't report at all —
            # caching may be working invisibly (a vLLM lane without
            # --enable-prompt-tokens-details emits no prompt_tokens_details,
            # which is indistinguishable from 0 unless we keep the difference).
            if "cache_read" in details or "cache_creation" in details:
                self._streak_reported.add(model)
            streak = self._zero_hit_streak.get(model, 0) + 1
            self._zero_hit_streak[model] = streak
            if streak >= ZERO_HIT_WARN_AFTER and model not in self._warned:
                self._warned.add(model)
                if model in self._streak_reported:
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
                else:
                    log.warning(
                        "[prompt-cache] %s: %d consecutive calls with cache_control attached and "
                        "NO cache-usage fields in the response — the provider doesn't report "
                        "cache activity, so caching may be working invisibly rather than not at "
                        "all. On vLLM lanes, start the server with --enable-prompt-tokens-details "
                        "to surface real numbers; until then telemetry reads 0%% cached here.",
                        model,
                        streak,
                    )
                    self._notify(
                        f"Prompt caching can't be observed for {model} — {streak} consecutive "
                        f"calls report no cache-usage fields at all. The provider may be caching "
                        f"without reporting it (vLLM needs --enable-prompt-tokens-details); "
                        f"telemetry will show 0% cached for this model until the lane reports."
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
