"""Fleet discovery (ADR 0042 §I) — the mDNS advertise lifecycle + its event-loop guard.

Sync zeroconf calls block on futures scheduled on the loop they're called from, so
``advertise``/``stop_advertise`` must run off the loop (``asyncio.to_thread``); the guard
turns a regressed on-loop call site into an instant warning instead of a ~10s
``EventLoopBlocked`` boot stall (seen live, roxy :7874 2026-06-09).
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from graph.fleet import discovery


@pytest.fixture(autouse=True)
def _reset_zc(monkeypatch):
    monkeypatch.setattr(discovery, "_zc", None)
    monkeypatch.setattr(discovery, "_info", None)


class _FakeZeroconf:
    def __init__(self):
        self.registered = None
        self.closed = False

    def register_service(self, info):
        self.registered = info

    def unregister_service(self, info):
        self.registered = None

    def close(self):
        self.closed = True


@pytest.fixture
def fake_zeroconf(monkeypatch):
    mod = types.ModuleType("zeroconf")
    mod.Zeroconf = _FakeZeroconf
    mod.ServiceInfo = lambda *a, **kw: {"args": a, "kw": kw}
    monkeypatch.setitem(sys.modules, "zeroconf", mod)
    return mod


def test_advertise_registers_off_loop(fake_zeroconf):
    discovery.advertise("alpha", 7871)
    assert isinstance(discovery._zc, _FakeZeroconf)
    assert discovery._zc.registered is not None

    discovery.advertise("alpha", 7871)  # idempotent — second call is a no-op
    discovery.stop_advertise()
    assert discovery._zc is None and discovery._info is None


def test_advertise_refuses_on_event_loop(fake_zeroconf, caplog):
    async def _on_loop():
        discovery.advertise("alpha", 7871)

    asyncio.run(_on_loop())
    assert discovery._zc is None  # bailed before touching zeroconf
    assert "asyncio.to_thread" in caplog.text


def test_stop_advertise_refuses_on_event_loop(caplog):
    zc = _FakeZeroconf()
    discovery._zc = zc

    async def _on_loop():
        discovery.stop_advertise()

    asyncio.run(_on_loop())
    assert discovery._zc is zc and not zc.closed  # untouched — refused, not deadlocked
    assert "asyncio.to_thread" in caplog.text

    discovery.stop_advertise()  # off the loop: cleans up
    assert zc.closed and discovery._zc is None


def test_advertise_without_port_is_noop(fake_zeroconf):
    discovery.advertise("alpha", 0)
    assert discovery._zc is None
