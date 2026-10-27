"""Sleep inhibitors use fake backends: never inhibit the test host's sleep."""

import asyncio
import ctypes
import logging
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from infra import keep_awake
from scheduler.local import LocalScheduler

pytestmark = pytest.mark.platform_sensitive


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv("PROTOAGENT_KEEP_AWAKE", "1")
    monkeypatch.setattr(keep_awake, "_users", 0)
    monkeypatch.setattr(keep_awake, "_release", None)
    monkeypatch.setattr(keep_awake, "_warned", False)
    monkeypatch.setattr(keep_awake, "_serving_release", None)
    monkeypatch.setattr(keep_awake, "_serving_warned", False)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
async def test_fire_releases(monkeypatch, tmp_path, outcome):
    release = Mock()
    acquire = Mock(return_value=release)
    monkeypatch.setattr(keep_awake, "_acquire", acquire)
    scheduler = LocalScheduler("test", invoke_url="http://unused", db_dir=tmp_path)
    scheduler._settle_cron_expiry = Mock()
    job = SimpleNamespace(id="job")
    scheduler._inflight_ids.add(job.id)

    async def fire(job):
        acquire.assert_called_once_with("scheduled run job")
        release.assert_not_called()
        if outcome == "error":
            raise ValueError("failed")
        if outcome == "cancel":
            raise asyncio.CancelledError()
        return True

    monkeypatch.setattr(scheduler, "_fire", fire)
    if outcome == "success":
        await scheduler._fire_and_settle(job, True)
    else:
        with pytest.raises(ValueError if outcome == "error" else asyncio.CancelledError):
            await scheduler._fire_and_settle(job, True)
    release.assert_called_once_with()
    assert not scheduler._inflight_ids


@pytest.mark.asyncio
async def test_overlapping_fires(monkeypatch, tmp_path):
    release = Mock()
    acquire = Mock(return_value=release)
    monkeypatch.setattr(keep_awake, "_acquire", acquire)
    scheduler = LocalScheduler("test", invoke_url="http://unused", db_dir=tmp_path)
    scheduler._settle_cron_expiry = Mock()
    entered = [asyncio.Event(), asyncio.Event()]
    finish = [asyncio.Event(), asyncio.Event()]

    async def fire(job):
        entered[job.id].set()
        await finish[job.id].wait()
        return True

    monkeypatch.setattr(scheduler, "_fire", fire)
    tasks = [asyncio.create_task(scheduler._fire_and_settle(SimpleNamespace(id=i), True)) for i in range(2)]
    try:
        await asyncio.gather(*(event.wait() for event in entered))
        acquire.assert_called_once()
        finish[0].set()
        await tasks[0]
        release.assert_not_called()
        finish[1].set()
        await tasks[1]
        release.assert_called_once()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_backend_failure_is_logged_once(monkeypatch, caplog, platform):
    monkeypatch.setattr(keep_awake.sys, "platform", platform)
    monkeypatch.setattr(keep_awake.subprocess, "Popen", Mock(side_effect=FileNotFoundError("missing")))
    monkeypatch.setattr(keep_awake.ctypes, "windll", SimpleNamespace(kernel32=SimpleNamespace(
        SetThreadExecutionState=Mock(side_effect=OSError("ctypes failure")))), raising=False)
    with caplog.at_level(logging.INFO):
        for _ in range(2):
            with keep_awake.hold("test"):
                pass
    assert len(caplog.records) == 1
    assert keep_awake._users == 0


def test_disabled(monkeypatch):
    monkeypatch.setenv("PROTOAGENT_KEEP_AWAKE", "0")
    acquire = Mock()
    monkeypatch.setattr(keep_awake, "_acquire", acquire)
    with keep_awake.hold("test"):
        pass
    acquire.assert_not_called()


def test_desktop_default(monkeypatch):
    monkeypatch.delenv("PROTOAGENT_KEEP_AWAKE")
    monkeypatch.delenv("PROTOAGENT_PARENT_PID", raising=False)
    assert not keep_awake._enabled()
    monkeypatch.setenv("PROTOAGENT_PARENT_PID", "123")
    assert keep_awake._enabled()


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_process_backend(monkeypatch, platform):
    monkeypatch.setattr(keep_awake.sys, "platform", platform)
    process = Mock()
    spawn = Mock(return_value=process)
    monkeypatch.setattr(keep_awake.subprocess, "Popen", spawn)
    with keep_awake.hold("test"):
        process.terminate.assert_not_called()
    expected = (["caffeinate", "-i", "-w", str(os.getpid())] if platform == "darwin" else
                ["systemd-inhibit", "--what=sleep", "--why=test", "--mode=block", "sleep", "infinity"])
    assert spawn.call_args.args[0] == expected
    process.terminate.assert_called_once()
    process.wait.assert_called_once_with(timeout=1)


def test_windows_backend(monkeypatch, caplog):
    monkeypatch.setattr(keep_awake.sys, "platform", "win32")
    state = Mock(side_effect=[0x80000000, 0x80000001])
    monkeypatch.setattr(keep_awake.ctypes, "windll", SimpleNamespace(kernel32=SimpleNamespace(
        SetThreadExecutionState=state)), raising=False)
    with keep_awake.hold("test"):
        state.assert_called_once_with(0x80000001)
        assert state.argtypes == [ctypes.c_uint32]
        assert state.restype is ctypes.c_uint32
    assert [call.args for call in state.call_args_list] == [(0x80000001,), (0x80000000,)]
    assert not keep_awake._warned
    assert not caplog.records


def test_windows_zero_return_is_not_treated_as_failure(monkeypatch, caplog):
    """SetThreadExecutionState returns the PREVIOUS state; a fresh thread can return 0 on
    success, so a zero must still hold and release the assertion, with no warning."""
    monkeypatch.setattr(keep_awake.sys, "platform", "win32")
    state = Mock(side_effect=[0, 0] * 2)
    monkeypatch.setattr(keep_awake.ctypes, "windll", SimpleNamespace(kernel32=SimpleNamespace(
        SetThreadExecutionState=state)), raising=False)
    with caplog.at_level(logging.INFO):
        for _ in range(2):
            with keep_awake.hold("test"):
                assert keep_awake._users == 1
    assert [call.args for call in state.call_args_list] == [(0x80000001,), (0x80000000,)] * 2
    assert not caplog.records
    assert keep_awake._users == 0
    assert keep_awake._release is None


def test_release_failure_does_not_mask_run_error(monkeypatch):
    monkeypatch.setattr(keep_awake, "_acquire", Mock(return_value=Mock(side_effect=OSError("release"))))
    with pytest.raises(ValueError, match="run"):
        with keep_awake.hold("test"):
            raise ValueError("run")
    assert keep_awake._users == 0


@pytest.mark.asyncio
async def test_backend_failure_does_not_interrupt_fire(monkeypatch, tmp_path):
    monkeypatch.setattr(keep_awake, "_acquire", Mock(side_effect=FileNotFoundError("missing")))
    scheduler = LocalScheduler("test", invoke_url="http://unused", db_dir=tmp_path)
    settled = Mock()
    monkeypatch.setattr(scheduler, "_settle_cron_expiry", settled)
    fired = []

    async def fire(job):
        fired.append(job.id)
        return True

    monkeypatch.setattr(scheduler, "_fire", fire)
    job = SimpleNamespace(id="job")
    await scheduler._fire_and_settle(job, True)
    assert fired == ["job"]
    settled.assert_called_once_with(job)


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_serving_hold_backend(monkeypatch, platform):
    monkeypatch.setattr(keep_awake.sys, "platform", platform)
    process = Mock()
    spawn = Mock(return_value=process)
    monkeypatch.setattr(keep_awake.subprocess, "Popen", spawn)
    assert keep_awake.start_serving_hold() is True
    expected = (["caffeinate", "-s", "-w", str(os.getpid())] if platform == "darwin" else
                ["systemd-inhibit", "--what=sleep", "--why=protoAgent serving", "--mode=block", "sleep", "infinity"])
    assert spawn.call_args.args[0] == expected
    process.terminate.assert_not_called()
    keep_awake.stop_serving_hold()
    process.terminate.assert_called_once()
    process.wait.assert_called_once_with(timeout=1)


def test_serving_hold_idempotent(monkeypatch):
    monkeypatch.setattr(keep_awake.sys, "platform", "darwin")
    processes = [Mock(), Mock()]
    spawn = Mock(side_effect=processes)
    monkeypatch.setattr(keep_awake.subprocess, "Popen", spawn)
    # A second start while one is held spawns nothing and returns True.
    assert keep_awake.start_serving_hold() is True
    assert keep_awake.start_serving_hold() is True
    assert spawn.call_count == 1
    keep_awake.stop_serving_hold()
    processes[0].terminate.assert_called_once()
    # Stop with nothing held is a no-op; a later start spawns a fresh process.
    keep_awake.stop_serving_hold()
    assert keep_awake.start_serving_hold() is True
    assert spawn.call_count == 2
    processes[1].terminate.assert_not_called()


def test_serving_stop_with_nothing_is_noop(monkeypatch):
    spawn = Mock()
    monkeypatch.setattr(keep_awake.subprocess, "Popen", spawn)
    keep_awake.stop_serving_hold()
    spawn.assert_not_called()


def test_serving_hold_independent_of_scheduled(monkeypatch):
    """Entering and exiting hold() leaves the serving process untouched."""
    monkeypatch.setattr(keep_awake.sys, "platform", "darwin")
    serving, scheduled = Mock(), Mock()
    spawn = Mock(side_effect=[serving, scheduled])
    monkeypatch.setattr(keep_awake.subprocess, "Popen", spawn)
    assert keep_awake.start_serving_hold() is True
    with keep_awake.hold("scheduled run job"):
        pass
    scheduled.terminate.assert_called_once()
    serving.terminate.assert_not_called()
    keep_awake.stop_serving_hold()
    serving.terminate.assert_called_once()


def test_stop_serving_inside_hold_leaves_scheduled(monkeypatch):
    """Stopping the serving hold inside hold() does not call the scheduled release."""
    monkeypatch.setattr(keep_awake.sys, "platform", "darwin")
    serving, scheduled = Mock(), Mock()
    spawn = Mock(side_effect=[serving, scheduled])
    monkeypatch.setattr(keep_awake.subprocess, "Popen", spawn)
    assert keep_awake.start_serving_hold() is True
    with keep_awake.hold("scheduled run job"):
        keep_awake.stop_serving_hold()
        serving.terminate.assert_called_once()
        scheduled.terminate.assert_not_called()
    scheduled.terminate.assert_called_once()


def test_serving_disabled(monkeypatch):
    monkeypatch.setenv("PROTOAGENT_KEEP_AWAKE", "0")
    spawn = Mock()
    monkeypatch.setattr(keep_awake.subprocess, "Popen", spawn)
    assert keep_awake.start_serving_hold() is False
    spawn.assert_not_called()


def test_serving_ignores_parent_pid(monkeypatch):
    """An unset PROTOAGENT_PARENT_PID does not disable the serving hold."""
    monkeypatch.delenv("PROTOAGENT_KEEP_AWAKE", raising=False)
    monkeypatch.delenv("PROTOAGENT_PARENT_PID", raising=False)
    monkeypatch.setattr(keep_awake.sys, "platform", "darwin")
    spawn = Mock(return_value=Mock())
    monkeypatch.setattr(keep_awake.subprocess, "Popen", spawn)
    assert keep_awake.start_serving_hold() is True
    spawn.assert_called_once()


def test_serving_unsupported_platform(monkeypatch, caplog):
    monkeypatch.setattr(keep_awake.sys, "platform", "win32")
    spawn = Mock()
    monkeypatch.setattr(keep_awake.subprocess, "Popen", spawn)
    with caplog.at_level(logging.INFO):
        assert keep_awake.start_serving_hold() is False
        assert keep_awake.start_serving_hold() is False
    spawn.assert_not_called()
    assert len(caplog.records) == 1


def test_serving_popen_error_logged_once(monkeypatch, caplog):
    monkeypatch.setattr(keep_awake.sys, "platform", "darwin")
    monkeypatch.setattr(keep_awake.subprocess, "Popen", Mock(side_effect=FileNotFoundError("missing")))
    with caplog.at_level(logging.INFO):
        assert keep_awake.start_serving_hold() is False
        assert keep_awake.start_serving_hold() is False
    assert len(caplog.records) == 1
    assert keep_awake._serving_release is None
