"""A failed dispatch degrades the delegate's ONE health record (#5535, ADR 0135 D8).

The probe stops at the handshake, so a delegate whose card answers while every real
send times out used to keep a green badge: the failure reached only ``status`` (the
``last_dispatch`` row). These tests pin the failure twin of ``record_dispatch_success``
on the same record and the same ``_record_result`` backoff the probe uses.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import plugins.delegates as P
import plugins.delegates.health as H
from plugins.delegates import api, status, store
from plugins.delegates.adapters import ADAPTERS, DelegateError
from plugins.delegates.registry import DelegateRegistry

RAW = {"name": "codex", "type": "acp", "command": "x", "workdir": "/tmp"}
TIMEOUT = "delegate 'codex': send timed out\n  after 120s"
REASON = "delegate 'codex': send timed out after 120s"


@pytest.fixture(autouse=True)
def _clean_dispatch_status():
    status.reset()
    yield
    status.reset()


def _registry(monkeypatch, behaviour):
    """One acp delegate whose adapter dispatch does ``behaviour``."""

    async def _dispatch(d, query, *, timeout=None, item_id=None, resume_task_id=None):
        return behaviour()

    monkeypatch.setattr(ADAPTERS["acp"], "dispatch", _dispatch)
    return DelegateRegistry([dict(RAW)])


def _timeout():
    raise DelegateError(TIMEOUT)


async def _fail(reg):
    with pytest.raises(DelegateError):
        await reg.dispatch("codex", "go")


async def test_failed_dispatch_degrades_a_green_probe_record(monkeypatch):
    monkeypatch.setattr(H.time, "time", lambda: 1000)
    H._HEALTH["codex"] = {"ok": True, "checked_at": 950, "latency_ms": 4, "detail": "ok"}
    H._SUCCESSES["codex"] = 3
    H._NEXT_DUE["codex"] = 1070
    reg = _registry(monkeypatch, _timeout)
    await _fail(reg)

    assert H._HEALTH["codex"] == {
        "ok": False, "checked_at": 1000, "source": "dispatch", "detail": REASON, "error": REASON,
    }
    # The probe's own backoff state, via _record_result: failure streak 1, success reset.
    assert H._FAILURES["codex"] == 1 and "codex" not in H._SUCCESSES
    assert H._NEXT_DUE["codex"] == 1000 + H._backoff_delay(1)
    # The reason matches last_dispatch, and the superseded probe stays as evidence.
    assert status.snapshot()["codex"]["error"] == REASON
    assert H.health_peek()["codex"]["probe"] == {"ok": True, "checked_at": 950, "latency_ms": 4, "detail": "ok"}


async def test_repeated_dispatch_failures_share_the_probe_backoff(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(H.time, "time", lambda: now[0])
    monkeypatch.setattr(store, "merged_delegates", lambda: [dict(RAW)])

    async def bad_probe(d):
        return {"ok": False, "error": "card timeout"}

    monkeypatch.setattr(ADAPTERS["acp"], "probe", bad_probe)
    await H._probe_all(now=now[0])
    assert H._FAILURES["codex"] == 1
    reg = _registry(monkeypatch, _timeout)
    await _fail(reg)
    assert H._FAILURES["codex"] == 2  # one streak, not a second counter
    assert H._NEXT_DUE["codex"] == 1000 + H._backoff_delay(2)
    for _ in range(10):
        await _fail(reg)
    assert H._NEXT_DUE["codex"] == 1000 + H._BACKOFF_MAX_S
    assert H.health_peek()["codex"]["probe"]["error"] == "card timeout"


async def test_list_agents_and_console_payload_report_the_degraded_state(monkeypatch):
    monkeypatch.setattr(H.time, "time", lambda: 1000)
    monkeypatch.setattr(status.time, "time", lambda: 1000)
    H._HEALTH["codex"] = {"ok": True, "checked_at": 990, "detail": "ok"}
    reg = _registry(monkeypatch, _timeout)
    tool = P._build_list_agents(reg)
    assert tool.invoke({}).startswith("🟢 codex")
    await _fail(reg)

    text = tool.invoke({})
    assert text.startswith("🔴 codex")
    assert f"dispatch failed, 0s ago: {REASON}" in text
    row = json.loads(tool.invoke({"detail": True}))[0]
    assert row["health"]["ok"] is False and row["health"]["source"] == "dispatch"
    assert row["health"]["detail"] == REASON
    assert row["last_dispatch"]["ok"] is False and row["last_dispatch"]["error"] == row["health"]["detail"]
    assert row["probe"]["ok"] is True  # the handshake evidence is kept, not the verdict

    # The console badge reads the same record: one entry, red, with the reason.
    monkeypatch.setattr(store, "read_host_delegates_raw", lambda: [])
    monkeypatch.setattr(store, "read_delegates_raw", lambda: [dict(RAW)])
    listed = api._list_payload()
    view = next(d for d in listed["delegates"] if d["name"] == "codex")
    assert view["health"]["ok"] is False and view["health"]["error"] == REASON
    assert view["last_dispatch"]["ok"] is False
    assert list(H._HEALTH) == ["codex"]


async def test_degraded_record_goes_stale_like_a_failed_probe(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(H.time, "time", lambda: now[0])
    reg = _registry(monkeypatch, _timeout)
    await _fail(reg)
    tool = P._build_list_agents(reg)
    now[0] = 1000 + H._STALE_AFTER_S
    assert tool.invoke({}).startswith("🔴 codex")
    now[0] += 1
    assert tool.invoke({}).startswith("⚪ codex") and "stale" in tool.invoke({})


async def test_later_successful_dispatch_clears_it(monkeypatch):
    monkeypatch.setattr(H.time, "time", lambda: 1000)
    outcomes = [_timeout, lambda: "done"]
    reg = _registry(monkeypatch, lambda: outcomes.pop(0)())
    await _fail(reg)
    assert H._HEALTH["codex"]["ok"] is False
    assert await reg.dispatch("codex", "go") == "done"
    assert H._HEALTH["codex"] == {"ok": True, "checked_at": 1000, "source": "dispatch", "detail": "dispatch succeeded"}
    assert "codex" not in H._FAILURES
    assert H._NEXT_DUE["codex"] == 1000 + H._INTERVAL_S
    assert P._build_list_agents(reg).invoke({}).startswith("🟢 codex")


async def test_later_successful_probe_clears_it(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(H.time, "time", lambda: now[0])
    monkeypatch.setattr(store, "merged_delegates", lambda: [dict(RAW)])
    probes = []

    async def good_probe(d):
        probes.append(d.name)
        return {"ok": True, "latency_ms": 3, "detail": "ok"}

    monkeypatch.setattr(ADAPTERS["acp"], "probe", good_probe)
    reg = _registry(monkeypatch, _timeout)
    await _fail(reg)
    # Backed off like a failed probe: not re-probed before its due time...
    await H._probe_all(now=H._NEXT_DUE["codex"] - 1)
    assert probes == [] and H._HEALTH["codex"]["ok"] is False
    # ...and the first probe after it clears the record and the streak.
    now[0] = H._NEXT_DUE["codex"]
    await H._probe_all(now=now[0])
    assert probes == ["codex"]
    assert H._HEALTH["codex"] == {"ok": True, "latency_ms": 3, "detail": "ok", "checked_at": now[0]}
    assert "codex" not in H._FAILURES
    assert P._build_list_agents(reg).invoke({}).startswith("🟢 codex")


async def test_cancelled_dispatch_does_not_degrade_health(monkeypatch):
    monkeypatch.setattr(H.time, "time", lambda: 1000)
    healthy = {"ok": True, "checked_at": 990, "detail": "ok"}
    H._HEALTH["codex"] = dict(healthy)
    H._NEXT_DUE["codex"] = 1110

    def cancel():
        raise asyncio.CancelledError()

    reg = _registry(monkeypatch, cancel)
    with pytest.raises(asyncio.CancelledError):
        await reg.dispatch("codex", "go")
    assert H._HEALTH["codex"] == healthy
    assert "codex" not in H._FAILURES and H._NEXT_DUE["codex"] == 1110
    assert "codex" not in status.snapshot()
    assert P._build_list_agents(reg).invoke({}).startswith("🟢 codex")


async def test_cancelled_dispatch_leaves_a_never_probed_delegate_untouched(monkeypatch):
    def cancel():
        raise asyncio.CancelledError()

    reg = _registry(monkeypatch, cancel)
    with pytest.raises(asyncio.CancelledError):
        await reg.dispatch("codex", "go")
    assert H._HEALTH == {} and H._FAILURES == {} and H._NEXT_DUE == {}


async def test_inflight_probe_does_not_undo_a_newer_dispatch_failure(monkeypatch):
    monkeypatch.setattr(store, "merged_delegates", lambda: [dict(RAW)])
    started, finish = asyncio.Event(), asyncio.Event()

    async def probe(d):
        started.set()
        await finish.wait()
        return {"ok": True, "detail": "card answered"}

    monkeypatch.setattr(ADAPTERS["acp"], "probe", probe)
    reg = _registry(monkeypatch, _timeout)
    task = asyncio.create_task(H._probe_all())
    await started.wait()
    await _fail(reg)
    finish.set()
    await task
    # The card answered, but the send that failed while it was in flight is newer.
    assert H._HEALTH["codex"]["ok"] is False and H._HEALTH["codex"]["source"] == "dispatch"
    assert H._FAILURES["codex"] == 1
    assert H.health_peek()["codex"]["probe"]["detail"] == "card answered"


async def test_unknown_delegate_degrades_nothing():
    with pytest.raises(DelegateError):
        await DelegateRegistry([]).dispatch("ghost", "go")
    assert H._HEALTH == {} and H._FAILURES == {}


def test_empty_error_still_names_a_reason(monkeypatch):
    monkeypatch.setattr(H.time, "time", lambda: 1000)
    H.record_dispatch_failure("codex", "  ")
    assert H._HEALTH["codex"]["detail"] == "dispatch failed"
