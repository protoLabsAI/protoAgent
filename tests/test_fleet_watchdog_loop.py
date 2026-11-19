"""Health-loop wiring through the real process detector and restart policy."""
import asyncio
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from graph.fleet import supervisor
from graph.workspaces import manager
from tests.test_fleet_crash_watchdog import recovery as recovery
from tests.test_fleet_exit_capture import _kill, member as member
from tests.test_fleet_health_loop import events, health_loop as health_loop

pytestmark = pytest.mark.platform_sensitive


@pytest.fixture
def watchdog(recovery, health_loop, monkeypatch):
    ws, _, pids, logger = recovery
    health_loop.cfg.fleet_health_restart_dead = True
    health_loop.cfg.fleet_health_max_attempts = 2
    health_loop.cfg.fleet_health_backoff_seconds = 10
    original = supervisor.start
    main_thread = threading.get_ident()

    def start(*args, **kwargs):
        assert threading.get_ident() != main_thread
        result = original(*args, **kwargs)
        if result.get("pid"):
            pids.append(result["pid"])
        return result

    monkeypatch.setattr(supervisor, "start", start)
    return ws, pids, logger


async def test_crash_restarts_once_and_notifies_with_inbox_retry(watchdog, health_loop, monkeypatch):
    ws, pids, logger = watchdog
    await asyncio.to_thread(_kill, pids[0])
    add = health_loop.inbox.add

    def unavailable(*args, **kwargs):
        raise OSError("inbox unavailable")

    monkeypatch.setattr(health_loop.inbox, "add", unavailable)
    await health_loop.tick()
    assert len(pids) == 2 and supervisor.is_running(ws["id"])
    monkeypatch.setattr(health_loop.inbox, "add", add)
    await health_loop.tick()
    await health_loop.tick()
    restarted = [e for e in events(health_loop) if e["event"] == "fleet.health.restarted"]
    assert len(restarted) == 1
    assert len(logger.get_recent()) == 1
    assert len(pids) == 2
    notices = [n for n in health_loop.inbox.list(priority_floor="later") if ":restarted:" in n["dedup_key"]]
    assert len(notices) == 1 and not notices[0]["needs_human"]


@pytest.mark.parametrize("boots", ["fails", "short_lived"])
async def test_crash_loop_backs_off_then_pages_give_up_once(watchdog, health_loop, monkeypatch, boots):
    ws, pids, logger = watchdog
    now = [100.0]
    monkeypatch.setattr(supervisor, "time", SimpleNamespace(
        time=lambda: now[0], monotonic=time.monotonic, sleep=time.sleep,
    ))
    if boots == "fails":
        monkeypatch.setattr(manager, "run_exec", lambda *a: (
            {}, [sys.executable, "-c", "raise SystemExit(3)"],
        ))
    await asyncio.to_thread(_kill, pids[-1])
    await health_loop.tick()
    assert len(logger.get_recent()) == 1
    if boots == "short_lived":
        await asyncio.to_thread(_kill, pids[-1])
    await health_loop.tick()  # pending exit is retained while the policy backs off
    assert len(logger.get_recent()) == 1
    now[0] += 10
    await health_loop.tick()
    assert len(logger.get_recent()) == 2
    if boots == "short_lived":
        await asyncio.to_thread(_kill, pids[-1])
    await health_loop.tick()
    now[0] += 1000
    await health_loop.tick()
    assert len(logger.get_recent()) == 2
    assert not supervisor.is_running(ws["id"])
    pages = [n for n in health_loop.inbox.list() if ":gave_up:" in n["dedup_key"]]
    assert len(pages) == 1 and pages[0]["needs_human"]
    assert len([e for e in events(health_loop) if e["event"] == "fleet.health.gave_up"]) == 1


@pytest.mark.parametrize("mode", ["off", "stop", "wedged", "unconfigured", "remote"])
async def test_watchdog_leaves_ineligible_members_alone(watchdog, health_loop, monkeypatch, member, mode):
    ws, pids, logger = watchdog
    if mode == "off":
        health_loop.cfg.fleet_health_restart_dead = False
    elif mode == "unconfigured":
        monkeypatch.setattr(supervisor, "autostart_members", lambda: [])
    elif mode == "remote":
        supervisor._save_remotes({ws["id"]: {"name": "remote", "url": "http://example.invalid"}})
        monkeypatch.setattr(supervisor, "refresh_remote_probes", lambda timeout: None)
    if mode == "stop":
        await asyncio.to_thread(supervisor.stop, ws["id"])
    elif mode == "wedged":
        member[1].touch()
    else:
        await asyncio.to_thread(_kill, pids[-1])
    await health_loop.tick()
    await health_loop.tick()
    await health_loop.tick()
    assert len(pids) == 1 and logger.get_recent() == []
    if mode == "wedged":
        assert len([e for e in events(health_loop) if e["event"] == "fleet.health.wedged"]) == 1


@pytest.mark.parametrize("action", ["disable", "stop"])
async def test_pending_retry_respects_changed_intent(watchdog, health_loop, monkeypatch, action):
    ws, pids, logger = watchdog
    now = [100.0]
    monkeypatch.setattr(supervisor, "time", SimpleNamespace(
        time=lambda: now[0], monotonic=time.monotonic, sleep=time.sleep,
    ))
    monkeypatch.setattr(manager, "run_exec", lambda *a: (
        {}, [sys.executable, "-c", "raise SystemExit(3)"],
    ))
    await asyncio.to_thread(_kill, pids[0])
    await health_loop.tick()
    assert len(logger.get_recent()) == 1
    if action == "disable":
        health_loop.cfg.fleet_health_restart_dead = False
    else:
        with pytest.raises(supervisor.FleetError):
            await asyncio.to_thread(supervisor.stop, ws["id"])
    now[0] += 10
    await health_loop.tick()
    await health_loop.tick()
    assert len(logger.get_recent()) == 1
    assert not supervisor.is_running(ws["id"])
