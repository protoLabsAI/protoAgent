"""ops.fleet (ADR 0075 D2) — start/stop/status wrap graph.fleet.supervisor with op metadata."""

from __future__ import annotations

import pytest

from graph.fleet import supervisor
from graph.workspaces import manager
from ops import registry
from ops.fleet import create, down, order, remotes_add, remotes_remove, remotes_update, remove, rename, status, up


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


# ── management ops (#3471): the orchestration the routes and the offline CLI share ──


async def test_create_overlays_the_host_model_creates_and_starts(monkeypatch, tmp_path):
    seen: dict = {}
    cfg = tmp_path / "config" / "langgraph-config.yaml"
    cfg.parent.mkdir()
    cfg.write_text("x: 1")
    monkeypatch.setattr("graph.config_io.config_yaml_path", lambda: cfg)
    monkeypatch.setattr(manager, "create", lambda name, **kw: (seen.update(name=name, **kw) or {"id": "a-1", "port": 7901, "installed": ["hello"], "warnings": ["note"]}))
    monkeypatch.setattr(supervisor, "start", lambda name: {"name": name, "id": "a-1", "port": 7901, "pid": 4, "running": True})
    out = await create("alpha", bundle="", soul="be kind", inherit_config=True, inputs={"k": "v"})
    assert seen["name"] == "alpha" and seen["bundle"] is None and seen["soul"] == "be kind" and seen["inputs"] == {"k": "v"}
    assert seen["inherit_model"] == str(cfg.parent)  # the host's model connections travel
    assert out == {"agent": {"name": "alpha", "id": "a-1", "port": 7901, "pid": 4, "running": True}, "installed": ["hello"], "warnings": ["note"]}
    # no start, no inheritance: a blank agent, not spawned
    monkeypatch.setattr(supervisor, "start", lambda name: (_ for _ in ()).throw(AssertionError("must not start")))
    out = await create("beta", start=False, inherit_config=False)
    assert seen["inherit_model"] is None and out["agent"] == {"name": "beta", "id": "a-1", "port": 7901, "running": False}
    monkeypatch.setattr(manager, "create", lambda name, **kw: {"id": "a-2", "port": 7902, "installed": []})
    assert "warnings" not in await create("gamma", start=False)  # a clean create carries no warnings key


async def test_remove_stops_first_then_retires_or_purges_and_a_busy_workspace_propagates(monkeypatch):
    calls: list = []
    monkeypatch.setattr(supervisor, "stop", lambda ident, **kw: (calls.append(("stop", ident)) or {"stopped": True}))
    monkeypatch.setattr(manager, "remove", lambda ident, *, purge=False: (calls.append(("remove", ident, purge)) or {"name": ident, "removed": ["workspace"] if purge else []}))
    assert await remove("alpha") == {"name": "alpha", "removed": []}
    assert await remove("alpha", purge=True) == {"name": "alpha", "removed": ["workspace"]}
    assert calls == [("stop", "alpha"), ("remove", "alpha", False), ("stop", "alpha"), ("remove", "alpha", True)]
    # a member that was not running: stop's FleetError is not an error for remove
    monkeypatch.setattr(supervisor, "stop", lambda ident, **kw: (_ for _ in ()).throw(supervisor.FleetError("not running")))
    assert await remove("alpha") == {"name": "alpha", "removed": []}
    monkeypatch.setattr(manager, "remove", lambda ident, *, purge=False: (_ for _ in ()).throw(manager.WorkspaceBusy("workspace survived")))
    with pytest.raises(manager.WorkspaceBusy):
        await remove("alpha", purge=True)


async def test_rename_requires_a_name_and_wraps_the_manager(monkeypatch):
    monkeypatch.setattr(manager, "rename", lambda ident, new: {"id": "a-1", "name": new})
    assert await rename("alpha", "  Alpha Prime ") == {"id": "a-1", "name": "Alpha Prime"}
    with pytest.raises(manager.WorkspaceError, match="name is required"):
        await rename("alpha", "   ")


async def test_remotes_add_update_remove_probe_and_wrap_the_supervisor(monkeypatch):
    seen: list = []
    monkeypatch.setattr(supervisor, "add_remote", lambda name, url, token="": (seen.append(("add", name, url, token)) or {"id": "r-1", "name": name, "url": url, "remote": True}))
    monkeypatch.setattr(supervisor, "update_remote", lambda ident, *, name=None, url=None, token=None: (seen.append(("update", ident, name, url, token)) or {"id": ident, "name": name or "ava", "url": url or "https://ava:7870", "remote": True}))
    monkeypatch.setattr(supervisor, "remove_remote", lambda ident: {"id": ident, "name": "ava", "removed": ["remote"]})
    monkeypatch.setattr(supervisor, "probe_remote", lambda ident, timeout=1.0: (True, "0.165.0"))
    out = await remotes_add("ava", "https://ava:7870", "tok")
    assert out == {"agent": {"id": "r-1", "name": "ava", "url": "https://ava:7870", "remote": True}, "reachable": True, "version": "0.165.0"}
    assert "tok" not in str(out)  # the token is stored, never returned
    out = await remotes_update("r-1", url="https://ava2:7870", token="")
    assert seen[-1] == ("update", "r-1", None, "https://ava2:7870", "") and out["reachable"] is True
    assert await remotes_remove("r-1") == {"id": "r-1", "name": "ava", "removed": ["remote"]}


async def test_order_wraps_the_supervisor(monkeypatch):
    monkeypatch.setattr(supervisor, "set_roster_order", lambda order: list(order))
    assert await order(["b", "a"]) == ["b", "a"]


def test_management_ops_are_registered_as_mutating():
    reg = registry()
    for name in ("fleet.create", "fleet.remove", "fleet.rename", "fleet.remotes.add", "fleet.remotes.update", "fleet.remotes.remove", "fleet.order"):
        assert reg[name].mutates is True, name
