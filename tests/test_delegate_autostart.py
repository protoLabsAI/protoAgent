"""Starting a stopped local delegate on demand, with consent (#3126).

The recoverable case is narrow on purpose: a member of THIS box's fleet, reachable
only over loopback, that nothing is currently listening for. Everything else — a
remote peer, a timeout, an agent that answered with an error — must reach the operator
exactly as it did before, because restarting an agent underneath a live turn is a
footgun rather than a fix.
"""

from __future__ import annotations

import pytest

from plugins.delegates import autostart as A


@pytest.fixture(autouse=True)
def _clean_state():
    A._LAST_ATTEMPT.clear()
    A._GRANTS.clear()
    yield
    A._LAST_ATTEMPT.clear()
    A._GRANTS.clear()


def _roster(monkeypatch, entries):
    """Stand in for the fleet roster.

    Patches the ATTRIBUTE on the `graph.fleet` package, not `sys.modules` — replacing a
    module entry is visible to anything that imports it during the window, and these
    tests sit beside the supervisor's own. The attribute is what
    `from graph.fleet import supervisor` binds, so it is enough.
    """
    import types

    fake = types.SimpleNamespace(status=lambda: entries, _port_listening=lambda p: True)
    monkeypatch.setattr("graph.fleet.supervisor", fake, raising=False)
    return fake


# ── who is startable ──────────────────────────────────────────────────────────


def test_a_stopped_loopback_member_is_startable(monkeypatch):
    _roster(monkeypatch, [{"name": "protoEngineer", "id": "protoEngineer-ba4c", "port": 7875, "running": False}])
    got = A.startable_member("http://127.0.0.1:7875/a2a")
    assert got == {"name": "protoEngineer", "id": "protoEngineer-ba4c", "port": 7875}


def test_a_running_member_is_not_startable(monkeypatch):
    """Something is already answering there — the failure is not a stopped member."""
    _roster(monkeypatch, [{"name": "protoEngineer", "id": "pe", "port": 7875, "running": True}])
    assert A.startable_member("http://127.0.0.1:7875/a2a") is None


def test_a_remote_peer_is_never_startable(monkeypatch):
    """Someone else's machine is not ours to spawn, whatever the roster says."""
    _roster(monkeypatch, [{"name": "peer", "id": "peer", "port": 7875, "running": False}])
    assert A.startable_member("https://peer.example.com:7875/a2a") is None


def test_an_unknown_port_is_not_startable(monkeypatch):
    _roster(monkeypatch, [{"name": "protoEngineer", "id": "pe", "port": 7875, "running": False}])
    assert A.startable_member("http://127.0.0.1:9999/a2a") is None


def test_the_host_entry_is_not_a_member(monkeypatch):
    """The host has no workspace to spawn; only members do."""
    _roster(monkeypatch, [{"name": "host", "id": "", "port": 7870, "running": False}])
    assert A.startable_member("http://127.0.0.1:7870/a2a") is None


def test_no_fleet_at_all_is_a_normal_answer(monkeypatch):
    import types

    monkeypatch.setattr("graph.fleet.supervisor", types.SimpleNamespace(status=lambda: []), raising=False)
    assert A.startable_member("http://127.0.0.1:7875/a2a") is None


# ── the guards that stop a prompt loop ────────────────────────────────────────


def test_a_member_is_only_attempted_once_per_window():
    """A member that dies at boot must degrade to the plain error, not re-prompt."""
    assert A.attempt_allowed("pe") is True
    A.record_attempt("pe")
    assert A.attempt_allowed("pe") is False


def test_the_window_expires():
    A.record_attempt("pe")
    A._LAST_ATTEMPT["pe"] -= A._RETRY_WINDOW_S + 1
    assert A.attempt_allowed("pe") is True


# ── the coordinating case: one grant covers the round ─────────────────────────


def test_a_session_grant_covers_later_participants():
    """Coordinating three agents must not mean three cards."""
    assert A.granted("s1") is False
    A.grant("s1")
    assert A.granted("s1") is True
    assert A.granted("other-session") is False  # scoped, not global


def test_a_grant_expires():
    """Permission to start processes should not outlive the task it was given for."""
    A.grant("s1")
    A._GRANTS["s1"] -= A._GRANT_TTL_S + 1
    assert A.granted("s1") is False


def test_no_session_id_never_counts_as_granted():
    assert A.granted("") is False


# ── the operator's answer ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ({"choice": "once"}, "once"),
        ({"choice": "session"}, "session"),
        ({"choice": "no"}, "no"),
        ("session", "session"),
        # A console that answers this like an ordinary approval still works.
        ("approve", "once"),
        ({"answer": "yes"}, "once"),
        # Anything unrecognised declines: starting a process is not the safe default.
        ({}, "no"),
        (None, "no"),
        ("something else", "no"),
    ],
)
def test_the_choice_is_read_conservatively(response, expected):
    assert A.read_choice(response) == expected


def test_the_form_offers_the_session_grant_and_names_the_member():
    form = A.consent_form({"name": "protoEngineer", "id": "pe", "port": 7875}, coordinating=True)
    assert form["kind"] == "form"
    assert "protoEngineer" in form["title"]
    assert "7875" in form["description"]
    assert "run this discussion" in form["description"]  # coordinating wording
    consts = [o["const"] for o in form["steps"][0]["schema"]["properties"]["choice"]["oneOf"]]
    assert consts == ["once", "session", "no"]


def test_a_boot_failure_surfaces_the_agent_log(monkeypatch):
    """supervisor.start raises WITH the log tail — that is the whole point (#1565)."""
    import types

    def _boom(_id):
        raise RuntimeError("member died at boot\n  agent.log: ImportError: no such plugin")

    monkeypatch.setattr(
        "graph.fleet.supervisor",
        types.SimpleNamespace(start=_boom, _port_listening=lambda p: False),
        raising=False,
    )
    ready, detail = A.start_and_wait({"name": "pe", "id": "pe", "port": 7875})
    assert ready is False
    assert "agent.log" in detail
