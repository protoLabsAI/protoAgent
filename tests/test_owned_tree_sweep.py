"""A tree whose owner was SIGKILLed or crashed is reaped by the next sweep (#3463).

The #3428 registry tears owned trees down when an exit STARTS — but it lives in memory,
so an owner that is SIGKILLed (the Tauri shell killing the sidecar, an OOM kill) or
crashes runs no hook at all, and its trees run on at ppid=1. Sixteen codex-acp binaries
did, for 36h, before a hand cleanup. So each owner mirrors its registry to
``<box_root>/.owned-trees/<pid>.json`` and any process sweeps the records of dead owners.

REAL processes throughout: the defect is which process is still alive after its owner
is SIGKILLed, which no mock can show. The owner is a separate interpreter because a
SIGKILL is the point.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from infra import paths as paths_mod
from infra import proc as proc_mod
from infra.proc import group_kwargs, pid_alive, sweep_orphaned_trees, track_tree, untrack_tree

REPO = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX process groups; Windows is ADR 0098's documented gap")


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A private box root, so records never touch the machine's real `.owned-trees/`."""
    root = tmp_path / "box"
    monkeypatch.setenv("PROTOAGENT_BOX_ROOT", str(root))
    paths_mod.reset_instance_paths()
    yield root
    paths_mod.reset_instance_paths()


@pytest.fixture(autouse=True)
def _isolated_registry():
    with proc_mod._TRACKED_LOCK:
        saved, saved_at = dict(proc_mod._TRACKED), dict(proc_mod._TRACKED_AT)
        proc_mod._TRACKED.clear()
        proc_mod._TRACKED_AT.clear()
    yield
    with proc_mod._TRACKED_LOCK:
        proc_mod._TRACKED.clear()
        proc_mod._TRACKED.update(saved)
        proc_mod._TRACKED_AT.clear()
        proc_mod._TRACKED_AT.update(saved_at)


_KILL_AFTER: list[int] = []


@pytest.fixture(autouse=True)
def _no_survivors():
    """A failing test must not leave `sleep 300`s behind for five minutes."""
    yield
    while _KILL_AFTER:
        pid = _KILL_AFTER.pop()
        for kill in (lambda: os.killpg(pid, signal.SIGKILL), lambda: os.kill(pid, signal.SIGKILL)):
            try:
                kill()
            except OSError:
                pass


def _alive(pid: int) -> bool:
    if not pid_alive(pid):
        return False
    stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(stat) and not stat.startswith("Z")


def _wait(pred, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


# The owner: spawns a group-anchored tree the way the ACP client does, tracks it, and
# waits to be killed. `leaderless` is the codex-acp shape — the launcher exits after a
# moment while its real work runs on in the group.
_OWNER = r"""
import os, subprocess, sys, time
sys.path.insert(0, sys.argv[1])
shape, pidfile, ready = sys.argv[2], sys.argv[3], sys.argv[4]
from infra.proc import group_kwargs, reap_tracked_trees, track_tree
tail = "; sleep 1" if shape == "leaderless" else "; wait"
root = subprocess.Popen(["sh", "-c", f'sleep 300 & echo $! > "{pidfile}"' + tail], **group_kwargs())
track_tree(root.pid)
while not (os.path.exists(pidfile) and open(pidfile).read().strip()):
    time.sleep(0.02)
if shape == "clean-exit":
    reap_tracked_trees()
    open(ready, "w").write("done")
    sys.exit(0)
open(ready, "w").write(str(root.pid))
time.sleep(300)
"""


def _spawn_owner(tmp_path: Path, box: Path, shape: str = "tree"):
    script, pidfile, ready = tmp_path / "owner.py", tmp_path / "gc.pid", tmp_path / "ready"
    script.write_text(_OWNER, encoding="utf-8")
    env = {**os.environ, "PROTOAGENT_BOX_ROOT": str(box)}
    env.pop("PROTOAGENT_PARENT_PID", None)
    owner = subprocess.Popen([sys.executable, str(script), str(REPO), shape, str(pidfile), str(ready)], env=env)
    assert _wait(lambda: ready.exists() and ready.read_text(), 30), "owner never got its tree up"
    grandchild = int(pidfile.read_text())
    _KILL_AFTER.append(grandchild)
    record = box / ".owned-trees" / f"{owner.pid}.json"
    return owner, grandchild, record


def _sigkill(owner: subprocess.Popen) -> None:
    owner.send_signal(signal.SIGKILL)
    owner.wait(timeout=10)


def test_a_sigkilled_owners_tree_is_reaped_by_the_next_sweep(tmp_path, box):
    owner, grandchild, record = _spawn_owner(tmp_path, box)
    assert record.exists(), "the owner never wrote its record"
    _sigkill(owner)
    time.sleep(0.5)
    # The gap this fixes: nothing in the dead owner could run, so its tree is still up.
    assert _alive(grandchild), "precondition: a SIGKILLed owner's tree survives it"

    assert sweep_orphaned_trees(grace=0.5) >= 1
    assert _wait(lambda: not _alive(grandchild)), "the sweep left the dead owner's tree running"
    assert not record.exists(), "a swept record must go"


def test_a_group_that_lost_its_leader_is_still_reaped(tmp_path, box):
    """codex-acp: the node launcher died, its native binary ran on in the group."""
    owner, grandchild, record = _spawn_owner(tmp_path, box, shape="leaderless")
    rec = json.loads(record.read_text())
    leader = rec["trees"][0]["pgid"]
    _sigkill(owner)
    assert _wait(lambda: not _alive(leader)), "precondition: the launcher is gone"
    assert _alive(grandchild), "precondition: its work runs on, leaderless"

    assert sweep_orphaned_trees(grace=0.5) >= 1
    assert _wait(lambda: not _alive(grandchild)), "a leaderless group survived the sweep"


def test_a_live_owners_trees_are_left_alone(tmp_path, box):
    owner, grandchild, record = _spawn_owner(tmp_path, box)
    try:
        assert sweep_orphaned_trees(grace=0.2) == 0
        assert _alive(grandchild) and record.exists()
    finally:
        _sigkill(owner)


def test_a_clean_exit_leaves_no_record(tmp_path, box):
    owner, grandchild, record = _spawn_owner(tmp_path, box, shape="clean-exit")
    assert owner.wait(timeout=15) == 0
    assert not record.exists(), "a clean exit must not leave a record for the sweep to act on"
    assert _wait(lambda: not _alive(grandchild))


def _dead_pid() -> int:
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def _write_record(box: Path, owner: int, pgid: int, tracked_at: float) -> Path:
    d = box / ".owned-trees"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{owner}.json"
    path.write_text(json.dumps({"owner": owner, "owner_start": None, "trees": [{"root": pgid, "pgid": pgid, "tracked_at": tracked_at}]}))
    return path


def test_a_recycled_group_id_is_never_signalled(box):
    """A dead owner's record names a pgid that a STRANGER now leads (it started after
    the record was written). The sweep must not touch it."""
    stranger = subprocess.Popen(["sleep", "300"], **group_kwargs())
    _KILL_AFTER.append(stranger.pid)
    record = _write_record(box, _dead_pid(), stranger.pid, tracked_at=time.time() - 3600)

    assert sweep_orphaned_trees(grace=0.2) == 0
    assert _alive(stranger.pid), "the sweep signalled a group that was not the recorded one"
    assert not record.exists()


def test_a_leader_that_predates_its_record_is_the_recorded_one(box):
    """The same guard's other side — so it is a real comparison, not "never kill a live leader"."""
    ours = subprocess.Popen(["sleep", "300"], **group_kwargs())
    _KILL_AFTER.append(ours.pid)
    _write_record(box, _dead_pid(), ours.pid, tracked_at=time.time() + 60)

    assert sweep_orphaned_trees(grace=0.2) == 1
    assert _wait(lambda: ours.poll() is not None)


def test_track_and_untrack_keep_the_record_in_step(box):
    child = subprocess.Popen(["sleep", "300"], **group_kwargs())
    _KILL_AFTER.append(child.pid)
    record = box / ".owned-trees" / f"{os.getpid()}.json"

    track_tree(child.pid)
    rec = json.loads(record.read_text())
    assert rec["owner"] == os.getpid() and [t["root"] for t in rec["trees"]] == [child.pid]
    assert rec["trees"][0]["pgid"] == os.getpgid(child.pid)

    untrack_tree(child.pid)
    assert not record.exists(), "an owner with nothing tracked keeps no record"


def test_the_sweep_never_reads_its_own_record_as_a_dead_owner(box):
    child = subprocess.Popen(["sleep", "300"], **group_kwargs())
    _KILL_AFTER.append(child.pid)
    track_tree(child.pid)
    assert sweep_orphaned_trees(grace=0.2) == 0
    assert _alive(child.pid)
    untrack_tree(child.pid)


def test_one_sweep_logs_what_it_reaped_and_never_raises(monkeypatch, caplog):
    import server

    monkeypatch.setattr(proc_mod, "sweep_orphaned_trees", lambda **k: 3)
    with caplog.at_level("WARNING", logger="protoagent.server"):
        assert asyncio.run(server._sweep_orphaned_trees_once()) == 3
    assert "reaped 3 orphaned process tree(s)" in caplog.text

    def _boom(**k):
        raise RuntimeError("ps exploded")

    monkeypatch.setattr(proc_mod, "sweep_orphaned_trees", _boom)
    assert asyncio.run(server._sweep_orphaned_trees_once()) == 0  # logged, not raised


def test_the_periodic_sweep_keeps_ticking_through_a_failure_and_stops_on_cancel(monkeypatch):
    import server

    ticks: list[int] = []

    def _sweep(**k):
        ticks.append(1)
        if len(ticks) == 2:
            raise RuntimeError("one bad tick")
        return 0

    monkeypatch.setattr(proc_mod, "sweep_orphaned_trees", _sweep)

    async def _run():
        task = asyncio.create_task(server._sweep_orphaned_trees_forever(interval=0.01))
        for _ in range(200):
            if len(ticks) >= 4:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run())
    assert len(ticks) >= 4, "the loop stopped after a failing tick"


def _calls_in(func: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            f = node.func
            names.add(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
    return names


def test_the_servers_startup_and_shutdown_hooks_run_the_sweep():
    """The real handlers (nested in app construction, so no harness boots them) must
    run a sweep, start the periodic task, and cancel it — checked on their parsed
    bodies, so reformatting can't break this but dropping the wiring does."""
    import server

    tree = ast.parse(inspect.getsource(server))
    hooks = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)}
    startup, shutdown = hooks["_scheduler_startup"], hooks["_scheduler_shutdown"]
    assert {"_sweep_orphaned_trees_once", "_sweep_orphaned_trees_forever", "create_task"} <= _calls_in(startup)
    cancels = [
        n for n in ast.walk(shutdown)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "cancel"
        and isinstance(n.func.value, ast.Name) and n.func.value.id == "_ORPHAN_SWEEP_TASK"
    ]
    assert cancels, "shutdown no longer cancels the periodic sweep"
    assert server.ORPHAN_SWEEP_INTERVAL_S > 0
