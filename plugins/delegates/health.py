"""Background health prober for delegates (ADR 0025, PR4).

A lifecycle surface (register_surface) that periodically probes every configured
delegate and caches the result, so the panel shows a live status badge instead of
only on-demand Test. Reads ``merged_delegates()`` each tick, so it tracks
add/edit/remove without a restart; entries for removed delegates are pruned.

Ported in spirit from ORBIS's ``health_loop`` (simplified: fixed interval + jitter,
no per-delegate backoff yet).
"""

from __future__ import annotations

import asyncio
import logging
import time

from .adapters import ADAPTERS

log = logging.getLogger("protoagent.plugins.delegates")

# name -> {ok, latency_ms?, error?, detail?, checked_at}
_HEALTH: dict[str, dict] = {}
_INTERVAL_S = 120.0
_INITIAL_DELAY_S = 15.0
_task: asyncio.Task | None = None


def health_snapshot() -> dict[str, dict]:
    """Current cached health per delegate name (copy)."""
    return {k: dict(v) for k, v in _HEALTH.items()}


async def _probe_all() -> None:
    from .store import merged_delegates

    seen: set[str] = set()
    for raw in merged_delegates():
        if not isinstance(raw, dict):
            continue
        name = raw.get("name")
        adapter = ADAPTERS.get(str(raw.get("type", "")))
        if not (name and adapter):
            continue
        seen.add(name)
        try:
            d = adapter.parse(raw)
            res = await adapter.probe(d)
        except Exception as exc:  # noqa: BLE001 — a bad delegate shouldn't kill the loop
            res = {"ok": False, "error": str(exc)[:200]}
        res["checked_at"] = time.time()
        _HEALTH[name] = res
    for stale in [n for n in _HEALTH if n not in seen]:
        _HEALTH.pop(stale, None)


async def _loop(interval: float = _INTERVAL_S, initial_delay: float = _INITIAL_DELAY_S) -> None:
    await asyncio.sleep(initial_delay)  # let boot settle before the first sweep
    while True:
        try:
            await _probe_all()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("[delegates/health] probe sweep failed")
        await asyncio.sleep(interval)


async def start() -> None:
    global _task
    if _task and not _task.done():
        return
    _task = asyncio.create_task(_loop())
    log.info("[delegates/health] prober started (every %ss)", int(_INTERVAL_S))


async def stop() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
    _task = None
    # Server shutdown: reap every pooled ACP client so dispatch agents don't strand
    # as init-reparented orphans. (This surface's stop() is the delegates plugin's
    # process-scoped shutdown hook.)
    try:
        from plugins.coding_agent import close_all

        await close_all()
    except Exception:  # noqa: BLE001 — shutdown reap is best-effort
        log.exception("[delegates/health] reaping ACP clients on shutdown failed")
