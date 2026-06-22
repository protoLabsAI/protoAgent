"""Workspaces (ADR 0041) — create / list / run / remove."""

from __future__ import annotations

import pytest
import yaml

from graph.workspaces import manager


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path / "ws"))
    # Make port selection machine-independent: treat every port as OS-free, so _pick_port
    # exercises only the registry logic (the OS probe is covered by its own test below —
    # otherwise these assertions depend on whatever's listening on the test host).
    monkeypatch.setattr(manager, "_port_is_free", lambda port: True)
    return tmp_path / "ws"


def test_new_ls_run_rm(root):
    s = manager.create("alpha")
    # The id is opaque + immutable (`alpha-<4hex>`) and keys the dir; the name is display.
    assert s["name"] == "alpha" and s["id"].startswith("alpha-") and s["id"] != "alpha"
    assert s["port"] == 7871
    ws = root / s["id"]
    assert (ws / "langgraph-config.yaml").exists() and (ws / "workspace.yaml").exists()
    cfg = yaml.safe_load((ws / "langgraph-config.yaml").read_text())
    assert cfg["instance"]["id"] == s["id"] and cfg["identity"]["name"] == "alpha"

    assert [w["name"] for w in manager.list_workspaces()] == ["alpha"]

    env, argv = manager.run_exec("alpha", [])  # resolves by display name too
    assert env["PROTOAGENT_CONFIG_DIR"] == str(ws)
    assert env["PROTOAGENT_INSTANCE"] == s["id"]
    assert "--port" in argv and "7871" in argv

    assert manager.create("beta")["port"] == 7872  # next free port
    with pytest.raises(manager.WorkspaceError):
        manager.create("alpha")  # display-name collision

    assert "workspace" in manager.remove("alpha")["removed"] and not ws.exists()


def test_pick_port_skips_os_occupied(root, monkeypatch):
    """_pick_port skips a port held by an UNRELATED process (not just fleet-known ones), so
    a spawned agent doesn't die with EADDRINUSE (the pokemonAgent-on-:7871 collision)."""
    # 7871 is "occupied" by something outside the fleet registry → must be skipped.
    monkeypatch.setattr(manager, "_port_is_free", lambda port: port != 7871)
    assert manager.create("alpha")["port"] == 7872


def test_pick_port_raises_when_range_saturated(root, monkeypatch):
    """A fully-occupied range fails loudly instead of looping forever."""
    monkeypatch.setattr(manager, "_port_is_free", lambda port: False)
    with pytest.raises(manager.WorkspaceError):
        manager.create("alpha")


def test_rename_changes_display_not_id(root):
    s = manager.create("ava")
    out = manager.rename("ava", "nova")
    assert out == {"id": s["id"], "name": "nova"}  # id (slug/data scope) untouched
    ws = manager._find("nova")
    assert ws and ws["id"] == s["id"] and (root / s["id"]).exists()
    cfg = yaml.safe_load((root / s["id"] / "langgraph-config.yaml").read_text())
    assert cfg["identity"]["name"] == "nova" and cfg["instance"]["id"] == s["id"]
    assert manager._find("nova-x") is None and manager._find(s["id"])["name"] == "nova"

    manager.create("taken")
    with pytest.raises(manager.WorkspaceError):
        manager.rename("nova", "taken")  # display names stay unique
    with pytest.raises(manager.WorkspaceError):
        manager.rename("nova", "host")  # reserved slug


def test_from_config_clones_and_restamps(root, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "langgraph-config.yaml").write_text(
        "identity: { name: orig }\ninstance: { id: orig }\nmodel: { name: keep-me }\n"
    )
    (src / "secrets.yaml").write_text("model: { api_key: k }\n")
    s = manager.create("clone", from_config=str(src), shared_skills=True)
    cfg = yaml.safe_load((root / s["id"] / "langgraph-config.yaml").read_text())
    assert cfg["identity"]["name"] == "clone" and cfg["instance"]["id"] == s["id"]
    assert cfg["model"]["name"] == "keep-me"  # other config preserved
    assert cfg["skills"]["shared"] is True
    assert (root / s["id"] / "secrets.yaml").exists()  # secrets cloned too


def test_bad_name_rejected(root):
    with pytest.raises(manager.WorkspaceError):
        manager.create("bad name")


def test_root_is_instance_scoped(root, monkeypatch):
    """ADR 0004: a scoped instance owns its own workspaces root (and so its own
    fleet.json) — two co-located hubs must not share one fleet registry."""
    assert manager.workspaces_root() == root  # unscoped → the plain root

    monkeypatch.setenv("PROTOAGENT_INSTANCE", "roxy")
    scoped = manager.workspaces_root()
    assert scoped == root.parent / "roxy" / root.name  # scope_leaf nesting
    assert scoped != root

    monkeypatch.setenv("PROTOAGENT_INSTANCE", "other")
    assert manager.workspaces_root() != scoped  # siblings don't share


def test_fleet_state_follows_scoped_root(root, monkeypatch):
    """fleet.json lives under the scoped root — a scoped hub's registry is its own."""
    from graph.fleet import supervisor

    monkeypatch.setenv("PROTOAGENT_INSTANCE", "roxy")
    assert supervisor._state_path() == manager.workspaces_root() / "fleet.json"
    assert "roxy" in supervisor._state_path().parts
