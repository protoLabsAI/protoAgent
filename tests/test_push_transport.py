"""Delivery lifecycle through the real device registry and durable attempt ledger."""
import asyncio
import json
import threading
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from graph.plugins.chat_surface import ChatNotice
from graph.plugins.host import HOST
from graph.push_transport import PushResult, PushTransport, WebPushProvider
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


def notice(expiry=2000, kind="notify", importance="high"):
    # High importance: the delivery tests below need a notice the push policy lets through.
    return ChatNotice(kind, importance, "private text", "/private/path", "notice-1", 1000, expiry)


def attempts(sender):
    with sqlite3.connect(sender.path) as db:
        return db.execute("SELECT count(*) FROM attempts").fetchone()[0]


@pytest.mark.parametrize("kind,importance", [("notify", "normal"), ("review", "normal"), ("notify", "low")])
async def test_routine_notice_is_not_pushed_by_default(setup, monkeypatch, kind, importance):
    _, provider, transport, enroll = setup
    enroll()
    monkeypatch.setattr(devices, "list_devices", lambda: pytest.fail("policy must run before devices are read"))
    sender = transport()
    assert sender.enqueue(notice(kind=kind, importance=importance)) == 0
    await sender.accept(notice(kind=kind, importance=importance))
    assert not sender._wake.is_set()
    await sender.deliver(notice(kind=kind, importance=importance))
    assert attempts(sender) == 0 and not provider.calls


@pytest.mark.parametrize("kind,importance", [
    ("question", "normal"), ("question", "low"), ("notify", "high"), ("review", "high"),
])
def test_question_and_urgent_notices_are_pushed_by_default(setup, kind, importance):
    _, _, transport, enroll = setup
    enroll()
    sender = transport()
    assert sender.enqueue(notice(kind=kind, importance=importance)) == 1
    assert attempts(sender) == 1


def test_all_notices_setting_is_read_live(setup, monkeypatch):
    _, _, transport, enroll = setup
    enroll()
    live = SimpleNamespace(push_all_notices=False)
    monkeypatch.setattr(HOST, "config", lambda: live)
    sender = transport()
    routine = notice(kind="review", importance="normal")
    assert sender.enqueue(routine) == 0
    live.push_all_notices = True  # the operator flips it; same transport, no restart
    assert sender.enqueue(routine) == 1
    assert attempts(sender) == 1
    live.push_all_notices = False
    assert sender.enqueue(replace(routine, notice_id="after-off")) == 0


@pytest.mark.parametrize("config", [None, lambda: None, lambda: SimpleNamespace(), lambda: 1 / 0])
def test_missing_or_unreadable_config_means_routine_notices_stay_off(setup, monkeypatch, config):
    _, _, transport, enroll = setup
    enroll()
    monkeypatch.setattr(HOST, "config", config)
    sender = transport()
    assert sender.enqueue(notice(kind="review", importance="normal")) == 0
    assert sender.enqueue(notice(kind="question", importance="low")) == 1


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


async def test_failed_ends_the_attempt_and_keeps_the_binding(setup):
    _, provider, transport, enroll = setup
    device_id, _ = enroll()
    provider.status = "failed"  # e.g. the service answered 401/403: not proof of unpairing
    await transport().deliver(notice())
    await transport().drain()
    assert len(provider.calls) == 1
    assert devices.get_device(device_id).push is not None


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


@pytest.mark.parametrize("failure", [
    "timeout", "network", "service_retry", "rejected", "retries_exhausted",
    "transport_timeout", "expiry",
])
async def test_delivery_status_survives_failure_then_acceptance(setup, monkeypatch, failure):
    now, provider, transport, enroll = setup
    device_id, token = enroll()
    binding = devices.get_device(device_id).push
    private = "private response " + binding.endpoint + binding.auth + binding.p256dh

    def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout(private, request=request)
        if failure == "network":
            raise httpx.ConnectError(private, request=request)
        return httpx.Response(403 if failure == "rejected" else 503, text=private)

    web = WebPushProvider(clock=lambda: now[0], transport=httpx.MockTransport(respond))
    monkeypatch.setattr(web, "_prepare", lambda b, p: (b.endpoint, {}, b"encrypted"))
    sender = transport(max_attempts=1 if failure == "retries_exhausted" else 5)
    sender.provider = web
    if failure == "transport_timeout":
        # Only the stalled send needs a short deadline. Thread dispatch in the
        # real provider can exceed 10 ms on Windows before the mock responds.
        sender.timeout = 0.01
        async def stalled(binding, payload):
            await asyncio.Event().wait()
        monkeypatch.setattr(web, "send", stalled)
    writes = []
    save = devices._save
    loop_thread = threading.get_ident()

    def track_save(rows):
        writes.append(threading.get_ident())
        save(rows)

    monkeypatch.setattr(devices, "_save", track_save)
    await sender.deliver(notice(1010 if failure == "expiry" else 2000))
    expected = {"transport_timeout": "timeout", "expiry": "retries_exhausted"}.get(failure, failure)
    failed = devices.push_status(device_id)
    assert failed["last_attempt_at"] == failed["last_error_at"] == 1000
    assert failed["last_error"] == expected
    assert failed["last_accepted_at"] is None
    assert len(writes) == 1 and writes[0] != loop_thread

    now[0] = 1100
    await transport().deliver(replace(notice(), notice_id="later"))
    accepted = devices.push_status(device_id)
    assert accepted["last_attempt_at"] == accepted["last_accepted_at"] == 1100
    assert accepted["last_error"] == expected and accepted["last_error_at"] == 1000
    for secret in (private, binding.endpoint, binding.auth, binding.p256dh):
        assert secret not in json.dumps(accepted)
    renewed = bind(device_id, token, "phone")
    assert renewed == {**accepted, "renewed_at": 1100}  # same generation retains history


@pytest.mark.parametrize("operation", ["_load", "_save"])
@pytest.mark.parametrize("status, state", [("accepted", "accepted"), ("retry", "pending"), ("failed", "failed")])
async def test_status_storage_failure_preserves_delivery_progress(setup, monkeypatch, caplog, operation, status, state):
    now, provider, transport, enroll = setup
    device_id, _ = enroll()
    sender = transport()
    private = "private registry error with endpoint and client keys"

    def unavailable(*args):
        raise OSError(private)

    with monkeypatch.context() as patch:
        async def send(binding, payload):
            # Fail only after the pre-send device read and the provider result.
            patch.setattr(devices, operation, unavailable)
            return PushResult(status)

        provider.hook = send
        await sender.deliver(notice())

    with sqlite3.connect(sender.path) as db:
        assert db.execute("SELECT state, attempts, next_at FROM attempts").fetchone() == (
            state, 1, 1030 if status == "retry" else 1000,
        )
    assert devices.push_status(device_id)["last_attempt_at"] is None
    assert "delivery status could not be persisted" in caplog.text
    assert private not in caplog.text
    provider.hook = None
    now[0] = 1030
    await transport().deliver(notice())
    assert len(provider.calls) == (2 if status == "retry" else 1)
    if status == "retry":
        assert devices.push_status(device_id)["last_accepted_at"] == 1030


async def test_accepted_delivery_is_visible_in_device_route(setup, monkeypatch):
    from fastapi import FastAPI
    from a2a_impl import auth
    from operator_api.pairing_routes import register_pairing_routes

    _, _, transport, enroll = setup
    device_id, _ = enroll()
    await transport().deliver(notice())
    monkeypatch.delenv("PROTOAGENT_UI", raising=False)
    app = FastAPI()
    register_pairing_routes(app, agent_name=lambda: "box")
    auth.install(app, bearer_token="operator-secret", api_key="", allowed_origins_raw="")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.get("/api/devices", headers={"Authorization": "Bearer operator-secret"})
    assert response.status_code == 200
    row = next(d for d in response.json()["devices"] if d["id"] == device_id)
    assert row["push"]["last_attempt_at"] == row["push"]["last_accepted_at"] == 1000


@pytest.mark.parametrize("outcome", ["accepted", "retry", "failed"])
@pytest.mark.parametrize("change", ["replace", "clear", "revoke"])
async def test_late_results_do_not_touch_changed_bindings(setup, outcome, change):
    _, provider, transport, enroll = setup
    device_id, token = enroll()

    async def late(binding, payload):
        if change == "replace":
            bind(device_id, token, "replacement")
        elif change == "clear":
            devices.clear_push_binding(device_id, token)
        else:
            devices.revoke_device(device_id)
        return PushResult(outcome)

    provider.hook = late
    await transport().deliver(notice())
    current = devices.get_device(device_id)
    if change == "replace":
        assert current.push.generation == 2
        assert current.push.last_attempt_at is None
        assert current.push.last_accepted_at is None
        assert current.push.last_error is None
        assert current.push.last_error_at is None
    else:
        assert current is None or current.push is None


def test_old_binding_rows_and_unknown_errors_are_safe(setup):
    _, _, _, enroll = setup
    device_id, _ = enroll()
    path = devices._registry_path()
    rows = json.loads(path.read_text())
    for key in ("last_attempt_at", "last_accepted_at", "last_error", "last_error_at"):
        rows[0]["push"].pop(key)
    path.write_text(json.dumps(rows))
    status = devices.push_status(device_id)
    assert all(status[key] is None for key in ("last_attempt_at", "last_accepted_at", "last_error", "last_error_at"))
    with pytest.raises(ValueError, match="unknown push outcome"):
        devices.record_push_result(device_id, 1, "secret response body", 1000)
    assert devices.push_status(device_id) == status
    rows[0]["push"]["last_error"] = "secret response body"
    path.write_text(json.dumps(rows))
    assert devices.push_status(device_id) == status
