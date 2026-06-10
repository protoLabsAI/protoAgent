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
    monkeypatch.setattr(supervisor, "_is_our_agent", lambda pid: True)

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
    # The host self-registers as an always-running entry (ADR 0042); only peers stop.
    assert not any(s["running"] for s in supervisor.status() if not s.get("host"))


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
    monkeypatch.setattr(supervisor, "_is_our_agent", lambda pid: True)
    monkeypatch.setattr(supervisor.os, "kill", lambda pid, sig: alive.discard(int(pid)))

    ids = {}
    for nm in ("a", "b", "c"):           # a started first → least-recently-active
        ids[nm] = manager.create(nm)["id"]
        supervisor.start(nm)             # display name resolves to the id

    evicted = supervisor.enforce_warm_cap(keep=2, protect="c")  # protect by name too
    assert evicted == [ids["a"]]         # LRU evicted (state keys = ids), protected one kept
    assert not supervisor.is_running("a")
    assert supervisor.is_running("b") and supervisor.is_running("c")
    assert supervisor.enforce_warm_cap(keep=0) == []   # 0 = unlimited, no-op


# ── remote fleet members (ADR 0042 §I) ────────────────────────────────────────
def test_remote_member_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path / "ws"))
    rec = supervisor.add_remote("ava", "http://100.101.189.45:7871/", token="sek")
    assert rec["name"] == "ava" and rec["id"].startswith("ava-")
    assert rec["url"] == "http://100.101.189.45:7871"  # trailing slash trimmed
    assert "token" not in rec                           # never returned

    # proxy-side lookup DOES carry the token
    assert supervisor.remote_for_slug(rec["id"])["token"] == "sek"

    with pytest.raises(supervisor.FleetError):
        supervisor.add_remote("ava", "http://other:1")          # name taken
    with pytest.raises(supervisor.FleetError):
        supervisor.add_remote("ava2", "http://100.101.189.45:7871")  # url taken
    with pytest.raises(supervisor.FleetError):
        supervisor.add_remote("bad", "ftp://nope")              # not http(s)
    with pytest.raises(supervisor.FleetError):
        supervisor.add_remote("host", "http://h:1")             # reserved slug

    # status(): remote rides along with the cached probe (no probe yet → stopped)
    entry = next(a for a in supervisor.status() if a.get("remote"))
    assert entry["name"] == "ava" and entry["running"] is False
    assert entry["a2a"] == "http://100.101.189.45:7871/a2a" and entry["pid"] is None

    supervisor._probe_cache[rec["id"]] = (True, supervisor.time.monotonic())
    assert next(a for a in supervisor.status() if a.get("remote"))["running"] is True

    out = supervisor.remove_remote("ava")  # by name (id works too)
    assert out["removed"] == ["remote"] and supervisor.list_remotes() == []


def test_remote_name_collides_with_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path / "ws"))
    manager.create("alpha")
    with pytest.raises(supervisor.FleetError):
        supervisor.add_remote("alpha", "http://h:1")


def test_refresh_remote_probes_ttl(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path / "ws"))
    supervisor._probe_cache.clear()
    rec = supervisor.add_remote("ava", "http://h:9")
    calls = {"n": 0}

    class FakeResp:
        status_code = 200

    def fake_get(url, timeout):
        calls["n"] += 1
        return FakeResp()

    import httpx
    monkeypatch.setattr(httpx, "get", fake_get)
    supervisor.refresh_remote_probes()
    supervisor.refresh_remote_probes()  # within TTL — no second call
    assert calls["n"] == 1
    assert supervisor._probe_cache[rec["id"]][0] is True
