"""ops.fleet (ADR 0075 D2) — start/stop/status wrap graph.fleet.supervisor with op metadata."""

from __future__ import annotations

from graph.fleet import supervisor
from ops import registry
from ops.fleet import down, status, up


async def test_up_wraps_supervisor(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(supervisor, "up", lambda names=None: (seen.update(names=names) or [{"name": "a", "started": True}]))
    rows = await up(["a"])
    assert rows == [{"name": "a", "started": True}] and seen["names"] == ["a"]


async def test_down_wraps_supervisor(monkeypatch):
    monkeypatch.setattr(supervisor, "down", lambda names=None: [{"name": "a", "stopped": True}])
    assert await down() == [{"name": "a", "stopped": True}]


async def test_status_wraps_supervisor(monkeypatch):
    monkeypatch.setattr(supervisor, "status", lambda: [{"name": "host", "running": True}])
    assert await status() == [{"name": "host", "running": True}]


def test_fleet_ops_registered_with_metadata():
    reg = registry()
    assert reg["fleet.up"].mutates is True and reg["fleet.down"].mutates is True
    assert reg["fleet.status"].mutates is False  # read-only admissible
