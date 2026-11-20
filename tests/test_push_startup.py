"""Hub startup wiring: Web Push behind the notice seam, drained by one maintenance loop."""
import asyncio
import sqlite3
import time

import pytest

from graph.decisions import DecisionStore
from graph.plugins import chat_surface
from graph.plugins.chat_surface import ChatNotice, notify_chat_surfaces
from graph.push_transport import NOTICE_ADAPTER_KEY, PushResult, PushTransport
from infra.paths import reset_instance_paths
from runtime.state import STATE
from security import devices
from server import maintenance_loops as loops

pytestmark = pytest.mark.platform_sensitive


class Provider:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    async def send(self, binding, payload):
        self.calls.append(payload["notice_id"])
        if self.fail:
            raise RuntimeError("push service down")
        return PushResult("accepted")


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path))
    reset_instance_paths()
    monkeypatch.setattr("graph.plugins.host.HOST.publish", None)
    device, token = devices._register("phone")
    devices.set_push_binding(
        device.id, token, endpoint="https://fcm.googleapis.com/phone",
        p256dh="client-key", auth="secret", vapid_version=1,
    )
    yield
    reset_instance_paths()


async def settle(check):
    for _ in range(200):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("push delivery did not settle")


def rows(transport):
    with sqlite3.connect(transport.path) as db:
        return db.execute("SELECT notice_id, state FROM attempts").fetchall()


async def test_notice_reaches_the_push_provider_once_without_a_chat_plugin(hub):
    provider = Provider()
    transport = loops._start_push_delivery(provider)
    try:
        assert list(chat_surface._notifiers) == [NOTICE_ADAPTER_KEY]  # no chat surface
        notice_id = await asyncio.to_thread(notify_chat_surfaces, "private text", "/private", importance="high")
        await settle(lambda: rows(transport) == [(notice_id, "accepted")])
        await transport.drain()  # an idle pass sends nothing more
        assert provider.calls == [notice_id]
    finally:
        await loops._stop_push_delivery()
    assert NOTICE_ADAPTER_KEY not in chat_surface._notifiers
    assert STATE.push_delivery_task is None


async def test_routine_notice_reaches_other_adapters_but_not_push(hub):
    provider, seen = Provider(), []

    async def desktop(notice):
        seen.append(notice.notice_id)

    transport = loops._start_push_delivery(provider)
    chat_surface.register_notice_adapter("desktop", desktop)
    try:
        notice_id = notify_chat_surfaces("scheduled result", "", kind="review")  # normal importance
        await settle(lambda: seen)
        assert seen == [notice_id]
        await transport.drain()
        assert rows(transport) == [] and provider.calls == []
    finally:
        chat_surface.unregister_notice_adapter("desktop")
        await loops._stop_push_delivery()


async def test_failing_provider_does_not_stop_another_adapter(hub):
    provider, seen = Provider(fail=True), []

    async def desktop(notice):
        seen.append(notice.notice_id)

    transport = loops._start_push_delivery(provider)
    chat_surface.register_notice_adapter("desktop", desktop)
    try:
        notice_id = notify_chat_surfaces("hello", "", importance="high")  # returns; nothing raises
        await settle(lambda: provider.calls and seen)
        assert seen == [notice_id]
        await settle(lambda: rows(transport) == [(notice_id, "pending")])  # kept for a retry
    finally:
        chat_surface.unregister_notice_adapter("desktop")
        await loops._stop_push_delivery()


async def test_restart_sends_pending_and_drops_expired(hub):
    now = time.time()
    before = PushTransport(Provider())  # the hub that stopped before sending
    for notice_id in ("live", "stale"):
        assert before.enqueue(ChatNotice("notify", "high", "t", "", notice_id, now, now + 600)) == 1
    with sqlite3.connect(before.path) as db:
        db.execute("UPDATE attempts SET expires_at = ? WHERE notice_id = 'stale'", (now - 1,))
    provider = Provider()
    transport = loops._start_push_delivery(provider)
    try:
        await settle(lambda: rows(transport) == [("live", "accepted")])
        assert provider.calls == ["live"]
    finally:
        await loops._stop_push_delivery()


def queue(notice_id, kind="question"):
    """Leave one unsent push in the ledger, as a hub that stopped before sending would."""
    now = time.time()
    assert PushTransport(Provider()).enqueue(ChatNotice(kind, "high", "t", "", notice_id, now, now + 600)) == 1


@pytest.mark.parametrize("close", ["answer", "withdraw", "expire", "cancel"])
async def test_closed_decision_is_not_pushed(hub, close):
    store = DecisionStore()  # no adapter is registered yet, so creating pushes nothing itself
    # A deadline already past lets ``expire`` close it; the other closes ignore the deadline.
    closed = store.create("s1", "Ship it?", ["yes", "no"], default_option="yes", expires_at="2020-01-01T00:00:00+00:00")
    open_ = store.create("s1", "Merge it?", ["yes", "no"])
    queue(f"decision-{closed.id}")
    queue(f"decision-{open_.id}")
    queue("decision-from-a-member")  # a relayed member decision: its id is not in the hub's store
    queue("plain-question")
    queue("heads-up", kind="notify")
    getattr(store, close)(closed.id, *(["yes"] if close == "answer" else []))
    provider = Provider()
    transport = loops._start_push_delivery(provider)
    try:
        sent = {f"decision-{open_.id}", "decision-from-a-member", "plain-question", "heads-up"}
        expected = {(f"decision-{closed.id}", "obsolete"), *((n, "accepted") for n in sent)}
        await settle(lambda: set(rows(transport)) == expected)
        await transport.drain()  # the obsolete row is not picked up again
        assert set(provider.calls) == sent and len(provider.calls) == len(sent)
    finally:
        await loops._stop_push_delivery()


async def test_decision_store_read_failure_keeps_the_push_pending(hub, monkeypatch):
    rec = DecisionStore().create("s1", "Ship it?", ["yes", "no"])
    queue(f"decision-{rec.id}")
    queue("heads-up", kind="notify")

    def broken(self, decision_id):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(DecisionStore, "get", broken)
    provider = Provider()
    transport = loops._start_push_delivery(provider)
    try:
        expected = {(f"decision-{rec.id}", "pending"), ("heads-up", "accepted")}
        await settle(lambda: set(rows(transport)) == expected)
        await transport.drain()
        assert provider.calls == ["heads-up"]  # deferred: not sent, not dropped
        monkeypatch.undo()  # the store reads again: the held push goes out
        await transport.drain()
        assert set(rows(transport)) == {(f"decision-{rec.id}", "accepted"), ("heads-up", "accepted")}
    finally:
        await loops._stop_push_delivery()


async def test_workspace_member_registers_nothing(hub, monkeypatch):
    monkeypatch.setattr("graph.workspaces.manager.is_workspace_member", lambda: True)
    assert loops._start_push_delivery(Provider()) is None
    assert NOTICE_ADAPTER_KEY not in chat_surface._notifiers
    assert STATE.push_delivery_task is None
    await loops._stop_push_delivery()  # shutdown is harmless when nothing started
