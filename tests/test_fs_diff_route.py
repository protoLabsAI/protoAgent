"""``GET /api/fs/diff`` — the code pane's working-tree diff (ADR 0112), against REAL git.

No git mocks: every case builds a real repository in a temp dir, because the property
that matters — "opening the Diff tab executes nothing the repository asks for" — is
only meaningful against the real binary (a mocked subprocess would pass forever).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from graph.config import LangGraphConfig
from operator_api.browse_routes import register_browse_routes
from runtime.state import STATE
from tools import git_read
from tools.git_read import working_tree_diff

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs the git binary")

@pytest.fixture(autouse=True)
def _unwired_host_config(monkeypatch):
    """The fs registry prefers the LIVE ``HOST.config`` seam over the config it was given;
    pin it unwired so a test elsewhere that wired it can't swap this module's projects."""
    from graph.plugins.host import HOST

    monkeypatch.setattr(HOST, "config", None)


_ENV = {
    **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=_ENV, check=True, capture_output=True, text=True
    ).stdout


def _repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_bytes(b"one\ntwo\nthree\n")
    (root / "gone.txt").write_bytes(b"bye\n")
    (root / "old_name.txt").write_bytes(b"".join(f"line {i}\n".encode() for i in range(20)))
    (root / ".env").write_bytes(b"API_KEY=hunter2\n")
    (root / "blob.bin").write_bytes(b"\x00\x01\x02")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _client(monkeypatch, root: Path, name: str = "repo") -> TestClient:
    cfg = LangGraphConfig(filesystem_projects=[{"name": name, "path": str(root)}])
    monkeypatch.setattr(STATE, "graph_config", cfg, raising=False)
    app = FastAPI()
    register_browse_routes(app)
    return TestClient(app)


def _diff(client, project="repo"):
    return client.get("/api/fs/diff", params={"project": project})


def test_tracked_untracked_renamed_deleted_and_secret(tmp_path, monkeypatch):
    root = _repo(tmp_path / "repo")
    (root / "src" / "app.py").write_bytes(b"one\nTWO\nthree\nfour\n")
    (root / "gone.txt").unlink()
    _git(root, "mv", "old_name.txt", "new_name.txt")
    (root / ".env").write_bytes(b"API_KEY=hunter3\n")
    (root / "blob.bin").write_bytes(b"\x00\x01\x02\x03")
    (root / "notes").mkdir()
    (root / "notes" / "todo.md").write_bytes(b"- a\n- b")
    (root / "new.pem").write_bytes(b"-----BEGIN PRIVATE KEY-----\nsekrit\n")
    (root / "pic.png").write_bytes(b"\x89PNG\x00\x00")

    r = _diff(_client(monkeypatch, root))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_git"] is True and body["branch"] == "main" and len(body["head"]) >= 40
    assert body["truncated"] is False
    files = {f["path"]: f for f in body["files"]}

    app = files["src/app.py"]
    assert (app["status"], app["additions"], app["deletions"], app["denied"]) == ("M", 2, 1, False)
    assert files["gone.txt"]["status"] == "D" and files["gone.txt"]["deletions"] == 1
    assert files["new_name.txt"]["status"] == "R" and files["new_name.txt"]["old_path"] == "old_name.txt"
    assert files["blob.bin"]["binary"] is True
    assert files["notes/todo.md"] == {
        "path": "notes/todo.md",
        "status": "?",
        "additions": 2,
        "deletions": 0,
        "binary": False,
        "denied": False,
    }
    assert files["pic.png"]["binary"] is True and files["pic.png"]["status"] == "?"
    # Secrets are LISTED (the operator should know they changed) but never shown.
    assert files[".env"]["denied"] is True and files[".env"]["status"] == "M"
    assert files["new.pem"]["denied"] is True and files["new.pem"]["status"] == "?"

    patch = body["patch"]
    assert "+TWO" in patch and "-two" in patch
    assert "diff --git a/notes/todo.md b/notes/todo.md\nnew file mode 100644" in patch
    assert "+- b\n\\ No newline at end of file" in patch
    for leaked in ("hunter2", "hunter3", "sekrit", "PRIVATE KEY"):
        assert leaked not in patch
        assert leaked not in r.text


def test_not_a_git_repo(tmp_path, monkeypatch):
    root = tmp_path / "plain"
    root.mkdir()
    (root / "a.txt").write_text("x", encoding="utf-8")
    # Keep git from discovering an enclosing repository above tmp_path.
    monkeypatch.setattr(git_read, "git_env", lambda: {**_ENV, "GIT_CEILING_DIRECTORIES": str(tmp_path)})
    body = _diff(_client(monkeypatch, root)).json()
    assert body == {"project": "repo", "is_git": False, "files": [], "patch": ""}


def test_unknown_project_is_bad_path(tmp_path, monkeypatch):
    root = _repo(tmp_path / "repo")
    r = _diff(_client(monkeypatch, root), project="nope")
    assert r.status_code == 400 and r.json()["detail"]["code"] == "bad_path"


def test_project_that_is_a_repo_subdirectory_stays_fenced(tmp_path, monkeypatch):
    """The fence is the PROJECT root, not the repository: a sibling's changes and
    untracked files must not appear, and paths are project-relative."""
    top = _repo(tmp_path / "top")
    (top / "src" / "app.py").write_bytes(b"changed\n")
    (top / "gone.txt").write_bytes(b"sibling change\n")
    (top / "src" / "fresh.py").write_bytes(b"x = 1\n")
    (top / "untracked_sibling.txt").write_bytes(b"not in the fence\n")
    body = _diff(_client(monkeypatch, top / "src")).json()
    assert body["is_git"] is True
    assert sorted(f["path"] for f in body["files"]) == ["app.py", "fresh.py"]
    assert "sibling change" not in body["patch"] and "not in the fence" not in body["patch"]


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_untracked_symlink_is_never_followed(tmp_path, monkeypatch):
    root = _repo(tmp_path / "repo")
    (tmp_path / "outside.txt").write_bytes(b"outside the fence\n")
    (root / "link.txt").symlink_to(tmp_path / "outside.txt")
    body = _diff(_client(monkeypatch, root)).json()
    files = {f["path"]: f for f in body["files"]}
    assert files["link.txt"]["status"] == "?"
    assert "outside the fence" not in body["patch"]
    assert "new file mode 120000" in body["patch"]


def test_unborn_head(tmp_path, monkeypatch):
    root = tmp_path / "fresh"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "a.txt").write_bytes(b"hi\n")
    _git(root, "add", "a.txt")
    body = _diff(_client(monkeypatch, root)).json()
    assert body["is_git"] is True and body["head"] is None and body["branch"] == "main"
    assert [(f["path"], f["status"], f["additions"]) for f in body["files"]] == [("a.txt", "A", 1)]


def test_patch_cap_truncates_on_a_line(tmp_path, monkeypatch):
    root = _repo(tmp_path / "repo")
    (root / "src" / "app.py").write_bytes(b"".join(f"new line {i}\n".encode() for i in range(500)))
    monkeypatch.setattr(git_read, "MAX_PATCH_BYTES", 1000)
    body = _diff(_client(monkeypatch, root)).json()
    assert body["truncated"] is True
    assert len(body["patch"].encode()) <= 1000 and body["patch"].endswith("\n")


def test_large_untracked_file_content_omitted(tmp_path, monkeypatch):
    root = _repo(tmp_path / "repo")
    monkeypatch.setattr(git_read, "MAX_UNTRACKED_BYTES", 10)
    (root / "big.txt").write_bytes(b"0123456789abcdef\n")
    body = _diff(_client(monkeypatch, root)).json()
    assert "0123456789" not in body["patch"]
    assert any(f["path"] == "big.txt" for f in body["files"])


def test_timeout_is_504(tmp_path, monkeypatch):
    root = _repo(tmp_path / "repo")
    # The route calls working_tree_diff(root) with the 10s default; force a spent deadline.
    real = git_read.working_tree_diff
    monkeypatch.setattr(git_read, "working_tree_diff", lambda r: real(r, timeout=0.0))
    r = _diff(_client(monkeypatch, root))
    assert r.status_code == 504 and r.json()["detail"]["code"] == "timeout"


def test_inherited_git_env_is_scrubbed(tmp_path, monkeypatch):
    """A server started from inside a hook / another worktree inherits GIT_DIR etc. —
    those must not redirect the read to a different repository."""
    root = _repo(tmp_path / "repo")
    other = _repo(tmp_path / "other")
    (other / "src" / "app.py").write_bytes(b"OTHER REPO CHANGE\n")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other / ".git" / "index"))
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", "false")
    d = working_tree_diff(root)
    assert d.is_git and d.files == [] and "OTHER REPO" not in d.patch


# ── hostile repository: nothing may execute ──────────────────────────────────


@pytest.mark.skipif(os.name == "nt", reason="the marker commands and hook are POSIX sh")
def test_hostile_repo_config_and_attributes_execute_nothing(tmp_path, monkeypatch):
    """Every git config/attribute vector that can run a program during status/diff is
    armed to create a marker file. Sanity-check first that PLAIN git really does run
    them here (so the test isn't vacuous), then prove the hardened read runs none."""
    root = _repo(tmp_path / "repo")
    markers = tmp_path / "markers"
    markers.mkdir()

    def cmd(tag: str, then: str = "") -> str:
        target = (markers / tag).as_posix()
        return f"touch '{target}'{'; ' + then if then else ''}"

    (root / ".gitattributes").write_bytes(b"* diff=evil filter=evil\n")
    _git(root, "config", "diff.evil.command", cmd("diff-command"))
    _git(root, "config", "diff.evil.textconv", cmd("textconv", "cat"))
    _git(root, "config", "diff.external", cmd("diff-external"))
    _git(root, "config", "filter.evil.clean", cmd("filter-clean", "cat"))
    _git(root, "config", "filter.evil.smudge", cmd("filter-smudge", "cat"))
    _git(root, "config", "filter.evil.required", "true")
    _git(root, "config", "core.fsmonitor", cmd("fsmonitor", "true"))
    _git(root, "config", "core.pager", cmd("pager", "cat"))
    hooks = root / ".git" / "hooks"
    hook = hooks / "post-index-change"
    hook.write_text(f"#!/bin/sh\n{cmd('hook')}\n", encoding="utf-8")
    hook.chmod(0o755)
    # Stat-dirty tracked files force git to re-hash (and so to run the clean filter).
    (root / "src" / "app.py").write_bytes(b"one\nTWO\nthree\n")
    os.utime(root / "gone.txt", (1, 1))

    # Not vacuous: stock git DOES run these for this repository.
    subprocess.run(["git", "status", "--porcelain"], cwd=root, env=_ENV, capture_output=True, check=False)
    subprocess.run(["git", "diff", "HEAD"], cwd=root, env=_ENV, capture_output=True, check=False)
    fired = sorted(p.name for p in markers.iterdir())
    assert fired, "stock git ran none of the armed vectors — the test would prove nothing"
    for p in markers.iterdir():
        p.unlink()
    # Re-dirty the stat info the stock run may have refreshed.
    os.utime(root / "src" / "app.py", (2, 2))
    os.utime(root / "gone.txt", (3, 3))

    body = _diff(_client(monkeypatch, root)).json()
    assert body["is_git"] is True
    assert "+TWO" in body["patch"]
    assert sorted(p.name for p in markers.iterdir()) == []


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFOs")
def test_untracked_fifo_is_never_read(tmp_path, monkeypatch):
    root = _repo(tmp_path / "repo")
    os.mkfifo(root / "pipe")
    (root / "real.txt").write_bytes(b"ok\n")
    body = _diff(_client(monkeypatch, root)).json()  # would hang forever if the FIFO were opened
    assert "+ok" in body["patch"]


def test_file_list_cap_sets_truncated(tmp_path, monkeypatch):
    root = _repo(tmp_path / "repo")
    for i in range(6):
        (root / f"u{i}.txt").write_bytes(b"x\n")
    monkeypatch.setattr(git_read, "MAX_FILES", 3)
    body = _diff(_client(monkeypatch, root)).json()
    assert len(body["files"]) == 3 and body["truncated"] is True
