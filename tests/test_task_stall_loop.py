"""The stall sweep pass (ADR 0129 D9 items 2–4 / S11b): ``run_task_stall_pass`` against a
real ``TaskStore`` on a tmp path, a fake inbox and injected fake driver sources.

The driver logic itself (``is_driven`` / ``find_stalled``) and the store's ``stall_nudges``
column live in ``test_task_stall.py``; here we exercise only the pass that queues one nudge
per stalled task, honours the dedup/cap contract, and parks a task ``waiting`` after the cap.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from inbox.store import InboxStore
from server.maintenance_loops import run_task_stall_pass
from tasks.stall import STALL_NUDGE_CAP, DriverSources
from tasks.store import TaskStore

STALL_ASK = "Stalled: no progress after 3 reminders. Continue, defer or close?"


def _sources(**overrides) -> DriverSources:
    """An inert ``DriverSources`` (nothing driving anything), with per-test overrides."""
    base = dict(
        list_jobs=lambda: [],
        active_watches=lambda: [],
        running_delegations=lambda _sid: 0,
        running_background=lambda _sid: 0,
        turn_running=lambda _sid: False,
    )
    base.update(overrides)
    return DriverSources(**base)


class _FakeInbox:
    """Records ``add()`` calls. By default every add inserts (returns a row); set
    ``dedup_all`` to simulate the real store's dedup kicking in (the same key is already
    queued within the window) so ``add`` returns ``None``."""

    def __init__(self) -> None:
        self.added: list[dict] = []
        self.dedup_all = False

    def add(
        self,
        text,
        *,
        priority="next",
        source="",
        dedup_key="",
        delivery="",
        target_session="",
        needs_human=False,
        origin_session=None,
        now=None,
    ):
        if self.dedup_all:
            return None
        row = {
            "id": len(self.added) + 1,
            "text": text,
            "priority": priority,
            "source": source,
            "dedup_key": dedup_key,
            "needs_human": needs_human,
        }
        self.added.append(row)
        return row


@pytest.fixture
def store(tmp_path):
    return TaskStore(db_path=str(tmp_path / "issues.db"))


def _stalled_task(store, *, session_id="sess-1", title="ship the thing") -> str:
    """Create an ``in_progress`` task and return its id. Its ``updated_at`` is real
    wall-clock now; tests pass a ``now`` 31 min later so ``find_stalled`` reads it as
    old enough (and the inert sources leave it undriven)."""
    task = store.create(title, session_id=session_id)
    store.update(task["id"], status="in_progress")
    return task["id"]


def _now_past(store, task_id, minutes) -> datetime:
    """A ``now`` ``minutes`` after the task's current ``updated_at`` — so ``find_stalled``
    sees exactly that age without depending on how fast the test runs."""
    upd = datetime.fromisoformat(store.get(task_id)["updated_at"])
    if upd.tzinfo is None:
        upd = upd.replace(tzinfo=UTC)
    return upd + timedelta(minutes=minutes)


async def test_one_nudge_per_stalled_task_per_pass(store):
    a = _stalled_task(store, session_id="sess-a", title="A")
    b = _stalled_task(store, session_id="sess-b", title="B")
    inbox = _FakeInbox()
    now = _now_past(store, b, 31)  # b updated last → both are ≥31 min old relative to this

    res = await run_task_stall_pass(store, inbox, now=now, sources=_sources())

    assert res == {"nudged": 2, "parked": 0}
    assert len(inbox.added) == 2
    for item in inbox.added:
        assert item["source"] == "task-stall"
        assert item["priority"] == "next"
        assert item["needs_human"] is False  # never an operator page
    assert store.get(a)["stall_nudges"] == 1
    assert store.get(b)["stall_nudges"] == 1
    # The nudge names the task, its age and its title.
    texts = {item["text"] for item in inbox.added}
    assert any(
        t.startswith(f"Task {a} is in progress but nothing is driving it") and "min ago: A." in t
        for t in texts
    )


async def test_driven_task_is_not_nudged(store):
    tid = _stalled_task(store, session_id="sess-live")
    inbox = _FakeInbox()
    now = _now_past(store, tid, 31)
    # A live turn in the task's session drives it → find_stalled skips it.
    res = await run_task_stall_pass(store, inbox, now=now, sources=_sources(turn_running=lambda _sid: True))

    assert res == {"nudged": 0, "parked": 0}
    assert inbox.added == []
    assert store.get(tid)["stall_nudges"] == 0


@pytest.mark.parametrize(
    ("session_id", "expected_origin"),
    [("chat-a", "chat-a"), ("", None), ("job:abc", None), ("background:abc", None)],
)
async def test_nudge_preserves_only_conversation_origin(store, tmp_path, session_id, expected_origin):
    tid = _stalled_task(store, session_id=session_id)
    inbox = InboxStore(db_path=str(tmp_path / "inbox.db"))

    res = await run_task_stall_pass(
        store, inbox, now=_now_past(store, tid, 31), sources=_sources()
    )

    assert res == {"nudged": 1, "parked": 0}
    items = inbox.list()
    assert len(items) == 1
    assert items[0]["origin_session"] == expected_origin


async def test_deduped_add_does_not_increment(store):
    tid = _stalled_task(store)
    inbox = _FakeInbox()
    inbox.dedup_all = True  # the inbox already holds this nudge (same key within the window)
    now = _now_past(store, tid, 31)

    res = await run_task_stall_pass(store, inbox, now=now, sources=_sources())

    assert res == {"nudged": 0, "parked": 0}
    assert inbox.added == []
    assert store.get(tid)["stall_nudges"] == 0  # a deduped add must not advance toward the cap


async def test_fourth_pass_parks_waiting_after_three_nudges(store):
    tid = _stalled_task(store)
    inbox = _FakeInbox()
    # record_stall_nudge never touches updated_at, so the task stays this old across passes.
    now = _now_past(store, tid, 31)

    for expected in (1, 2, 3):
        res = await run_task_stall_pass(store, inbox, now=now, sources=_sources())
        assert res == {"nudged": 1, "parked": 0}
        assert store.get(tid)["stall_nudges"] == expected

    assert len(inbox.added) == STALL_NUDGE_CAP
    keys = [item["dedup_key"] for item in inbox.added]
    assert len(set(keys)) == STALL_NUDGE_CAP  # one distinct nudge per stall step
    assert [k.rsplit(":", 1)[1] for k in keys] == ["0", "1", "2"]  # keyed by the current count

    # The 4th pass is at the cap: park the task waiting with the ask, queue nothing.
    res = await run_task_stall_pass(store, inbox, now=now, sources=_sources())
    assert res == {"nudged": 0, "parked": 1}
    assert len(inbox.added) == STALL_NUDGE_CAP  # nothing new queued
    parked = store.get(tid)
    assert parked["status"] == "waiting"
    assert parked["ask"] == STALL_ASK


async def test_real_update_resets_and_no_nudge_until_30_min_later(store):
    tid = _stalled_task(store)
    inbox = _FakeInbox()

    # First pass at 31 min: one nudge.
    res = await run_task_stall_pass(store, inbox, now=_now_past(store, tid, 31), sources=_sources())
    assert res == {"nudged": 1, "parked": 0}
    assert store.get(tid)["stall_nudges"] == 1

    # A real update moves updated_at and zeroes the count (TaskStore.update).
    store.update(tid, status="in_progress")
    assert store.get(tid)["stall_nudges"] == 0

    # 29 min after the update → still inside the 30-min threshold → no nudge.
    res = await run_task_stall_pass(store, inbox, now=_now_past(store, tid, 29), sources=_sources())
    assert res == {"nudged": 0, "parked": 0}
    assert store.get(tid)["stall_nudges"] == 0

    # 31 min after the update → stalled again → one fresh nudge.
    res = await run_task_stall_pass(store, inbox, now=_now_past(store, tid, 31), sources=_sources())
    assert res == {"nudged": 1, "parked": 0}
    assert store.get(tid)["stall_nudges"] == 1
