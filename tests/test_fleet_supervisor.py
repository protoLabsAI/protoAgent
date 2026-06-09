"""Fleet supervisor (ADR 0042 slice 1) — start/stop/status over workspaces."""

from __future__ import annotations

import pytest

from graph.workspaces import manager
from graph.fleet import supervisor


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path / "ws"))
    alive: set[int] = set()
    monkeypatch.setattr(supervisor, "_alive", lambda pid: int(pid) in alive if pid else False)

    class FakeProc:
        def __init__(self, *a, **k):
            self.pid = 99001
            alive.add(99001)

    monkeypatch.setattr(supervisor.subprocess, "Popen", FakeProc)

    def fake_kill(pid, sig):  # SIGTERM/SIGKILL "kills" the fake process
        alive.discard(int(pid))

    monkeypatch.setattr(supervisor.os, "kill", fake_kill)
    return alive


def test_start_status_stop(fleet):
    manager.create("alpha", port=7890)

    r = supervisor.start("alpha")
    assert r["running"] and r["pid"] == 99001 and r["port"] == 7890 and not r["already"]
    assert supervisor.is_running("alpha")

    st = {s["name"]: s for s in supervisor.status()}
    assert st["alpha"]["running"] and st["alpha"]["port"] == 7890

    assert supervisor.start("alpha")["already"]  # idempotent

    supervisor.stop("alpha")
    assert not supervisor.is_running("alpha")
    assert not any(s["running"] for s in supervisor.status())


def test_start_unknown_workspace_errors(fleet):
    with pytest.raises(supervisor.FleetError):
        supervisor.start("ghost")


def test_stop_not_running_errors(fleet):
    manager.create("beta")
    with pytest.raises(supervisor.FleetError):
        supervisor.stop("beta")  # never started


def test_keep_n_warm_evicts_lru(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path / "ws"))
    alive: set[int] = set()
    seq = {"n": 5000}
    monkeypatch.setattr(supervisor, "_alive", lambda pid: int(pid) in alive if pid else False)

    class FakeProc:
        def __init__(self, *a, **k):
            seq["n"] += 1
            self.pid = seq["n"]
            alive.add(self.pid)

    monkeypatch.setattr(supervisor.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(supervisor.os, "kill", lambda pid, sig: alive.discard(int(pid)))

    for nm in ("a", "b", "c"):           # a started first → least-recently-active
        manager.create(nm)
        supervisor.start(nm)

    evicted = supervisor.enforce_warm_cap(keep=2, protect="c")
    assert evicted == ["a"]              # LRU evicted, protected one kept
    assert not supervisor.is_running("a")
    assert supervisor.is_running("b") and supervisor.is_running("c")
    assert supervisor.enforce_warm_cap(keep=0) == []   # 0 = unlimited, no-op
