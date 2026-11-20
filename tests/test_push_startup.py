"""Hub startup wiring: Web Push behind the notice seam, drained by one maintenance loop."""
import asyncio
import sqlite3
import time

import pytest

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


async def test_workspace_member_registers_nothing(hub, monkeypatch):
    monkeypatch.setattr("graph.workspaces.manager.is_workspace_member", lambda: True)
    assert loops._start_push_delivery(Provider()) is None
    assert NOTICE_ADAPTER_KEY not in chat_surface._notifiers
    assert STATE.push_delivery_task is None
    await loops._stop_push_delivery()  # shutdown is harmless when nothing started
