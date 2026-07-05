"""Fleet supervisor (ADR 0042 slice 1) — start/stop/status over workspaces."""

from __future__ import annotations

import os
import signal
import socket
import time

import pytest

from graph.workspaces import manager
from graph.fleet import supervisor


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path / "ws"))
    alive: set[int] = set()
    monkeypatch.setattr(supervisor, "_alive", lambda pid: int(pid) in alive if pid else False)

    class FakeProc:
        returncode = None

        def __init__(self, *a, **k):
            self.pid = 99001
            alive.add(99001)

        def poll(self):  # boot watch: still running
            return None

    monkeypatch.setattr(supervisor.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(supervisor, "_is_our_agent", lambda pid: True)
    # Fake spawns never bind a port — short-circuit the boot watch to "it's up".
    monkeypatch.setattr(supervisor, "_port_listening", lambda port, timeout=0.25: True)

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
        returncode = None

        def __init__(self, *a, **k):
            seq["n"] += 1
            self.pid = seq["n"]
            alive.add(self.pid)

        def poll(self):  # boot watch: still running
            return None

    monkeypatch.setattr(supervisor.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(supervisor, "_is_our_agent", lambda pid: True)
    monkeypatch.setattr(supervisor, "_port_listening", lambda port, timeout=0.25: True)
    monkeypatch.setattr(supervisor.os, "kill", lambda pid, sig: alive.discard(int(pid)))

    ids = {}
    for nm in ("a", "b", "c"):  # a started first → least-recently-active
        ids[nm] = manager.create(nm)["id"]
        supervisor.start(nm)  # display name resolves to the id

    evicted = supervisor.enforce_warm_cap(keep=2, protect="c")  # protect by name too
    assert evicted == [ids["a"]]  # LRU evicted (state keys = ids), protected one kept
    assert not supervisor.is_running("a")
    assert supervisor.is_running("b") and supervisor.is_running("c")
    assert supervisor.enforce_warm_cap(keep=0) == []  # 0 = unlimited, no-op


# ── remote fleet members (ADR 0042 §I) ────────────────────────────────────────
def test_remote_member_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path / "ws"))
    rec = supervisor.add_remote("ava", "http://100.101.189.45:7871/", token="sek")
    assert rec["name"] == "ava" and rec["id"].startswith("ava-")
    assert rec["url"] == "http://100.101.189.45:7871"  # trailing slash trimmed
    assert "token" not in rec  # never returned

    # proxy-side lookup DOES carry the token
    assert supervisor.remote_for_slug(rec["id"])["token"] == "sek"

    with pytest.raises(supervisor.FleetError):
        supervisor.add_remote("ava", "http://other:1")  # name taken
    with pytest.raises(supervisor.FleetError):
        supervisor.add_remote("ava2", "http://100.101.189.45:7871")  # url taken
    with pytest.raises(supervisor.FleetError):
        supervisor.add_remote("bad", "ftp://nope")  # not http(s)
    with pytest.raises(supervisor.FleetError):
        supervisor.add_remote("host", "http://h:1")  # reserved slug
    # SSRF guard (#871): cloud-metadata / link-local is blocked even though private
    # LAN/tailnet remotes (like ava above) are allowed.
    with pytest.raises(supervisor.FleetError):
        supervisor.add_remote("meta", "http://169.254.169.254/latest/meta-data/")

    # status(): remote rides along with the cached probe (no probe yet → stopped)
    entry = next(a for a in supervisor.status() if a.get("remote"))
    assert entry["name"] == "ava" and entry["running"] is False
    assert entry["a2a"] == "http://100.101.189.45:7871/a2a" and entry["pid"] is None
    assert entry["version"] == ""  # no probe yet — version unknown
    assert "token" not in entry  # the bearer NEVER leaves the registry via status()

    supervisor._probe_cache[rec["id"]] = (True, supervisor.time.monotonic())
    assert next(a for a in supervisor.status() if a.get("remote"))["running"] is True

    out = supervisor.remove_remote("ava")  # by name (id works too)
    assert out["removed"] == ["remote"] and supervisor.list_remotes() == []


def test_remote_name_collides_with_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path / "ws"))
    manager.create("alpha")
    with pytest.raises(supervisor.FleetError):
        supervisor.add_remote("alpha", "http://h:1")


def test_update_remote_edits_url_token_name_in_place(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path / "ws"))
    rec = supervisor.add_remote("ava", "http://100.64.0.9:7870", token="old")
    rid = rec["id"]

    # Only-provided-fields change; the id (slug + data scope) is stable.
    out = supervisor.update_remote(rid, url="http://100.64.0.9:7999", token="new", name="ava2")
    assert out["id"] == rid and out["name"] == "ava2" and out["url"] == "http://100.64.0.9:7999"
    assert "token" not in out  # sanitized — the bearer never leaves via the API
    stored = supervisor.remote_for_slug(rid)  # proxy-side lookup DOES carry it
    assert stored["token"] == "new" and stored["url"] == "http://100.64.0.9:7999"

    # token=None keeps the stored bearer; token="" clears it.
    supervisor.update_remote(rid, url="http://100.64.0.9:8001")  # no token kwarg
    assert supervisor.remote_for_slug(rid)["token"] == "new"
    supervisor.update_remote(rid, token="")
    assert supervisor.remote_for_slug(rid)["token"] == ""


def test_update_remote_rejects_bad_url_collision_and_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path / "ws"))
    a = supervisor.add_remote("ava", "http://100.64.0.9:7870")
    supervisor.add_remote("bo", "http://100.64.0.10:7870")

    with pytest.raises(supervisor.FleetError):
        supervisor.update_remote(a["id"], url="ftp://nope")  # not http(s)
    with pytest.raises(supervisor.FleetError):
        supervisor.update_remote(a["id"], url="http://169.254.169.254/")  # SSRF egress guard
    with pytest.raises(supervisor.FleetError):
        supervisor.update_remote(a["id"], url="http://100.64.0.10:7870")  # bo's url — collision
    with pytest.raises(supervisor.FleetError):
        supervisor.update_remote("ghost", token="x")  # no such remote
    # Editing to its OWN current url is fine (the collision check excludes self).
    assert supervisor.update_remote(a["id"], url="http://100.64.0.9:7870")["url"] == "http://100.64.0.9:7870"


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


def test_probe_ttl_is_three_seconds():
    """The reachability cache TTL is aligned to the 3s console poll (remote-member
    health robustness) so a just-downed remote isn't shown 'running' for ~10s."""
    assert supervisor._PROBE_TTL == 3.0


def test_probe_remote_reachable_returns_version(tmp_path, monkeypatch):
    """probe_remote() probes ONE member immediately (TTL-bypass) and returns
    (reachable, version) — what the register route surfaces so the console can warn
    up front. It refreshes the cache and persists the probed version."""
    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path / "ws"))
    supervisor._probe_cache.clear()
    rec = supervisor.add_remote("ava", "http://h:9", token="sek")

    class FakeResp:
        status_code = 200

        def json(self):
            return {"name": "ava", "version": "0.31.0"}

    import httpx

    monkeypatch.setattr(httpx, "get", lambda url, timeout: FakeResp())
    reachable, version = supervisor.probe_remote(rec["id"])
    assert reachable is True and version == "0.31.0"
    assert supervisor._probe_cache[rec["id"]][0] is True
    # persisted on the record (token left intact for the proxy)
    stored = supervisor.remote_for_slug(rec["id"])
    assert stored["version"] == "0.31.0" and stored["token"] == "sek"
    # by NAME resolves too
    assert supervisor.probe_remote("ava")[0] is True


def test_probe_remote_unreachable_is_false(tmp_path, monkeypatch):
    """An unreachable peer probes reachable:false (no version) — registration is NOT
    rejected for it (deferred registration is intentional)."""
    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path / "ws"))
    supervisor._probe_cache.clear()
    rec = supervisor.add_remote("ghosty", "http://h:9")

    import httpx

    def boom(url, timeout):
        raise httpx.HTTPError("connection refused")

    monkeypatch.setattr(httpx, "get", boom)
    reachable, version = supervisor.probe_remote(rec["id"])
    assert reachable is False and version == ""
    assert supervisor._probe_cache[rec["id"]][0] is False


def test_probe_remote_unknown_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path / "ws"))
    with pytest.raises(supervisor.FleetError):
        supervisor.probe_remote("nope")


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

    threads = [threading.Thread(target=add, args=(n, p)) for n, p in (("r-one", 1), ("r-two", 2))]
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
        returncode = None

        def __init__(self, *a, **k):
            seq["n"] += 1
            self.pid = seq["n"]
            alive.add(self.pid)

        def poll(self):  # boot watch: still running
            return None

    monkeypatch.setattr(supervisor.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(supervisor, "_is_our_agent", lambda pid: True)
    monkeypatch.setattr(supervisor, "_port_listening", lambda port, timeout=0.25: True)

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
    assert len(alive) == 1 and not killed  # member untouched
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
        returncode = None

        def __init__(self, *a, **k):
            seq["n"] += 1
            self.pid = seq["n"]
            alive.add(self.pid)

        def poll(self):  # boot watch: still running
            return None

    monkeypatch.setattr(supervisor.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(supervisor, "_is_our_agent", lambda pid: True)
    monkeypatch.setattr(supervisor, "_port_listening", lambda port, timeout=0.25: True)

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


# ── autostart roster (ADR 0072 slice) ────────────────────────────────────────
# A container recreate / host restart kills the detached member processes; the hub
# (re)starts every configured `fleet.autostart` member on boot so the declared crew
# comes back up instead of staying down until re-activated by hand.


class _Cfg:
    def __init__(self, roster):
        self.fleet_autostart = roster


def test_autostart_members_reads_config_then_env(monkeypatch):
    # Live config wins.
    monkeypatch.setattr(supervisor, "_live_config", lambda: _Cfg(["cindi", "matt"]))
    assert supervisor.autostart_members() == ["cindi", "matt"]

    # A comma-separated string (hand-edited YAML) is normalised, blanks dropped.
    monkeypatch.setattr(supervisor, "_live_config", lambda: _Cfg(" a , , b "))
    assert supervisor.autostart_members() == ["a", "b"]

    # No config → the PROTOAGENT_FLEET_AUTOSTART env fallback (comma-separated).
    monkeypatch.setattr(supervisor, "_live_config", lambda: None)
    monkeypatch.setenv("PROTOAGENT_FLEET_AUTOSTART", "x, y ,")
    assert supervisor.autostart_members() == ["x", "y"]

    # Nothing configured → empty (the common case; boot hook no-ops).
    monkeypatch.delenv("PROTOAGENT_FLEET_AUTOSTART", raising=False)
    assert supervisor.autostart_members() == []


def test_start_autostart_members_starts_declared(fleet, monkeypatch):
    manager.create("cindi", port=7891)
    manager.create("matt", port=7892)
    monkeypatch.setattr(supervisor, "autostart_members", lambda: ["cindi", "matt"])

    started = supervisor.start_autostart_members()
    assert set(started) == {"cindi", "matt"}
    assert supervisor.is_running("cindi") and supervisor.is_running("matt")


def test_start_autostart_skips_running_and_missing(fleet, monkeypatch):
    manager.create("cindi", port=7891)
    manager.create("matt", port=7892)
    supervisor.start("cindi")  # already up → must be skipped, not double-spawned
    # "ghost" has no workspace → skipped with a warning, never raises.
    monkeypatch.setattr(supervisor, "autostart_members", lambda: ["cindi", "ghost", "matt"])

    started = supervisor.start_autostart_members()
    assert started == ["matt"]  # only the down, existing member is (re)started
    assert supervisor.is_running("matt")


def test_start_autostart_no_roster_is_noop(fleet, monkeypatch):
    monkeypatch.setattr(supervisor, "autostart_members", lambda: [])
    assert supervisor.start_autostart_members() == []


def test_start_autostart_is_hub_only_even_with_inherited_env(fleet, monkeypatch):
    # A member inherits the hub's PROTOAGENT_FLEET_AUTOSTART, so the roster is non-empty
    # inside a member too. Acting on it would scan the member's own (empty) workspaces root
    # and warn "no workspace <id> — skipping" for every id. It must bail before that.
    manager.create("cindi", port=7891)
    monkeypatch.setattr(supervisor, "autostart_members", lambda: ["cindi", "matt"])
    monkeypatch.setattr(supervisor.manager, "is_workspace_member", lambda: True)

    assert supervisor.start_autostart_members() == []
    assert not supervisor.is_running("cindi")  # nothing spawned inside a member


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


# ── boot-failure feedback (#1565 fallout) ─────────────────────────────────────
# A member that dies at boot used to report `running: true` (the pid existed for a
# moment) and just show up dead on the next poll — the reason lived only in
# agent.log, which nothing surfaced. start() now watches the fresh spawn and raises
# with the log tail; these run REAL subprocesses (no FakeProc) to prove it.


def _real_spawn_env(tmp_path, monkeypatch, argv):
    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path / "ws"))
    monkeypatch.setattr(manager, "run_exec", lambda ident, passthrough: ({}, argv))


def test_start_surfaces_boot_death(tmp_path, monkeypatch):
    import sys

    _real_spawn_env(
        tmp_path,
        monkeypatch,
        [sys.executable, "-c", "import sys; sys.stderr.write('kaboom: unrecognized arguments'); sys.exit(3)"],
    )
    ws = manager.create("dies", port=_free_port())  # free port — nothing binds it; the child exits first
    with pytest.raises(supervisor.FleetError) as exc:
        supervisor.start("dies")
    msg = str(exc.value)
    assert "exit code 3" in msg and "kaboom" in msg  # the reason, not a generic failure
    assert not supervisor.is_running("dies")  # the dead entry was reaped, not left as a zombie
    assert (tmp_path / "ws" / ws["id"] / "agent.log").exists()


def test_start_returns_once_port_binds(tmp_path, monkeypatch):
    import sys

    port = _free_port()  # a genuinely free port for the fake member to bind
    child = (
        "import socket,time\n"
        f"s=socket.socket(); s.bind(('127.0.0.1',{port})); s.listen(1)\n"
        "time.sleep(30)\n"
    )
    _real_spawn_env(tmp_path, monkeypatch, [sys.executable, "-c", child])
    manager.create("binds", port=port)
    t0 = time.monotonic()
    try:
        rec = supervisor.start("binds")
        assert rec["running"] and not rec["already"]
        assert time.monotonic() - t0 < supervisor._BOOT_WATCH_SECONDS  # early exit on bind, not the full watch
    finally:
        try:
            os.kill(rec["pid"], signal.SIGKILL)
        except (OSError, UnboundLocalError):
            pass


def test_up_reports_boot_death_and_continues(tmp_path, monkeypatch):
    import sys

    _real_spawn_env(tmp_path, monkeypatch, [sys.executable, "-c", "raise SystemExit(2)"])
    manager.create("brokey", port=_free_port())
    rows = supervisor.up(["brokey"])  # must not raise — the row carries the error
    assert rows[0]["name"] == "brokey" and rows[0]["running"] is False
    assert "exit code 2" in rows[0]["error"]


# ── member self-report on the host entry (#1708) ──────────────────────────────
def test_host_entry_member_flag(fleet, monkeypatch):
    """A spawned workspace member self-reports ``member: True`` on its own /api/fleet
    host entry (the console's signal to gate hub-only affordances); a hub/standalone
    host entry doesn't carry the key at all — the payload shape is unchanged for them."""
    host = next(a for a in supervisor.status() if a.get("host"))
    assert "member" not in host  # hub/standalone: no new field

    monkeypatch.setattr(manager, "is_workspace_member", lambda: True)
    host = next(a for a in supervisor.status() if a.get("host"))
    assert host["member"] is True
