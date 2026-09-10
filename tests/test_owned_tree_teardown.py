"""An exit takes the process trees this process OWNS down with it (#3428).

A tree spawned with ``infra.proc.group_kwargs()`` leads its own process group, so
signalling this process's group never reaches it; only its owner's cleanup path did.
Two exits pre-empt that path entirely:

* the hub SIGKILLs a member 3s after SIGTERM (``supervisor.shutdown_all``) while the
  member's uvicorn drain can take 5s, so the member's lifespan teardown never starts;
* on the desktop, the Tauri shell SIGKILLs the sidecar and the parent-death watchdog
  ``os._exit(0)``s, which skips lifespan AND atexit, in the hub and in every member.

These run REAL processes, through the production ``server.build_uvicorn_server`` and
``server._install_parent_death_watchdog``. A mocked signal proves nothing here: the
defect is exactly which process is still alive afterwards.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from infra import proc as proc_mod
from infra.proc import (
    begin_tree_teardown,
    group_kwargs,
    pid_alive,
    reap_tracked_trees,
    signal_tree,
    track_tree,
    tracked_trees,
    untrack_tree,
)

REPO = Path(__file__).resolve().parent.parent

posix_only = pytest.mark.skipif(
    os.name == "nt", reason="process groups + SIGTERM semantics; Windows walks the tree with taskkill"
)


@pytest.fixture(autouse=True)
def _isolated_registry():
    """Every test starts with an empty registry and no pending KILL timer, and
    nothing it tracked outlives it."""
    def _cancel_escalation():
        # The KILL timer signals whatever is tracked when it FIRES, so one left
        # pending from a test kills the next test's trees mid-run.
        if proc_mod._escalation is not None:
            proc_mod._escalation.cancel()
        proc_mod._escalation = None

    with proc_mod._TRACKED_LOCK:
        saved = dict(proc_mod._TRACKED)
        proc_mod._TRACKED.clear()
        _cancel_escalation()
    yield
    for pid in tracked_trees():
        signal_tree(pid, force=True)
    with proc_mod._TRACKED_LOCK:
        proc_mod._TRACKED.clear()
        proc_mod._TRACKED.update(saved)
        _cancel_escalation()


def _alive(pid: int) -> bool:
    """Live and not a zombie. A reaped-late zombie is dead for every purpose here."""
    if not pid_alive(pid):
        return False
    if os.name == "nt":
        return True
    stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(stat) and not stat.startswith("Z")


def _wait_dead(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


def _hub_straggler_budget() -> float:
    """How long the hub waits before SIGKILLing a member — read from the production
    signature, so this test follows the real budget if it ever changes."""
    from graph.fleet import supervisor

    return float(inspect.signature(supervisor.shutdown_all).parameters["timeout"].default)


#: `sh` prints "ready" only AFTER installing the trap. Signalled any sooner, it dies of
#: the SIGTERM it was meant to ignore, and the test measures nothing.
TERM_IGNORING = ["sh", "-c", "trap '' TERM; echo ready; exec sleep 300"]


def _spawn_term_ignoring() -> subprocess.Popen:
    child = subprocess.Popen(TERM_IGNORING, stdout=subprocess.PIPE, text=True, **group_kwargs())
    assert child.stdout.readline().strip() == "ready"
    return child


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# A stand-in member: the production uvicorn server factory, an owned tree, and an
# endless SSE-style stream — the kind of connection that holds the graceful drain open.
_MEMBER = r"""
import asyncio, json, os, subprocess, sys, time
sys.path.insert(0, sys.argv[1])
mode, out, port, ignore_term = sys.argv[2], sys.argv[3], int(sys.argv[4]), sys.argv[5] == "1"

from infra.proc import group_kwargs, track_tree
import server

if ignore_term:
    owned = subprocess.Popen(["sh", "-c", "trap '' TERM; echo ready; exec sleep 300"],
                             stdout=subprocess.PIPE, text=True, **group_kwargs())
    assert owned.stdout.readline().strip() == "ready"  # the trap is in place
else:
    owned = subprocess.Popen(["sleep", "300"], **group_kwargs())
track_tree(owned.pid)

def publish(info):
    # Atomic: the test polls for the file, so it must never see it half-written.
    with open(out + ".tmp", "w") as f:
        json.dump(info, f)
    os.replace(out + ".tmp", out)

if mode == "watchdog":
    server._install_parent_death_watchdog()
    publish({"owned": owned.pid})
    time.sleep(300)
    sys.exit(0)

async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            msg = await receive()
            if msg["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif msg["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]})
    while True:
        await send({"type": "http.response.body", "body": b"data: .\n\n", "more_body": True})
        await asyncio.sleep(0.2)

import uvicorn
publish({"owned": owned.pid})
server.build_uvicorn_server(
    uvicorn.Config(app, host="127.0.0.1", port=port, timeout_graceful_shutdown=5, log_level="warning")
).run()
"""


def _spawn_member(tmp_path, *, mode: str, ignore_term: bool = False, env: dict | None = None):
    script = tmp_path / "member.py"
    script.write_text(_MEMBER, encoding="utf-8")
    out = tmp_path / "member.json"
    port = _free_port()
    full_env = {**os.environ, "PROTOAGENT_HOME": str(tmp_path / "home")}
    # A runner that is itself a desktop sidecar would hand its watchdog to the member.
    full_env.pop("PROTOAGENT_PARENT_PID", None)
    full_env.update(env or {})
    member = subprocess.Popen(
        [sys.executable, str(script), str(REPO), mode, str(out), str(port), "1" if ignore_term else "0"],
        env=full_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        # Members are spawned detached (their own session), exactly as the hub does.
        start_new_session=True,
    )
    deadline = time.monotonic() + 30
    while not out.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert out.exists(), "stand-in member never started"
    return member, json.loads(out.read_text()), port


def _hold_a_stream_open(port: int) -> socket.socket:
    deadline = time.monotonic() + 30
    while True:
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=2)
            break
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)
    sock.sendall(b"GET /stream HTTP/1.1\r\nHost: x\r\n\r\n")
    assert b"200" in sock.recv(4096)
    return sock


def _run_the_hubs_shutdown_against(member: subprocess.Popen) -> bool:
    """What `supervisor.shutdown_all` does to a member: tree-SIGTERM, wait the shared
    budget, tree-SIGKILL. Returns whether the member was still running at the SIGKILL
    — i.e. whether the drain really outlasted the budget."""
    signal_tree(member.pid, force=False)
    try:
        member.wait(timeout=_hub_straggler_budget())
        return False
    except subprocess.TimeoutExpired:
        return True


def _kill_member(member: subprocess.Popen) -> None:
    signal_tree(member.pid, force=True)
    try:
        member.wait(timeout=5)
    except subprocess.TimeoutExpired:
        member.kill()


# ── the two exits that pre-empt the owner's cleanup ──────────────────────────────────


@posix_only
def test_a_member_sigkilled_mid_drain_takes_its_owned_trees_with_it(tmp_path):
    member, info, port = _spawn_member(tmp_path, mode="server")
    stream = _hold_a_stream_open(port)
    try:
        still_draining = _run_the_hubs_shutdown_against(member)
        # Without this the test proves nothing: #3428 is the member being SIGKILLed
        # BEFORE its teardown, which only happens while the drain is still running.
        assert still_draining, "the open stream should hold the drain past the hub's budget"
        # The tree is gone BEFORE the hub's SIGKILL lands — so it no longer depends on
        # the drain, the lifespan, or whether the member gets to finish either.
        assert _wait_dead(info["owned"], 0.5), "the owned tree outlived its member (ppid=1)"
    finally:
        stream.close()
        _kill_member(member)
        signal_tree(info["owned"], force=True)


@posix_only
def test_a_tree_that_ignores_sigterm_is_dead_inside_the_hubs_window(tmp_path):
    """SIGTERM alone would leave this one running; the KILL escalation has to land
    inside the hub's budget, before the member it depends on is itself SIGKILLed."""
    assert proc_mod.TEARDOWN_GRACE < _hub_straggler_budget()
    member, info, port = _spawn_member(tmp_path, mode="server", ignore_term=True)
    stream = _hold_a_stream_open(port)
    try:
        assert _run_the_hubs_shutdown_against(member), "the open stream should hold the drain past the hub's budget"
        assert _wait_dead(info["owned"], 0.5), "a SIGTERM-ignoring owned tree outlived its member"
    finally:
        stream.close()
        _kill_member(member)
        signal_tree(info["owned"], force=True)


@posix_only
def test_a_desktop_quit_through_the_watchdog_takes_owned_trees_with_it(tmp_path):
    """The desktop path: the launcher dies, the watchdog `os._exit(0)`s — no lifespan,
    no atexit. Members inherit PROTOAGENT_PARENT_PID, so every one of them exits this
    way on a desktop quit."""
    launcher = subprocess.Popen(["sleep", "300"])
    member, info, _ = _spawn_member(tmp_path, mode="watchdog", env={"PROTOAGENT_PARENT_PID": str(launcher.pid)})
    try:
        assert _alive(info["owned"])
        launcher.kill()
        launcher.wait()
        member.wait(timeout=15)  # the watchdog polls every 2s, then the sweep's grace
        assert member.returncode == 0, "the watchdog's os._exit(0) is the path under test"
        assert _wait_dead(info["owned"], 2.0), "the owned tree outlived a desktop quit (ppid=1)"
    finally:
        _kill_member(member)
        signal_tree(info["owned"], force=True)


# ── the registry itself ──────────────────────────────────────────────────────────────


@posix_only
def test_a_group_is_torn_down_after_its_leader_has_already_died(tmp_path):
    """The orphan the issue found was usually this shape: a short-lived root
    (`sh -c`) whose grandchild (`pnpm install`) is the long-running part. The root
    exits first; the group lives on. Looking the group up from the root pid at
    teardown time would find nothing and kill nothing."""
    pidfile = tmp_path / "grandchild.pid"
    root = subprocess.Popen(["sh", "-c", f"sleep 300 & echo $! > {pidfile}; exit 0"], **group_kwargs())
    track_tree(root.pid)
    root.wait()
    deadline = time.monotonic() + 5
    while not pidfile.exists() or not pidfile.read_text().strip():
        assert time.monotonic() < deadline
        time.sleep(0.02)
    grandchild = int(pidfile.read_text().strip())
    assert not _alive(root.pid) and _alive(grandchild)

    reap_tracked_trees(grace=0.2)

    assert _wait_dead(grandchild, 2.0), "the group outlived its leader and the sweep missed it"


def test_this_process_never_tracks_itself():
    # The sweep would take this process's whole tree down, the server included.
    # Runs on the Windows gate too, where there are no groups to compare.
    track_tree(os.getpid())
    assert tracked_trees() == []


@posix_only
def test_a_child_that_shares_our_group_is_refused():
    # Spawned WITHOUT group_kwargs(), it lives in our group: killing it by group
    # would kill us.
    child = subprocess.Popen(["sleep", "300"])
    try:
        track_tree(child.pid)
        assert tracked_trees() == []
    finally:
        child.kill()
        child.wait()


@posix_only
def test_a_reaped_tree_is_forgotten():
    child = subprocess.Popen(["sleep", "300"], **group_kwargs())
    track_tree(child.pid)
    assert tracked_trees() == [child.pid]
    untrack_tree(child.pid)
    assert tracked_trees() == []
    child.kill()
    child.wait()


@posix_only
def test_a_second_exit_signal_does_not_stack_kill_timers():
    # A double Ctrl-C calls handle_exit twice. One escalation is enough; a second
    # would be harmless, but an unbounded stack of timers is not.
    child = _spawn_term_ignoring()
    track_tree(child.pid)
    begin_tree_teardown(grace=0.3)
    first = proc_mod._escalation
    begin_tree_teardown(grace=0.3)
    assert proc_mod._escalation is first is not None
    time.sleep(0.15)
    assert _alive(child.pid), "SIGTERM is ignored; only the escalation may end it"
    assert _wait_dead(child.pid, 3.0)
    child.wait()


def test_the_production_server_tears_down_on_the_exit_signal(monkeypatch):
    """The unit-level pin on the wiring the real-process tests rely on: the server
    this process runs starts the teardown from `handle_exit`, before the drain."""
    import uvicorn

    import server

    calls: list[str] = []
    monkeypatch.setattr("infra.proc.begin_tree_teardown", lambda **_: calls.append("teardown"))
    srv = server.build_uvicorn_server(uvicorn.Config(lambda *a: None))
    assert isinstance(srv, uvicorn.Server)
    srv.handle_exit(signal.SIGTERM, None)
    assert calls == ["teardown"]
    assert srv.should_exit is True


# ── the spawners track what they own ────────────────────────────────────────────────


@posix_only
async def test_the_shell_tool_owns_its_command_until_it_is_reaped():
    from tools.shell import run_command

    task = asyncio.create_task(run_command(["sleep", "0.5"]))
    for _ in range(100):
        if tracked_trees():
            break
        await asyncio.sleep(0.01)
    assert len(tracked_trees()) == 1
    result = await task
    assert result.returncode == 0
    assert tracked_trees() == []


@posix_only
async def test_a_cancelled_shell_command_stays_owned_so_the_exit_still_reaches_it():
    """Cancelling the turn abandons the command without reaping it. Forgetting it
    there is how it would outlive the process; staying tracked is what lets the
    exit sweep take it down."""
    from tools.shell import run_command

    task = asyncio.create_task(run_command(["sleep", "300"]))
    for _ in range(100):
        if tracked_trees():
            break
        await asyncio.sleep(0.01)
    [pid] = tracked_trees()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert tracked_trees() == [pid] and _alive(pid)
    reap_tracked_trees(grace=0.2)
    assert _wait_dead(pid, 2.0)


@posix_only
async def test_a_cancelled_shell_command_is_forgotten_once_it_finishes_on_its_own():
    """Staying tracked forever is not harmless: once the group is gone its pgid is free
    for reuse, and the exit sweep would signal whichever unrelated group took it."""
    from tools.shell import run_command

    task = asyncio.create_task(run_command(["sleep", "0.6"]))
    for _ in range(100):
        if tracked_trees():
            break
        await asyncio.sleep(0.01)
    [pid] = tracked_trees()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert tracked_trees() == [pid], "still running — the exit must still be able to reach it"
    for _ in range(300):
        if not tracked_trees():
            break
        await asyncio.sleep(0.01)
    assert tracked_trees() == [], "finished on its own, but its (reusable) pgid is still tracked"


@posix_only
def test_tracking_a_tree_drops_groups_that_are_already_gone():
    # Whatever an owner failed to untrack (a hard kill whose close() never ran) must
    # not wait for the exit sweep with a pgid someone else may have been handed.
    gone = subprocess.Popen(["sleep", "300"], **group_kwargs())
    track_tree(gone.pid)
    gone.kill()
    gone.wait()
    live = subprocess.Popen(["sleep", "300"], **group_kwargs())
    try:
        track_tree(live.pid)
        assert tracked_trees() == [live.pid]
    finally:
        live.kill()
        live.wait()


@posix_only
async def test_execute_code_owns_its_script_until_it_is_reaped():
    from plugins.execute_code.engine import run_code

    task = asyncio.create_task(run_code("import time; time.sleep(0.5); print('done')", {}))
    for _ in range(200):
        if tracked_trees():
            break
        await asyncio.sleep(0.01)
    assert len(tracked_trees()) == 1
    assert (await task) == "done"
    assert tracked_trees() == []


_FAKE_ACP_AGENT = r"""
import sys, json
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": 1}}) + "\n")
        sys.stdout.flush()
"""


@posix_only
async def test_the_acp_client_owns_its_agent_until_close_reaps_it(tmp_path):
    from plugins.coding_agent.acp_client import AcpClient

    agent = tmp_path / "agent.py"
    agent.write_text(_FAKE_ACP_AGENT, encoding="utf-8")
    client = AcpClient(sys.executable, [str(agent)], cwd=str(tmp_path), name="fake")
    try:
        await client.handshake()
        assert tracked_trees() == [client._proc.pid]
    finally:
        await client.close()
    assert tracked_trees() == []
