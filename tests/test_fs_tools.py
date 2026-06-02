"""Tests for the fenced multi-project filesystem toolset (ADR 0007)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from tools.fs_tools import Project, ProjectRegistry, build_fs_tools


@dataclass
class _Cfg:
    filesystem_enabled: bool = True
    filesystem_allow_run: bool = False
    filesystem_projects: list = field(default_factory=list)


@pytest.fixture
def workspace(tmp_path):
    a = tmp_path / "projA"
    (a / "src").mkdir(parents=True)
    (a / "src" / "main.py").write_text("print('hello')\nTODO: fix\n")
    (a / "README.md").write_text("# A")
    b = tmp_path / "projB"
    b.mkdir()
    (b / "notes.txt").write_text("read only")
    return tmp_path, a, b


# ── registry / fence ──────────────────────────────────────────────────────────


def test_registry_resolves_within_root(workspace):
    _, a, _ = workspace
    reg = ProjectRegistry([Project("a", a, write=True)])
    assert reg.resolve("a", "src/main.py") == a / "src" / "main.py"
    assert reg.resolve("a", ".") == a


def test_registry_rejects_escape(workspace):
    _, a, _ = workspace
    reg = ProjectRegistry([Project("a", a)])
    for bad in ["../etc/passwd", "../../x", "/etc/passwd", "~/secrets"]:
        with pytest.raises(ValueError):
            reg.resolve("a", bad)


def test_registry_unknown_project(workspace):
    _, a, _ = workspace
    reg = ProjectRegistry([Project("a", a)])
    with pytest.raises(ValueError, match="unknown project"):
        reg.resolve("nope", ".")


# ── build_fs_tools wiring ──────────────────────────────────────────────────────


def _tools(cfg):
    return {t.name: t for t in build_fs_tools(cfg)}


def test_no_tools_without_valid_projects():
    assert build_fs_tools(_Cfg(filesystem_projects=[])) == []
    # Nonexistent path → skipped → no tools.
    assert build_fs_tools(_Cfg(filesystem_projects=[{"name": "x", "path": "/nope/zzz"}])) == []


def test_read_list_find_search(workspace):
    _, a, _ = workspace
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}]))
    assert "hello" in t["read_file"].invoke({"project": "a", "path": "src/main.py"})
    assert "README.md" in t["list_dir"].invoke({"project": "a", "path": "."})
    assert "src/main.py" in t["find_files"].invoke({"project": "a", "pattern": "**/*.py"})
    hit = t["search_files"].invoke({"project": "a", "query": "TODO"})
    assert "main.py" in hit and "TODO" in hit


def test_read_file_escape_is_refused(workspace):
    _, a, _ = workspace
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a)}]))
    out = t["read_file"].invoke({"project": "a", "path": "../projB/notes.txt"})
    assert out.startswith("Error:") and "escape" in out


def test_write_and_edit_in_rw_project(workspace):
    _, a, _ = workspace
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}]))
    assert "Created" in t["write_file"].invoke({"project": "a", "path": "new.txt", "content": "v1"})
    assert (a / "new.txt").read_text() == "v1"
    assert "Edited" in t["edit_file"].invoke({"project": "a", "path": "new.txt", "old": "v1", "new": "v2"})
    assert (a / "new.txt").read_text() == "v2"


def test_write_blocked_in_readonly_project(workspace):
    _, _, b = workspace
    t = _tools(_Cfg(filesystem_projects=[{"name": "b", "path": str(b), "write": False}]))
    out = t["write_file"].invoke({"project": "b", "path": "x.txt", "content": "nope"})
    assert out.startswith("Error:") and "read-only" in out
    assert not (b / "x.txt").exists()


def test_edit_requires_unique_old(workspace):
    _, a, _ = workspace
    (a / "dup.txt").write_text("x\nx\n")
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}]))
    out = t["edit_file"].invoke({"project": "a", "path": "dup.txt", "old": "x", "new": "y"})
    assert out.startswith("Error:") and "not unique" in out


# ── run_command gating ─────────────────────────────────────────────────────────


def test_run_command_absent_unless_allowed(workspace):
    _, a, _ = workspace
    base = {"name": "a", "path": str(a), "write": True}
    assert "run_command" not in _tools(_Cfg(filesystem_projects=[base], filesystem_allow_run=False))
    assert "run_command" in _tools(_Cfg(filesystem_projects=[base], filesystem_allow_run=True))


def test_run_command_executes_in_project_cwd(workspace):
    _, a, _ = workspace
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a)}], filesystem_allow_run=True))
    out = asyncio.run(t["run_command"].ainvoke({"project": "a", "command": "ls"}))
    assert "README.md" in out


# ── config round-trip ──────────────────────────────────────────────────────────


def test_config_parses_filesystem(tmp_path):
    from graph.config import LangGraphConfig

    p = tmp_path / "c.yaml"
    p.write_text(
        "filesystem:\n"
        "  enabled: true\n"
        "  allow_run: true\n"
        "  projects:\n"
        "    - {name: orbis, path: /tmp, write: false}\n"
    )
    cfg = LangGraphConfig.from_yaml(p)
    assert cfg.filesystem_enabled is True
    assert cfg.filesystem_allow_run is True
    assert cfg.filesystem_projects[0]["name"] == "orbis"


def test_config_filesystem_default_on_fenced_workspace(tmp_path, monkeypatch):
    """Filesystem is ON by default (fenced to a workspace); run_command stays opt-in."""
    from graph.config import LangGraphConfig

    cfg = LangGraphConfig()
    assert cfg.filesystem_enabled is True
    # run_command (arbitrary argv, unsandboxed like execute_code) stays opt-in.
    assert cfg.filesystem_allow_run is False
    # No explicit projects → a single default `workspace` project, fenced + writable.
    monkeypatch.setenv("PROTOAGENT_WORKSPACE", str(tmp_path / "ws"))
    projects = cfg.effective_filesystem_projects(create=True)
    assert len(projects) == 1
    assert projects[0]["name"] == "workspace" and projects[0]["write"] is True
    assert (tmp_path / "ws").is_dir()  # created


def test_effective_projects_explicit_wins_and_disabled_is_empty(tmp_path):
    from graph.config import LangGraphConfig

    explicit = [{"name": "repo", "path": str(tmp_path), "write": False}]
    cfg = LangGraphConfig(filesystem_projects=explicit)
    assert cfg.effective_filesystem_projects() == explicit  # explicit registry wins
    off = LangGraphConfig(filesystem_enabled=False)
    assert off.effective_filesystem_projects() == []  # disabled → no projects
