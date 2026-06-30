"""Tests for the two-tier (box / instance) path resolution — ``infra.paths.InstancePaths``.

Covers the four deployment shapes (HOME / INSTANCE / default / frozen), the
box-vs-instance split, the env overrides, and the cached-singleton hygiene that
``reset_instance_paths()`` guarantees.
"""

from __future__ import annotations

import pytest

import infra.paths as paths


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Pin box_root to a tmp dir, clear every root env var, and reset the cached
    singleton before AND after each test so resolution is deterministic and never
    leaks the dev shell's PROTOAGENT_INSTANCE."""
    box = tmp_path / "box"
    box.mkdir()
    monkeypatch.setattr(paths, "data_home", lambda: box)
    for var in (
        "PROTOAGENT_HOME",
        "PROTOAGENT_INSTANCE",
        "PROTOAGENT_BOX_ROOT",
        "PROTOAGENT_HOST_CONFIG",
        "PROTOAGENT_PLUGINS_DIR",
        "PROTOAGENT_PLUGINS_LOCK",
        "PROTOAGENT_WORKSPACE",
    ):
        monkeypatch.delenv(var, raising=False)
    paths.reset_instance_paths()
    yield box
    paths.reset_instance_paths()


def test_default_shape(_isolate):
    box = _isolate
    p = paths.instance_paths()
    assert p.instance_id == "default"
    assert p.box_root == box
    assert p.instance_root == box / "default"
    # config + plugins + every store sit under the instance root
    assert p.config_yaml == box / "default" / "config" / "langgraph-config.yaml"
    assert p.secrets_yaml == box / "default" / "config" / "secrets.yaml"
    assert p.plugins_dir == box / "default" / "plugins"
    assert p.store("knowledge") == box / "default" / "knowledge"


def test_instance_shape(monkeypatch, _isolate):
    box = _isolate
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "alice")
    paths.reset_instance_paths()
    p = paths.instance_paths()
    assert p.instance_id == "alice"
    assert p.box_root == box
    assert p.instance_root == box / "alice"
    assert p.config_yaml == box / "alice" / "config" / "langgraph-config.yaml"


def test_home_shape_is_terminal(monkeypatch, tmp_path, _isolate):
    """PROTOAGENT_HOME relocates ONLY the instance tier; box_root stays data_home()."""
    box = _isolate
    home = tmp_path / "app-data"
    monkeypatch.setenv("PROTOAGENT_HOME", str(home))
    paths.reset_instance_paths()
    p = paths.instance_paths()
    assert p.instance_root == home
    assert p.instance_id == "app-data"  # basename of HOME
    assert p.box_root == box  # NOT moved by HOME
    assert p.config_yaml == home / "config" / "langgraph-config.yaml"


def test_home_plus_instance_is_fleet_member(monkeypatch, tmp_path, _isolate):
    """Fleet member: HOME=<workspace> + INSTANCE=<wid>. Root is the workspace,
    id is the workspace id, and the box tier is still the shared data home."""
    box = _isolate
    ws = tmp_path / "workspaces" / "ava-7f3a"
    monkeypatch.setenv("PROTOAGENT_HOME", str(ws))
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "ava-7f3a")
    paths.reset_instance_paths()
    p = paths.instance_paths()
    assert p.instance_root == ws
    assert p.instance_id == "ava-7f3a"
    assert p.box_root == box
    # the member shares the box-level host layer + commons with the hub
    assert p.host_config == box / "host-config.yaml"
    assert p.commons_dir == box / "commons"


def test_box_root_override(monkeypatch, tmp_path, _isolate):
    other = tmp_path / "other-box"
    monkeypatch.setenv("PROTOAGENT_BOX_ROOT", str(other))
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "dev")
    paths.reset_instance_paths()
    p = paths.instance_paths()
    assert p.box_root == other
    assert p.instance_root == other / "dev"
    assert p.host_config == other / "host-config.yaml"


def test_box_tier_is_shared_not_under_instance(_isolate):
    """host-config / commons / heartbeats / data-version live at box_root, NOT under
    the instance root (the machine-wide Host layer + shared library)."""
    box = _isolate
    p = paths.instance_paths()
    assert p.host_config == box / "host-config.yaml"
    assert p.commons_dir == box / "commons"
    assert p.commons_skills_db == box / "commons" / "skills.db"
    assert p.instances_dir == box / ".instances"
    assert p.data_version_file == box / ".data-version"
    # explicitly NOT nested under the instance root
    assert p.instance_root not in p.host_config.parents


def test_fleet_registry_is_hub_instance_scoped(_isolate):
    """fleet.json / workspaces live under the instance root (the hub's), so a member's
    own (empty) workspaces/ keeps shutdown_all hub-only by construction."""
    box = _isolate
    p = paths.instance_paths()
    assert p.workspaces_dir == box / "default" / "workspaces"
    assert p.fleet_json == box / "default" / "workspaces" / "fleet.json"
    assert p.remotes_json == box / "default" / "workspaces" / "remotes.json"


def test_env_overrides(monkeypatch, tmp_path, _isolate):
    monkeypatch.setenv("PROTOAGENT_HOST_CONFIG", str(tmp_path / "h.yaml"))
    monkeypatch.setenv("PROTOAGENT_PLUGINS_DIR", str(tmp_path / "plug"))
    monkeypatch.setenv("PROTOAGENT_PLUGINS_LOCK", str(tmp_path / "plug.lock"))
    monkeypatch.setenv("PROTOAGENT_WORKSPACE", str(tmp_path / "fence"))
    paths.reset_instance_paths()
    p = paths.instance_paths()
    assert p.host_config == tmp_path / "h.yaml"
    assert p.plugins_dir == tmp_path / "plug"
    assert p.plugins_lock == tmp_path / "plug.lock"
    assert p.workspace_dir == tmp_path / "fence"


def test_frozen_app_root(monkeypatch, tmp_path, _isolate):
    """When PyInstaller-frozen, app_root is _MEIPASS; otherwise the repo root."""
    meipass = tmp_path / "meipass"
    monkeypatch.setattr(paths.sys if hasattr(paths, "sys") else __import__("sys"), "frozen", True, raising=False)
    monkeypatch.setattr(__import__("sys"), "_MEIPASS", str(meipass), raising=False)
    paths.reset_instance_paths()
    p = paths.instance_paths()
    assert p.app_root == meipass
    assert p.config_example == meipass / "config" / "langgraph-config.example.yaml"


def test_app_root_is_repo_when_not_frozen(_isolate):
    p = paths.instance_paths()
    # the repo root holds pyproject.toml + the config bundle dir
    assert (p.app_root / "pyproject.toml").exists()
    assert p.bundle_dir == p.app_root / "config"


def test_singleton_is_cached_and_resettable(monkeypatch, _isolate):
    first = paths.instance_paths()
    assert paths.instance_paths() is first  # cached
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "bob")
    assert paths.instance_paths() is first  # still cached — env change not seen yet
    paths.reset_instance_paths()
    second = paths.instance_paths()
    assert second is not first
    assert second.instance_id == "bob"


def test_id_is_path_safe(monkeypatch, _isolate):
    box = _isolate
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "../../etc/evil")
    paths.reset_instance_paths()
    p = paths.instance_paths()
    assert "/" not in p.instance_id
    assert p.instance_root.parent == box  # stays a single leaf under the box


def test_explain_shape(_isolate):
    p = paths.instance_paths()
    ex = p.explain()
    assert ex["instance_id"] == "default"
    assert set(ex) == {"instance_id", "box_root", "instance_root", "app_root", "paths"}
    assert ex["paths"]["config_yaml"].endswith("default/config/langgraph-config.yaml")
