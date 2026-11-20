"""Quiet hours preserve live console events and defer only routine adapters."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from graph import notice_hold
from graph.plugins import chat_surface
from graph.plugins.host import HOST


def stamp(value):
    return datetime.fromisoformat(value).timestamp()


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    cfg = SimpleNamespace(quiet_hours_enabled=True, quiet_hours_start="22:00",
                          quiet_hours_end="07:00", quiet_hours_timezone="UTC")
    clock = [stamp("2026-11-19T03:00:00+00:00")]
    monkeypatch.setattr(notice_hold.time, "time", lambda: clock[0])
    monkeypatch.setattr(HOST, "config", lambda: cfg)
    bus, sent = [], []
    monkeypatch.setattr(HOST, "publish", lambda event, data: bus.append((event, data)))
    monkeypatch.setattr(chat_surface, "_notifiers", {"test": sent.append})
    monkeypatch.setattr(notice_hold, "_invalid_logged", False)
    service = notice_hold.QuietHoursDelivery()
    service.store = notice_hold.NoticeHold(tmp_path / "hold.sqlite3")
    monkeypatch.setattr(notice_hold, "DELIVERY", service)
    loop = Mock()
    loop.call_soon_threadsafe.side_effect = lambda callback: callback()
    monkeypatch.setattr(notice_hold.asyncio, "get_running_loop", lambda: loop)
    return SimpleNamespace(cfg=cfg, clock=clock, bus=bus, sent=sent, service=service, loop=loop)


def test_hold_bus_bypasses_and_one_expired_digest_at_timer_end(delivery):
    d = delivery
    d.service.start()
    chat_surface.notify_chat_surfaces("nightly report", "/report", ttl_seconds=0)
    chat_surface.notify_chat_surfaces("routine review", "/review", kind="review", importance="low")
    assert len(d.bus) == 2 and not d.sent
    assert d.bus[0][1]["text"] == "nightly report"
    chat_surface.notify_chat_surfaces("urgent", "/urgent", importance="high")
    chat_surface.notify_chat_surfaces("approve?", "/decision", kind="question")
    assert [n.text for n in d.sent] == ["urgent", "approve?"]
    d.clock[0] = stamp("2026-11-19T07:00:00+00:00")
    # Fire the callback armed by the first held notice, without a wall-clock sleep.
    d.loop.call_later.call_args.args[1]()
    assert len(d.sent) == 3
    digest = d.sent[-1]
    assert (digest.kind, digest.importance) == ("notify", "normal")
    assert digest.text == "Quiet hours digest: 2 notices\n\nnightly report\nroutine review"
    assert digest.expires_at > d.clock[0]
    assert d.service.store.rows() == []
    d.service.check()
    assert len(d.sent) == 3


@pytest.mark.parametrize("restart_at,held", [("03:30", True), ("07:00", False)])
def test_restart_recovers_persistent_hold(delivery, restart_at, held):
    d = delivery
    chat_surface.notify_chat_surfaces("survives restart", "/")
    recovered = notice_hold.QuietHoursDelivery()
    recovered.store = notice_hold.NoticeHold(d.service.store.path)
    assert len(recovered.store.rows()) == 1
    d.clock[0] = stamp(f"2026-11-19T{restart_at}:00+00:00")
    recovered.start()
    assert bool(recovered.store.rows()) is held
    assert len(d.sent) == (0 if held else 1)
    if held:
        assert d.loop.call_later.called
    else:
        assert "survives restart" in d.sent[0].text
    recovered.stop()


@pytest.mark.parametrize("now,held", [
    ("2026-11-19T05:59:00+00:00", False),  # Los Angeles 21:59
    ("2026-11-19T06:00:00+00:00", True),   # 22:00
    ("2026-11-19T14:59:00+00:00", True),   # 06:59
    ("2026-11-19T15:00:00+00:00", False),  # 07:00
])
def test_local_midnight_window(delivery, now, held):
    d = delivery
    d.cfg.quiet_hours_timezone = "America/Los_Angeles"
    d.clock[0] = stamp(now)
    d.service.start()
    chat_surface.notify_chat_surfaces("local night", "/")
    assert bool(d.service.store.rows()) is held
    assert bool(d.sent) is not held
    if held:
        d.clock[0] = stamp("2026-11-19T15:00:00+00:00")
        d.loop.call_later.call_args.args[1]()
        assert len(d.sent) == 1 and not d.service.store.rows()


@pytest.mark.parametrize("field,value", [
    ("quiet_hours_timezone", "Unknown/Nowhere"),
    ("quiet_hours_start", "25:00"), ("quiet_hours_end", "7:00"),
])
def test_invalid_config_delivers_and_logs_once(delivery, caplog, field, value):
    setattr(delivery.cfg, field, value)
    for _ in range(2):
        chat_surface.notify_chat_surfaces("deliver", "/")
    assert len(delivery.sent) == 2
    assert sum("Invalid quiet hours" in record.message for record in caplog.records) == 1


def test_disable_releases_pending_and_store_failure_fails_open(delivery, monkeypatch):
    d = delivery
    d.service.start()
    chat_surface.notify_chat_surfaces("held", "/")
    d.cfg.quiet_hours_enabled = False
    d.loop.call_later.call_args.args[1]()
    chat_surface.notify_chat_surfaces("immediate", "/")
    assert len(d.sent) == 2 and not d.service.store.rows()
    d.cfg.quiet_hours_enabled = True
    monkeypatch.setattr(d.service.store, "add", Mock(side_effect=OSError("unavailable")))
    chat_surface.notify_chat_surfaces("storage failed", "/")
    assert d.sent[-1].text == "storage failed"


def test_no_adapter_keeps_digest_for_retry(delivery, monkeypatch):
    d = delivery
    d.service.start()
    chat_surface.notify_chat_surfaces("keep until adapters return", "/")
    d.clock[0] = stamp("2026-11-19T07:00:00+00:00")
    monkeypatch.setattr(chat_surface, "_notifiers", {})
    d.service.check()
    assert d.service.store.rows()
    monkeypatch.setattr(chat_surface, "_notifiers", {"test": d.sent.append})
    d.loop.call_later.call_args.args[1]()
    assert len(d.sent) == 1 and not d.service.store.rows()
