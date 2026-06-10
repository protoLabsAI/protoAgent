"""CacheWarmer — optional heartbeat that keeps the Anthropic prompt cache warm.

bd-pe2.2. **Off by default.** When enabled, a background asyncio task issues a
minimal generation on an interval that reproduces the agent's cached
system-prompt prefix (with ``cache_control``), refreshing the cache entry so
the *first* real request after an idle gap hits a warm cache instead of paying
a full cache-miss on the (large, stable) system prefix.

When this is worth it:

- Sporadic, latency-sensitive traffic on the **"1h" persistent** cache tier,
  with the interval set just under the TTL (default 55m). The warm ping costs
  one cache-write + a 1-token generation per interval — cheap relative to a
  cold miss on a multi-thousand-token prefix when a user is waiting.

When it is *not* worth it (hence off by default):

- Steady traffic keeps the cache warm on its own — warming is pure cost.
- Non-Anthropic models have no prompt cache to warm (start() no-ops with a log
  unless ``prompt_cache_force`` is set, mirroring PromptCacheMiddleware).

We deliberately run our own asyncio loop rather than going through the bundled
scheduler: the scheduler fires *full agent turns* over A2A (the wrong, far more
expensive primitive for a 1-token keep-alive). The lifecycle (start/stop)
mirrors the scheduler's so server.py can manage it the same way.
"""

from __future__ import annotations

import asyncio
import logging
import re

from graph.config import LangGraphConfig

log = logging.getLogger(__name__)

_ANTHROPIC_RE = re.compile(r"(claude|anthropic|sonnet|opus|haiku)", re.IGNORECASE)


class CacheWarmer:
    def __init__(self, config: LangGraphConfig, *, knowledge_store=None, scheduler=None):
        self._config = config
        self._knowledge_store = knowledge_store
        self._scheduler = scheduler
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    name = "cache-warmer"

    # --- gating -------------------------------------------------------------
    def _should_run(self) -> bool:
        c = self._config
        if not c.cache_warming_enabled:
            return False
        if not c.prompt_cache_enabled:
            log.info("[cache-warmer] disabled: prompt_cache.enabled is false")
            return False
        if c.cache_warming_interval_seconds <= 0:
            log.warning("[cache-warmer] disabled: non-positive interval")
            return False
        if not (c.prompt_cache_force or _ANTHROPIC_RE.search(c.model_name)):
            log.info(
                "[cache-warmer] disabled: model '%s' isn't Anthropic-family "
                "(set prompt_cache.force to override)", c.model_name,
            )
            return False
        return True

    # --- the warm prefix ----------------------------------------------------
    def _cache_control(self) -> dict:
        cc = {"type": "ephemeral"}
        ttl = self._config.prompt_cache_ttl
        if ttl and ttl != "5m":
            cc["ttl"] = ttl
        return cc

    def _build_caller(self):
        """Build the bound model + system block once, reproducing the agent prefix.

        Returns a no-arg coroutine factory that issues one warm ping. Kept
        separate from the loop so it can be unit-tested without a timer.
        """
        from langchain_core.messages import HumanMessage, SystemMessage

        from graph.llm import create_llm
        from graph.prompts import build_system_prompt
        from tools.lg_tools import get_all_tools

        llm = create_llm(self._config)
        tools = get_all_tools(self._knowledge_store, scheduler=self._scheduler)
        bound = llm.bind_tools(tools) if tools else llm

        # Same stable prefix + cache breakpoint PromptCacheMiddleware writes,
        # so we warm the cache key real requests will hit. include_subagents
        # matches the default graph build (task/task_batch in the toolset).
        stable = build_system_prompt(include_subagents=True)
        system = SystemMessage(content=[
            {"type": "text", "text": stable, "cache_control": self._cache_control()},
        ])
        # 1-token generation: enough to register a cache hit/write, no more.
        ping = HumanMessage(content="ping")

        async def _warm_once() -> None:
            await bound.ainvoke([system, ping], config={"max_tokens": 1})

        return _warm_once

    # --- lifecycle ----------------------------------------------------------
    async def start(self) -> None:
        if not self._should_run():
            return
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        try:
            warm_once = self._build_caller()
        except Exception:
            log.exception("[cache-warmer] failed to build warm caller; not starting")
            return
        self._task = asyncio.create_task(self._loop(warm_once))
        log.info(
            "[cache-warmer] started (every %ss, ttl=%s)",
            self._config.cache_warming_interval_seconds, self._config.prompt_cache_ttl,
        )

    async def _loop(self, warm_once) -> None:
        interval = self._config.cache_warming_interval_seconds
        while not self._stop.is_set():
            try:
                await warm_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # never let a transient failure kill the loop
                log.warning("[cache-warmer] warm ping failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue  # interval elapsed → warm again

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass  # expected — we just cancelled the warm-cache task
        except Exception:  # noqa: BLE001 — a failing teardown must not break stop()
            pass
