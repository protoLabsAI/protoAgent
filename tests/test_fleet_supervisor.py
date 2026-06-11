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


# ── spin-down on host exit (version-coherence Axis 1) ─────────────────────────
def _multi_fleet(tmp_path, monkeypatch):
    """Fleet with INCREMENTING fake pids (distinct members) + a recording kill.
    Returns (alive set, killed list of (pid, signal))."""
    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path / "ws"))
    alive: set[int] = set()
    seq = {"n": 6000}
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(supervisor, "_alive", lambda pid: int(pid) in alive if pid else False)

    class FakeProc:
        def __init__(self, *a, **k):
            seq["n"] += 1
            self.pid = seq["n"]
            alive.add(self.pid)

    monkeypatch.setattr(supervisor.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(supervisor, "_is_our_agent", lambda pid: True)

    def fake_kill(pid, sig):
        killed.append((int(pid), int(sig)))
        alive.discard(int(pid))  # the fake dies on its first signal (SIGTERM)

    monkeypatch.setattr(supervisor.os, "kill", fake_kill)
    return alive, killed


def test_shutdown_all_stops_every_member(tmp_path, monkeypatch):
    alive, _ = _multi_fleet(tmp_path, monkeypatch)
    for nm in ("a", "b", "c"):
        manager.create(nm)
        supervisor.start(nm)
    assert len(alive) == 3

    stopped = supervisor.shutdown_all(timeout=1.0)

    assert len(stopped) == 3
    assert not alive  # every member signalled dead
    # registry reaped — nothing running but the always-present host entry
    assert not any(s["running"] for s in supervisor.status() if not s.get("host"))


def test_shutdown_all_opt_out_keeps_members(tmp_path, monkeypatch):
    alive, killed = _multi_fleet(tmp_path, monkeypatch)
    monkeypatch.setenv("PROTOAGENT_FLEET_KEEP_MEMBERS_ON_EXIT", "1")
    manager.create("a")
    supervisor.start("a")

    assert supervisor.shutdown_all(timeout=1.0) == []  # opt-out → no-op
    assert len(alive) == 1 and not killed              # member untouched
    assert supervisor.is_running("a")


def test_shutdown_all_no_members_is_noop(tmp_path, monkeypatch):
    _multi_fleet(tmp_path, monkeypatch)  # mocks wired, nothing started
    # Empty registry — also the member-scope case (a member's own fleet.json is empty).
    assert supervisor.shutdown_all(timeout=1.0) == []


def test_shutdown_all_sigkills_straggler(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path / "ws"))
    alive: set[int] = set()
    seq = {"n": 7000}
    sigs: list[int] = []
    monkeypatch.setattr(supervisor, "_alive", lambda pid: int(pid) in alive if pid else False)

    class FakeProc:
        def __init__(self, *a, **k):
            seq["n"] += 1
            self.pid = seq["n"]
            alive.add(self.pid)

    monkeypatch.setattr(supervisor.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(supervisor, "_is_our_agent", lambda pid: True)

    def stubborn_kill(pid, sig):  # ignores SIGTERM; dies only on SIGKILL
        sigs.append(int(sig))
        if int(sig) == int(supervisor.signal.SIGKILL):
            alive.discard(int(pid))

    monkeypatch.setattr(supervisor.os, "kill", stubborn_kill)
    manager.create("a")
    supervisor.start("a")

    assert supervisor.shutdown_all(timeout=0.2)  # returns the stopped member
    assert not alive  # SIGKILL'd after the bounded wait
    assert int(supervisor.signal.SIGTERM) in sigs and int(supervisor.signal.SIGKILL) in sigs


# ── first-boot-after-update reconcile (version-coherence P2) ──────────────────
def test_start_stamps_spawner_version(fleet):
    from infra.paths import package_version

    manager.create("alpha", port=7890)
    supervisor.start("alpha")
    rec = next(iter(supervisor._load_state().values()))
    assert rec["version"] == package_version()
    entry = next(a for a in supervisor.status() if a["name"] == "alpha")
    assert entry["version"] == package_version()


def test_version_skew_warning_flags_and_clears(tmp_path, monkeypatch):
    """A live member spawned by an older binary is flagged; restarting it (so the
    current binary respawns it) clears the warning — no hub restart needed."""
    import infra.paths as paths_mod

    _multi_fleet(tmp_path, monkeypatch)
    monkeypatch.setattr(paths_mod, "package_version", lambda: "0.1.0")
    manager.create("a")
    supervisor.start("a")  # spawned at 0.1.0
    assert supervisor.version_skew_warning() is None  # versions match

    # "App update": this process now runs 0.2.0; the member still runs 0.1.0.
    monkeypatch.setattr(paths_mod, "package_version", lambda: "0.2.0")
    warn = supervisor.version_skew_warning()
    assert warn and "a (v0.1.0)" in warn and "0.2.0" in warn

    supervisor.stop("a")
    supervisor.start("a")  # respawned by the "new" binary
    assert supervisor.version_skew_warning() is None


def test_version_skew_flags_prestamp_records_as_unknown(tmp_path, monkeypatch):
    """A record written before version stamping existed can't be told apart from a
    stale one — flag it as unknown rather than assuming it's current."""
    _multi_fleet(tmp_path, monkeypatch)
    manager.create("a")
    supervisor.start("a")
    with supervisor._state_lock():
        state = supervisor._load_state()
        next(iter(state.values())).pop("version", None)
        supervisor._save_state(state)
    warn = supervisor.version_skew_warning()
    assert warn and "version unknown" in warn


def test_reconcile_on_boot_stamps_and_returns_previous(tmp_path, monkeypatch):
    import infra.paths as paths_mod

    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path / "ws"))
    monkeypatch.setattr(paths_mod, "package_version", lambda: "0.1.0")
    assert supervisor.reconcile_on_boot() == ""  # first boot — no stamp yet
    assert supervisor._version_stamp_path().read_text().strip() == "0.1.0"
    assert supervisor.reconcile_on_boot() == "0.1.0"  # same version, stamp stable

    monkeypatch.setattr(paths_mod, "package_version", lambda: "0.2.0")
    assert supervisor.reconcile_on_boot() == "0.1.0"  # the update is visible once…
    assert supervisor._version_stamp_path().read_text().strip() == "0.2.0"  # …then re-stamped
