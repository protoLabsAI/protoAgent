"""Background health prober for delegates (ADR 0025, PR4).

A lifecycle surface (register_surface) that periodically probes every configured
delegate and caches the result, so the panel shows a live status badge instead of
only on-demand Test. Reads ``merged_delegates()`` each tick, so it tracks
add/edit/remove without a restart; entries for removed delegates are pruned.

Ported in spirit from ORBIS's ``health_loop``, now with PER-DELEGATE exponential
backoff: the loop still ticks on a fixed base interval, but a delegate that keeps
failing is re-probed less often (its next-due time backs off) so a flaky peer degrades
gracefully instead of getting hammered every tick. A success resets it to the base
cadence.
"""

from __future__ import annotations

import asyncio
import logging
import time

from .adapters import ADAPTERS

log = logging.getLogger("protoagent.plugins.delegates")

# name -> {ok, latency_ms?, error?, detail?, checked_at}
_HEALTH: dict[str, dict] = {}
# Per-delegate adaptive backoff state: consecutive-failure count + the monotonic-ish
# wall-clock time the delegate is next due for a probe. A healthy delegate is probed
# every base interval; each consecutive failure roughly doubles the wait, capped.
_FAILURES: dict[str, int] = {}
_NEXT_DUE: dict[str, float] = {}
_INTERVAL_S = 120.0
_INITIAL_DELAY_S = 15.0
_BACKOFF_BASE_S = 120.0
_BACKOFF_MAX_S = 960.0
_task: asyncio.Task | None = None


def health_snapshot() -> dict[str, dict]:
    """Current cached health per delegate name (copy)."""
    return {k: dict(v) for k, v in _HEALTH.items()}


def _backoff_delay(failures: int) -> float:
    """Seconds until a delegate's next probe given its consecutive-failure count:
    ``base`` when healthy (failures<=0), ``base * 2**failures`` capped at ``max`` after
    that — so the cadence grows monotonically with failures then pins at the ceiling."""
    if failures <= 0:
        return _BACKOFF_BASE_S
    return min(_BACKOFF_BASE_S * (2**failures), _BACKOFF_MAX_S)


def _record_result(name: str, ok: bool, now: float) -> None:
    """Update a delegate's backoff state after a probe: reset to the base cadence on
    success, otherwise count the failure and push the next-due time out per
    ``_backoff_delay``."""
    if ok:
        _FAILURES.pop(name, None)
    else:
        _FAILURES[name] = _FAILURES.get(name, 0) + 1
    _NEXT_DUE[name] = now + _backoff_delay(_FAILURES.get(name, 0))


async def _probe_all(now: float | None = None) -> None:
    from .store import merged_delegates

    if now is None:
        now = time.time()
    seen: set[str] = set()
    for raw in merged_delegates():
        if not isinstance(raw, dict):
            continue
        name = raw.get("name")
        adapter = ADAPTERS.get(str(raw.get("type", "")))
        if not (name and adapter):
            continue
        seen.add(name)
        # Per-delegate backoff: a flaky delegate that isn't due yet is skipped this tick
        # (its cached health is left intact) so we don't ping-pong a known-bad peer.
        if now < _NEXT_DUE.get(name, 0.0):
            continue
        try:
            d = adapter.parse(raw)
            res = await adapter.probe(d)
        except Exception as exc:  # noqa: BLE001 — a bad delegate shouldn't kill the loop
            res = {"ok": False, "error": str(exc)[:200]}
        res["checked_at"] = now
        _HEALTH[name] = res
        _record_result(name, bool(res.get("ok")), now)
    for stale in [n for n in _HEALTH if n not in seen]:
        _HEALTH.pop(stale, None)
        _FAILURES.pop(stale, None)
        _NEXT_DUE.pop(stale, None)


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
