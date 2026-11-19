"""Delivery lifecycle through the real device registry and durable attempt ledger."""
import asyncio
import sqlite3
from dataclasses import replace

import pytest

from graph.plugins.chat_surface import ChatNotice
from graph.push_transport import PushResult, PushTransport
from infra.paths import reset_instance_paths
from security import devices

pytestmark = pytest.mark.platform_sensitive


class Provider:
    def __init__(self, status="accepted"):
        self.status = status
        self.calls = []
        self.hook = None

    async def send(self, binding, payload):
        self.calls.append((binding, payload))
        if self.hook:
            return await self.hook(binding, payload)
        return PushResult(self.status)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path))
    reset_instance_paths()
    now = [1000.0]
    monkeypatch.setattr(devices, "_now", lambda: now[0])
    provider = Provider()

    def transport(**kwargs):
        return PushTransport(provider, clock=lambda: now[0], jitter=lambda: 0, **kwargs)

    def enroll(name="phone", bound=True):
        device, token = devices._register(name)
        if bound:
            bind(device.id, token, name)
        return device.id, token

    yield now, provider, transport, enroll
    reset_instance_paths()


def bind(device_id, token, name):
    return devices.set_push_binding(
        device_id, token, endpoint="https://fcm.googleapis.com/" + name,
        p256dh="client-key", auth="secret", vapid_version=1,
    )


def notice(expiry=2000):
    return ChatNotice("notify", "normal", "private text", "/private/path", "notice-1", 1000, expiry)


async def test_fanout_and_replay_after_restart(setup):
    _, provider, transport, enroll = setup
    for name in ("one", "two", "three"):
        enroll(name)
    enroll("unbound", bound=False)
    sender = transport()
    await sender.deliver(notice())
    assert len(provider.calls) == 3
    assert {p["notice_id"] for _, p in provider.calls} == {"notice-1"}
    assert "private" not in str(provider.calls)
    await transport().deliver(notice())
    assert len(provider.calls) == 3
    with sqlite3.connect(sender.path) as db:
        assert db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 3


@pytest.mark.parametrize("expiry, expected", [(2000, 5), (1030, 1)])
@pytest.mark.parametrize("failure", ["retry", "network", "timeout"])
async def test_backoff_cap_and_expiry_across_restart(setup, expiry, expected, failure):
    now, provider, transport, enroll = setup
    enroll()
    provider.status = "retry"

    async def fail(binding, payload):
        if failure == "network":
            raise ConnectionError("unreachable push service")
        await asyncio.Event().wait()

    if failure != "retry":
        provider.hook = fail
    await transport(timeout=0.01).deliver(notice(expiry))
    for timestamp, count in [(1029, 1), (1030, 2), (1090, 3), (1210, 4), (1450, 5), (1900, 5)]:
        now[0] = timestamp
        await transport(timeout=0.01).drain()
        assert len(provider.calls) == min(count, expected)
    now[0] = 2100
    sender = transport()
    await sender.drain()
    with sqlite3.connect(sender.path) as db:
        assert db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["replace", "revoke", "clear", "idle"])
async def test_stale_recipients_are_dropped(setup, change):
    now, provider, transport, enroll = setup
    device_id, token = enroll()
    sender = transport()
    sender.enqueue(notice(10**10))
    if change == "replace":
        bind(device_id, token, "replacement")
    elif change == "revoke":
        devices.revoke_device(device_id)
    elif change == "clear":
        devices.clear_push_binding(device_id, token)
    else:
        now[0] += 31 * 86400
    await transport().drain()
    assert not provider.calls
    with sqlite3.connect(sender.path) as db:
        assert db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0


@pytest.mark.parametrize("replacement", [False, True])
async def test_gone_only_invalidates_its_generation(setup, replacement):
    _, provider, transport, enroll = setup
    device_id, token = enroll()

    async def gone(binding, payload):
        if replacement:
            bind(device_id, token, "replacement")
        return PushResult("gone")

    provider.hook = gone
    await transport().deliver(notice())
    current = devices.get_device(device_id).push
    assert (current is not None) == replacement
    if current:
        assert current.generation == 2
    await transport().drain()
    assert len(provider.calls) == 1


async def test_slow_failure_retries_independently_after_restart(setup):
    now, provider, transport, enroll = setup
    enroll("slow")
    enroll("fast")
    fast_done = asyncio.Event()

    async def send(binding, payload):
        if binding.endpoint.endswith("slow"):
            await fast_done.wait()
            raise TimeoutError("may contain a secret endpoint")
        fast_done.set()
        return PushResult("accepted")

    provider.hook = send
    await transport().deliver(notice())
    assert fast_done.is_set()
    await transport().drain()
    assert len(provider.calls) == 2
    now[0] += 30
    provider.hook = None
    await transport().drain()
    assert [binding.endpoint.rsplit("/", 1)[-1] for binding, _ in provider.calls].count("slow") == 2
    assert [binding.endpoint.rsplit("/", 1)[-1] for binding, _ in provider.calls].count("fast") == 1
    assert {payload["notice_id"] for _, payload in provider.calls} == {"notice-1"}
    now[0] += 60
    await transport().deliver(notice())
    assert len(provider.calls) == 3


async def test_cancelled_send_stays_claimed_after_restart(setup):
    _, provider, transport, enroll = setup
    enroll()
    started = asyncio.Event()

    async def send(binding, payload):
        started.set()
        await asyncio.Event().wait()

    provider.hook = send
    pending = asyncio.create_task(transport().deliver(notice()))
    await started.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await transport().deliver(notice())
    assert len(provider.calls) == 1


async def test_retry_after_obsolete_and_overflow(setup):
    now, provider, transport, enroll = setup
    enroll()
    sender = transport(capacity=1)
    sender.enqueue(notice())
    with pytest.raises(OverflowError):
        sender.enqueue(replace(notice(), notice_id="second"))

    async def retry(binding, payload):
        return PushResult("retry", retry_after=100)

    provider.hook = retry
    await sender.drain()
    now[0] = 1099
    await transport().drain()
    assert len(provider.calls) == 1
    now[0] = 1100
    await transport(eligible=lambda _: False).drain()
    await transport().deliver(notice())
    assert len(provider.calls) == 1


async def test_competing_drains_claim_once_and_failed_read_defers(setup):
    _, provider, transport, enroll = setup
    enroll()

    def unavailable(_):
        raise OSError("authoritative record unavailable")

    sender = transport(eligible=unavailable)
    sender.enqueue(notice())
    await sender.drain()
    assert not provider.calls
    entered, release = asyncio.Event(), asyncio.Event()

    async def send(binding, payload):
        entered.set()
        await release.wait()
        return PushResult("accepted")

    provider.hook = send
    first = asyncio.create_task(transport().drain())
    await entered.wait()
    try:
        await transport().drain()
        assert len(provider.calls) == 1
    finally:
        release.set()
        await first
