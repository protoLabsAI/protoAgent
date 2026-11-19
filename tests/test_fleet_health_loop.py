"""Exercise the host loop against C1a and real child processes, without timed sleeps."""

import asyncio
import threading
from contextlib import suppress
from types import SimpleNamespace

import pytest

from events.bus import EventBus
from graph.config import LangGraphConfig
from graph.fleet import supervisor
from graph.workspaces import manager
from inbox.store import InboxStore
from runtime.state import STATE
from server import maintenance_loops as loops
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

    yield SimpleNamespace(tick=tick, inbox=inbox, bus=bus, cfg=cfg)
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def events(loop):
    return [entry for entry in loop.bus._ring if entry["event"].startswith("fleet.health.")]


@pytest.mark.platform_sensitive
@pytest.mark.parametrize("polled_first", [False, True])
async def test_real_exit_pages_once_even_before_first_sweep(member, health_loop, polled_first):
    ws, _, pids = member
    await asyncio.to_thread(_kill, pids[0])
    if polled_first:
        await asyncio.to_thread(supervisor.status)
    assert await health_loop.tick() == 7
    await health_loop.tick()
    pages = health_loop.inbox.list()
    assert len(pages) == len(events(health_loop)) == 1
    assert pages[0]["needs_human"]
    assert ws["id"] in pages[0]["dedup_key"]
    event = events(health_loop)[0]
    assert event["event"] == "fleet.health.down"
    assert event["data"]["reason"] == "unexpected_exit"
    assert "log_tail" not in event["data"]
    assert not next(row for row in supervisor.status() if row["id"] == ws["id"])["running"]


@pytest.mark.platform_sensitive
async def test_intentional_stop_does_not_page(member, health_loop):
    await health_loop.tick()
    await asyncio.to_thread(supervisor.stop, "m")
    await health_loop.tick()
    assert health_loop.inbox.list() == events(health_loop) == []


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
    assert len(health_loop.inbox.list()) == 4  # no dedup suppression of a new episode
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
    assert health_loop.inbox.list() == events(health_loop) == []


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
    assert events(health_loop) == []
    health_loop.cfg.fleet_health_interval_seconds = 11
    assert await health_loop.tick() == 11
    await health_loop.tick()
    assert len(health_loop.inbox.list()) == len(events(health_loop)) == 1
    assert len(attempts) == 2
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
    assert len(health_loop.inbox.list()) == len(events(health_loop)) == before + 1
    assert events(health_loop)[-1]["event"] == f"fleet.health.{state}"
