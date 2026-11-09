"""Decision notifications use the live chat-surface seam, never ledger updates."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio

from graph.decisions import DecisionStore
from graph.plugins.chat_surface import register_chat_surface


class Surface:
    chunk_limit = 0

    def __init__(self, name):
        self.id = name
        self.messages = []
        self.delivered = asyncio.Event()

    def configured(self, cfg):
        return bool(cfg.get("token"))

    async def run(self, handle, *, cfg, host):
        await asyncio.Event().wait()

    async def validate(self, cfg):
        return True, self.id, None

    async def notify(self, text, link):
        self.messages.append((text, link))
        self.delivered.set()


@pytest_asyncio.fixture
async def wire(monkeypatch):
    monkeypatch.setattr("graph.sdk.console_url", lambda: "https://console.example")
    monkeypatch.setattr("security.public_console.public_origin", lambda: "")
    surfaces = []

    def register(adapter, **config):
        reg = SimpleNamespace(
            config={"enabled": True, "token": "test", **config},
            host=SimpleNamespace(invoke=lambda: None),
            register_surface=lambda start, **kw: surfaces.append({"start": start, **kw}),
            register_router=lambda *args, **kwargs: None,
        )
        register_chat_surface(reg, adapter)
        surfaces[-1]["start"]()
        return surfaces[-1]

    yield register
    for surface in surfaces:
        await surface["stop"]()
    await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True])
async def test_creation_notifies_every_surface_once_with_content_and_link(wire, worker):
    adapters = [Surface("first"), Surface("second")]
    for adapter in adapters:
        wire(adapter)
    store = DecisionStore(":memory:")

    def create():
        return store.create(
            "slack:room & topic", "Ship it?",
            [{"id": "yes", "label": "Ship", "recommended": True}, "Wait"],
            agent="Ada", thread_id="thread", interrupt_id="interrupt",
            expires_at="2099-01-01T12:00:00+00:00", default_option="Wait",
            default_action="wait for review",
        )

    rec = await asyncio.to_thread(create) if worker else create()
    assert create().id == rec.id  # checkpoint replay must not notify again
    for adapter in adapters:
        await asyncio.wait_for(adapter.delivered.wait(), 2)
    await asyncio.sleep(0)
    expected = (
        "Ada needs a decision: Ship it?\n- Ship (suggested)\n- Wait\n"
        "If nobody answers by 2099-01-01T12:00:00+00:00, Ada will wait for review",
        "https://console.example/app/?session=slack%3Aroom+%26+topic",
    )
    assert all(adapter.messages == [expected] for adapter in adapters)


@pytest.mark.asyncio
async def test_optional_hook_disabled_unconfigured_and_stopped_are_skipped(wire):
    legacy = Surface("legacy")
    wire(SimpleNamespace(
        id=legacy.id, chunk_limit=legacy.chunk_limit, configured=legacy.configured,
        run=legacy.run, validate=legacy.validate,
    ))  # an older adapter has no notify attribute at all
    disabled, unconfigured, stopped = (Surface(n) for n in ("disabled", "unconfigured", "stopped"))
    wire(disabled, enabled=False)
    wire(unconfigured, token="")
    lifecycle = wire(stopped)
    await lifecycle["stop"]()
    good = Surface("good")
    wire(good)
    assert DecisionStore(":memory:").create("session", "Continue?", ["Yes"]).pending
    await asyncio.wait_for(good.delivered.wait(), 2)
    assert all(not a.messages for a in (legacy, disabled, unconfigured, stopped))
    assert good.messages == [("Agent needs a decision: Continue?\n- Yes", "https://console.example/app/?session=session")]


@pytest.mark.asyncio
async def test_failure_is_logged_and_slow_surface_does_not_block_creation_or_peers(wire, caplog):
    entered, release = asyncio.Event(), asyncio.Event()

    class Failing(Surface):
        async def notify(self, text, link):
            entered.set()
            await release.wait()
            raise RuntimeError("transport unavailable")

    wire(Failing("broken"))
    good = Surface("good")
    wire(good)
    store = DecisionStore(":memory:")
    rec = store.create("session", "Continue?", ["Yes"])
    assert store.get(rec.id).pending
    assert not entered.is_set()  # no transport runs inline with the insert
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.wait_for(good.delivered.wait(), 2)
    release.set()
    await asyncio.sleep(0)
    assert "[broken] decision notification failed" in caplog.text
    assert "transport unavailable" in caplog.text
    assert store.get(rec.id).pending


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["answer", "expire"])
async def test_answer_and_expiry_send_no_notification(wire, transition):
    adapter = Surface("fake")
    wire(adapter)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    store = DecisionStore(":memory:", clock=lambda: now)
    rec = store.create("session", "Continue?", ["Yes"], agent="Ada", expires_in_minutes=1, default_option="Yes")
    await asyncio.wait_for(adapter.delivered.wait(), 2)
    assert "Ada will carry on without this step" in adapter.messages[0][0]
    now += timedelta(minutes=2)
    if transition == "answer":
        store.answer(rec.id, "Yes")
    else:
        store.expire(rec.id)
    # Drain queued callbacks and tasks, so an accidental update notification is observable.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(adapter.messages) == 1


@pytest.mark.asyncio
async def test_sync_hook_runs_off_loop_and_reload_removes_notification(wire):
    import threading

    main_thread = threading.get_ident()
    called = threading.Event()
    threads = []
    adapter = Surface("sync")

    def notify(text, link):
        threads.append(threading.get_ident())
        called.set()

    adapter.notify = notify
    lifecycle = wire(adapter)
    store = DecisionStore(":memory:")
    store.create("session", "Continue?", ["Yes"])
    assert await asyncio.to_thread(called.wait, 2)
    assert threads and threads[0] != main_thread
    await lifecycle["reload"](SimpleNamespace(plugin_config={"sync": {"enabled": False}}))
    store.create("session", "Another decision?", ["Yes"])
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(threads) == 1
