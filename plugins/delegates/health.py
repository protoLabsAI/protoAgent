"""Background health prober for delegates (ADR 0025, PR4).

A lifecycle surface (register_surface) that periodically probes every configured
delegate and caches the result, so the panel shows a live status badge instead of
only on-demand Test. Reads ``merged_delegates()`` each tick, so it tracks
add/edit/remove without a restart; entries for removed delegates are pruned.

Ported in spirit from ORBIS's ``health_loop``, now with PER-DELEGATE exponential
backoff in BOTH directions: the loop still ticks on a fixed base interval, but each
delegate carries its own next-due time. A delegate that keeps failing is re-probed less
often, so a flaky peer degrades gracefully instead of getting hammered every tick; a
delegate that keeps SUCCEEDING while nobody is watching the panel also relaxes, because
each ACP probe is a whole subprocess and the healthy path used to pay full price forever
(#2542). Either streak resets the other, and reading the health snapshot — which is what
rendering the badge does — pulls healthy delegates back to the base cadence.
"""

from __future__ import annotations

import asyncio
import logging
import time

from . import status
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

# The healthy path used to pay full price forever: the existing backoff only slows
# FAILING delegates, so every configured delegate was re-probed every 120s, and an ACP
# probe is a fresh subprocess each time (spawn → `initialize` → teardown). Seven ACP
# delegates — a realistic PM/engineer setup — is ~5,000 process launches a day to keep a
# status badge warm (#2542).
#
# So consecutive successes stretch the cadence too, symmetrically with failures, up to a
# steady state. A binary "was reachable recently" badge does not need 2-minute freshness
# — especially since the probe stops at the handshake, so a delegate can look green and
# still fail on dispatch (the known probe-depth limit).
_SUCCESSES: dict[str, int] = {}
_HEALTHY_MAX_S = 900.0
# ...unless someone is actually looking. Reading the snapshot is what the console panel
# does to render the badge, so it doubles as "a human is watching this right now" —
# while that's true a healthy delegate stays on the base cadence and the panel behaves
# exactly as before. Nobody watching, nothing changing: 30 sweeps/hour becomes 4.
_OBSERVED_WINDOW_S = 180.0
_last_observed = 0.0


def health_snapshot() -> dict[str, dict]:
    """Current cached health per delegate name (copy).

    Also the "someone is looking" signal — see ``_OBSERVED_WINDOW_S``. Both callers
    qualify: the console panel renders this to draw the badge, and the agent's
    ``list_agents`` tool reads it when it's about to pick a delegate."""
    global _last_observed
    _last_observed = time.time()
    return {k: dict(v) for k, v in _HEALTH.items()}


def _observed() -> bool:
    """Has anything read the health snapshot recently?

    Deliberately on the wall clock rather than ``_probe_all``'s ``now``: that argument
    exists so tests can drive the cadence with synthetic timestamps, and "is a human
    looking at the panel right now" is a real-time question either way."""
    return (time.time() - _last_observed) <= _OBSERVED_WINDOW_S


def _backoff_delay(failures: int, successes: int = 0, *, observed: bool = True) -> float:
    """Seconds until a delegate's next probe.

    Failing: ``base * 2**failures`` capped at ``_BACKOFF_MAX_S``, so a flaky peer isn't
    hammered. Healthy: ``base`` while observed, otherwise ``base * 2**(successes-1)``
    capped at ``_HEALTHY_MAX_S`` — the cadence relaxes as a delegate proves itself and
    snaps back the moment it fails or someone opens the panel."""
    if failures > 0:
        return min(_BACKOFF_BASE_S * (2**failures), _BACKOFF_MAX_S)
    if observed or successes <= 1:
        return _BACKOFF_BASE_S
    return min(_BACKOFF_BASE_S * (2 ** (successes - 1)), _HEALTHY_MAX_S)


def _record_result(name: str, ok: bool, now: float) -> None:
    """Update a delegate's cadence state after a probe. Success and failure are
    mirror images: each resets the other's streak and re-derives the next-due time."""
    if ok:
        _FAILURES.pop(name, None)
        _SUCCESSES[name] = _SUCCESSES.get(name, 0) + 1
    else:
        _SUCCESSES.pop(name, None)
        _FAILURES[name] = _FAILURES.get(name, 0) + 1
    _NEXT_DUE[name] = now + _backoff_delay(_FAILURES.get(name, 0), _SUCCESSES.get(name, 0), observed=_observed())


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
        _SUCCESSES.pop(stale, None)
        _NEXT_DUE.pop(stale, None)
    # This sweep already walks the live roster to prune its own caches; the
    # last-dispatch cache needs the same treatment and has no loop of its own.
    status.prune(seen)


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
    log.info(
        "[delegates/health] prober started (tick %ss; per-delegate %s–%ss healthy, up to %ss when failing)",
        int(_INTERVAL_S),
        int(_BACKOFF_BASE_S),
        int(_HEALTHY_MAX_S),
        int(_BACKOFF_MAX_S),
    )


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
