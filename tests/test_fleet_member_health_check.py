"""The built-in ``fleet:member_health`` watch check (ADR 0135 D8, #5534).

It lives in the ONE verifier registry plugins contribute to, so it is there with no plugin
installed and survives the rebind a plugin reload does. It reads the supervisor's health
records (C1a) and never probes.
"""

from __future__ import annotations

import pytest

from graph.fleet import supervisor
from graph.goals.verifiers import (
    FLEET_MEMBER_HEALTH_CHECK,
    VerifyContext,
    plugin_verifier_names,
    run_verifier,
    set_plugin_verifiers,
    verifier_catalog,
)

CHECK = "fleet:member_health"


def _health(monkeypatch, *records):
    """Stand in for the supervisor's in-memory health records."""
    monkeypatch.setattr(supervisor, "member_health", lambda: [dict(r) for r in records])


def _rec(state, *, mid="ws-1", name="Scout", kind="local", changed_at="2026-11-18T10:00:00+00:00", failures=0):
    return {
        "id": mid,
        "name": name,
        "kind": kind,
        "state": state,
        "failures": failures,
        "changed_at": changed_at,
        "pid": 4242 if state != "down" else None,
    }


async def _check(**args):
    return await run_verifier({"type": "plugin", "check": CHECK, "args": args}, VerifyContext())


def test_registered_with_no_plugin_installed():
    assert FLEET_MEMBER_HEALTH_CHECK == CHECK
    assert CHECK in plugin_verifier_names()
    entry = next(c for c in verifier_catalog()["plugin_checks"] if c["name"] == CHECK)
    assert entry["plugin_id"] == "fleet"
    assert "member" in entry["description"] and "wedged" in entry["description"]


def test_a_plugin_reload_keeps_it():
    async def _other(spec, ctx):  # pragma: no cover - never called
        raise AssertionError

    set_plugin_verifiers({"demo:check": _other}, {"demo:check": {"plugin_id": "demo", "description": "d"}})
    assert plugin_verifier_names() == ["demo:check", CHECK]
    set_plugin_verifiers({})  # the plugin went away
    assert plugin_verifier_names() == [CHECK]
    set_plugin_verifiers(None)
    assert plugin_verifier_names() == [CHECK]
    assert [c["name"] for c in verifier_catalog()["plugin_checks"]] == [CHECK]


@pytest.mark.asyncio
async def test_a_plugin_cannot_shadow_the_builtin(monkeypatch):
    async def _impostor(spec, ctx):
        raise AssertionError("the built-in must win")

    _health(monkeypatch, _rec("down"))
    set_plugin_verifiers({CHECK: _impostor}, {CHECK: {"plugin_id": "evil", "description": "x"}})
    assert (await _check(member="ws-1", state="down")).met is True
    assert next(c for c in verifier_catalog()["plugin_checks"] if c["name"] == CHECK)["plugin_id"] == "fleet"


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
async def test_member_matches_by_id_or_name(monkeypatch):
    _health(monkeypatch, _rec("wedged", mid="ws-1", name="Scout"), _rec("healthy", mid="Scout", name="Decoy"))
    assert (await _check(member="ws-1", state="wedged")).met is True
    assert (await _check(member="scout", state="wedged")).met is True  # name, any case
    assert (await _check(member="Scout", state="wedged")).met is False  # an exact id wins over a name


@pytest.mark.asyncio
async def test_state_defaults_to_down_and_accepts_a_list(monkeypatch):
    _health(monkeypatch, _rec("wedged"))
    assert (await _check(member="ws-1")).met is False
    assert (await _check(member="ws-1", state=["down", "wedged"])).met is True
    _health(monkeypatch, _rec("down"))
    assert (await _check(member="ws-1")).met is True
    assert (await _check(member="ws-1", state="DOWN")).met is True


@pytest.mark.asyncio
async def test_no_health_record_is_not_met_in_any_state(monkeypatch):
    # Stopped on purpose, not a member of this fleet, or not swept yet: no record, no state.
    _health(monkeypatch)
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
async def test_bad_args_are_not_met_and_never_read_health(monkeypatch, args, needle):
    def _boom():
        raise AssertionError("bad args must not reach the supervisor")

    monkeypatch.setattr(supervisor, "member_health", _boom)
    res = await _check(**args)
    assert res.met is False and needle in res.reason


@pytest.mark.asyncio
async def test_result_is_stable_within_a_state_and_moves_on_a_change(monkeypatch):
    """An on_change watch fires when reason or evidence differ, so neither may carry a value
    that moves between checks of the same state (the failure counter does)."""
    _health(monkeypatch, _rec("healthy", failures=0))
    first = await _check(member="ws-1", state="down")
    _health(monkeypatch, _rec("healthy", failures=2))
    again = await _check(member="ws-1", state="down")
    assert (first.reason, first.evidence) == (again.reason, again.evidence)
    _health(monkeypatch, _rec("down", changed_at="2026-11-18T10:05:00+00:00"))
    moved = await _check(member="ws-1", state="down")
    assert moved.met is True and (moved.reason, moved.evidence) != (first.reason, first.evidence)


@pytest.mark.asyncio
async def test_reads_the_supervisors_own_records(monkeypatch):
    # No stand-in: the check reads what health_sweep recorded in the supervisor.
    monkeypatch.setattr(supervisor, "_health", {"ws-9": _rec("wedged", mid="ws-9", name="Nine")})
    res = await _check(member="Nine", state="wedged")
    assert res.met is True and "member=ws-9" in res.evidence


@pytest.mark.asyncio
async def test_a_failing_health_read_is_contained(monkeypatch):
    def _boom():
        raise RuntimeError("supervisor unavailable")

    monkeypatch.setattr(supervisor, "member_health", _boom)
    res = await _check(member="ws-1", state="down")
    assert res.met is False and res.reason == "verifier error: RuntimeError"
