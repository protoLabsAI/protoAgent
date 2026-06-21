"""bd-2mf: the setup wizard's project directory is authoritative — it persists as
``operator.project_dir`` and ``server._resolve_operator_project_root`` honors it
(env > configured-and-exists > default), so the console's tasks/notes actually
operate in the chosen directory."""

from __future__ import annotations

from types import SimpleNamespace

import server
from graph.config import LangGraphConfig
from operator_api.console_handlers import _operator_allowed_dirs


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
    # tasks/notes call) — fall through to the default instead.
    assert server._resolve_operator_project_root() != str(missing)


def test_resolver_blank_config_uses_default(monkeypatch):
    monkeypatch.delenv("PROTOAGENT_PROJECT_DIR", raising=False)
    monkeypatch.setattr(server.STATE, "graph_config", SimpleNamespace(operator_project_dir=""))
    # Default (source checkout) is the bundle root — a real, existing directory.
    import os

    assert os.path.isdir(server._resolve_operator_project_root())


def test_allowed_dirs_deduplicates_project_root(tmp_path, monkeypatch):
    """bd-a7f: project root in operator_allowed_dirs should appear only once."""
    monkeypatch.delenv("PROTOAGENT_PROJECT_DIR", raising=False)
    project = str(tmp_path.resolve())
    monkeypatch.setattr(
        server.STATE,
        "graph_config",
        SimpleNamespace(
            operator_project_dir=project,
            operator_allowed_dirs=[project, "/other/dir"],
        ),
    )
    dirs = _operator_allowed_dirs()
    assert dirs == [project, "/other/dir"]


def test_allowed_dirs_preserves_order_on_dedup(tmp_path, monkeypatch):
    """bd-a7f: first-seen order is preserved when deduping."""
    monkeypatch.delenv("PROTOAGENT_PROJECT_DIR", raising=False)
    project = str(tmp_path.resolve())
    monkeypatch.setattr(
        server.STATE,
        "graph_config",
        SimpleNamespace(
            operator_project_dir=project,
            operator_allowed_dirs=["/alpha", project, "/beta", project],
        ),
    )
    dirs = _operator_allowed_dirs()
    assert dirs == [project, "/alpha", "/beta"]


def test_allowed_dirs_unchanged_without_duplicates(tmp_path, monkeypatch):
    """bd-a7f: no-duplicate lists pass through unchanged."""
    monkeypatch.delenv("PROTOAGENT_PROJECT_DIR", raising=False)
    project = str(tmp_path.resolve())
    monkeypatch.setattr(
        server.STATE,
        "graph_config",
        SimpleNamespace(
            operator_project_dir=project,
            operator_allowed_dirs=["/alpha", "/beta"],
        ),
    )
    dirs = _operator_allowed_dirs()
    assert dirs == [project, "/alpha", "/beta"]
