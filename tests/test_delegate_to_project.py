"""``delegate_to(project=…)`` — scope an ACP coder to one fenced project per call, and
hand back what it changed.

The change-summary tests run a REAL ACP subprocess (a tiny fake agent speaking the wire
protocol) against a REAL git repository — no git mocks. The agent edits files in its own
cwd, so a passing test also proves the project root actually reached the child's
working directory, not just a spec dict.
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys
from pathlib import Path

import pytest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

import plugins.coding_agent as CA
import plugins.delegates as P
from graph.config import LangGraphConfig
from plugins.delegates import _dispatch_into_room
from plugins.delegates import change_summary as cs
from plugins.delegates.adapters import ADAPTERS, AcpAdapter, DelegateError
from plugins.delegates.projects import ProjectScope, resolve
from plugins.delegates.registry import DelegateRegistry

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0, reason="git not installed"
)

# A minimal ACP agent: handshakes, opens a session, and on a prompt edits `app.py`
# (appends a line), creates `NOTES.md`, then replies with its cwd.
_EDITING_AGENT = r"""
import json, os, sys

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

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
        with open("app.py", "a", encoding="utf-8") as fh:
            fh.write("print('fixed')\n")
        with open("NOTES.md", "w", encoding="utf-8") as fh:
            fh.write("# notes\n")
        send({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": "s1",
            "update": {"sessionUpdate": "agent_message_chunk",
                       "content": {"type": "text", "text": "done in " + os.getcwd()}}}})
        send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
"""


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    _git(path, "config", "commit.gpgsign", "false")
    (path / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (path / "README.md").write_text("readme\n", encoding="utf-8")
    (path / ".gitignore").write_text("*.log\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    return path


@pytest.fixture
def agent_script(tmp_path):
    script = tmp_path / "editing_agent.py"
    script.write_text(_EDITING_AGENT, encoding="utf-8")
    return script


@pytest.fixture
def coder_registry(agent_script, tmp_path):
    home = tmp_path / "coder-home"
    home.mkdir()
    return DelegateRegistry(
        [
            {
                "name": "coder",
                "type": "acp",
                "command": sys.executable,
                "args": [str(agent_script)],
                "workdir": str(home),
            },
            {"name": "peer", "type": "a2a", "url": "http://127.0.0.1:9/a2a"},
            {"name": "opus", "type": "openai", "url": "https://g/v1", "model": "m"},
        ]
    )


@pytest.fixture(autouse=True)
async def _reap_clients():
    yield
    await CA.close_all()


@pytest.fixture
def projects_config(tmp_path, monkeypatch):
    """Register projects through the SAME live-config seam the fs tools read."""
    from graph.plugins.host import HOST

    rw = _repo(tmp_path / "rw-proj")
    ro = tmp_path / "ro-proj"
    ro.mkdir()
    cfg = LangGraphConfig(
        filesystem_enabled=True,
        filesystem_projects=[
            {"name": "rw", "path": str(rw), "write": True},
            {"name": "ro", "path": str(ro), "write": False},
        ],
    )
    monkeypatch.setattr(HOST, "config", lambda: cfg)
    return {"cfg": cfg, "rw": rw, "ro": ro}


# ── project resolution through the fs fence ─────────────────────────────────────


def test_resolve_known_project_returns_fenced_root(projects_config):
    scope = resolve("rw")
    assert scope == ProjectScope(name="rw", root=str(projects_config["rw"].resolve()), write=True)


def test_resolve_unknown_project_lists_the_registered_ones(projects_config):
    with pytest.raises(DelegateError) as exc:
        resolve("nope")
    msg = str(exc.value)
    assert "unknown project 'nope'" in msg
    assert "rw (rw)" in msg and "ro (ro)" in msg


def test_resolve_refuses_a_path_instead_of_a_name(projects_config):
    """The model names a project; it can never smuggle a path through `project`."""
    with pytest.raises(DelegateError, match="unknown project"):
        resolve(str(projects_config["rw"]))
    with pytest.raises(DelegateError, match="unknown project"):
        resolve("../rw")


def test_resolve_refuses_a_read_only_project(projects_config):
    with pytest.raises(DelegateError, match="read-only"):
        resolve("ro")


def test_resolve_sees_nothing_when_filesystem_is_disabled(projects_config, monkeypatch):
    from graph.plugins.host import HOST

    off = dataclasses.replace(projects_config["cfg"], filesystem_enabled=False)
    monkeypatch.setattr(HOST, "config", lambda: off)
    with pytest.raises(DelegateError, match="unknown project 'rw'"):
        resolve("rw")


# ── registry: only the workdir changes, on a copy ──────────────────────────────


async def test_project_on_non_acp_delegate_is_refused(coder_registry, projects_config):
    scope = resolve("rw")
    for name in ("peer", "opus"):
        with pytest.raises(DelegateError, match="only applies to acp"):
            await coder_registry.dispatch(name, "hi", project=scope)


async def test_project_dispatch_sets_only_workdir_and_never_mutates_the_roster(coder_registry, projects_config, monkeypatch):
    configured = dataclasses.asdict(coder_registry.get("coder"))
    seen = {}

    class _Stub:
        last_stop_reason = "end_turn"

        async def prompt(self, query, timeout=None):
            return "ok"

    def _client_for(spec):
        seen["spec"] = spec
        return _Stub()

    monkeypatch.setattr(CA, "_client_for", _client_for)
    scope = resolve("rw")
    await coder_registry.dispatch("coder", "fix it", project=scope)

    spec = seen["spec"]
    assert spec["workdir"] == scope.root
    base = AcpAdapter._spec(coder_registry.get("coder"))
    assert {k: v for k, v in spec.items() if k != "workdir"} == {k: v for k, v in base.items() if k != "workdir"}
    assert dataclasses.asdict(coder_registry.get("coder")) == configured  # roster untouched


def test_cache_key_and_session_file_are_separate_per_project_and_stable_per_project(coder_registry, tmp_path):
    """A second call into the same project resumes the same pooled client + persisted ACP
    session (same workdir); a different project never shares either."""
    d = coder_registry.get("coder")
    a1 = dataclasses.replace(d, workdir=str(tmp_path / "a"))
    a2 = dataclasses.replace(d, workdir=str(tmp_path / "a"))
    b = dataclasses.replace(d, workdir=str(tmp_path / "b"))
    ka1, ka2, kb = (CA._cache_key(AcpAdapter._spec(x)) for x in (a1, a2, b))
    assert ka1 == ka2 and ka1 != kb
    assert ka1 != CA._cache_key(AcpAdapter._spec(d))
    assert CA._session_id_path(AcpAdapter._spec(a1)) == CA._session_id_path(AcpAdapter._spec(a2))
    assert CA._session_id_path(AcpAdapter._spec(a1)) != CA._session_id_path(AcpAdapter._spec(b))


async def test_second_dispatch_into_same_project_reuses_its_client_and_workdir(coder_registry, projects_config):
    scope = resolve("rw")
    await coder_registry.dispatch("coder", "one", project=scope)
    spec = AcpAdapter._spec(dataclasses.replace(coder_registry.get("coder"), workdir=scope.root))
    client = CA._CLIENTS[CA._cache_key(spec)]
    await coder_registry.dispatch("coder", "two", project=scope)
    assert CA._CLIENTS[CA._cache_key(spec)] is client
    assert client.cwd == scope.root


def test_return_diff_parses_and_defaults_on():
    acp = ADAPTERS["acp"]
    assert acp.parse({"name": "c", "type": "acp", "command": "x", "workdir": "/tmp"}).return_diff is True
    assert acp.parse({"name": "c", "type": "acp", "command": "x", "workdir": "/tmp", "return_diff": "false"}).return_diff is False
    assert acp.parse({"name": "c", "type": "acp", "command": "x", "workdir": "/tmp", "return_diff": False}).return_diff is False
    assert "return_diff" in {f.key for f in acp.config_schema()}


# ── the diff comes back: real ACP child, real git ──────────────────────────────


async def test_unmanaged_project_dispatch_returns_the_delegates_diff(coder_registry, projects_config):
    root = projects_config["rw"]
    reply = await coder_registry.dispatch("coder", "add a line", project=resolve("rw"))

    assert f"done in {root.resolve()}" in reply  # the child really ran in the project root
    assert "Changes in project `rw`" in reply
    assert "app.py" in reply and "+print('fixed')" in reply
    assert "New files: NOTES.md" in reply
    assert "already modified" not in reply  # clean tree before → nothing to exclude
    # The coder's edits are left in the working tree, uncommitted; the index is untouched.
    assert _git(root, "diff", "--cached", "--name-only") == ""
    assert "app.py" in _git(root, "status", "--porcelain")


async def test_pre_existing_dirty_state_is_not_attributed_to_the_delegate(coder_registry, projects_config):
    root = projects_config["rw"]
    (root / "README.md").write_text("operator was here\n", encoding="utf-8")  # tracked, dirty
    (root / "scratch.txt").write_text("untracked before\n", encoding="utf-8")  # untracked
    (root / "debug.log").write_text("ignored\n", encoding="utf-8")  # ignored

    reply = await coder_registry.dispatch("coder", "add a line", project=resolve("rw"))

    assert "+print('fixed')" in reply
    assert "README.md" not in reply and "operator was here" not in reply
    assert "scratch.txt" not in reply and "debug.log" not in reply
    assert "2 path(s) were already modified or untracked BEFORE" in reply


async def test_return_diff_false_opts_out(agent_script, projects_config, tmp_path):
    reg = DelegateRegistry(
        [
            {
                "name": "coder",
                "type": "acp",
                "command": sys.executable,
                "args": [str(agent_script)],
                "workdir": str(tmp_path),
                "return_diff": "false",
            }
        ]
    )
    reply = await reg.dispatch("coder", "add a line", project=resolve("rw"))
    assert reply.startswith("done in") and "Changes in" not in reply


async def test_dispatch_without_project_is_unchanged(coder_registry):
    reply = await coder_registry.dispatch("coder", "add a line")
    assert reply.startswith("done in") and "Changes in" not in reply


async def test_non_git_project_degrades_to_a_note(agent_script, tmp_path, monkeypatch):
    from graph.plugins.host import HOST

    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "app.py").write_text("x\n", encoding="utf-8")
    cfg = LangGraphConfig(filesystem_enabled=True, filesystem_projects=[{"name": "plain", "path": str(plain), "write": True}])
    monkeypatch.setattr(HOST, "config", lambda: cfg)
    reg = DelegateRegistry(
        [{"name": "coder", "type": "acp", "command": sys.executable, "args": [str(agent_script)], "workdir": str(tmp_path)}]
    )
    reply = await reg.dispatch("coder", "add a line", project=resolve("plain"))
    assert "done in" in reply and "is not a git repository" in reply


def test_render_truncates_and_points_at_the_full_diff(tmp_path):
    root = _repo(tmp_path / "big")
    before = cs.snapshot(str(root))
    (root / "app.py").write_text("".join(f"line {i}\n" for i in range(2000)), encoding="utf-8")
    after = cs.after(before)
    out = cs.render(before, after, project="big", cap=500)
    assert "[diff truncated at 500 of" in out
    assert f"diff {before.tree} {after.tree}" in out
    # The tree ids really reproduce the whole change.
    assert "line 1999" in _git(root, "diff", before.tree, after.tree)


def test_render_reports_a_commit_the_delegate_made(tmp_path):
    root = _repo(tmp_path / "commits")
    before = cs.snapshot(str(root))
    (root / "app.py").write_text("print('committed')\n", encoding="utf-8")
    _git(root, "commit", "-q", "-am", "delegate commit")
    out = cs.render(before, cs.after(before), project="commits")
    assert "HEAD moved" in out and "+print('committed')" in out


def test_overlap_tracking_flags_both_concurrent_captures():
    t1 = cs.begin("/r")
    t2 = cs.begin("/r")
    assert cs.end("/r", t1) is True
    assert cs.end("/r", t2) is True
    t3 = cs.begin("/r")
    assert cs.end("/r", t3) is False


# ── delegate_to: tool boundary, room path, background path ─────────────────────


def _tool(delegates, monkeypatch):
    monkeypatch.setattr(P, "_load_delegates_config", lambda: delegates)
    from graph.plugins.host import PluginHost

    class _Reg:
        def __init__(self):
            self.config = {}
            self.tools = []
            self.host = PluginHost()

        def register_tool(self, t):
            self.tools.append(t)

    r = _Reg()
    P.register(r)
    return next(t for t in r.tools if t.name == "delegate_to")


async def _call(tool, args):
    result = await tool.ainvoke({"name": "delegate_to", "args": args, "id": "call-1", "type": "tool_call"})
    return getattr(result, "content", result)


async def test_tool_refuses_project_for_non_acp_and_unknown_project(monkeypatch, projects_config):
    tool = _tool(
        [
            {"name": "peer", "type": "a2a", "url": "http://127.0.0.1:9/a2a"},
            {"name": "coder", "type": "acp", "command": "proto", "workdir": "/tmp"},
        ],
        monkeypatch,
    )
    out = await _call(tool, {"target": "peer", "query": "hi", "project": "rw"})
    assert "only applies to acp" in out
    out = await _call(tool, {"target": "coder", "query": "hi", "project": "ghost"})
    assert "unknown project 'ghost'" in out and "rw (rw)" in out
    out = await _call(tool, {"target": "coder", "query": "hi", "project": "ro"})
    assert "read-only" in out


async def test_tool_background_path_carries_the_project(monkeypatch, projects_config, agent_script, tmp_path):
    """No BackgroundManager wired → the background branch dispatches inline, and must still
    run in the project (the scope is captured at spawn, not re-read later)."""
    from runtime.state import STATE

    monkeypatch.setattr(STATE, "background_mgr", None, raising=False)
    tool = _tool(
        [{"name": "coder", "type": "acp", "command": sys.executable, "args": [str(agent_script)], "workdir": str(tmp_path)}],
        monkeypatch,
    )
    out = await _call(tool, {"target": "coder", "query": "add a line", "project": "rw", "background": True})
    assert f"done in {projects_config['rw'].resolve()}" in out and "+print('fixed')" in out


async def test_room_path_carries_the_project_across_mention_op(coder_registry, projects_config):
    """The foreground room path reaches `registry.dispatch` through host-free
    `graph/mention_op`, which can't take a project argument — the scope rides a
    ContextVar, and the diff lands in the ToolMessage."""
    out = await _dispatch_into_room(
        coder_registry,
        "coder",
        "add a line",
        {"session_id": "room-proj", "messages": []},
        tool_call_id="call-1",
        project=resolve("rw"),
    )
    assert isinstance(out, Command)
    tool_msg = out.update["messages"][-1]
    assert isinstance(tool_msg, ToolMessage)
    assert f"done in {projects_config['rw'].resolve()}" in tool_msg.content
    assert "+print('fixed')" in tool_msg.content
    from plugins.delegates.projects import current_scope

    assert current_scope() is None  # the scope never leaks past the call
