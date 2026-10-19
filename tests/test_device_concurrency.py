"""Concurrency of the device registry and the pairing address cache.

* Every read-modify-write of ``devices.json`` holds ``security.devices._LOCK`` — rename,
  scope, revoke, claim, rotate, handoff, and `verify_token`'s own transitions (first use of a
  rotated token, last-seen, reuse revocation). Two writers can't interleave and lose one.
* `verify_token` decides each transition under that lock from a registry read under it; only a
  request that changes nothing is answered without the lock.
* ``pairing_routes._local_addresses`` probes once for a burst of callers (single-flight) and
  caches an empty answer too.

Threads, real files: the windows are widened by a sleep inside ``_load`` so an unlocked
read-decide-write would actually interleave here.
"""

from __future__ import annotations

import importlib
import threading
import time

import pytest

# Writes devices.json (chmod + os.replace) through security.devices.
pytestmark = pytest.mark.platform_sensitive


@pytest.fixture
def devices(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_BOX_ROOT", str(tmp_path))
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "test-device-concurrency")
    import infra.paths

    infra.paths.reset_instance_paths()
    import security.devices as mod

    importlib.reload(mod)
    mod.cancel_pairings()
    from security import claim_throttle

    claim_throttle.reset()
    yield mod
    claim_throttle.reset()
    infra.paths.reset_instance_paths()


@pytest.fixture
def clock(devices, monkeypatch):
    """A frozen registry clock: no grace or idle expiry can run out mid-test."""
    t = {"now": time.time()}
    monkeypatch.setattr(devices, "_now", lambda: t["now"])
    return t


@pytest.fixture
def audit(devices, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(devices, "_audit", lambda action, *_a, **_k: seen.append(action))
    return seen


class _CountingLock:
    """``_LOCK`` with a count of acquisitions (re-entrant, like the real one)."""

    def __init__(self) -> None:
        self.real = threading.RLock()
        self.acquired = 0

    def __enter__(self):
        self.real.acquire()
        self.acquired += 1
        return self

    def __exit__(self, *exc):
        self.real.release()
        return False

    def _is_owned(self) -> bool:
        return self.real._is_owned()


@pytest.fixture
def lock(devices, monkeypatch):
    counting = _CountingLock()
    monkeypatch.setattr(devices, "_LOCK", counting)
    return counting


@pytest.fixture
def saves(devices, lock, monkeypatch):
    """Every `_save`, asserting the registry lock is held by the writing thread."""
    real_save = devices._save
    seen: list[bool] = []
    guard = threading.Lock()

    def checked(rows):
        with guard:
            seen.append(lock._is_owned())
        real_save(rows)

    monkeypatch.setattr(devices, "_save", checked)
    return seen


@pytest.fixture
def slow_load(devices, monkeypatch):
    """Widen every read-decide-write window: a read is followed by a short pause."""
    real_load = devices._load

    def slow():
        rows = real_load()
        time.sleep(0.0005)
        return rows

    monkeypatch.setattr(devices, "_load", slow)


def _phone(devices, name="phone"):
    code, _ = devices.start_pairing()
    return devices.claim_pairing(code, name)


def _row(devices, device_id):
    return next((d for d in devices._load() if d.id == device_id), None)


# ── Every registry write holds the lock ───────────────────────────────────────────────────


def test_every_registry_writer_holds_the_lock(devices, clock, audit, saves):
    phone, token = _phone(devices)  # claim → _register
    tablet, _ = _phone(devices, "tablet")
    devices.rename_device(phone["id"], "renamed")
    devices.set_device_scope(phone["id"], "full")
    devices.set_device_scope(phone["id"], "device")
    devices.verify_token(token)  # last-seen stamp
    _view, token2 = devices.rotate_device(phone["id"], token)
    devices.verify_token(token2)  # first use of the new token: the grace starts
    code, _ = devices.start_handoff(phone["id"], token2)
    _view, token3 = devices.claim_pairing(code, "x")  # handoff claim → _rebind
    devices.revoke_device(tablet["id"])
    # A reuse revocation: rotate, use the new token, then present the old one after its grace.
    _view, token4 = devices.rotate_device(phone["id"], token3)
    devices.verify_token(token4)
    clock["now"] += devices.ROTATION_GRACE_SECONDS + 1
    assert devices.verify_token(token3) is None
    assert audit[-1] == "device.revoked_token_reuse"
    assert len(saves) >= 10
    assert all(saves), "a registry write ran without _LOCK"


def test_a_verify_that_changes_nothing_takes_no_lock(devices, clock, lock):
    phone, token = _phone(devices)
    assert devices.verify_token(token).id == phone["id"]  # stamps last-seen (locked)
    before = lock.acquired
    for _ in range(50):
        assert devices.verify_token(token).id == phone["id"]
    assert lock.acquired == before  # read-mostly: the common path never queues on the lock


# ── verify_token's transitions under concurrency ──────────────────────────────────────────


def test_a_burst_of_first_presentations_starts_the_grace_once(devices, clock, saves, slow_load):
    phone, token = _phone(devices)
    devices.verify_token(token)
    _view, new = devices.rotate_device(phone["id"], token)
    saves.clear()

    n = 12
    barrier = threading.Barrier(n)
    results: list[object] = [None] * n

    def present(i):
        barrier.wait()
        results[i] = devices.verify_token(new)

    threads = [threading.Thread(target=present, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(r is not None and r.id == phone["id"] for r in results)
    assert len(saves) == 1, f"the first-use transition was written {len(saves)} times"
    row = _row(devices, phone["id"])
    assert row.prev_retired_at == clock["now"]
    assert devices.verify_token(token).id == phone["id"]  # the old token is in its grace


def test_concurrent_verify_rotate_and_rename_lose_nothing_and_revoke_nothing(
    devices, clock, audit, saves, slow_load, monkeypatch
):
    """Rotations, a verify on every request (each one a last-seen write: throttle 0), renames
    and an unrelated revoke, all at once. Afterwards the device holds the LAST rotation's token
    and the LAST rename — a stale read written back would revert one — and nothing revoked it."""
    monkeypatch.setattr(devices, "_LAST_SEEN_THROTTLE_SECONDS", 0)
    phone, token = _phone(devices)
    other, other_token = _phone(devices, "tablet")
    held = {"token": token, "prev": token}
    stop = threading.Event()
    errors: list[BaseException] = []
    last_name = {"name": "phone"}

    def guard(fn):
        def run():
            try:
                fn()
            except BaseException as exc:  # noqa: BLE001 — surfaced by the assert below
                errors.append(exc)
                stop.set()

        return run

    @guard
    def rotator():
        for _ in range(15):
            current = held["token"]
            rotated = devices.rotate_device(phone["id"], current)
            assert rotated is not None, "a rotate presenting the current token was refused"
            _view, new = rotated
            assert devices.verify_token(new).id == phone["id"]
            held["prev"], held["token"] = current, new
        stop.set()

    @guard
    def verifier():
        while not stop.is_set():
            for tok in (held["token"], held["prev"]):
                found = devices.verify_token(tok)
                # A token rotated out twice is simply unknown (None) — never another device.
                assert found is None or found.id == phone["id"]

    @guard
    def renamer():
        i = 0
        while not stop.is_set():
            i += 1
            name = f"phone {i}"
            assert devices.rename_device(phone["id"], name) is not None
            last_name["name"] = name

    @guard
    def revoker():
        time.sleep(0.01)
        assert devices.revoke_device(other["id"]) is True

    threads = [threading.Thread(target=f) for f in (rotator, verifier, verifier, verifier, renamer, revoker)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors
    assert not any(t.is_alive() for t in threads)

    row = _row(devices, phone["id"])
    assert row is not None, "the phone was revoked"
    assert "device.revoked_token_reuse" not in audit
    assert row.token_sha256 == devices._hash(held["token"])  # the last rotation stuck
    assert row.name == last_name["name"]  # the last rename stuck
    assert row.scope == "device"
    assert devices.verify_token(held["token"]).id == phone["id"]
    assert devices.verify_token(other_token) is None and _row(devices, other["id"]) is None
    assert all(saves)


def test_the_previous_token_inside_its_grace_never_revokes_during_rotations(devices, clock, audit, slow_load):
    """Tabs still holding the previous token (in flight when the browser switched) keep
    presenting it while the new token is first used and rotated again: no revocation."""
    phone, token = _phone(devices)
    _view, new = devices.rotate_device(phone["id"], token)
    stop = threading.Event()
    errors: list[BaseException] = []

    def old_tab():
        try:
            while not stop.is_set():
                found = devices.verify_token(token)
                assert found is None or found.id == phone["id"]
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    tabs = [threading.Thread(target=old_tab) for _ in range(4)]
    for t in tabs:
        t.start()
    try:
        for _ in range(20):
            assert devices.verify_token(new).id == phone["id"]  # starts (or is inside) the grace
    finally:
        stop.set()
        for t in tabs:
            t.join()
    assert not errors, errors
    assert "device.revoked_token_reuse" not in audit
    assert _row(devices, phone["id"]) is not None
    assert devices.verify_token(token).id == phone["id"]  # still inside its grace


# ── The pairing address cache ─────────────────────────────────────────────────────────────


@pytest.fixture
def probes(monkeypatch):
    import operator_api.pairing_routes as pr

    monkeypatch.setattr(pr, "_ADDR_CACHE", {"at": None, "addrs": []})
    now = {"t": 1_000.0}
    monkeypatch.setattr(pr, "_monotonic", lambda: now["t"])
    state = {"calls": 0, "answer": {"8.8.8.8": "192.168.5.31", "100.100.100.100": "100.119.239.8"}}
    count = threading.Lock()

    def fake_probe(target):
        with count:
            state["calls"] += 1
        time.sleep(0.005)  # long enough for a burst of callers to pile up behind the probe
        return state["answer"].get(target)

    monkeypatch.setattr(pr, "_source_ip_for", fake_probe)
    return pr, now, state


def test_a_burst_after_the_ttl_probes_once(probes):
    pr, _now, state = probes
    n = 8
    barrier = threading.Barrier(n)
    results: list[list[str]] = [[] for _ in range(n)]

    def ask(i):
        barrier.wait()
        results[i] = pr._local_addresses()

    threads = [threading.Thread(target=ask, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert state["calls"] == len(pr._PROBE_TARGETS)  # one probe pass, not one per caller
    assert all(r == ["100.119.239.8", "192.168.5.31"] for r in results)


def test_a_found_answer_is_cached_for_the_ttl(probes):
    pr, now, state = probes
    first = pr._local_addresses()
    now["t"] += pr._ADDR_TTL_SECONDS - 0.1
    assert pr._local_addresses() == first
    assert state["calls"] == len(pr._PROBE_TARGETS)
    now["t"] += 0.2
    pr._local_addresses()
    assert state["calls"] == 2 * len(pr._PROBE_TARGETS)


def test_an_empty_answer_is_cached_for_the_shorter_ttl(probes):
    pr, now, state = probes
    state["answer"] = {}
    assert pr._local_addresses() == []
    now["t"] += pr._ADDR_EMPTY_TTL_SECONDS - 0.1
    assert pr._local_addresses() == []
    assert state["calls"] == len(pr._PROBE_TARGETS)  # no re-probe on every request
    # …but joining a network shows up within the short TTL.
    state["answer"] = {"8.8.8.8": "192.168.5.31"}
    now["t"] += 0.2
    assert pr._local_addresses() == ["192.168.5.31"]
    assert state["calls"] == 2 * len(pr._PROBE_TARGETS)
    assert pr._ADDR_EMPTY_TTL_SECONDS < pr._ADDR_TTL_SECONDS


def test_callers_get_a_copy_of_the_cache(probes):
    pr, _now, _state = probes
    pr._local_addresses().append("10.9.9.9")
    assert "10.9.9.9" not in pr._local_addresses()
