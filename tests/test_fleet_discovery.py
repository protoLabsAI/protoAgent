"""Fleet discovery (ADR 0042 §I) — the mDNS advertise lifecycle + its event-loop guard.

Sync zeroconf calls block on futures scheduled on the loop they're called from, so
``advertise``/``stop_advertise`` must run off the loop (``asyncio.to_thread``); the guard
turns a regressed on-loop call site into an instant warning instead of a ~10s
``EventLoopBlocked`` boot stall (seen live, roxy :7874 2026-06-09).
"""

from __future__ import annotations

import asyncio
import sys
import time
import types

import pytest

from graph.fleet import discovery


@pytest.fixture(autouse=True)
def _reset_zc(monkeypatch):
    monkeypatch.setattr(discovery, "_zc", None)
    monkeypatch.setattr(discovery, "_info", None)
    monkeypatch.setattr(discovery, "_peer_cache", {})  # fresh boot-sweep cache per test
    monkeypatch.setattr(discovery, "_sweep_task", None)


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


# ── tailnet channel ───────────────────────────────────────────────────────────
_TS_STATUS = {
    "Peer": {
        "k1": {"HostName": "ava", "Online": True, "TailscaleIPs": ["100.101.189.45", "fd7a::1"]},
        "k2": {"HostName": "beefcake", "Online": False, "TailscaleIPs": ["100.77.164.70"]},
        "k3": {"HostName": "no-ips", "Online": True, "TailscaleIPs": []},
    }
}


class _FakeRun:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


def test_tailnet_peer_ips_online_ipv4_only(monkeypatch):
    import json as _json

    monkeypatch.setattr(discovery, "_tailscale_cli", lambda: "/fake/tailscale")
    monkeypatch.setattr(discovery.subprocess, "run", lambda *a, **kw: _FakeRun(0, _json.dumps(_TS_STATUS)))
    assert discovery._tailnet_peer_ips() == ["100.101.189.45"]  # online + IPv4 only


def test_tailnet_peer_ips_quiet_without_tailscale(monkeypatch):
    monkeypatch.setattr(discovery, "_tailscale_cli", lambda: None)
    assert discovery._tailnet_peer_ips() == []

    monkeypatch.setattr(discovery, "_tailscale_cli", lambda: "/fake/tailscale")
    monkeypatch.setattr(discovery.subprocess, "run", lambda *a, **kw: _FakeRun(1, ""))
    assert discovery._tailnet_peer_ips() == []  # CLI errors → empty, never raises


def test_scan_tailnet_probes_peers_and_skips_known(monkeypatch):
    monkeypatch.setattr(discovery, "_tailnet_peer_ips", lambda: ["100.101.189.45"])
    probed: list[tuple] = []

    async def fake_probe(client, host, port):
        probed.append((host, port))
        if port == 7871:
            return {"name": "ava-agent", "url": f"http://{host}:{port}", "host": host, "port": port}
        return None

    monkeypatch.setattr(discovery, "_probe", fake_probe)
    found = asyncio.run(discovery._scan_tailnet((7870, 7872), known={("100.101.189.45", 7870)}))
    assert found == [{"name": "ava-agent", "url": "http://100.101.189.45:7871", "host": "100.101.189.45", "port": 7871}]
    assert ("100.101.189.45", 7870) not in probed  # known member skipped


def test_discover_merges_three_channels(monkeypatch):
    async def fake_local(port_range, skip):
        return [{"name": "loc", "url": "http://127.0.0.1:7871", "host": "127.0.0.1", "port": 7871}]

    async def fake_tailnet(port_range, known):
        return [{"name": "ava-agent", "url": "http://100.101.189.45:7874", "host": "100.101.189.45", "port": 7874}]

    monkeypatch.setattr(discovery, "_scan_local", fake_local)
    monkeypatch.setattr(discovery, "_scan_tailnet", fake_tailnet)
    monkeypatch.setattr(
        discovery,
        "_browse_mdns",
        lambda timeout: [
            {"name": "lan", "url": "http://192.168.5.40:7871", "host": "192.168.5.40", "port": 7871},
            {"name": "known", "url": "http://192.168.5.41:7871", "host": "192.168.5.41", "port": 7871},
        ],
    )
    found = asyncio.run(discovery.discover(known={("192.168.5.41", 7871)}))
    assert sorted(f["name"] for f in found) == ["ava-agent", "lan", "loc"]


def test_discover_collapses_colocated_mdns_with_local_scan(monkeypatch):
    """A co-located agent surfaces via BOTH channels (loopback scan + its own mDNS advert
    carrying the machine's LAN IP) — discover() must normalize the advert to loopback so
    the (host, port) dedupe collapses the pair into one entry."""
    monkeypatch.setattr(discovery, "_local_ip", lambda: "192.168.5.31")

    async def fake_local(port_range, skip):
        return [{"name": "roxy", "url": "http://127.0.0.1:7874", "host": "127.0.0.1", "port": 7874}]

    async def fake_tailnet(port_range, known):
        return []

    monkeypatch.setattr(discovery, "_scan_local", fake_local)
    monkeypatch.setattr(discovery, "_scan_tailnet", fake_tailnet)
    monkeypatch.setattr(
        discovery,
        "_browse_mdns",
        lambda timeout: [
            {"name": "roxy", "url": "http://192.168.5.31:7874", "host": "192.168.5.31", "port": 7874},
            {"name": "remote", "url": "http://192.168.5.40:7871", "host": "192.168.5.40", "port": 7871},
        ],
    )
    found = asyncio.run(discovery.discover())
    assert sorted((f["name"], f["host"]) for f in found) == [
        ("remote", "192.168.5.40"),  # a genuinely-remote sibling keeps its LAN address
        ("roxy", "127.0.0.1"),  # the co-located pair collapsed to the loopback entry
    ]

    # And a KNOWN fleet peer's own advert (LAN ip + its port) is excluded the same way.
    found = asyncio.run(discovery.discover(known={("127.0.0.1", 7874)}))
    assert [f["name"] for f in found] == ["remote"]


# ── boot sweep + peer cache (auto-sweep on hub boot) ──────────────────────────
def _stub_channels(monkeypatch, *, local=None, tailnet=None, mdns=None):
    """Replace the three live discovery channels with fixed results (or a raiser)."""

    async def fake_local(port_range, skip):
        if isinstance(local, Exception):
            raise local
        return list(local or [])

    async def fake_tailnet(port_range, known):
        if isinstance(tailnet, Exception):
            raise tailnet
        return list(tailnet or [])

    def fake_mdns(timeout):
        if isinstance(mdns, Exception):
            raise mdns
        return list(mdns or [])

    monkeypatch.setattr(discovery, "_scan_local", fake_local)
    monkeypatch.setattr(discovery, "_scan_tailnet", fake_tailnet)
    monkeypatch.setattr(discovery, "_browse_mdns", fake_mdns)
    monkeypatch.setattr(discovery, "_local_ip", lambda: "10.0.0.9")  # never matches the fakes


def test_boot_sweep_populates_cache(monkeypatch):
    """The at-boot sweep caches whatever the channels surfaced, keyed by (host, port)."""
    _stub_channels(
        monkeypatch,
        local=[{"name": "loc", "url": "http://127.0.0.1:7871", "host": "127.0.0.1", "port": 7871}],
        mdns=[{"name": "lan", "url": "http://192.168.5.40:7871", "host": "192.168.5.40", "port": 7871}],
    )
    assert discovery.cached_peers() == []  # cold before the sweep

    swept = asyncio.run(discovery.boot_sweep())
    assert sorted(p["name"] for p in swept) == ["lan", "loc"]
    assert sorted(p["name"] for p in discovery.cached_peers()) == ["lan", "loc"]
    assert ("192.168.5.40", 7871) in discovery._peer_cache  # stamped by (host, port)


def test_cached_peers_returned_without_fresh_scan(monkeypatch):
    """Once the cache is warm, a later discover() surfaces the cached peer even though the
    live scan finds nothing — the first console open is instant, not blank."""
    discovery._remember(
        [{"name": "booted-sibling", "url": "http://192.168.5.50:7872", "host": "192.168.5.50", "port": 7872}]
    )
    _stub_channels(monkeypatch)  # all three channels return nothing

    found = asyncio.run(discovery.discover())
    assert [f["name"] for f in found] == ["booted-sibling"]

    # …but a cached peer that's since been added to the fleet (in `known`) is NOT re-surfaced.
    found = asyncio.run(discovery.discover(known={("192.168.5.50", 7872)}))
    assert found == []


def test_boot_sweep_swallows_channel_failure(monkeypatch):
    """A channel blowing up never propagates out of the sweep — it returns [] and the cache
    stays empty rather than crashing boot."""
    _stub_channels(monkeypatch, local=RuntimeError("scan exploded"))

    result = asyncio.run(discovery.boot_sweep())  # must not raise
    assert result == []
    assert discovery.cached_peers() == []


def test_stale_cached_peers_age_out(monkeypatch):
    """Cached peers older than the TTL stop surfacing (a peer that went away)."""
    discovery._remember([{"name": "gone", "url": "http://192.168.5.60:7873", "host": "192.168.5.60", "port": 7873}])
    # Backdate the entry past the TTL.
    peer, _ = discovery._peer_cache[("192.168.5.60", 7873)]
    discovery._peer_cache[("192.168.5.60", 7873)] = (peer, time.time() - discovery._CACHE_TTL_S - 1)
    _stub_channels(monkeypatch)

    assert asyncio.run(discovery.discover()) == []
    assert ("192.168.5.60", 7873) not in discovery._peer_cache  # aged out on read


def test_start_boot_sweep_schedules_task_on_loop(monkeypatch):
    """start_boot_sweep() fires the sweep on the running loop and holds a task ref; with no
    running loop it just no-ops."""
    discovery.start_boot_sweep()  # no running loop here → skipped, no raise
    assert discovery._sweep_task is None

    _stub_channels(
        monkeypatch,
        local=[{"name": "loc", "url": "http://127.0.0.1:7871", "host": "127.0.0.1", "port": 7871}],
    )

    async def _drive():
        discovery.start_boot_sweep()
        assert discovery._sweep_task is not None
        await discovery._sweep_task  # let the fire-and-forget task complete

    asyncio.run(_drive())
    assert [p["name"] for p in discovery.cached_peers()] == ["loc"]
