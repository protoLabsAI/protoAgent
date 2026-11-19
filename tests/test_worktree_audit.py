"""scripts/worktree_audit.py: only worktrees that are provably finished are marked for removal."""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.platform_sensitive


def _load():
    spec = importlib.util.spec_from_file_location("worktree_audit", ROOT / "scripts" / "worktree_audit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def _commit(repo, name):
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", name)


@pytest.fixture
def world(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    dev = tmp_path / "dev"
    dev.mkdir()
    main = dev / "repo"
    subprocess.run(["git", "clone", "-q", str(origin), str(main)], check=True, capture_output=True)
    _git(main, "checkout", "-qb", "main")
    _commit(main, "base")
    _git(main, "push", "-q", "-u", "origin", "main")
    _git(main, "remote", "set-head", "origin", "main")

    def wt(name, branch=None, *, detach=False):
        path = dev / name
        if detach:
            _git(main, "worktree", "add", "-q", "--detach", str(path))
        else:
            _git(main, "worktree", "add", "-q", "-b", branch, str(path))
        return path

    merged = wt("wt-merged", "feat/merged")
    _commit(merged, "m")
    _git(main, "merge", "-q", "--ff-only", "feat/merged")
    _git(main, "push", "-q", "origin", "main")

    gone = wt("wt-gone", "feat/gone")
    _commit(gone, "g")
    _git(gone, "push", "-q", "-u", "origin", "feat/gone")
    _git(main, "push", "-q", "origin", "--delete", "feat/gone")  # squash-merged + auto-deleted

    local = wt("wt-never-pushed", "feat/local")
    _commit(local, "l")  # unmerged and never pushed: must be kept

    detached = wt("wt-detached", detach=True)
    _commit(detached, "d")  # detached with an unmerged commit: must be kept

    dirty = wt("wt-dirty", "feat/dirty")
    _git(dirty, "push", "-q", "-u", "origin", "feat/dirty")
    _git(main, "push", "-q", "origin", "--delete", "feat/dirty")
    (dirty / "base").write_text("edited", encoding="utf-8")  # tracked change: must be kept
    return dev


def test_only_provably_finished_worktrees_are_marked(world):
    rows = {pathlib.Path(r["path"]).name: r for r in _load().audit(world)}
    assert rows["wt-merged"]["finished"] is True
    assert rows["wt-gone"]["finished"] is True
    assert rows["wt-never-pushed"]["finished"] is False
    assert rows["wt-detached"]["finished"] is False
    assert rows["wt-dirty"]["finished"] is False


def test_apply_removes_finished_and_keeps_branches(world, capsys):
    mod = _load()
    assert mod.main(["--root", str(world), "--apply"]) == 0
    assert not (world / "wt-merged").exists() and not (world / "wt-gone").exists()
    for kept in ("wt-never-pushed", "wt-detached", "wt-dirty"):
        assert (world / kept).exists()
    branches = subprocess.run(["git", "-C", str(world / "repo"), "branch", "--list"], capture_output=True, text=True).stdout
    assert "feat/merged" in branches and "feat/gone" in branches  # removing a worktree never deletes its branch
