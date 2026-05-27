"""Tests for the cache-warming heartbeat (CacheWarmer) — bd-pe2.2.

The warm caller is monkeypatched so no real model/gateway is touched; we test
the gating logic, the start/stop lifecycle, and that the loop pings on the
configured interval.
"""

import asyncio

import pytest

import graph.cache_warmer as cw
from graph.cache_warmer import CacheWarmer
from graph.config import LangGraphConfig


def _cfg(**kw):
    base = dict(
        cache_warming_enabled=True,
        prompt_cache_enabled=True,
        cache_warming_interval_seconds=3300,
        model_name="claude-sonnet-4-6",
    )
    base.update(kw)
    return LangGraphConfig(**base)


# --- gating -----------------------------------------------------------------

def test_should_run_true_for_anthropic():
    assert CacheWarmer(_cfg())._should_run() is True


def test_should_not_run_when_disabled():
    assert CacheWarmer(_cfg(cache_warming_enabled=False))._should_run() is False


def test_should_not_run_when_prompt_cache_off():
    assert CacheWarmer(_cfg(prompt_cache_enabled=False))._should_run() is False


def test_should_not_run_for_non_anthropic():
    assert CacheWarmer(_cfg(model_name="protolabs/qwen"))._should_run() is False


def test_force_overrides_model_heuristic():
    assert CacheWarmer(_cfg(model_name="protolabs/qwen", prompt_cache_force=True))._should_run() is True


def test_non_positive_interval_disables():
    assert CacheWarmer(_cfg(cache_warming_interval_seconds=0))._should_run() is False


# --- cache_control tiering --------------------------------------------------

def test_cache_control_ephemeral_default():
    assert CacheWarmer(_cfg())._cache_control() == {"type": "ephemeral"}


def test_cache_control_persistent_tier():
    cc = CacheWarmer(_cfg(prompt_cache_ttl="1h"))._cache_control()
    assert cc == {"type": "ephemeral", "ttl": "1h"}


# --- lifecycle --------------------------------------------------------------

@pytest.mark.asyncio
async def test_disabled_start_is_noop(monkeypatch):
    built = []
    warmer = CacheWarmer(_cfg(cache_warming_enabled=False))
    monkeypatch.setattr(warmer, "_build_caller", lambda: built.append(1))
    await warmer.start()
    assert warmer._task is None
    assert built == []


@pytest.mark.asyncio
async def test_start_pings_then_stop(monkeypatch):
    pings = []

    async def fake_warm():
        pings.append(1)

    warmer = CacheWarmer(_cfg(cache_warming_interval_seconds=3600))
    monkeypatch.setattr(warmer, "_build_caller", lambda: fake_warm)
    await warmer.start()
    assert warmer._task is not None
    # first ping fires immediately (before the interval wait)
    await asyncio.sleep(0.02)
    await warmer.stop()
    assert pings  # at least one warm ping happened
    assert warmer._task is None


@pytest.mark.asyncio
async def test_loop_survives_ping_failure(monkeypatch):
    calls = {"n": 0}

    async def flaky_warm():
        calls["n"] += 1
        raise RuntimeError("transient")

    warmer = CacheWarmer(_cfg(cache_warming_interval_seconds=1))
    monkeypatch.setattr(warmer, "_build_caller", lambda: flaky_warm)
    await warmer.start()
    await asyncio.sleep(0.05)  # first ping raises, gets caught
    await warmer.stop()        # interrupts the interval wait
    assert calls["n"] >= 1     # the loop ran (and didn't crash) despite raising
