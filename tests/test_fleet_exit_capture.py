"""Exit capture + health sweep (ADR 0135 D8, #5533) against REAL child processes.

A member that dies on its own leaves a dead pid in fleet.json; every reader that prunes
it must record the exit first. A member stopped on purpose leaves nothing.
"""

from __future__ import annotations

import ast
import inspect
import json
import socket
import sys
import time
from types import SimpleNamespace

import pytest

from graph.fleet import supervisor
from graph.workspaces import manager
from infra.proc import kill_tree

pytestmark = pytest.mark.platform_sensitive

# A stand-in member: binds its port, answers /healthz 200 (503 while the flag file
# exists), and writes one line to its log.
_MEMBER = """
import http.server, os, sys
port, flag = int(sys.argv[1]), sys.argv[2]
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        ok = self.path == "/healthz" and not os.path.exists(flag)
        self.send_response(200 if ok else 503)
        self.send_header("Content-Length", "0")
        self.end_headers()
    def log_message(self, *a):
        pass
print("member booted token=sk-ant-api03-" + "a" * 40, flush=True)
http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def member(tmp_path, monkeypatch):
    """A running stand-in member ``m``. Yields ``(workspace, flag_path, pids)``; every
    pid appended to ``pids`` is killed at teardown."""
    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path / "ws"))
    monkeypatch.setattr(supervisor, "_is_our_agent", lambda pid: True)
    monkeypatch.setattr(supervisor, "_PORT_RELEASE_GRACE_SECONDS", 3.0)
    supervisor._health.clear()
    supervisor._probe_cache.clear()
    port, flag = _free_port(), tmp_path / "unhealthy"
    argv = [sys.executable, "-c", _MEMBER, str(port), str(flag)]
    monkeypatch.setattr(manager, "run_exec", lambda ident, passthrough: ({}, argv))
    ws = manager.create("m", port=port)
    pids = [supervisor.start("m")["pid"]]
    try:
        yield ws, flag, pids
    finally:
        for pid in pids:
            kill_tree(pid)
        supervisor._health.clear()


def _kill(pid: int) -> None:
    kill_tree(pid)
    deadline = time.monotonic() + 10
    while supervisor._alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not supervisor._alive(pid)


def _sweep(threshold: int = 2) -> list[tuple]:
    return [(c["id"], c["previous"], c["state"]) for c in supervisor.health_sweep(threshold, timeout=5.0)]


def test_killed_member_leaves_exactly_one_exit_record(member):
    ws, _flag, pids = member
    _kill(pids[0])

    # Readers that used to erase the evidence run BEFORE anyone collects it.
    assert not next(s for s in supervisor.status() if s["id"] == ws["id"])["running"]
    supervisor.status()
    supervisor.version_skew_warning()

    exits = supervisor.collect_exits()
    assert len(exits) == 1
    rec = exits[0]
    assert rec["id"] == ws["id"] and rec["pid"] == pids[0] and rec["detected_by"] == "status"
    assert rec["at"] and "member booted" in rec["log_tail"]
    assert "sk-ant-api03" not in rec["log_tail"]  # the tail is scrubbed
    assert supervisor.collect_exits() == []  # each exit is handed out once
    assert ws["id"] not in supervisor._load_state()


def test_stopping_a_member_records_no_exit(member):
    _ws, _flag, pids = member
    assert supervisor.stop("m")["stopped"]
    assert not supervisor._alive(pids[0])
    supervisor.status()
    supervisor.version_skew_warning()
    assert supervisor.health_sweep(1, timeout=5.0) == []
    assert supervisor.collect_exits() == []
    assert supervisor.member_health() == []


def _via_start(pids):
    pids.append(supervisor.start("m")["pid"])  # a restart replaces the dead record


def _via_stop(pids):
    with pytest.raises(supervisor.FleetError):
        supervisor.stop("m")


@pytest.mark.parametrize(
    ("seen_by", "reader"),
    [
        ("status", lambda pids: supervisor.status()),
        ("version_skew", lambda pids: supervisor.version_skew_warning()),
        ("start", _via_start),
        ("stop", _via_stop),
        ("health_sweep", lambda pids: supervisor.health_sweep(1, timeout=5.0)),
    ],
)
def test_every_reader_that_prunes_records_the_exit(member, seen_by, reader):
    ws, _flag, pids = member
    _kill(pids[0])
    reader(pids)
    exits = supervisor.collect_exits()
    assert [(x["id"], x["pid"], x["detected_by"]) for x in exits] == [(ws["id"], pids[0], seen_by)]
    assert (supervisor._load_state().get(ws["id"]) or {}).get("pid") != pids[0]


def test_boot_death_is_recorded_as_a_boot_exit(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path / "ws"))
    argv = [sys.executable, "-c", "import sys; sys.stderr.write('kaboom'); sys.exit(3)"]
    monkeypatch.setattr(manager, "run_exec", lambda ident, passthrough: ({}, argv))
    ws = manager.create("dies", port=_free_port())
    with pytest.raises(supervisor.FleetError):
        supervisor.start("dies")
    exits = supervisor.collect_exits()
    assert [(x["id"], x["detected_by"]) for x in exits] == [(ws["id"], "boot")]
    assert "kaboom" in exits[0]["log_tail"]


def test_exit_tail_quotes_only_the_last_run_and_the_record_is_bounded(member, monkeypatch):
    ws, _flag, pids = member
    _kill(pids[0])
    pids.append(supervisor.start("m")["pid"])  # run 2 appends to the same agent.log
    assert supervisor.collect_exits()[0]["log_tail"].count("member booted") == 1
    _kill(pids[1])
    supervisor.status()
    assert supervisor.collect_exits()[0]["log_tail"].count("member booted") == 1  # run 2 only

    monkeypatch.setattr(supervisor, "_EXITS_KEEP", 3)
    supervisor._save_exits([{"id": str(n), "collected": True} for n in range(9)])
    kept = json.loads(supervisor._exits_path().read_text())
    assert [x["id"] for x in kept] == ["6", "7", "8"]
    supervisor._exits_path().write_text("{not json")  # a corrupt file reads as empty
    assert supervisor.collect_exits() == []


def test_no_dead_record_prune_bypasses_the_helper():
    """Source guard: fleet.json records are dropped only (a) by the helper, (b) right
    after the helper ran in the same function, or (c) where the member is still alive
    and about to be stopped (stop / shutdown_all)."""
    tree = ast.parse(inspect.getsource(supervisor))
    offenders = []
    for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
        calls = [c for c in ast.walk(fn) if isinstance(c, ast.Call)]
        pops = [
            c
            for c in calls
            if isinstance(c.func, ast.Attribute)
            and c.func.attr == "pop"
            and isinstance(c.func.value, ast.Name)
            and c.func.value.id == "state"
        ]
        uses_helper = any(isinstance(c.func, ast.Name) and c.func.id == "_prune_dead_records" for c in calls)
        if pops and not uses_helper and fn.name not in ("_prune_dead_records", "shutdown_all"):
            offenders.append(fn.name)
    assert offenders == []


def test_health_sweep_reports_wedged_recovered_and_down_once_each(member):
    ws, flag, pids = member
    wid = ws["id"]
    assert _sweep() == []  # first seen healthy: a baseline, not a change
    assert [(h["id"], h["kind"], h["state"], h["pid"]) for h in supervisor.member_health()] == [
        (wid, "local", "healthy", pids[0])
    ]

    flag.write_text("")  # alive, but /healthz now fails
    assert _sweep() == []  # 1 failure < threshold 2
    assert supervisor.member_health()[0]["failures"] == 1
    assert _sweep() == [(wid, "healthy", "wedged")]
    assert _sweep() == []  # still wedged: nothing new
    assert supervisor.collect_exits() == []  # wedged is not an exit

    flag.unlink()
    assert _sweep() == [(wid, "wedged", "healthy")]
    assert _sweep() == []
    health = supervisor.member_health()[0]
    assert health["failures"] == 0 and health["changed_at"]

    _kill(pids[0])
    assert _sweep() == [(wid, "healthy", "down")]
    assert _sweep() == []  # still down
    assert supervisor.member_health()[0]["state"] == "down" and supervisor.member_health()[0]["pid"] is None
    assert len(supervisor.collect_exits()) == 1

    pids.append(supervisor.start("m")["pid"])
    assert _sweep() == [(wid, "down", "healthy")]
    assert supervisor.collect_exits() == []


def test_health_sweep_sees_a_crash_another_reader_pruned_first(member):
    ws, _flag, pids = member
    wid = ws["id"]
    assert _sweep() == []
    _kill(pids[0])
    supervisor.status()  # the console poll wins the race and prunes the record
    assert _sweep() == [(wid, "healthy", "down")]
    assert _sweep() == []

    # Crash + restart both land between two sweeps: down and recovered are still reported.
    pids.append(supervisor.start("m")["pid"])
    assert _sweep() == [(wid, "down", "healthy")]
    _kill(pids[1])
    pids.append(supervisor.start("m")["pid"])
    assert _sweep() == [(wid, "healthy", "down"), (wid, "down", "healthy")]
    assert len(supervisor.collect_exits()) == 2


def test_health_sweep_reports_crash_followed_by_failed_restart(member, monkeypatch):
    ws, _flag, pids = member
    wid = ws["id"]
    assert _sweep() == []
    _kill(pids[0])

    # Both exits are pruned between sweeps; the boot exit must not hide the crash.
    with monkeypatch.context() as patch:
        argv = [sys.executable, "-c", "import sys; sys.exit(3)"]
        patch.setattr(manager, "run_exec", lambda ident, passthrough: ({}, argv))
        with pytest.raises(supervisor.FleetError):
            supervisor.start("m")
    exits = supervisor.collect_exits()
    assert [(x["id"], x["detected_by"]) for x in exits] == [(wid, "start"), (wid, "boot")]
    assert exits[0]["pid"] == pids[0] and exits[1]["pid"] != pids[0]
    assert _sweep() == [(wid, "healthy", "down")]
    assert _sweep() == []
    assert supervisor.member_health()[0]["state"] == "down"
    assert supervisor.member_health()[0]["pid"] is None

    pids.append(supervisor.start("m")["pid"])
    assert _sweep() == [(wid, "down", "healthy")]
    assert _sweep() == []
    assert supervisor.collect_exits() == []


def test_health_sweep_tracks_remotes_through_the_probe_cache(tmp_path, monkeypatch):
    import httpx

    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path / "ws"))
    supervisor._health.clear()
    supervisor._probe_cache.clear()
    rec = supervisor.add_remote("ava", "http://h:9")
    # Distinct probes can finish in the same Windows monotonic clock tick.
    # Cache reuse must count once, but a new result at the same time must count again.
    monkeypatch.setattr(supervisor, "time", SimpleNamespace(monotonic=lambda: 123.0))
    up = {"ok": False, "calls": 0}

    class Resp:
        def json(self):
            return {}

    def fake_get(url, timeout, **kw):
        up["calls"] += 1
        resp = Resp()
        resp.status_code = 200 if up["ok"] else 503
        return resp

    monkeypatch.setattr(httpx, "get", fake_get)
    try:
        assert _sweep() == [] and _sweep() == []  # one probe inside its TTL counts once
        assert up["calls"] == 1 and supervisor.member_health()[0]["failures"] == 1

        monkeypatch.setattr(supervisor, "_PROBE_TTL", 0.0)
        assert _sweep() == [(rec["id"], "healthy", "down")]  # 2nd consecutive failed probe
        assert _sweep() == []
        up["ok"] = True
        assert _sweep() == [(rec["id"], "down", "healthy")]
        assert _sweep() == []
        assert supervisor.member_health()[0]["kind"] == "remote"

        supervisor.remove_remote(rec["id"])
        assert _sweep() == [] and supervisor.member_health() == []
    finally:
        supervisor._health.clear()
        supervisor._probe_cache.clear()


@pytest.mark.asyncio
async def test_member_health_watch_check_sweeps_for_itself(member, monkeypatch):
    # No sweep loop runs: the check sweeps for itself, and a later sweep is handed its change.
    from graph.goals import verifiers

    ws, _flag, pids = member
    monkeypatch.setattr(supervisor, "_last_sweep", None)
    monkeypatch.setattr(verifiers, "_MEMBER_HEALTH_MAX_AGE", 0.0)
    spec = {"type": "plugin", "check": "fleet:member_health", "args": {"member": "m"}}
    first = await verifiers.run_verifier(spec, verifiers.VerifyContext())
    assert (first.met, first.reason) == (False, "member m is healthy")
    _kill(pids[0])
    assert (await verifiers.run_verifier(spec, verifiers.VerifyContext())).reason == "member m is down"
    assert _sweep() == [(ws["id"], "healthy", "down")]
