"""``fs.changed`` — the code pane's live "these files just changed" signal (ADR 0112).

The Diff tab used to sit on "No changes vs HEAD" while ``@claude-code`` edited the project,
until the operator clicked Refresh. A write the runtime OBSERVES — a coding delegate's
settled edit tool call (``graph.delegate_progress``) or protoAgent's own ``write_file`` /
``edit_file`` — now goes out on the bus as ``fs.changed {project, paths, source, target?}``,
but only for paths inside a registered project and only while the pane is on.
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest

from graph import delegate_progress as dp
from graph import fs_changes
from graph.config import LangGraphConfig

# Resolves real paths through tools.fs_tools (platform-branching) and spawns a fake coder.
pytestmark = pytest.mark.platform_sensitive


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "app"
    (root / "src").mkdir(parents=True)
    (root / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    return root.resolve()


@pytest.fixture
def bus(monkeypatch, project):
    """A wired plugin host: a live config with the pane ON and one project, and a publish
    that records what went out."""
    from graph.plugins.host import HOST

    sent: list[tuple[str, dict, dict]] = []
    state = {
        "cfg": LangGraphConfig(
            filesystem_code_pane=True,
            filesystem_projects=[{"name": "app", "path": str(project), "write": True}],
        )
    }
    monkeypatch.setattr(HOST, "config", lambda: state["cfg"])
    monkeypatch.setattr(HOST, "publish", lambda topic, data, **kw: sent.append((topic, data, kw)))
    return SimpleNamespace(sent=sent, state=state)


async def _noop_sink(_snap):
    return None


async def _run_tool(tracker, *events):
    for e in events:
        await tracker.on_tool(e)
    tracker.close()


# ── the delegate tracker → fs.changed ─────────────────────────────────────────────


async def test_a_delegate_edit_inside_a_project_publishes_fs_changed(bus, project):
    t = dp.DelegateProgress("claude-code", _noop_sink)
    target = str(project / "src" / "calc.py")
    await _run_tool(
        t,
        # claude-agent-acp: opened with a placeholder, refined with kind + locations, then
        # settled with a BARE status — the end frame carries no locations.
        {"phase": "start", "id": "t1", "name": "Edit", "kind": ""},
        {"phase": "update", "id": "t1", "name": "Edit src/calc.py", "kind": "edit", "locations": [{"path": target}]},
        {"phase": "end", "id": "t1", "name": "Edit src/calc.py", "status": "completed"},
    )
    assert bus.sent == [
        (
            "fs.changed",
            {"project": "app", "paths": ["src/calc.py"], "source": "delegate", "target": "claude-code"},
            {"retain": False},
        )
    ]


@pytest.mark.parametrize(
    "event",
    [
        # outside every registered project
        {"kind": "edit", "path": "/definitely/not/a/project/x.py"},
        # a READ inside the project changes nothing
        {"kind": "read", "path": "INSIDE"},
        # a shell command is the fallback poll's job, not a guess here
        {"kind": "execute", "path": "INSIDE"},
    ],
)
async def test_no_event_for_a_path_outside_or_a_non_write_kind(bus, project, event):
    t = dp.DelegateProgress("claude-code", _noop_sink)
    path = str(project / "src" / "calc.py") if event["path"] == "INSIDE" else event["path"]
    await _run_tool(
        t,
        {"phase": "start", "id": "t1", "name": "tool", "kind": event["kind"], "locations": [{"path": path}]},
        {"phase": "end", "id": "t1", "status": "completed"},
    )
    assert bus.sent == []


async def test_a_failed_edit_publishes_nothing(bus, project):
    t = dp.DelegateProgress("claude-code", _noop_sink)
    loc = [{"path": str(project / "src" / "calc.py")}]
    await _run_tool(
        t,
        {"phase": "start", "id": "t1", "name": "Edit", "kind": "edit", "locations": loc},
        {"phase": "end", "id": "t1", "status": "failed"},
    )
    assert bus.sent == []


async def test_relative_locations_map_through_the_delegates_workdir_only(bus, project):
    """A relative path is the coder's-cwd relative: mapped with a workdir, dropped without
    one (a guessed base would name the wrong file)."""
    events = (
        {"phase": "start", "id": "w", "name": "Write src/new.py", "locations": [{"path": "src/new.py"}]},
        {"phase": "end", "id": "w", "status": "completed"},
    )
    await _run_tool(dp.DelegateProgress("coder", _noop_sink), *events)
    assert bus.sent == []
    await _run_tool(dp.DelegateProgress("coder", _noop_sink, workdir=str(project)), *events)
    assert [d for _, d, _ in bus.sent] == [
        {"project": "app", "paths": ["src/new.py"], "source": "delegate", "target": "coder"}
    ]


async def test_a_kindless_write_named_tool_counts(bus, project):
    """An A2A peer's tool-call frames carry a name and args, no ACP kind."""
    t = dp.DelegateProgress("peer", _noop_sink)
    await _run_tool(
        t,
        {"phase": "start", "id": "c1", "name": "write_file", "locations": [{"path": str(project / "README.md")}]},
        {"phase": "end", "id": "c1", "name": "write_file", "status": "completed"},
    )
    assert [d["paths"] for _, d, _ in bus.sent] == [["README.md"]]


async def test_nothing_is_published_while_the_code_pane_is_off(bus, project):
    bus.state["cfg"] = LangGraphConfig(filesystem_projects=[{"name": "app", "path": str(project), "write": True}])
    t = dp.DelegateProgress("claude-code", _noop_sink)
    await _run_tool(
        t,
        {"phase": "start", "id": "t1", "name": "Edit", "kind": "edit", "locations": [{"path": str(project / "a")}]},
        {"phase": "end", "id": "t1", "status": "completed"},
    )
    assert bus.sent == []


async def test_a_raising_publisher_never_costs_the_delegation(monkeypatch, bus, project):
    from graph.plugins.host import HOST

    def boom(*_a, **_k):
        raise RuntimeError("bus down")

    monkeypatch.setattr(HOST, "publish", boom)
    snaps = []

    async def sink(snap):
        snaps.append(snap)

    t = dp.DelegateProgress("claude-code", sink)
    await t.on_tool({"phase": "start", "id": "t1", "name": "Edit", "kind": "edit", "locations": [{"path": str(project / "x")}]})
    await t.on_tool({"phase": "end", "id": "t1", "status": "completed"})
    await t.finish(ok=True)
    assert snaps[-1]["done"] is True and snaps[-1]["recent_tools"][0]["status"] == "completed"


def test_is_write_tool():
    assert fs_changes.is_write_tool("edit", "anything")
    assert fs_changes.is_write_tool("delete", "")
    assert fs_changes.is_write_tool("", "Write /abs/x.py")
    assert fs_changes.is_write_tool("other", "MultiEdit src/a.ts")
    assert fs_changes.is_write_tool(None, "edit_file")
    assert not fs_changes.is_write_tool("read", "Edit src/a.ts")  # a specific kind wins
    assert not fs_changes.is_write_tool("execute", "write_file")
    assert not fs_changes.is_write_tool("", "Read src/a.ts")


def test_map_to_projects_handles_nesting_dupes_and_the_cap(tmp_path):
    outer = (tmp_path / "outer").resolve()
    inner = outer / "pkg"
    inner.mkdir(parents=True)
    roots = sorted([("outer", outer), ("inner", inner)], key=lambda r: len(str(r[1])), reverse=True)
    many = [str(inner / f"f{i}.py") for i in range(fs_changes.MAX_PATHS + 5)]
    out = fs_changes.map_to_projects([str(inner / "a.py"), str(inner / "a.py"), *many], roots)
    assert out["inner"][0] == "a.py" and out["outer"][0] == "pkg/a.py"
    assert len(out["inner"]) == fs_changes.MAX_PATHS


# ── a remote A2A peer can't forge writes; a flood is coalesced ───────────────────


def _forged_frames(project, n):
    """What a hostile streaming A2A peer can send: tool-call extension frames naming files
    on OUR disk (``args.path`` is entirely peer-supplied)."""
    from plugins.delegates.a2a_progress import TOOL_CALL_EXT_URI

    for i in range(n):
        for phase in ("started", "completed"):
            call = {"toolCallId": f"c{i}", "name": "write_file", "phase": phase}
            if phase == "started":
                call["args"] = {"path": str(project / "src" / f"f{i}.py")}
            yield {"statusUpdate": {"status": {"state": "TASK_STATE_WORKING", "message": {
                "role": "ROLE_AGENT", "parts": [], "metadata": {TOOL_CALL_EXT_URI: call}}}}}


async def test_a_remote_a2a_peer_cannot_forge_fs_changed(bus, project):
    """500 forged frames from a non-local peer → zero events (was: 500 events, each a diff
    refetch + a Follow jump in every open console)."""
    from plugins.delegates.a2a_progress import A2AProgressFeed

    t = dp.DelegateProgress("peer", _noop_sink, announce_writes=False)
    feed = A2AProgressFeed(t)
    for frame in _forged_frames(project, 500):
        await feed.frame(frame)
    await t.finish(ok=True)
    assert bus.sent == []
    assert t.tool_count == 500  # the card still shows the peer's work


async def test_a_flood_of_writes_is_coalesced_to_at_most_two_events_a_second(bus, project):
    """Even from a trusted (local) delegate, 500 settled writes in a burst publish the first
    at once and coalesce the rest into ONE trailing event — not 500."""
    from plugins.delegates.a2a_progress import A2AProgressFeed

    t = dp.DelegateProgress("peer", _noop_sink, write_interval=0.2)
    feed = A2AProgressFeed(t)
    for frame in _forged_frames(project, 500):
        await feed.frame(frame)
    assert len(bus.sent) == 1
    await asyncio.sleep(0.35)  # past the interval: the trailing flush lands on its own
    assert len(bus.sent) == 2
    second = bus.sent[1][1]
    assert second["project"] == "app" and len(second["paths"]) == fs_changes.MAX_PATHS
    t.close()


async def test_finish_flushes_coalesced_writes_and_close_drops_the_trailing_task(bus, project):
    t = dp.DelegateProgress("coder", _noop_sink, write_interval=60.0)
    for i in range(3):
        await t.on_tool({"phase": "start", "id": f"e{i}", "name": "Edit", "kind": "edit",
                         "locations": [{"path": str(project / f"f{i}.py")}]})
        await t.on_tool({"phase": "end", "id": f"e{i}", "status": "completed"})
    assert [d["paths"] for _, d, _ in bus.sent] == [["f0.py"]]
    await t.finish(ok=True)  # the run is over — its last edits must not wait out 60 s
    assert [d["paths"] for _, d, _ in bus.sent] == [["f0.py"], ["f1.py", "f2.py"]]


@pytest.mark.parametrize(
    "url, local",
    [
        ("http://127.0.0.1:7870/a2a", True),
        ("http://localhost:7871/a2a", True),
        ("http://127.0.0.1:7870/agents/host/a2a", True),
        # the hub's proxy to a member — which may be a remote, LAN-paired instance
        ("http://127.0.0.1:7870/agents/navaengineer/a2a", False),
        ("http://10.0.0.5:7870/a2a", False),
        ("https://peer.example.com/a2a", False),
    ],
)
def test_only_a_loopback_non_proxied_a2a_peer_announces_writes(url, local):
    from plugins.delegates.a2a import _writes_are_local

    assert _writes_are_local(url) is local


# ── protoAgent's own write tools ──────────────────────────────────────────────────


def test_own_write_and_edit_tools_publish_source_agent(bus, project):
    from tools.fs_tools import build_fs_tools

    tools = {t.name: t for t in build_fs_tools(bus.state["cfg"])}
    assert tools["write_file"].invoke({"project": "app", "path": "./src/new.py", "content": "x = 1\n"}).startswith("Created")
    assert tools["edit_file"].invoke({"project": "app", "path": "src/calc.py", "old": "a + b", "new": "b + a"}) == (
        "Edited src/calc.py."
    )
    # A refused write is not a change.
    assert tools["edit_file"].invoke({"project": "app", "path": "src/calc.py", "old": "nope", "new": "x"}).startswith(
        "Error"
    )
    assert [(t, d) for t, d, _ in bus.sent] == [
        ("fs.changed", {"project": "app", "paths": ["src/new.py"], "source": "agent"}),
        ("fs.changed", {"project": "app", "paths": ["src/calc.py"], "source": "agent"}),
    ]


# ── the real wire: an ACP coder subprocess edits a file ───────────────────────────
# What claude-agent-acp sends for an Edit: a tool_call opened with a placeholder, a
# tool_call_update naming it with kind "edit" + an ABSOLUTE location, a bare completion.
# Plus an edit OUTSIDE the project, which must not surface.
_CODER = r"""
import sys, json
def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()
def update(u):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "s1", "update": u}})
INSIDE, OUTSIDE = sys.argv[1], sys.argv[2]
while True:
    line = sys.stdin.readline()
    if not line:
        break
    if not line.strip():
        continue
    msg = json.loads(line)
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": 1}})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "s1"}})
    elif method == "session/prompt":
        for tid, path in (("e1", INSIDE), ("e2", OUTSIDE)):
            update({"sessionUpdate": "tool_call", "toolCallId": tid, "title": "Edit",
                    "kind": "edit", "status": "pending", "rawInput": {}, "locations": []})
            update({"sessionUpdate": "tool_call_update", "toolCallId": tid, "title": "Edit " + path,
                    "kind": "edit", "rawInput": {"file_path": path}, "locations": [{"path": path}]})
            update({"sessionUpdate": "tool_call_update", "toolCallId": tid, "status": "completed"})
        update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Done."}})
        send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
"""


async def test_a_real_acp_coder_edit_reaches_the_bus(bus, project, tmp_path):
    from plugins.delegates.adapters import AcpAdapter

    script = tmp_path / "fake_coder.py"
    script.write_text(_CODER, encoding="utf-8")
    outside = tmp_path / "elsewhere" / "x.py"
    adapter = AcpAdapter()
    d = adapter.parse(
        {
            "name": "claude-code",
            "type": "acp",
            "command": sys.executable,
            "args": [str(script), str(project / "src" / "calc.py"), str(outside)],
            "workdir": str(project),
            "return_diff": "false",
        }
    )
    try:
        with dp.progress_sink(_noop_sink):
            await adapter.dispatch(d, "edit calc")
    finally:
        await adapter.teardown(d)
    assert [(t, d) for t, d, _ in bus.sent] == [
        ("fs.changed", {"project": "app", "paths": ["src/calc.py"], "source": "delegate", "target": "claude-code"})
    ]
