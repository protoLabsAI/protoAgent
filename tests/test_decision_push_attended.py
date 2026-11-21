"""A decision whose chat is open in a console is not pushed to the phone (#5648 fix 9).

``push_eligible`` reads console attendance through a reader the server hands in when it
starts push delivery (``graph`` may not import ``server``); the last test runs that wiring.
"""
import asyncio
import sqlite3
import time

import pytest

from graph import decisions
from graph.decisions import DecisionStore, push_eligible
from graph.plugins.chat_surface import ChatNotice
from graph.push_transport import PushResult, PushTransport
from infra.paths import reset_instance_paths
from security import devices
from server import maintenance_loops as loops
from server import turn_control

pytestmark = pytest.mark.platform_sensitive


class Provider:
    async def send(self, binding, payload):
        return PushResult("accepted")


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path))
    reset_instance_paths()
    monkeypatch.setattr("graph.plugins.host.HOST.publish", None)
    monkeypatch.setattr("runtime.state.STATE.decision_store", None)
    monkeypatch.setattr(decisions, "_attendance_reader", [None])
    yield DecisionStore()
    reset_instance_paths()


def broken(session_id):
    raise RuntimeError("attendance registry unavailable")


@pytest.mark.parametrize(
    ("reader", "answered", "pushes"),
    [
        (lambda sid: sid == "the-chat", False, False),  # pending, its chat is open in a console
        (lambda sid: sid == "another-chat", False, True),  # pending, nobody has it open
        (None, False, True),  # no host handed in a reader
        (broken, False, True),  # the attendance read fails: the push goes out
        (lambda sid: False, True, False),  # answered
        (lambda sid: True, True, False),
    ],
)
def test_decision_push_eligibility(store, monkeypatch, reader, answered, pushes):
    rec = store.create("the-chat", "Ship it?", ["yes", "no"])
    if answered:
        store.answer(rec.id, "yes")
    monkeypatch.setattr(decisions, "_attendance_reader", [reader])
    assert push_eligible(f"decision-{rec.id}") is pushes


def test_other_notices_and_member_decisions_never_consult_attendance(store, monkeypatch):
    asked = []
    monkeypatch.setattr(decisions, "_attendance_reader", [lambda sid: asked.append(sid) or True])
    assert push_eligible("plain-question") is True
    assert push_eligible("decision-from-a-member") is True  # the hub's store has no such record
    assert asked == []


def states(transport):
    with sqlite3.connect(transport.path) as db:
        return dict(db.execute("SELECT notice_id, state FROM attempts").fetchall())


async def test_hub_wiring_marks_an_attended_decisions_push_obsolete(store, monkeypatch):
    """The server's real attendance registry, through the real startup wiring and transport."""
    device, token = devices._register("phone")
    keys = {"p256dh": "client-key", "auth": "secret", "vapid_version": 1}
    devices.set_push_binding(device.id, token, endpoint="https://fcm.googleapis.com/phone", **keys)
    monkeypatch.setattr(turn_control, "_ATTENDED_SESSIONS", {})
    assert turn_control.mark_session_attended("open-chat")
    # No adapter is registered yet, so creating pushes nothing: queue both by hand.
    watched = store.create("open-chat", "Ship it?", ["yes", "no"])
    unwatched = store.create("closed-chat", "Merge it?", ["yes", "no"])
    now = time.time()
    for rec in (watched, unwatched):
        notice = ChatNotice("question", "high", "t", "", f"decision-{rec.id}", now, now + 600)
        assert PushTransport(Provider()).enqueue(notice) == 1
    transport = loops._start_push_delivery(Provider())
    try:
        expected = {f"decision-{watched.id}": "obsolete", f"decision-{unwatched.id}": "accepted"}
        for _ in range(200):
            if states(transport) == expected:
                break
            await asyncio.sleep(0.01)
        assert states(transport) == expected
        # The operator leaves without answering: the dropped push does not come back.
        turn_control.release_session_attended("open-chat")
        await transport.drain()
        assert states(transport) == expected
    finally:
        await loops._stop_push_delivery()
    assert decisions._attendance_reader[0] is None  # shutdown hands the reader back
