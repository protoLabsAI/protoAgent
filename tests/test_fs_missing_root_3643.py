"""A registered work folder whose root is MISSING (#3643).

It used to be dropped from the fence at graph build; when that emptied the registry
``build_fs_tools`` returned ``[]`` and every fs tool unbound for the session. Re-cloning
the folder didn't bring them back, because nothing re-checked a dropped root. Now:

- the missing project is skipped, the others keep working;
- a call into the missing project says so by name (the model is told not to work around it);
- the tools stay bound even when every root is missing, and a root that comes back is
  reachable from the SAME tool closures — no new session, no graph rebuild;
- the fence is unchanged: a missing root resolves to nothing, and a write into it never
  recreates it.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field

import pytest

from tools.fs_tools import build_fs_tools, missing_projects_warning

pytestmark = pytest.mark.platform_sensitive


@dataclass
class _Cfg:
    filesystem_enabled: bool = True
    filesystem_allow_run: bool = False
    filesystem_run_requires_approval: bool = True
    filesystem_bypass_allowed: bool = True
    filesystem_projects: list = field(default_factory=list)
    tools_memoize_reads_enabled: bool = False
    filesystem_run_command_env_passthrough: list = field(default_factory=list)


@pytest.fixture
def two_projects(tmp_path):
    a = (tmp_path / "alpha").resolve()
    b = (tmp_path / "beta").resolve()
    for root, text in ((a, "alpha file"), (b, "beta file")):
        (root / "src").mkdir(parents=True)
        (root / "src" / "f.txt").write_text(text)
    cfg = _Cfg(
        filesystem_projects=[
            {"name": "alpha", "path": str(a), "write": True},
            {"name": "beta", "path": str(b), "write": True},
        ]
    )
    return cfg, a, b


def _tools(cfg):
    return {t.name: t for t in build_fs_tools(cfg)}


def _read(t, project, path="src/f.txt"):
    return t["read_file"].invoke({"project": project, "path": path})


def test_missing_root_at_build_keeps_the_other_projects(two_projects, caplog):
    cfg, a, b = two_projects
    shutil.rmtree(b)
    with caplog.at_level(logging.WARNING, logger="protoagent.fs"):
        t = _tools(cfg)
    assert "alpha file" in _read(t, "alpha")
    out = _read(t, "beta")
    assert out.startswith("Error:")
    assert "'beta'" in out and "missing" in out and str(b) in out
    assert "execute_code" in out  # told not to work around the fence
    listing = t["list_projects"].invoke({})
    assert "alpha" in listing and "beta" in listing and "missing" in listing
    # WARNING naming the project and the path.
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("'beta'" in m and str(b) in m for m in msgs), msgs


def test_all_roots_missing_still_binds_and_rebinds_when_back(two_projects):
    """The #2251 'empty registry unbinds everything' case: the tools stay bound and the
    SAME closures reach the folder the moment it's back (e.g. re-cloned by onboard_project)."""
    cfg, a, b = two_projects
    shutil.rmtree(a)
    shutil.rmtree(b)
    t = _tools(cfg)
    assert {"read_file", "list_dir", "search_files", "list_projects"} <= set(t)
    assert "missing" in _read(t, "alpha")

    (a / "src").mkdir(parents=True)
    (a / "src" / "f.txt").write_text("alpha re-cloned")
    assert "alpha re-cloned" in _read(t, "alpha")
    assert "missing" in _read(t, "beta")  # still gone, still named


def test_root_deleted_mid_session_then_restored(two_projects):
    cfg, a, b = two_projects
    t = _tools(cfg)
    assert "beta file" in _read(t, "beta")
    shutil.rmtree(b)
    out = _read(t, "beta")
    assert "missing" in out and "'beta'" in out
    assert "alpha file" in _read(t, "alpha")
    (b / "src").mkdir(parents=True)
    (b / "src" / "f.txt").write_text("beta again")
    assert "beta again" in _read(t, "beta")


def test_missing_root_never_resolves_and_is_never_recreated(two_projects):
    cfg, a, b = two_projects
    shutil.rmtree(b)
    t = _tools(cfg)
    out = t["write_file"].invoke({"project": "beta", "path": "new.txt", "content": "x"})
    assert out.startswith("Error:") and "missing" in out
    assert not b.exists()  # a write must not mkdir the vanished root back into being
    for bad in ("../alpha/src/f.txt", "/etc/passwd", "~/x", "."):
        assert _read(t, "beta", bad).startswith("Error:")


def test_fence_still_refuses_escapes_after_a_root_comes_back(two_projects):
    cfg, a, b = two_projects
    shutil.rmtree(b)
    t = _tools(cfg)
    b.mkdir()
    (b / "ok.txt").write_text("ok")
    assert "ok" in _read(t, "beta", "ok.txt")
    for bad in ("../alpha/src/f.txt", "../../etc/passwd", "/etc/passwd", "~/secrets"):
        assert _read(t, "beta", bad).startswith("Error:"), bad
    assert "Error:" in t["list_dir"].invoke({"project": "beta", "path": ".."})


def test_unknown_project_is_still_unknown(two_projects):
    cfg, a, b = two_projects
    shutil.rmtree(b)
    t = _tools(cfg)
    assert "unknown project" in _read(t, "gamma")


def test_runtime_warning_names_missing_project_and_clears(two_projects):
    cfg, a, b = two_projects
    assert missing_projects_warning(cfg) is None
    shutil.rmtree(b)
    warn = missing_projects_warning(cfg)
    assert warn and "beta" in warn and str(b) in warn and "alpha" not in warn
    b.mkdir()
    assert missing_projects_warning(cfg) is None


def test_runtime_warning_quiet_when_fs_disabled(two_projects):
    cfg, a, b = two_projects
    shutil.rmtree(b)
    cfg.filesystem_enabled = False
    assert missing_projects_warning(cfg) is None


async def test_runtime_status_carries_missing_folder_warning(two_projects, monkeypatch):
    import runtime.state as rs
    from operator_api import console_handlers as ch

    cfg, a, b = two_projects
    monkeypatch.setattr(rs.STATE, "graph_config", cfg, raising=False)
    shutil.rmtree(b)
    status = await ch._operator_runtime_status()
    assert any("beta" in w and str(b) in w for w in status["warnings"]), status["warnings"]
    b.mkdir()
    status = await ch._operator_runtime_status()
    assert not any(str(b) in w for w in status["warnings"])
