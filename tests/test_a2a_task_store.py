"""Tests for durable A2A task-record persistence (A2A spec / ADR 0003)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from a2a_handler import (
    COMPLETED,
    SUBMITTED,
    WORKING,
    A2ATaskStore,
    PushNotificationConfig,
    TaskRecord,
    _now_iso,
    _record_to_row,
    _row_to_record,
)
from a2a_task_store import A2ATaskPersistence


def _rec(**kw) -> TaskRecord:
    now = _now_iso()
    d = dict(id="t1", context_id="ctx", state=SUBMITTED, created_at=now,
             updated_at=now, message_text="hi")
    d.update(kw)
    return TaskRecord(**d)


# ── serialization ─────────────────────────────────────────────────────────────


def test_record_roundtrip_preserves_durable_fields():
    rec = _rec(
        state=COMPLETED, accumulated_text="the answer",
        usage={"input_tokens": 5, "output_tokens": 7, "total_tokens": 12},
        confidence=0.9, deltas=[{"domain": "x", "op": "inc"}],
        push_config=PushNotificationConfig(url="https://h/cb", token="sek", id="c1"),
    )
    back = _row_to_record(_record_to_row(rec))
    assert back.id == "t1" and back.state == COMPLETED
    assert back.accumulated_text == "the answer"
    assert back.usage["total_tokens"] == 12
    assert back.confidence == 0.9
    assert back.deltas == [{"domain": "x", "op": "inc"}]
    assert back.push_config.url == "https://h/cb" and back.push_config.token == "sek"


# ── persistence layer ─────────────────────────────────────────────────────────


def test_save_get_delete(tmp_path):
    p = A2ATaskPersistence(str(tmp_path / "a2a-tasks.db"))
    p.save(_record_to_row(_rec(id="t1", state=COMPLETED, accumulated_text="x")))
    assert p.get("t1")["accumulated_text"] == "x"
    p.delete("t1")
    assert p.get("t1") is None


def test_sweep_expired(tmp_path):
    p = A2ATaskPersistence(str(tmp_path / "a2a-tasks.db"), ttl_s=60)
    old = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    p.save({"id": "stale", "state": COMPLETED, "updated_at": old})
    p.save(_record_to_row(_rec(id="fresh", state=COMPLETED)))
    assert p.sweep_expired() == 1
    assert p.get("stale") is None and p.get("fresh") is not None


def test_fail_interrupted_marks_nonterminal(tmp_path):
    p = A2ATaskPersistence(str(tmp_path / "a2a-tasks.db"))
    p.save(_record_to_row(_rec(id="live", state=WORKING)))
    p.save(_record_to_row(_rec(id="done", state=COMPLETED, accumulated_text="ok")))
    n = p.fail_interrupted((COMPLETED, "failed", "canceled"), error="interrupted by server restart")
    assert n == 1
    assert p.get("live")["state"] == "failed"
    assert p.get("live")["error_message"] == "interrupted by server restart"
    assert p.get("done")["state"] == COMPLETED  # untouched


# ── store integration ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_store_persists_on_create_and_terminal(tmp_path):
    p = A2ATaskPersistence(str(tmp_path / "a2a-tasks.db"))
    store = A2ATaskStore()
    store.attach_persistence(p)
    await store.create(_rec(id="t1", state=SUBMITTED))
    assert p.get("t1")["state"] == SUBMITTED  # persisted on create
    await store.update_state("t1", WORKING)
    # WORKING is non-terminal → not re-persisted (still SUBMITTED on disk)
    assert p.get("t1")["state"] == SUBMITTED
    await store.update_state("t1", COMPLETED, accumulated_text="done")
    assert p.get("t1")["state"] == COMPLETED  # terminal persisted
    assert p.get("t1")["accumulated_text"] == "done"


@pytest.mark.asyncio
async def test_store_lazy_loads_from_disk_after_eviction(tmp_path):
    p = A2ATaskPersistence(str(tmp_path / "a2a-tasks.db"))
    p.save(_record_to_row(_rec(id="t1", state=COMPLETED, accumulated_text="recovered")))
    store = A2ATaskStore()
    store.attach_persistence(p)
    assert "t1" not in store._tasks  # not in memory (e.g. evicted / post-restart)
    got = await store.get("t1")
    assert got is not None and got.state == COMPLETED
    assert got.accumulated_text == "recovered"
    assert "t1" in store._tasks  # cached back in
