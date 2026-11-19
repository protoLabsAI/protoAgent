"""Exercise the host loop against C1a and real child processes, without timed sleeps."""

import asyncio
import threading
from contextlib import suppress
from types import SimpleNamespace

import pytest

from events.bus import EventBus
from graph.config import LangGraphConfig
from graph.fleet import supervisor
from graph.plugins import chat_surface
from graph.workspaces import manager
from inbox.store import InboxStore
from runtime.state import STATE
from server import maintenance_loops as loops
from server import stores
from tests.test_fleet_crash_watchdog import recovery as recovery
from tests.test_fleet_exit_capture import _kill, member as member


@pytest.fixture
async def health_loop(tmp_path, monkeypatch):
    inbox = InboxStore(str(tmp_path / "inbox.db"))
    bus = EventBus()
    cfg = LangGraphConfig.from_dict({"fleet": {"health": {
        "enabled": True, "interval_seconds": 7, "failure_threshold": 2,
    }}})
    monkeypatch.setattr(STATE, "graph_config", cfg)
    monkeypatch.setattr(STATE, "inbox_store", inbox)
    monkeypatch.setattr(loops, "_event_bus", bus)
    monkeypatch.setattr(manager, "is_workspace_member", lambda: False)
    notices = []
    monkeypatch.setattr(
        chat_surface, "notify_chat_surfaces",
        lambda text, link, **kw: notices.append(SimpleNamespace(text=text, link=link, **kw)),
    )
    monkeypatch.setenv("A2A_PUBLIC_URL", "https://hub.example")
    sleeping = asyncio.Queue()
    resume = asyncio.Queue()

    async def sleep(delay):
        await sleeping.put(delay)
        await resume.get()

    # Patch this loop's clock only, leaving asyncio's task orchestration intact.
    monkeypatch.setattr(loops, "asyncio", SimpleNamespace(to_thread=asyncio.to_thread, sleep=sleep))
    task = None

    async def tick():
        nonlocal task
        if task is None:
            task = asyncio.create_task(loops._fleet_health_loop())
        else:
            resume.put_nowait(None)
        return await asyncio.wait_for(sleeping.get(), timeout=10)

    yield SimpleNamespace(tick=tick, inbox=inbox, bus=bus, cfg=cfg, notices=notices)
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.fixture
def inbox_turns(monkeypatch):
    fires = []

    async def deliver(item):
        fires.append(item)
        return True

    monkeypatch.setattr(STATE, "inbox_now_delivery", deliver)
    monkeypatch.setattr(STATE, "storm_guard", None)
    monkeypatch.setattr(stores, "_INBOX_NOW_RETRY_ACTIVE", False)
    return fires


def events(loop):
    return [entry for entry in loop.bus._ring if entry["event"].startswith("fleet.health.")]


@pytest.mark.platform_sensitive
@pytest.mark.parametrize("polled_first", [False, True])
async def test_real_exit_pages_once_even_before_first_sweep(member, health_loop, polled_first, inbox_turns):
    ws, _, pids = member
    await asyncio.to_thread(_kill, pids[0])
    if polled_first:
        await asyncio.to_thread(supervisor.status)
    assert await health_loop.tick() == 7
    await health_loop.tick()
    pages = health_loop.inbox.list()
    assert len(pages) == len(events(health_loop)) == 1
    assert pages[0]["needs_human"]
    assert pages[0]["priority"] == "now"
    assert ws["id"] in pages[0]["dedup_key"]
    event = events(health_loop)[0]
    assert event["event"] == "fleet.health.down"
    assert event["data"]["reason"] == "unexpected_exit"
    assert "log_tail" not in event["data"]
    assert "_notice_sent" not in event["data"]
    (notice,) = health_loop.notices  # one typed notice per page; none on the unchanged tick
    assert (notice.kind, notice.importance) == ("notify", "high")
    assert notice.text.startswith("Fleet member m") and "has stopped" in notice.text and "Open the console" in notice.text
    assert notice.link == "https://hub.example/app/"
    assert not next(row for row in supervisor.status() if row["id"] == ws["id"])["running"]
    for _ in range(2):
        await stores.retry_pending_now_inbox_items_when_free()
    assert [p["id"] for p in inbox_turns] == [pages[0]["id"]]


@pytest.mark.platform_sensitive
async def test_intentional_stop_does_not_page(member, health_loop):
    await health_loop.tick()
    await asyncio.to_thread(supervisor.stop, "m")
    await health_loop.tick()
    assert health_loop.inbox.list() == events(health_loop) == health_loop.notices == []


@pytest.mark.platform_sensitive
async def test_wedge_recovery_and_second_episode_each_page_once(member, health_loop):
    ws, flag, pids = member
    await health_loop.tick()
    for _ in range(2):
        flag.touch()
        await health_loop.tick()  # below threshold
        await health_loop.tick()
        await health_loop.tick()  # unchanged
        flag.unlink()
        await health_loop.tick()
        await health_loop.tick()  # unchanged
    assert [e["event"] for e in events(health_loop)] == [
        "fleet.health.wedged", "fleet.health.recovered",
        "fleet.health.wedged", "fleet.health.recovered",
    ]
    assert sorted((p["priority"], bool(p["needs_human"])) for p in health_loop.inbox.list(priority_floor="later")) == [
        ("later", False), ("later", False), ("now", True), ("now", True),
    ]
    assert len(health_loop.inbox.list(priority_floor="later")) == 4  # no dedup suppression of a new episode
    assert [(n.importance, "not responding" in n.text, "responding again" in n.text) for n in health_loop.notices] == [
        ("high", True, False), ("normal", False, True),
    ] * 2  # one notice per transition, recovery included
    assert next(row for row in supervisor.status() if row["id"] == ws["id"])["pid"] == pids[0]  # never restarted


@pytest.mark.parametrize("mode", ["disabled", "member", "no_inbox"])
async def test_loop_guards_do_not_collect_or_probe(health_loop, monkeypatch, mode):
    def forbidden(*args, **kwargs):
        pytest.fail("disabled/member loop must not touch the supervisor")

    monkeypatch.setattr(supervisor, "health_sweep", forbidden)
    monkeypatch.setattr(supervisor, "collect_exits", forbidden)
    if mode == "disabled":
        health_loop.cfg.fleet_health_enabled = False
    elif mode == "member":
        monkeypatch.setattr(manager, "is_workspace_member", lambda: True)
    else:
        monkeypatch.setattr(STATE, "inbox_store", None)
    await health_loop.tick()
    assert health_loop.inbox.list() == events(health_loop) == health_loop.notices == []


@pytest.mark.platform_sensitive
async def test_pending_exit_retries_inbox_off_loop_and_reads_live_config(member, health_loop, monkeypatch):
    _, _, pids = member
    await asyncio.to_thread(_kill, pids[0])
    original_add = health_loop.inbox.add
    main_thread = threading.get_ident()
    attempts = []

    def flaky_add(*args, **kwargs):
        attempts.append(threading.get_ident())
        if len(attempts) == 1:
            raise OSError("temporarily unavailable")
        return original_add(*args, **kwargs)

    monkeypatch.setattr(health_loop.inbox, "add", flaky_add)
    await health_loop.tick()
    assert events(health_loop) == health_loop.notices == []  # no notice before its inbox item
    health_loop.cfg.fleet_health_interval_seconds = 11
    assert await health_loop.tick() == 11
    await health_loop.tick()
    assert len(health_loop.inbox.list()) == len(events(health_loop)) == 1
    assert len(attempts) == 2
    assert len(health_loop.notices) == 1
    assert all(tid != main_thread for tid in attempts)


@pytest.mark.platform_sensitive
@pytest.mark.parametrize("previously_enabled", [False, True])
async def test_enabling_does_not_page_exits_recorded_while_disabled(member, health_loop, previously_enabled):
    _, _, pids = member
    if previously_enabled:
        await health_loop.tick()
    health_loop.cfg.fleet_health_enabled = False
    await health_loop.tick()
    await asyncio.to_thread(_kill, pids[0])
    await asyncio.to_thread(supervisor.status)  # a console reader records the disabled exit
    await health_loop.tick()
    health_loop.cfg.fleet_health_enabled = True
    await health_loop.tick()
    await health_loop.tick()
    assert health_loop.inbox.list() == events(health_loop) == []

    # A new exit while watching still pages, even for the same member.
    pids.append((await asyncio.to_thread(supervisor.start, "m"))["pid"])
    await asyncio.to_thread(_kill, pids[-1])
    await health_loop.tick()
    await health_loop.tick()
    assert len(health_loop.inbox.list()) == len(events(health_loop)) == 1
    assert events(health_loop)[0]["data"]["pid"] == pids[-1]


@pytest.mark.platform_sensitive
@pytest.mark.parametrize("state", ["wedged", "down", "recovered"])
async def test_collection_timeout_retains_committed_transition(member, health_loop, monkeypatch, state):
    from filelock import Timeout

    _, flag, pids = member
    health_loop.cfg.fleet_health_failure_threshold = 1
    await health_loop.tick()
    if state == "recovered":
        flag.touch()
        await health_loop.tick()
        flag.unlink()
    elif state == "wedged":
        flag.touch()
    else:
        await asyncio.to_thread(_kill, pids[0])
    before = len(events(health_loop))
    collect = supervisor.collect_exits

    def unavailable():
        raise Timeout("fleet.lock")

    monkeypatch.setattr(supervisor, "collect_exits", unavailable)
    await health_loop.tick()
    assert supervisor.member_health()[0]["state"] == ("healthy" if state == "recovered" else state)
    assert len(events(health_loop)) == before
    monkeypatch.setattr(supervisor, "collect_exits", collect)
    await health_loop.tick()
    await health_loop.tick()
    assert len(health_loop.inbox.list(priority_floor="later")) == len(events(health_loop)) == before + 1
    assert events(health_loop)[-1]["event"] == f"fleet.health.{state}"


async def test_notice_failure_and_event_retry_never_repeat_the_notice(health_loop, monkeypatch):
    change = {"id": "w1", "name": "m", "kind": "local", "state": "gave_up", "pid": 1, "changed_at": "t"}
    batches = [[dict(change)]]
    monkeypatch.setattr(loops, "_fleet_health_notices", lambda *a: batches.pop() if batches else [])
    publish, calls = health_loop.bus.publish, []

    def flaky_publish(*args):
        calls.append(args)
        if len(calls) == 1:
            raise RuntimeError("bus down")
        publish(*args)

    monkeypatch.setattr(health_loop.bus, "publish", flaky_publish)
    await health_loop.tick()
    await health_loop.tick()
    assert len(health_loop.inbox.list()) == len(events(health_loop)) == len(health_loop.notices) == 1
    assert health_loop.notices[0].importance == "high" and "given up" in health_loop.notices[0].text

    batches.append([dict(change, state="recovered", changed_at="t2")])
    monkeypatch.setattr(chat_surface, "notify_chat_surfaces", lambda *a, **kw: 1 / 0)
    await health_loop.tick()
    assert len(health_loop.inbox.list(priority_floor="later")) == len(events(health_loop)) == 2  # the page survives a notice failure


@pytest.mark.platform_sensitive
async def test_restarts_and_recovery_stay_readable_without_turns(recovery, health_loop, monkeypatch, inbox_turns):
    # #5585: exercise the actual idle delivery path, substituting only its turn hook.
    ws, _, pids, _ = recovery
    health_loop.cfg.fleet_health_restart_dead = True
    health_loop.cfg.fleet_health_max_attempts = 1
    health_loop.cfg.fleet_health_backoff_seconds = 0
    fires = inbox_turns
    original_start = supervisor.start

    def start(*args, **kwargs):
        result = original_start(*args, **kwargs)
        pids.append(result["pid"])
        return result

    monkeypatch.setattr(supervisor, "start", start)
    await health_loop.tick()
    await asyncio.to_thread(_kill, pids[0])
    for _ in range(3):
        await health_loop.tick()
        await stores.retry_pending_now_inbox_items_when_free()
    assert fires == []
    assert health_loop.inbox.list() == []
    records = health_loop.inbox.list(priority_floor="later")
    assert len(records) == 2
    assert all(p["priority"] == "later" and not p["needs_human"] for p in records)
    assert all(p["delivered_at"] is None and p["recovery_claimed_at"] is None for p in records)
    assert {p["text"] for p in records} == {
        f"Fleet member m ({ws['id']}) is restarted. Process exited unexpectedly.",
        f"Fleet member m ({ws['id']}) is recovered.",
    }
    assert [e["event"] for e in events(health_loop)] == ["fleet.health.restarted", "fleet.health.recovered"]
    assert [(n.kind, n.importance, n.text, n.link) for n in health_loop.notices] == [
        ("notify", "normal", "Fleet member m stopped unexpectedly and was restarted automatically. Nothing to do.", "https://hub.example/app/"),
        ("notify", "normal", "Fleet member m is responding again. Nothing to do.", "https://hub.example/app/"),
    ]

    # Exhaust the single restart allowance: down and gave_up still each wake once.
    await asyncio.to_thread(_kill, pids[-1])
    for _ in range(3):
        await health_loop.tick()
        await stores.retry_pending_now_inbox_items_when_free()
    assert len(fires) == 2
    assert all(p["priority"] == "now" and p["needs_human"] for p in fires)
    assert sum(":down:" in p["dedup_key"] for p in fires) == 1
    assert sum(":gave_up:" in p["dedup_key"] for p in fires) == 1
    assert [n.importance for n in health_loop.notices[2:]] == ["high", "high"]
    assert "has stopped" in health_loop.notices[2].text
    assert "given up" in health_loop.notices[3].text
    assert health_loop.inbox.list(priority_floor="later") == records
