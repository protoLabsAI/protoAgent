"""One dependency install at a time per Python environment, proven with REAL processes.

Install deps used to run pip unguarded. Two clicks, two tabs, two plugins' installs,
or the CLI beside the console each ran their own pip into the same site-packages at
once, and one install's post-install refresh could re-import a package another was
still writing.

What stands in for pip here is a real child process, a script that holds for a while
and records whether another copy was inside at the same moment (an ``O_EXCL`` marker
file). The concurrency is real: threads posting to the real route, and separate
interpreters calling the real ``install_deps``. The lock itself is never mocked. Replace
``infra.install_lock.install_lock`` with a no-op and every test here goes red.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import runtime.state as rs
from graph.plugins import installer

REPO = Path(__file__).resolve().parents[1]
PID = "depdemo"

# The pip stand-in. argv: <log> <marker> <hold> then what the installer passes
# ("install -- <spec>"). <hold> is seconds, or a path: hold until that file exists (a test
# releases it, so nothing depends on timing). One line per event, so the test can count
# runs and overlaps.
FAKE_PIP = """\
import os, sys, time
log, mark, hold = sys.argv[1], sys.argv[2], sys.argv[3]

def note(event):
    with open(log, "a", encoding="utf-8") as f:
        f.write(f"{event} {os.getpid()}\\n")

note("start")
try:
    os.close(os.open(mark, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    inside = True
except FileExistsError:
    note("OVERLAP")
    inside = False
try:
    time.sleep(float(hold))
except ValueError:
    deadline = time.time() + 120  # never hang a suite on a lost release
    while not os.path.exists(hold) and time.time() < deadline:
        time.sleep(0.01)
if inside:
    os.remove(mark)
note("end")
"""


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    """A live-dir plugin with one declared dep no environment has, so ``install_deps``
    always has something to pip."""
    live = tmp_path / "installed"
    (live / PID).mkdir(parents=True)
    (live / PID / "protoagent.plugin.yaml").write_text(
        f"id: {PID}\nname: Dep Demo\nversion: 0.1.0\nrequires_pip:\n  - protoagent-fake-dep-{PID}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PROTOAGENT_PLUGINS_DIR", str(live))
    monkeypatch.setattr(rs.STATE, "plugin_meta", [], raising=False)
    return tmp_path


def _fake_pip(tmp_path: Path, hold: float | Path) -> tuple[list[str], Path]:
    script = tmp_path / "fake_pip.py"
    script.write_text(FAKE_PIP, encoding="utf-8")
    log = tmp_path / "pip.log"
    return [sys.executable, str(script), str(log), str(tmp_path / "pip.inside"), str(hold)], log


def _events(log: Path) -> list[str]:
    return [line.split()[0] for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []


def _app() -> FastAPI:
    from operator_api.plugin_routes import register_plugin_routes

    app = FastAPI()
    register_plugin_routes(app)
    return app


def test_two_concurrent_install_deps_requests_run_one_pip(plugin, monkeypatch):
    """Two requests at the same instant: one pip runs, and the other gets a 409 that names
    what's running. It never starts a second pip. Once the first finishes, the environment
    is free again."""
    argv, log = _fake_pip(plugin, hold=1.5)
    monkeypatch.setattr(installer, "_host_pip", lambda: list(argv))
    app = _app()
    barrier = threading.Barrier(2)
    responses = []

    def post():
        client = TestClient(app)  # one per thread: two independent requests
        barrier.wait()
        responses.append(client.post("/api/plugins/install-deps", json={"id": PID}))

    threads = [threading.Thread(target=post) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)

    assert sorted(r.status_code for r in responses) == [200, 409], [r.text for r in responses]
    busy = next(r for r in responses if r.status_code == 409).json()["detail"]
    assert "already running" in busy and PID in busy
    ok = next(r for r in responses if r.status_code == 200).json()
    assert ok["ok"] is True and ok["installed"] == [f"protoagent-fake-dep-{PID}"]
    assert _events(log).count("start") == 1 and "OVERLAP" not in _events(log)

    # Released: the next install runs (the fake dep is still "missing", so pip runs again).
    again = TestClient(app).post("/api/plugins/install-deps", json={"id": PID})
    assert again.status_code == 200
    assert _events(log).count("start") == 2 and "OVERLAP" not in _events(log)
    assert installer.deps_install_running() is None


def test_the_hold_spans_the_refresh_that_follows_pip(plugin, monkeypatch):
    """The route holds the environment through the post-install refresh, not just pip. A
    request that lands while the first one is refreshing is refused, so it can't pip into
    the environment the refresh is reading. The inventory reports the running install for
    other tabs while it lasts."""
    from graph.plugins import loader

    argv, log = _fake_pip(plugin, hold=0.1)
    monkeypatch.setattr(installer, "_host_pip", lambda: list(argv))
    # Loaded and enabled, so a newly landed dep triggers the per-plugin refresh.
    monkeypatch.setattr(rs.STATE, "plugin_meta", [{"id": PID, "enabled": True, "loaded": True}], raising=False)
    refreshing, release = threading.Event(), threading.Event()

    def slow_refresh(pid):
        refreshing.set()
        release.wait(30)
        return []

    monkeypatch.setattr(loader, "refresh_plugin_deps", slow_refresh)
    app = _app()
    first: list = []
    t = threading.Thread(target=lambda: first.append(TestClient(app).post("/api/plugins/install-deps", json={"id": PID})))
    t.start()
    try:
        assert refreshing.wait(30), "the first install never reached its refresh"
        running = TestClient(app).get("/api/plugins/installed").json()["deps_installing"]
        assert running["id"] == PID and running["target"] == "this server's Python environment"
        second = TestClient(app).post("/api/plugins/install-deps", json={"id": PID})
        assert second.status_code == 409, second.text
    finally:
        release.set()
        t.join(60)
    assert first[0].status_code == 200 and first[0].json()["refresh"] == "plugin"
    assert _events(log).count("start") == 1


CHILD = """\
import pathlib, sys, time
ready, go = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
pip_argv = sys.argv[3:]
from graph.plugins import installer
installer._host_pip = lambda: list(pip_argv)
ready.touch()
deadline = time.time() + 60
while not go.exists():
    if time.time() > deadline:
        sys.exit(4)
    time.sleep(0.005)
try:
    installer.install_deps("depdemo")
except installer.DepsInstallBusy as exc:
    print("BUSY", exc)
    sys.exit(3)
print("DONE")
"""


def _wait_for(cond, what: str, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while not cond():
        assert time.time() < deadline, f"timed out waiting for {what}"
        time.sleep(0.01)


def test_two_processes_on_one_environment_run_one_pip(plugin, monkeypatch):
    """Two interpreters sharing one environment and one box root: a dev and a default
    instance on one checkout's venv, fleet members on the managed runtime, the CLI beside a
    server. While one is mid-pip, the other is refused and names the holder's pid. The
    per-process table can't see this. Only the OS file lock can.

    A handshake, not a race. The fake pip starts only inside ``install_deps``, which runs
    after the holder has taken the OS lock AND written its holder note (both happen before
    ``install_lock`` yields). So B is released only once pip's "start" is logged, and
    it then finds the lock held with the note in place. The fake pip holds until the test
    releases it, so no step depends on timing. With the lock broken, B's pip starts too,
    and the test sees the second "start" rather than waiting for B."""
    release = plugin / "release"
    argv, log = _fake_pip(plugin, hold=release)
    child = plugin / "child.py"
    child.write_text(CHILD, encoding="utf-8")
    env = {
        **os.environ,
        # The conftest pins data_home() in THIS process only; the children need the same
        # sandboxed box root, or they'd lock under the real ~/.protoagent.
        "PROTOAGENT_BOX_ROOT": str(plugin / "box-root"),
        "PYTHONPATH": str(REPO),
    }
    kids = []
    for name in ("a", "b"):
        ready, go = plugin / f"ready-{name}", plugin / f"go-{name}"
        proc = subprocess.Popen(
            [sys.executable, str(child), str(ready), str(go), *argv],
            cwd=REPO,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        kids.append((ready, go, proc))
    (ready_a, go_a, a), (ready_b, go_b, b) = kids
    try:
        _wait_for(lambda: ready_a.exists() and ready_b.exists(), "the children to import")
        go_a.touch()
        # A holds the lock and has written its note: its pip is running.
        _wait_for(lambda: "start" in _events(log) or a.poll() is not None, "A's pip to start")
        assert a.poll() is None, a.communicate()
        go_b.touch()
        # B is refused, or with a broken lock its own pip starts. Either ends the wait.
        _wait_for(lambda: b.poll() is not None or _events(log).count("start") >= 2, "B to be refused")
    finally:
        release.touch()
    a_code, a_out, a_err = a.wait(60), *a.communicate()
    b_code, b_out, b_err = b.wait(60), *b.communicate()

    assert (a_code, b_code) == (0, 3), (a_out, a_err, b_out, b_err)
    assert "already running" in b_out and f"pid {a.pid}" in b_out, b_out
    assert _events(log).count("start") == 1 and "OVERLAP" not in _events(log)
