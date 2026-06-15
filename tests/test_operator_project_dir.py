"""bd-2mf: the setup wizard's project directory is authoritative — it persists as
``operator.project_dir`` and ``server._resolve_operator_project_root`` honors it
(env > configured-and-exists > default), so the console's beads/notes actually
operate in the chosen directory."""

from __future__ import annotations

from types import SimpleNamespace

import server
from graph.config import LangGraphConfig


def test_operator_project_dir_loads_from_dict():
    cfg = LangGraphConfig.from_dict({"operator": {"project_dir": "/tmp/whatever", "allowed_dirs": []}})
    assert cfg.operator_project_dir == "/tmp/whatever"


def test_operator_project_dir_defaults_blank():
    cfg = LangGraphConfig.from_dict({})
    assert cfg.operator_project_dir == ""


def test_resolver_env_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr(server.STATE, "graph_config", SimpleNamespace(operator_project_dir="/some/other"))
    assert server._resolve_operator_project_root() == str(tmp_path.resolve())


def test_resolver_honors_configured_existing_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("PROTOAGENT_PROJECT_DIR", raising=False)
    monkeypatch.setattr(server.STATE, "graph_config", SimpleNamespace(operator_project_dir=str(tmp_path)))
    assert server._resolve_operator_project_root() == str(tmp_path.resolve())


def test_resolver_falls_back_when_configured_dir_missing(tmp_path, monkeypatch):
    missing = tmp_path / "does-not-exist"
    monkeypatch.delenv("PROTOAGENT_PROJECT_DIR", raising=False)
    monkeypatch.setattr(server.STATE, "graph_config", SimpleNamespace(operator_project_dir=str(missing)))
    # A configured-but-missing dir must NOT be returned (it would break every
    # beads/notes call) — fall through to the default instead.
    assert server._resolve_operator_project_root() != str(missing)


def test_resolver_blank_config_uses_default(monkeypatch):
    monkeypatch.delenv("PROTOAGENT_PROJECT_DIR", raising=False)
    monkeypatch.setattr(server.STATE, "graph_config", SimpleNamespace(operator_project_dir=""))
    # Default (source checkout) is the bundle root — a real, existing directory.
    import os

    assert os.path.isdir(server._resolve_operator_project_root())
