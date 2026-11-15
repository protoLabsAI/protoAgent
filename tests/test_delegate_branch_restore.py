"""A ``delegate_to(project=…)`` coder shares the board's base checkout, so when it leaves
that checkout on another branch (a release changelog fix on ``prepare-release/…``, bd-nfaen)
the board is stranded. These tests use a REAL temporary git repo behind a REAL bare origin —
no git mocks — driving both the ``change_summary.restore_branch`` seam and the full
``AcpAdapter`` dispatch (a fake ACP agent that runs git in its cwd)."""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from pathlib import Path

import pytest

import plugins.coding_agent as CA
from plugins.delegates import change_summary as cs
from plugins.delegates.adapters import AcpAdapter

pytestmark = [
    pytest.mark.skipif(
        subprocess.run(["git", "--version"], capture_output=True).returncode != 0, reason="git not installed"
    ),
    # Real git + ACP subprocesses — OS-sensitive, so the Windows CI lane must see it.
    pytest.mark.platform_sensitive,
]


# A minimal ACP agent: handshakes, opens a session, and on a prompt runs a baked-in list
# of actions in its cwd, then replies. Each action is either ["WRITE", path, text] or a
# git argv (run with stdout/stderr swallowed so nothing corrupts the JSON-RPC channel).
_AGENT_BODY = r"""
def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

def run_actions():
    for argv in ACTIONS:
        if argv and argv[0] == "WRITE":
            with open(argv[1], "w", encoding="utf-8") as fh:
                fh.write(argv[2])
        else:
            subprocess.run(argv, check=True, cwd=os.getcwd(),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

while True:
    line = sys.stdin.readline()
    if not line:
        break
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": 1}})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "s1"}})
    elif method == "session/prompt":
        run_actions()
        send({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": "s1",
            "update": {"sessionUpdate": "agent_message_chunk",
                       "content": {"type": "text", "text": "done"}}}})
        send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
"""


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout


def _branch(root: Path) -> str:
    return _git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()


def _repo_with_origin(tmp_path: Path) -> tuple[Path, str]:
    """A committed repo wired to a bare origin, with its initial commit already pushed.
    Returns (working checkout, its branch name) — the name isn't hard-coded, so this is
    robust to the host's ``init.defaultBranch`` (master vs main)."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    root = tmp_path / "work"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "app.py").write_text("print('hello')\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    start = _branch(root)
    _git(root, "remote", "add", "origin", str(origin))
    _git(root, "push", "-q", "origin", start)  # creates refs/remotes/origin/<start>
    return root, start


def _agent_script(tmp_path: Path, name: str, actions: list[list[str]]) -> Path:
    body = "import json, os, subprocess, sys\n" + "ACTIONS = " + json.dumps(actions) + "\n" + _AGENT_BODY
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def _make_delegate(script: Path, workdir: Path):
    d = AcpAdapter().parse(
        {"name": "coder", "type": "acp", "command": sys.executable, "args": [str(script)], "workdir": str(workdir)}
    )
    # capture_diff/project_name are per-invocation (the registry sets them on a replace
    # copy when it scopes a project); do the same here without a registry.
    return dataclasses.replace(d, capture_diff=True, project_name="proj")


@pytest.fixture(autouse=True)
async def _reap_clients():
    yield
    await CA.close_all()


# ── the Snapshot now records the checked-out branch ────────────────────────────


def test_snapshot_records_branch_and_blanks_when_detached(tmp_path):
    root, start = _repo_with_origin(tmp_path)
    assert cs.snapshot(str(root)).branch == start
    _git(root, "checkout", "-q", "--detach")
    assert cs.snapshot(str(root)).branch == ""  # detached ⇒ empty, not "HEAD"


# ── restore_branch: the switch-back decision, against real git ─────────────────


def test_restore_branch_switches_back_when_clean_and_pushed(tmp_path):
    root, start = _repo_with_origin(tmp_path)
    before = cs.snapshot(str(root))
    _git(root, "checkout", "-q", "-b", "feature")
    (root / "app.py").write_text("print('fix')\n", encoding="utf-8")
    _git(root, "commit", "-q", "-am", "fix")
    _git(root, "push", "-q", "origin", "feature")
    res = cs.restore_branch(before, cs.after(before))
    assert res.changed and res.switched and res.reason == ""
    assert res.original == start and res.left_on == "feature"
    assert _branch(root) == start  # really put back


def test_restore_branch_refuses_a_dirty_tree(tmp_path):
    root, start = _repo_with_origin(tmp_path)
    before = cs.snapshot(str(root))
    _git(root, "checkout", "-q", "-b", "feature")
    (root / "app.py").write_text("print('fix')\n", encoding="utf-8")
    _git(root, "commit", "-q", "-am", "fix")
    _git(root, "push", "-q", "origin", "feature")
    (root / "app.py").write_text("print('still editing')\n", encoding="utf-8")  # uncommitted, tracked
    res = cs.restore_branch(before, cs.after(before))
    assert res.changed and not res.switched
    assert "uncommitted" in res.reason
    assert _branch(root) == "feature"  # left untouched


def test_restore_branch_refuses_unpushed_commits(tmp_path):
    root, start = _repo_with_origin(tmp_path)
    before = cs.snapshot(str(root))
    _git(root, "checkout", "-q", "-b", "feature")
    (root / "app.py").write_text("print('fix')\n", encoding="utf-8")
    _git(root, "commit", "-q", "-am", "fix")  # committed but never pushed
    res = cs.restore_branch(before, cs.after(before))
    assert res.changed and not res.switched
    assert "not pushed" in res.reason and "feature" in res.reason
    assert _branch(root) == "feature"


def test_restore_branch_is_a_noop_when_the_branch_never_changed(tmp_path):
    root, start = _repo_with_origin(tmp_path)
    before = cs.snapshot(str(root))
    (root / "app.py").write_text("print('edit in place')\n", encoding="utf-8")  # edit, same branch
    res = cs.restore_branch(before, cs.after(before))
    assert not res.changed and not res.switched and res.reason == ""
    assert _branch(root) == start


# ── end to end: a real ACP child moves the branch, the adapter reacts ──────────


async def test_dispatch_switches_back_after_the_coder_pushes_a_branch(tmp_path):
    root, start = _repo_with_origin(tmp_path)
    script = _agent_script(
        tmp_path,
        "agent_push.py",
        [
            ["git", "checkout", "-q", "-b", "feature"],
            ["WRITE", "app.py", "print('fixed')\n"],
            ["git", "commit", "-q", "-am", "fix"],
            ["git", "push", "-q", "origin", "feature"],
        ],
    )
    reply = await AcpAdapter().dispatch(_make_delegate(script, root), "fix the changelog")
    assert "left the checkout on `feature`" in reply
    assert f"switched it back to `{start}`" in reply
    assert not reply.startswith("Warning")
    assert _branch(root) == start  # the board's base checkout is no longer stranded


async def test_dispatch_warns_and_leaves_a_dirty_checkout_alone(tmp_path):
    root, start = _repo_with_origin(tmp_path)
    script = _agent_script(
        tmp_path,
        "agent_dirty.py",
        [
            ["git", "checkout", "-q", "-b", "feature"],
            ["WRITE", "app.py", "print('fixed')\n"],
            ["git", "commit", "-q", "-am", "fix"],
            ["git", "push", "-q", "origin", "feature"],
            ["WRITE", "app.py", "print('still editing')\n"],  # uncommitted tracked change
        ],
    )
    reply = await AcpAdapter().dispatch(_make_delegate(script, root), "fix it")
    assert reply.startswith(f"Warning: the coder left `{root}` on `feature`, not `{start}`")
    assert "not switched back: the working tree has uncommitted changes" in reply
    assert _branch(root) == "feature"  # untouched


async def test_dispatch_warns_and_leaves_unpushed_commits_alone(tmp_path):
    root, start = _repo_with_origin(tmp_path)
    script = _agent_script(
        tmp_path,
        "agent_unpushed.py",
        [
            ["git", "checkout", "-q", "-b", "feature"],
            ["WRITE", "app.py", "print('fixed')\n"],
            ["git", "commit", "-q", "-am", "fix"],  # no push
        ],
    )
    reply = await AcpAdapter().dispatch(_make_delegate(script, root), "fix it")
    assert reply.startswith(f"Warning: the coder left `{root}` on `feature`, not `{start}`")
    assert "not pushed to any remote" in reply
    assert _branch(root) == "feature"


async def test_dispatch_is_unchanged_when_the_coder_stays_on_its_branch(tmp_path):
    root, start = _repo_with_origin(tmp_path)
    script = _agent_script(tmp_path, "agent_stay.py", [["WRITE", "app.py", "print('fixed')\n"]])
    reply = await AcpAdapter().dispatch(_make_delegate(script, root), "fix it")
    assert "switched it back" not in reply
    assert "Warning: the coder left" not in reply
    assert "+print('fixed')" in reply  # the normal change summary is still produced
    assert _branch(root) == start
