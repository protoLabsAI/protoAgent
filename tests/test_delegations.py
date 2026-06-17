"""Cancellable subagent delegations (Tier 2) — graph/delegations.py: a per-session
registry of in-flight foreground `task` delegations so the console can abort ONE
delegation without killing the whole turn.
"""

from __future__ import annotations

import asyncio

import pytest

from graph import delegations


@pytest.fixture(autouse=True)
def _clear():
    delegations._RUNNING.clear()
    yield
    delegations._RUNNING.clear()


class _FakeTask:
    """Minimal asyncio.Task stand-in for registry tests that don't need a loop."""

    def __init__(self, done: bool = False):
        self._done = done
        self.cancel_called = False

    def done(self) -> bool:
        return self._done

    def cancel(self) -> None:
        self.cancel_called = True


# ── the registry ───────────────────────────────────────────────────────────────


def test_register_lists_and_counts_in_order():
    delegations.register("s1", "d1", _FakeTask(), label="research X")
    delegations.register("s1", "d2", _FakeTask(), label="research Y")
    assert delegations.running("s1") == 2
    assert delegations.running_items("s1") == [
        {"id": "d1", "label": "research X"},
        {"id": "d2", "label": "research Y"},
    ]
    assert delegations.running_items("other") == []  # per-session


def test_register_ignores_blank_session_or_id():
    delegations.register("", "d1", _FakeTask())
    delegations.register("s1", "", _FakeTask())
    assert delegations.running("s1") == 0


def test_unregister_drops_entry_and_empty_session():
    delegations.register("s1", "d1", _FakeTask())
    delegations.unregister("s1", "d1")
    assert "s1" not in delegations._RUNNING  # no empty session dict lingers
    delegations.unregister("s1", "gone")  # safe no-op on a missing entry/session


# ── cancel semantics ───────────────────────────────────────────────────────────


def test_cancel_marks_flag_and_cancels_the_task():
    f = _FakeTask()
    delegations.register("s1", "d1", f, label="long one")
    assert delegations.cancel("s1", "d1") is True
    assert f.cancel_called is True
    assert delegations.was_cancelled("s1", "d1") is True
    # second cancel is a no-op — already cancelling (too late to "find" it live)
    assert delegations.cancel("s1", "d1") is False


def test_cancel_false_when_absent_or_already_done():
    assert delegations.cancel("s1", "missing") is False  # never registered
    done = _FakeTask(done=True)
    delegations.register("s1", "d1", done)
    assert delegations.cancel("s1", "d1") is False  # already finished → nothing to abort
    assert done.cancel_called is False  # we don't cancel a done task
    assert delegations.was_cancelled("s1", "d1") is False


def test_was_cancelled_false_for_unflagged_or_absent():
    delegations.register("s1", "d1", _FakeTask())
    assert delegations.was_cancelled("s1", "d1") is False  # registered but not cancelled
    assert delegations.was_cancelled("s1", "nope") is False
    assert delegations.was_cancelled("other", "d1") is False


# ── the real cancel mechanism the tool relies on ───────────────────────────────


@pytest.mark.asyncio
async def test_cancel_aborts_a_real_asyncio_task():
    t = asyncio.ensure_future(asyncio.sleep(3600))
    await asyncio.sleep(0)  # let it start running
    delegations.register("s1", "d1", t, label="real")
    assert delegations.cancel("s1", "d1") is True
    with pytest.raises(asyncio.CancelledError):
        await t
    assert t.cancelled()
