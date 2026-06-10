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
    assert entry["version"] == ""      # no probe yet — version unknown
    assert "token" not in entry        # the bearer NEVER leaves the registry via status()

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

        def json(self):
            return {"name": "ava", "version": "0.9.9"}

    def fake_get(url, timeout):
        calls["n"] += 1
        return FakeResp()

    import httpx
    monkeypatch.setattr(httpx, "get", fake_get)
    supervisor.refresh_remote_probes()
    supervisor.refresh_remote_probes()  # within TTL — no second call
    assert calls["n"] == 1
    assert supervisor._probe_cache[rec["id"]][0] is True


def test_probe_captures_remote_version(tmp_path, monkeypatch):
    """Hub↔remote version handshake: the probe lifts ``version`` off the remote's A2A
    card, persists it on the registry record (survives a hub restart), and status()
    surfaces it — while the stored bearer token still never leaves via status()."""
    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path / "ws"))
    supervisor._probe_cache.clear()
    rec = supervisor.add_remote("ava", "http://h:9", token="sek")

    class FakeResp:
        status_code = 200

        def json(self):
            return {"name": "ava", "version": "0.30.0"}

    import httpx
    monkeypatch.setattr(httpx, "get", lambda url, timeout: FakeResp())
    supervisor.refresh_remote_probes()

    entry = next(a for a in supervisor.status() if a.get("remote"))
    assert entry["version"] == "0.30.0" and entry["running"] is True
    assert "token" not in entry
    # persisted on the record (token intact for the proxy)
    stored = supervisor.remote_for_slug(rec["id"])
    assert stored["version"] == "0.30.0" and stored["token"] == "sek"
    # …and the hub's own version rides on the host entry, so the console can compare.
    host = next(a for a in supervisor.status() if a.get("host"))
    assert host["version"]


def test_probe_card_without_version_keeps_last_known(tmp_path, monkeypatch):
    """A card with no/blank version (or unparseable JSON) must not clobber the
    last-known value — last-good wins until the remote reports something new."""
    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path / "ws"))
    supervisor._probe_cache.clear()
    rec = supervisor.add_remote("ava", "http://h:9")
    supervisor._record_remote_version(rec["id"], "0.28.0")

    class NoVersionResp:
        status_code = 200

        def json(self):
            raise ValueError("not json")

    import httpx
    monkeypatch.setattr(httpx, "get", lambda url, timeout: NoVersionResp())
    supervisor.refresh_remote_probes()
    assert supervisor.remote_for_slug(rec["id"])["version"] == "0.28.0"
    assert next(a for a in supervisor.status() if a.get("remote"))["version"] == "0.28.0"


def test_remotes_lock_serializes_concurrent_adds(tmp_path, monkeypatch):
    """remotes.json RMW is FileLock-guarded (sibling of the fleet.json lock): two
    concurrent add_remote calls — e.g. two route handlers — must both land. The
    widened load→save window (sleep) loses one of them if the lock is ever dropped."""
    import threading

    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path / "ws"))
    orig_load = supervisor._load_remotes

    def slow_load():
        d = orig_load()
        supervisor.time.sleep(0.05)  # widen the read→write window
        return d

    monkeypatch.setattr(supervisor, "_load_remotes", slow_load)
    errs: list[Exception] = []

    def add(name, port):
        try:
            supervisor.add_remote(name, f"http://h:{port}")
        except Exception as e:  # noqa: BLE001 — surfaced below
            errs.append(e)

    threads = [threading.Thread(target=add, args=(n, p))
               for n, p in (("r-one", 1), ("r-two", 2))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errs
    assert {r["name"] for r in supervisor.list_remotes()} == {"r-one", "r-two"}
