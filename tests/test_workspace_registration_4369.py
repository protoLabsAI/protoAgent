"""Registration preserves the tool-owned scratch workspace without widening overrides."""

import pytest

from graph.config import LangGraphConfig
from graph.plugins.host import HOST
from infra.paths import workspace_dir
from tools.fs_tools import _registry_from_config, build_fs_tools
from tools.onboard_tools import build_onboard_tools

pytestmark = pytest.mark.platform_sensitive


@pytest.fixture
def isolated_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path / "state"))
    return workspace_dir()


@pytest.mark.asyncio
async def test_registration_retains_workspace(tmp_path, monkeypatch, isolated_workspace):
    reference = tmp_path / "reference"
    reference.mkdir()
    config = LangGraphConfig(onboarding_root=str(tmp_path))

    def apply(updates):
        config.projects = updates["projects"]
        return True, []

    monkeypatch.setattr(HOST, "config", lambda: config)
    monkeypatch.setattr(HOST, "apply_settings", apply)
    tool = next(t for t in build_onboard_tools(config) if t.name == "register_local_project")
    result = await tool.ainvoke({"path": str(reference), "name": "reference", "write": False})
    assert "implicit workspace remains available read-write" in result
    registry = _registry_from_config(config)
    assert registry.names() == ["reference", "workspace"]
    assert not registry.get("reference").write
    assert registry.get("workspace").write
    listing = next(t for t in build_fs_tools(config) if t.name == "list_projects").invoke({})
    assert "reference  [ro]" in listing
    assert "workspace  [rw]" in listing
    isolated_workspace.rmdir()
    assert registry.resolve("workspace", "a.txt") == isolated_workspace / "a.txt"
    assert isolated_workspace.is_dir()
    reference.rmdir()
    with pytest.raises(ValueError, match="missing"):
        registry.resolve("reference", "a.txt")
    assert not reference.exists()


@pytest.mark.parametrize("explicit", [False, True])
def test_configured_workspace_never_recreated(isolated_workspace, explicit):
    isolated_workspace.mkdir(parents=True)
    entries = [{"name": "workspace", "path": str(isolated_workspace), "write": False}]
    config = LangGraphConfig(**{"filesystem_projects" if explicit else "projects": entries})
    assert config.effective_filesystem_projects() == entries
    registry = _registry_from_config(config)
    assert not registry.get("workspace").write
    assert not registry.get("workspace").recreate_root
    isolated_workspace.rmdir()
    with pytest.raises(ValueError, match="missing"):
        registry.resolve("workspace", "a.txt")
    _registry_from_config(config)
    assert not isolated_workspace.exists()


@pytest.mark.parametrize("override", [{"fs": False}, {"fs": "false"}, {"path": "relative"}, {"path": ""}])
def test_workspace_opt_out_blocks_synthesis(tmp_path, isolated_workspace, override):
    entries = [
        {"name": "reference", "path": str(tmp_path), "write": False},
        {"name": " workspace ", "path": str(isolated_workspace), **override},
    ]
    config = LangGraphConfig(projects=entries)
    assert [e["name"] for e in config.effective_filesystem_projects(create=True)] == ["reference"]
    assert not isolated_workspace.exists()


@pytest.mark.parametrize("enabled,fs", [(False, True), (True, False), (True, "false")])
def test_disabled_and_all_opt_out_stay_empty(tmp_path, isolated_workspace, enabled, fs):
    config = LangGraphConfig(filesystem_enabled=enabled, projects=[{"name": "ref", "path": str(tmp_path), "fs": fs}])
    assert config.effective_filesystem_projects(create=True) == []
    assert not isolated_workspace.exists()


def test_explicit_list_wins_unchanged(tmp_path, isolated_workspace):
    entries = [{"name": "reference", "path": str(tmp_path), "write": False}]
    config = LangGraphConfig(filesystem_projects=entries, projects=[{"name": "other", "path": str(tmp_path)}])
    assert config.effective_filesystem_projects(create=True) is entries
    assert not isolated_workspace.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
async def test_registration_does_not_claim_overridden_workspace(tmp_path, monkeypatch, isolated_workspace, explicit):
    reference = tmp_path / "reference"
    reference.mkdir()
    entries = [{"name": "workspace", "path": str(isolated_workspace), "write": False}]
    config = LangGraphConfig(
        onboarding_root=str(tmp_path),
        **{"filesystem_projects" if explicit else "projects": entries},
    )
    monkeypatch.setattr(HOST, "config", lambda: config)
    monkeypatch.setattr(HOST, "apply_settings", lambda updates: (True, []))
    tool = next(t for t in build_onboard_tools(config) if t.name == "register_local_project")
    result = await tool.ainvoke({"path": str(reference), "name": "reference", "write": False})
    assert "Registered reference" in result
    assert "implicit workspace remains" not in result
    assert not isolated_workspace.exists()


def test_read_only_projection_does_not_create_workspace(tmp_path, isolated_workspace):
    config = LangGraphConfig(projects=[{"name": "reference", "path": str(tmp_path), "write": False}])
    assert [e["name"] for e in config.effective_filesystem_projects()] == ["reference", "workspace"]
    _registry_from_config(config, create=False)
    assert not isolated_workspace.exists()
