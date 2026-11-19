"""Built-in member-health verification and shared supervisor sweep behavior."""

from __future__ import annotations

import pytest

from graph.fleet import supervisor
from graph.goals.verifiers import (
    VerifyContext,
    plugin_verifier_names,
    run_verifier,
    set_plugin_verifiers,
    verifier_catalog,
)

CHECK = "fleet:member_health"


@pytest.fixture(autouse=True)
def sweeps(monkeypatch):
    """Stub the supervisor's sweep pass and start stale. Yields its (threshold, timeout) calls."""
    calls: list[tuple] = []
    monkeypatch.setattr(supervisor, "_last_sweep", None)
    monkeypatch.setattr(supervisor, "_unclaimed_changes", [])

    def _pass(threshold, timeout):
        calls.append((threshold, timeout))
        monkeypatch.setattr(supervisor, "_last_sweep", supervisor.time.monotonic())
        return []

    monkeypatch.setattr(supervisor, "_sweep_once", _pass)
    return calls


def _health(monkeypatch, *records):
    """Stand in for the supervisor's in-memory health records."""
    monkeypatch.setattr(supervisor, "member_health", lambda: [dict(r) for r in records])


def _rec(state, *, mid="ws-1", name="Scout", **extra):
    return dict(id=mid, name=name, kind="local", state=state, **extra)


async def _check(**args):
    return await run_verifier({"type": "plugin", "check": CHECK, "args": args}, VerifyContext())


@pytest.mark.asyncio
async def test_builtin_survives_registration_reload_and_shadowing(monkeypatch):
    async def impostor(spec, ctx):
        raise AssertionError("the built-in must win")

    _health(monkeypatch, _rec("down"))
    for mapping in ({}, {"demo:check": impostor}, None, {CHECK: impostor}, {}):
        set_plugin_verifiers(mapping, {CHECK: {"plugin_id": "evil"}})
        assert plugin_verifier_names() == sorted({CHECK, *(mapping or {})})
        entry = next(c for c in verifier_catalog()["plugin_checks"] if c["name"] == CHECK)
        assert entry["plugin_id"] == "fleet"
        assert "member" in entry["description"] and "wedged" in entry["description"]
        assert (await _check(member="ws-1", state="down")).met is True


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["healthy", "down", "wedged"])
async def test_met_when_the_member_is_in_the_requested_state(monkeypatch, state):
    _health(monkeypatch, _rec(state), _rec("healthy", mid="ws-2", name="Other"))
    res = await _check(member="ws-1", state=state)
    assert res.met is True
    assert res.reason == f"member Scout is {state}"
    assert f"state={state}" in res.evidence and "member=ws-1" in res.evidence
    for other in {"healthy", "down", "wedged"} - {state}:
        assert (await _check(member="ws-1", state=other)).met is False


@pytest.mark.asyncio
async def test_member_by_id_or_name_state_default_list_and_no_record(monkeypatch):
    _health(monkeypatch, _rec("wedged", mid="ws-1", name="Scout"), _rec("healthy", mid="Scout", name="Decoy"))
    assert (await _check(member="scout", state="wedged")).met is True  # name, any case
    assert (await _check(member="Scout", state="wedged")).met is False  # an exact id wins over a name
    assert (await _check(member="ws-1")).met is False  # state defaults to down
    assert (await _check(member="ws-1", state=["down", "wedged"])).met is True
    _health(monkeypatch, _rec("down"))
    assert (await _check(member="ws-1")).met is True
    assert (await _check(member="ws-1", state="DOWN")).met is True
    _health(monkeypatch)  # stopped on purpose, or not in this fleet: no record, no state
    for state in ("healthy", "down", "wedged"):
        res = await _check(member="ws-1", state=state)
        assert res.met is False and "no health record" in res.reason and res.evidence == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "needle"),
    [
        ({}, "needs a 'member'"),
        ({"member": "  "}, "needs a 'member'"),
        ({"member": "ws-1", "state": "asleep"}, "unknown state asleep"),
        ({"member": "ws-1", "state": ["down", "gone"]}, "unknown state gone"),
    ],
)
async def test_bad_args_are_not_met_and_never_reach_the_supervisor(args, needle, sweeps):
    res = await _check(**args)
    assert res.met is False and needle in res.reason and sweeps == []


@pytest.mark.asyncio
async def test_result_is_stable_within_a_state_and_moves_on_a_change(monkeypatch):
    # Failure counts must not trigger an on_change watch within the same state.
    _health(monkeypatch, _rec("healthy", failures=0))
    first = await _check(member="ws-1", state="down")
    _health(monkeypatch, _rec("healthy", failures=2))
    again = await _check(member="ws-1", state="down")
    assert (first.reason, first.evidence) == (again.reason, again.evidence)
    _health(monkeypatch, _rec("down", changed_at="2026-11-18T10:05:00+00:00"))
    moved = await _check(member="ws-1", state="down")
    assert moved.met is True and (moved.reason, moved.evidence) != (first.reason, first.evidence)


@pytest.mark.asyncio
async def test_a_stale_read_runs_the_supervisors_sweep_and_a_fresh_one_does_not(monkeypatch, sweeps):
    _health(monkeypatch, _rec("down"))
    assert (await _check(member="ws-1")).met is True
    await _check(member="ws-2")
    assert len(sweeps) == 1  # inside the freshness window: watches share one sweep
    monkeypatch.setattr(supervisor, "_last_sweep", supervisor.time.monotonic() - 16)
    await _check(member="ws-1")
    assert len(sweeps) == 2


@pytest.mark.asyncio
async def test_failed_health_read_is_not_met_and_can_recover(monkeypatch):
    def fail(threshold, timeout):
        raise OSError("unavailable")

    monkeypatch.setattr(supervisor, "_sweep_once", fail)
    assert (await _check(member="ws-1")).met is False
    monkeypatch.setattr(supervisor, "_sweep_once", lambda threshold, timeout: [])
    _health(monkeypatch, _rec("down"))
    assert (await _check(member="ws-1")).met is True


def test_failed_background_sweep_retains_watch_discovered_changes(monkeypatch):
    change = {"id": "ws-1", "previous": "healthy", "state": "down"}
    monkeypatch.setattr(supervisor, "_sweep_once", lambda threshold, timeout: [change])
    _health(monkeypatch, _rec("down"))
    supervisor.fresh_member_health(15, 3)

    def fail(threshold, timeout):
        raise OSError("unavailable")

    monkeypatch.setattr(supervisor, "_sweep_once", fail)
    with pytest.raises(OSError):
        supervisor.health_sweep(3)
    monkeypatch.setattr(supervisor, "_sweep_once", lambda threshold, timeout: [])
    assert supervisor.health_sweep(3) == [change]
    assert supervisor.health_sweep(3) == []


def test_background_sweep_waits_for_watch_sweep_and_claims_its_changes(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    started, release, background_started, overlapping = Event(), Event(), Event(), Event()
    change = {"id": "ws-1", "previous": "healthy", "state": "down"}

    def sweep(threshold, timeout):
        if not started.is_set():
            started.set()
            assert release.wait(5)
            return [change]
        if not release.is_set():
            overlapping.set()
        return []

    def background():
        background_started.set()
        return supervisor.health_sweep(3)

    monkeypatch.setattr(supervisor, "_sweep_once", sweep)
    _health(monkeypatch, _rec("down"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        watch = pool.submit(supervisor.fresh_member_health, 15, 3)
        try:
            assert started.wait(5)
            loop = pool.submit(background)
            assert background_started.wait(5)
            assert not overlapping.wait(0.1)
        finally:
            release.set()
        assert watch.result(timeout=5)[0]["state"] == "down"
        assert loop.result(timeout=5) == [change]
    assert supervisor.health_sweep(3) == []
