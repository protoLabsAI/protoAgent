"""A pending decision always pushes, even when its chat is open in a console (#5648).

#5663 dropped the phone push for a decision whose chat a console had open. A desktop app
that keeps a chat open then stopped every decision push, so that rule is reverted. These
tests hold the rule that remains: only a decision that is no longer pending is dropped.
The last one runs the server's real startup wiring with the chat marked attended.
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
    monkeypatch.setattr(turn_control, "_ATTENDED_SESSIONS", {})
    yield DecisionStore()
    reset_instance_paths()


@pytest.mark.parametrize(
    ("close", "pushes"),
    [
        (None, True),  # still pending
        (lambda s, i: s.answer(i, "yes"), False),
        (lambda s, i: s.cancel(i), False),
        (lambda s, i: s.withdraw(i, "no longer needed"), False),
        (lambda s, i: s.expire(i), False),
    ],
    ids=["pending", "answered", "cancelled", "withdrawn", "expired"],
)
@pytest.mark.parametrize("attended", [False, True], ids=["chat-closed", "chat-open-in-console"])
def test_decision_push_eligibility(store, close, pushes, attended):
    """Only the decision's own state decides; a console holding the chat open changes nothing."""
    # Already past its deadline, so the store lets the expiry case close it.
    rec = store.create("the-chat", "Ship it?", ["yes", "no"], default_option="yes", expires_at="2020-01-01T00:00:00+00:00")
    if close is not None:
        close(store, rec.id)
    if attended:
        assert turn_control.mark_session_attended("the-chat")
    assert push_eligible(f"decision-{rec.id}") is pushes


def test_other_notices_and_member_decisions_are_eligible(store):
    assert push_eligible("plain-question") is True
    assert push_eligible("decision-from-a-member") is True  # the hub's store has no such record


def test_the_attendance_hook_is_gone():
    """Nothing can hand ``push_eligible`` an attendance read again by accident."""
    for name in ("set_attendance_reader", "_attendance_reader", "_session_attended"):
        assert not hasattr(decisions, name)


def states(transport):
    with sqlite3.connect(transport.path) as db:
        return dict(db.execute("SELECT notice_id, state FROM attempts").fetchall())


async def test_hub_wiring_pushes_a_pending_decision_whose_chat_is_open_in_a_console(store):
    """The server's real attendance registry, through the real startup wiring and transport."""
    device, token = devices._register("phone")
    keys = {"p256dh": "client-key", "auth": "secret", "vapid_version": 1}
    devices.set_push_binding(device.id, token, endpoint="https://fcm.googleapis.com/phone", **keys)
    assert turn_control.mark_session_attended("open-chat")
    # No adapter is registered yet, so creating pushes nothing: queue all three by hand.
    watched = store.create("open-chat", "Ship it?", ["yes", "no"])
    unwatched = store.create("closed-chat", "Merge it?", ["yes", "no"])
    answered = store.create("open-chat", "Tag it?", ["yes", "no"])
    now = time.time()
    for rec in (watched, unwatched, answered):
        notice = ChatNotice("question", "high", "t", "", f"decision-{rec.id}", now, now + 600)
        assert PushTransport(Provider()).enqueue(notice) == 1
    store.answer(answered.id, "yes")
    transport = loops._start_push_delivery(Provider())
    try:
        expected = {
            f"decision-{watched.id}": "accepted",
            f"decision-{unwatched.id}": "accepted",
            f"decision-{answered.id}": "obsolete",  # the remaining rule still drops this one
        }
        for _ in range(200):
            if states(transport) == expected:
                break
            await asyncio.sleep(0.01)
        assert states(transport) == expected
        assert turn_control.is_session_attended("open-chat")  # held open the whole time
    finally:
        await loops._stop_push_delivery()
        turn_control.release_session_attended("open-chat")
