"""Workspaces (ADR 0041) — create / list / run / remove."""

from __future__ import annotations

import pytest
import yaml

from graph.workspaces import manager


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path / "ws"))
    return tmp_path / "ws"


def test_new_ls_run_rm(root):
    s = manager.create("alpha")
    assert s["id"] == "alpha" and s["port"] == 7871
    ws = root / "alpha"
    assert (ws / "langgraph-config.yaml").exists() and (ws / "workspace.yaml").exists()
    cfg = yaml.safe_load((ws / "langgraph-config.yaml").read_text())
    assert cfg["instance"]["id"] == "alpha" and cfg["identity"]["name"] == "alpha"

    assert [w["name"] for w in manager.list_workspaces()] == ["alpha"]

    env, argv = manager.run_exec("alpha", [])
    assert env["PROTOAGENT_CONFIG_DIR"] == str(ws)
    assert env["PROTOAGENT_INSTANCE"] == "alpha"
    assert "--port" in argv and "7871" in argv

    assert manager.create("beta")["port"] == 7872  # next free port
    with pytest.raises(manager.WorkspaceError):
        manager.create("alpha")  # collision

    assert "workspace" in manager.remove("alpha")["removed"] and not ws.exists()


def test_from_config_clones_and_restamps(root, tmp_path):
    src = tmp_path / "src"; src.mkdir()
    (src / "langgraph-config.yaml").write_text(
        "identity: { name: orig }\ninstance: { id: orig }\nmodel: { name: keep-me }\n")
    (src / "secrets.yaml").write_text("model: { api_key: k }\n")
    manager.create("clone", from_config=str(src), shared_skills=True)
    cfg = yaml.safe_load((root / "clone" / "langgraph-config.yaml").read_text())
    assert cfg["identity"]["name"] == "clone" and cfg["instance"]["id"] == "clone"
    assert cfg["model"]["name"] == "keep-me"          # other config preserved
    assert cfg["skills"]["shared"] is True
    assert (root / "clone" / "secrets.yaml").exists()  # secrets cloned too


def test_bad_name_rejected(root):
    with pytest.raises(manager.WorkspaceError):
        manager.create("bad name")
