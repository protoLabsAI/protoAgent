"""A hub decision's first phone push waits a grace while Josh is at that chat (#5669, part 3).

The rule: DELAY, NEVER DROP. Presence is read once, when the push row is written, and only
moves that row's first ``next_at`` later. Whether the push still goes out when it comes due
is decided by the decision's own state (``push_eligible``), never by presence.

Each test says what Josh sees on his phone.
"""
import sqlite3
import time

import pytest

from graph import decisions, operator_presence
from graph.decisions import DecisionStore, push_delay, push_eligible
from graph.plugins.chat_surface import ChatNotice
from graph.push_transport import PushResult, PushTransport
from infra.paths import reset_instance_paths
from security import devices
from server import maintenance_loops as loops

pytestmark = pytest.mark.platform_sensitive

GRACE = 75.0


class Provider:
    def __init__(self):
        self.sent = []

    async def send(self, binding, payload):
        self.sent.append(payload["notice_id"])
        return PushResult("accepted")


class Clock:
    """The transport's clock: starts at the real time the presence store reads, then is moved by hand."""

    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path))
    reset_instance_paths()
    monkeypatch.setattr("graph.plugins.host.HOST.publish", None)
    monkeypatch.setattr("runtime.state.STATE.decision_store", None)
    monkeypatch.setattr(decisions, "GRACE_S", GRACE)
    operator_presence.reset()
    device, token = devices._register("phone")
    keys = {"p256dh": "client-key", "auth": "secret", "vapid_version": 1}
    devices.set_push_binding(device.id, token, endpoint="https://fcm.googleapis.com/phone", **keys)
    yield DecisionStore()
    operator_presence.reset()
    reset_instance_paths()


clock = pytest.fixture(lambda: Clock())
provider = pytest.fixture(lambda: Provider())


@pytest.fixture
def transport(store, provider, clock):
    """The hub's wiring: the same two hooks ``_start_push_delivery`` passes."""
    return PushTransport(provider, clock=clock, eligible=push_eligible, delay=push_delay)


def raise_decision(store, transport, clock, session="the-chat"):
    """Create a hub decision and queue its phone push, as the notice seam does."""
    rec = store.create(session, "Ship it?", ["yes", "no"])
    notice_id = f"decision-{rec.id}"
    notice = ChatNotice("question", "high", "t", "", notice_id, clock.now, clock.now + 3600)
    assert transport.enqueue(notice) == 1
    return rec, notice_id


def row(transport, notice_id):
    with sqlite3.connect(transport.path) as db:
        db.row_factory = sqlite3.Row
        return db.execute("SELECT * FROM attempts WHERE notice_id = ?", (notice_id,)).fetchone()


async def test_nobody_at_the_chat_pushes_at_once(store, transport, provider, clock):
    """Josh's phone buzzes at once: no console reported him at the chat, so nothing is held.
    Matches the rule: with no presence there is no delay, and nothing is dropped.
    """
    rec, notice_id = raise_decision(store, transport, clock)
    assert push_delay(notice_id) == 0.0
    assert row(transport, notice_id)["next_at"] == clock.now
    await transport.drain()
    assert provider.sent == [notice_id]
    assert row(transport, notice_id)["state"] == "accepted"


async def test_at_the_chat_and_answered_inside_the_grace_never_buzzes(store, transport, provider, clock):
    """Josh's phone stays silent: he was at the chat and answered there within the 75 s grace.
    Matches the rule: presence only delayed the push; it was the answer (the decision's own
    state) that made the row obsolete, with no send attempt at all.
    """
    start = clock.now
    operator_presence.record("console-1", "the-chat", True, 5)
    rec, notice_id = raise_decision(store, transport, clock)
    assert push_delay(notice_id) == GRACE
    assert row(transport, notice_id)["next_at"] == start + GRACE

    clock.now = start + 30
    await transport.drain()
    assert (provider.sent, row(transport, notice_id)["state"]) == ([], "pending")
    store.answer(rec.id, "yes")
    assert push_delay(notice_id) == 0.0  # a closed decision is never held
    clock.now = start + 76
    await transport.drain()
    assert provider.sent == []
    held = row(transport, notice_id)
    assert (held["state"], held["attempts"]) == ("obsolete", 0)


async def test_at_the_chat_but_not_answered_buzzes_after_the_grace(store, transport, provider, clock):
    """Josh's phone buzzes about 75 s late: he was at the chat but did not answer in the grace.
    Matches the rule: the push was delayed, never dropped. Presence is not read again at
    send time, so a still-fresh report cannot hold it any longer.
    """
    start = clock.now
    operator_presence.record("console-1", "the-chat", True, 5)
    rec, notice_id = raise_decision(store, transport, clock)
    assert row(transport, notice_id)["next_at"] == start + GRACE

    clock.now = start + 30
    await transport.drain()
    assert provider.sent == []
    # Raised again with nobody there: the held row neither restarts its grace nor loses it.
    operator_presence.reset()
    assert transport.enqueue(ChatNotice("question", "high", "t", "", notice_id, start, start + 3600)) == 0
    assert row(transport, notice_id)["next_at"] == start + GRACE

    operator_presence.record("console-1", "the-chat", True, 0)  # back at the chat: must not matter
    clock.now = start + 76
    await transport.drain()
    assert provider.sent == [notice_id]
    sent = row(transport, notice_id)
    assert (sent["state"], sent["attempts"]) == ("accepted", 1)


async def test_at_a_different_chat_pushes_at_once(store, transport, provider, clock):
    """Josh's phone buzzes at once: he is active in the console, but on another chat.
    Matches the rule: only presence on the decision's own chat earns the grace.
    """
    operator_presence.record("console-1", "another-chat", True, 5)
    rec, notice_id = raise_decision(store, transport, clock)
    assert push_delay(notice_id) == 0.0
    assert row(transport, notice_id)["next_at"] == clock.now
    await transport.drain()
    assert provider.sent == [notice_id]


@pytest.mark.parametrize(
    ("received_ago", "input_age"),
    [(91, 0), (0, 61)],
    ids=["report-91s-old", "input-61s-old"],
)
async def test_a_stale_report_or_old_input_pushes_at_once(store, transport, provider, clock, received_ago, input_age):
    """Josh's phone buzzes at once: the console's report is stale, or he has not typed or clicked lately.
    Matches the rule: old evidence is not presence, so it fails open to an immediate push.
    """
    operator_presence.record("console-1", "the-chat", True, input_age, now=time.time() - received_ago)
    rec, notice_id = raise_decision(store, transport, clock)
    assert push_delay(notice_id) == 0.0
    assert row(transport, notice_id)["next_at"] == clock.now
    await transport.drain()
    assert provider.sent == [notice_id]


async def test_a_failing_presence_read_pushes_at_once(store, transport, provider, clock, monkeypatch):
    """Josh's phone buzzes at once: the presence read broke, and a broken read never holds a push.
    Matches the rule: every error fails open.
    """
    operator_presence.record("console-1", "the-chat", True, 5)

    def broken(session_id, now=None):
        raise RuntimeError("presence store is broken")

    monkeypatch.setattr(operator_presence, "operator_at", broken)
    rec, notice_id = raise_decision(store, transport, clock)
    assert push_delay(notice_id) == 0.0
    assert row(transport, notice_id)["next_at"] == clock.now
    await transport.drain()
    assert provider.sent == [notice_id]


async def test_a_member_decision_pushes_at_once(store, transport, provider, clock):
    """Josh's phone buzzes at once for a member agent's decision, even while he is at a hub chat.
    Matches the rule: the grace covers hub decisions only; a member's decision id is not in
    the hub's store, so it is pushed as before.
    """
    operator_presence.record("console-1", "the-chat", True, 5)
    notice_id = "decision-from-a-member"
    assert push_delay(notice_id) == 0.0
    assert push_delay("plain-question") == 0.0  # so does any notice that is not a decision
    notice = ChatNotice("question", "high", "t", "", notice_id, clock.now, clock.now + 3600)
    assert transport.enqueue(notice) == 1
    assert row(transport, notice_id)["next_at"] == clock.now
    await transport.drain()
    assert provider.sent == [notice_id]


def test_presence_never_makes_a_pending_decision_ineligible(store):
    """Josh's phone still gets the push in the end: being at the chat never drops it.
    Matches the rule: ``push_eligible`` reads only the decision's own state.
    """
    operator_presence.record("console-1", "the-chat", True, 5)
    rec = store.create("the-chat", "Ship it?", ["yes", "no"])
    assert operator_presence.operator_at("the-chat") is True
    assert push_eligible(f"decision-{rec.id}") is True


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -5.0, "boom"], ids=["nan", "inf", "negative", "raises"])
async def test_an_unusable_delay_means_no_hold(store, provider, clock, bad):
    """Josh's phone buzzes at once: a delay hook that misbehaves cannot hold or lose a push.
    Matches the rule: fail open.
    """

    def delay(notice_id):
        if bad == "boom":
            raise RuntimeError("delay hook is broken")
        return bad

    transport = PushTransport(provider, clock=clock, delay=delay)
    notice = ChatNotice("question", "high", "t", "", "n1", clock.now, clock.now + 3600)
    assert transport.enqueue(notice) == 1
    assert row(transport, "n1")["next_at"] == clock.now
    await transport.drain()
    assert provider.sent == ["n1"]


async def test_the_hub_startup_wiring_passes_the_grace(store):
    """Josh's phone gets the grace on the real hub: startup wires both hooks into the transport.
    Matches the rule: the hub's transport delays with ``push_delay`` and drops only on
    ``push_eligible``.
    """
    transport = loops._start_push_delivery(Provider())
    try:
        assert transport.delay is push_delay
        assert transport.eligible is push_eligible
    finally:
        await loops._stop_push_delivery()
