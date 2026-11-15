"""ADR 0129 S13: ``task_update`` warns when a task is set ``in_progress`` with nothing
driving it.

Exercises the tool end-to-end against a real ``TaskStore``, with the shared driver test
(``tasks.stall``) stubbed so each case is deterministic: an undriven ``in_progress``
update gets the stall-reminder note; a task linked to an unresolved card (driven) and any
non-``in_progress`` status get none; and a driver check that raises is swallowed — no note,
and the update still applies.
"""

from __future__ import annotations

import pytest

import tasks.stall as stall
from tasks.stall import DriverSources
from tasks.store import TaskStore
from tools.scheduler_tools import _UNDRIVEN_IN_PROGRESS_NOTE, _build_task_tools

NOTE_MARK = "nothing is driving this task"


@pytest.fixture
def store(tmp_path):
    return TaskStore(db_path=str(tmp_path / "issues.db"))


def _task_update(store):
    return next(t for t in _build_task_tools(store) if t.name == "task_update")


def _inert_sources(**overrides) -> DriverSources:
    """A ``DriverSources`` with nothing driving anything (per-test overrides)."""
    base = dict(
        list_jobs=lambda: [],
        active_watches=lambda: [],
        running_delegations=lambda _sid: 0,
        running_background=lambda _sid: 0,
        turn_running=lambda _sid: False,
    )
    base.update(overrides)
    return DriverSources(**base)


def test_undriven_in_progress_gets_the_note(store, monkeypatch):
    monkeypatch.setattr(stall, "default_sources", _inert_sources)
    i = store.create("ship it", session_id="sess-1")
    out = _task_update(store).invoke({"issue_id": i["id"], "status": "in_progress"})
    # The plain success line is preserved and the note is appended after it.
    assert out == f"Updated {i['id']}: [in_progress] {i['title']}" + _UNDRIVEN_IN_PROGRESS_NOTE
    assert NOTE_MARK in out
    # The update applied regardless of the appended note.
    assert store.get(i["id"])["status"] == "in_progress"


def test_linked_to_unresolved_card_gets_no_note(store, monkeypatch):
    # An unresolved link drives the task — is_driven short-circuits on links before it
    # ever reads a source, so even inert sources leave no note.
    monkeypatch.setattr(stall, "default_sources", _inert_sources)
    i = store.create("ship it", session_id="sess-1")
    out = _task_update(store).invoke({"issue_id": i["id"], "status": "in_progress", "links": ["bd-9"]})
    assert out == f"Updated {i['id']}: [in_progress] {i['title']}"
    assert NOTE_MARK not in out


def test_other_statuses_get_no_note(store, monkeypatch):
    # Only an in_progress result is checked; moving an (undriven) task to blocked, open or
    # deferred is never warned.
    monkeypatch.setattr(stall, "default_sources", _inert_sources)
    i = store.create("ship it", session_id="sess-1")
    out = _task_update(store).invoke({"issue_id": i["id"], "status": "blocked"})
    assert out == f"Updated {i['id']}: [blocked] {i['title']}"
    assert NOTE_MARK not in out


def test_raising_source_gets_no_note_and_update_applies(store, monkeypatch):
    # A live source that raises reads as driven (is_driven fail-quiet: never nudge on
    # missing evidence), so no note — and the update still took effect.
    def _boom():
        raise RuntimeError("scheduler DB down")

    monkeypatch.setattr(stall, "default_sources", lambda: _inert_sources(list_jobs=_boom))
    i = store.create("ship it", session_id="sess-1")
    out = _task_update(store).invoke({"issue_id": i["id"], "status": "in_progress"})
    assert out == f"Updated {i['id']}: [in_progress] {i['title']}"
    assert NOTE_MARK not in out
    assert store.get(i["id"])["status"] == "in_progress"


def test_check_that_raises_outright_is_swallowed(store, monkeypatch):
    # Even if the driver check itself (not just a source) blows up, the helper is
    # fail-quiet: the plain success string stands and the update is unchanged.
    def _explode(*_a, **_k):
        raise RuntimeError("driver test broke")

    monkeypatch.setattr(stall, "default_sources", _inert_sources)
    monkeypatch.setattr(stall, "is_driven", _explode)
    i = store.create("ship it", session_id="sess-1")
    out = _task_update(store).invoke({"issue_id": i["id"], "status": "in_progress"})
    assert out == f"Updated {i['id']}: [in_progress] {i['title']}"
    assert NOTE_MARK not in out
    assert store.get(i["id"])["status"] == "in_progress"
