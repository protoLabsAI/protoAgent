"""Restart policy through the real supervisor spawn/exit path."""
import sys
import threading

import pytest

from graph.fleet import supervisor
from graph.workspaces import manager
from observability import audit
from tests.test_fleet_exit_capture import _kill, member  # noqa: F401 — shared real-process fixture

pytestmark = pytest.mark.platform_sensitive


@pytest.fixture
def recovery(member, tmp_path, monkeypatch):  # noqa: F811 — pytest fixture injection
    ws, flag, pids = member
    monkeypatch.setattr(supervisor, "autostart_members", lambda: [ws["name"]])
    logger = audit.AuditLogger(tmp_path / "audit.jsonl")
    monkeypatch.setattr(audit, "audit_logger", logger)
    monkeypatch.setattr(supervisor, "_shutting_down", False)
    monkeypatch.setattr(supervisor, "_unsaved_stops", set())
    return ws, flag, pids, logger


def crash(pids):
    _kill(pids[-1])
    supervisor.health_sweep(1)
    return supervisor.collect_exits()[-1]


def apply(record, **kw):
    return supervisor.restart_policy(record, max_attempts=2, window=60, backoff=10, **kw)


def test_restart_once_through_start_with_audit_and_duplicate_call(recovery, monkeypatch):
    ws, _, pids, logger = recovery
    record = crash(pids)
    original = supervisor.start
    calls = []

    def start(*args, **kwargs):
        calls.append(args[0])
        assert apply(record)["outcome"] == "backoff"  # concurrent/reentrant claim cannot spawn twice
        result = original(*args, **kwargs)
        pids.append(result["pid"])
        return result

    monkeypatch.setattr(supervisor, "start", start)
    assert apply(record)["outcome"] == "restarted"
    assert supervisor.is_running(ws["id"])
    assert apply(record)["outcome"] == "ignored"
    assert calls == [ws["id"]]
    rows = logger.get_recent()
    assert len(rows) == 1 and rows[0]["success"]
    assert "exited unexpectedly" in rows[0]["reason"]
    assert "member booted" in rows[0]["log_tail"]
    assert "sk-ant-api03" not in rows[0]["log_tail"]


def test_boot_failures_backoff_and_single_give_up(recovery, monkeypatch):
    _, _, pids, logger = recovery
    record = crash(pids)
    argv = [sys.executable, "-c", "print('boot failure', flush=True); raise SystemExit(3)"]
    monkeypatch.setattr(manager, "run_exec", lambda *a: ({}, argv))
    now = [100.0]
    monkeypatch.setattr(supervisor.time, "time", lambda: now[0])
    assert apply(record)["outcome"] == "failed"
    boot_exit = supervisor.collect_exits()[-1]
    assert apply(boot_exit)["outcome"] == "ignored"
    assert apply(record) == {"outcome": "backoff", "id": record["id"], "retry_after": 10.0}
    now[0] += 10
    assert apply(record)["outcome"] == "gave_up"
    now[0] += 100
    assert apply(record)["outcome"] == "ignored"
    assert len(logger.get_recent()) == 2
    assert not supervisor.is_running(record["id"])


@pytest.mark.parametrize("action", ["stop", "shutdown", "shutdown_keep", "event", "remote", "unconfigured", "wedged", "race", "corrupt"])
def test_ineligible_members_never_restart(recovery, monkeypatch, action):
    ws, flag, pids, logger = recovery
    if action == "wedged":
        flag.write_text("")
        supervisor.health_sweep(1)
        record = {"id": ws["id"], "pid": pids[0], "at": "2099-01-01", "started_at": ""}
    else:
        record = crash(pids)
    stop = threading.Event()
    if action == "stop":
        with pytest.raises(supervisor.FleetError):
            supervisor.stop(ws["id"])
        assert supervisor.stopped_on_purpose(ws["id"])
    elif action.startswith("shutdown"):
        monkeypatch.setattr(supervisor, "keep_members_on_exit", lambda: action == "shutdown_keep")
        supervisor.shutdown_all()
        assert supervisor.stopped_on_purpose(ws["id"])
        if action == "shutdown_keep":  # kept members carry no durable stop into the next hub
            assert "stopped" not in supervisor._load_restarts().get("members", {}).get(ws["id"], {})
    elif action == "event":
        stop.set()
    elif action == "remote":
        supervisor._save_remotes({ws["id"]: {"name": "remote", "url": "http://example.invalid"}})
    elif action == "unconfigured":
        monkeypatch.setattr(supervisor, "autostart_members", lambda: [])
    elif action == "corrupt":
        supervisor._restart_path().write_text("{invalid")
    elif action == "race":
        original = supervisor.start

        def start(*a, **kw):
            with pytest.raises(supervisor.FleetError):
                supervisor.stop(ws["id"])
            return original(*a, **kw)

        monkeypatch.setattr(supervisor, "start", start)
    assert apply(record, stop=stop)["outcome"] == "ignored"
    assert logger.get_recent() == []
    assert supervisor.is_running(ws["id"]) == (action == "wedged")


def test_warm_eviction_and_explicit_start_clear_stop_intent(recovery, monkeypatch):
    ws, _, pids, _ = recovery
    # Two real members: the older one is evicted by the existing stop path.
    from tests.test_fleet_exit_capture import _free_port, _MEMBER

    other = manager.create("other", port=_free_port())
    argv = [sys.executable, "-c", _MEMBER, str(other["port"]), str(ws["path"] + "/unused")]
    monkeypatch.setattr(manager, "run_exec", lambda *a: ({}, argv))
    pids.append(supervisor.start(other["id"])["pid"])
    monkeypatch.setattr(supervisor, "_warm_grace_seconds", lambda: 0)
    assert supervisor.enforce_warm_cap(1, protect=other["id"]) == [ws["id"]]
    assert supervisor.stopped_on_purpose(ws["id"])
    assert not supervisor.is_running(ws["id"])
    assert supervisor.collect_exits() == []
    supervisor.stop(other["id"])
    argv[3] = str(ws["port"])
    pids.append(supervisor.start(ws["id"])["pid"])
    assert not supervisor.stopped_on_purpose(ws["id"])


def test_window_expires_attempts_and_manual_start_invalidates_old_exit(recovery, monkeypatch):
    _, _, pids, _ = recovery
    record = crash(pids)
    now = [100.0]
    monkeypatch.setattr(supervisor.time, "time", lambda: now[0])
    original_run = manager.run_exec
    monkeypatch.setattr(manager, "run_exec", lambda *a: ({}, [sys.executable, "-c", "raise SystemExit(3)"]))
    assert apply(record)["outcome"] == "failed"
    now[0] += 61
    delayed = supervisor.restart_policy(record, max_attempts=2, window=60, backoff=120)
    assert delayed["outcome"] == "backoff" and delayed["retry_after"] == 59
    assert apply(record)["outcome"] == "failed"  # prior failure aged out
    monkeypatch.setattr(manager, "run_exec", original_run)
    pids.append(supervisor.start(record["id"])["pid"])
    _kill(pids[-1])
    supervisor.status()
    assert apply(record)["outcome"] == "ignored"
    fresh = supervisor.collect_exits()[-1]
    assert apply(fresh)["outcome"] == "restarted"
    pids.append(supervisor._load_state()[record["id"]]["pid"])


def test_short_lived_successful_boots_also_reach_cap(recovery, monkeypatch):
    _, _, pids, logger = recovery
    now = [100.0]
    monkeypatch.setattr(supervisor.time, "time", lambda: now[0])
    for _ in range(2):
        record = crash(pids)
        assert apply(record)["outcome"] == "restarted"
        pids.append(supervisor._load_state()[record["id"]]["pid"])
        now[0] += 10
    record = crash(pids)
    assert apply(record)["outcome"] == "gave_up"
    assert apply(record)["outcome"] == "ignored"
    assert len(logger.get_recent()) == 2


def test_starting_another_member_preserves_shutdown_stop_intent(recovery, monkeypatch):
    from tests.test_fleet_exit_capture import _free_port, _MEMBER

    ws, _, pids, _ = recovery
    record = crash(pids)
    supervisor.shutdown_all()
    other = manager.create("other", port=_free_port())
    argv = [sys.executable, "-c", _MEMBER, str(other["port"]), str(ws["path"] + "/unused")]
    monkeypatch.setattr(manager, "run_exec", lambda *a: ({}, argv))
    pids.append(supervisor.start(other["id"])["pid"])
    assert supervisor.stopped_on_purpose(ws["id"])
    assert not supervisor.stopped_on_purpose(other["id"])
    assert apply(record)["outcome"] == "ignored"


def _broken_save(monkeypatch):
    def fail(records):
        raise OSError("disk full")

    monkeypatch.setattr(supervisor, "_save_restarts", fail)


@pytest.mark.parametrize("how", ["stop", "shutdown"])
def test_stop_intent_write_failure_never_keeps_a_member_alive(recovery, monkeypatch, how):
    ws, _, pids, logger = recovery
    _broken_save(monkeypatch)
    if how == "stop":
        assert supervisor.stop(ws["id"])["stopped"]
    else:
        assert supervisor.shutdown_all() == [ws["id"]]
    assert not supervisor._alive(pids[-1])
    assert supervisor.stopped_on_purpose(ws["id"])  # held in memory for this hub's lifetime
    record = {"id": ws["id"], "pid": pids[-1], "at": "2099-01-01", "started_at": ""}
    assert apply(record)["outcome"] == "ignored"
    assert logger.get_recent() == []


def test_corrupt_history_is_not_operator_intent_and_explicit_start_recovers(recovery):
    ws, _, pids, _ = recovery
    supervisor._restart_path().write_text("{invalid")
    assert not supervisor.stopped_on_purpose(ws["id"])
    assert apply(crash(pids))["outcome"] == "ignored"
    pids.append(supervisor.start(ws["id"])["pid"])
    assert "disabled" not in supervisor._load_restarts()
    assert apply(crash(pids))["outcome"] == "restarted"
    pids.append(supervisor._load_state()[ws["id"]]["pid"])


def test_unexpected_start_error_is_still_audited(recovery, monkeypatch):
    _, _, pids, logger = recovery
    record = crash(pids)

    def boom(*a, **kw):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(supervisor, "start", boom)
    assert apply(record)["outcome"] == "failed"
    rows = logger.get_recent()
    assert len(rows) == 1 and not rows[0]["success"]
