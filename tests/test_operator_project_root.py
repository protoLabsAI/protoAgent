"""The operator console's default project root must resolve to a real, stable
dir — never PyInstaller's ephemeral _MEIxxxx onefile extraction dir (which broke
notes/tasks in the frozen desktop sidecar with "project_path does not exist")."""

from __future__ import annotations

import sys
from pathlib import Path

import server


def test_dev_checkout_uses_repo_root(monkeypatch):
    monkeypatch.delenv("PROTOAGENT_PROJECT_DIR", raising=False)
    monkeypatch.delenv("PROTOAGENT_HOME", raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert server._resolve_operator_project_root() == str(Path("server.py").resolve().parent)


def test_explicit_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("PROTOAGENT_PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("PROTOAGENT_HOME", "/some/other/dir")
    assert server._resolve_operator_project_root() == str(tmp_path.resolve())


def test_frozen_falls_back_to_home(monkeypatch, tmp_path):
    monkeypatch.delenv("PROTOAGENT_PROJECT_DIR", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path))
    root = server._resolve_operator_project_root()
    assert root == str(tmp_path.resolve())
    assert "_MEI" not in root  # never the PyInstaller temp dir


def test_frozen_without_home_uses_home_dir(monkeypatch):
    monkeypatch.delenv("PROTOAGENT_PROJECT_DIR", raising=False)
    monkeypatch.delenv("PROTOAGENT_HOME", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert server._resolve_operator_project_root() == str(Path.home().resolve())
