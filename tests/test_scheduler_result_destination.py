"""Result preferences survive storage, migration, and scheduling tool calls."""

import sqlite3

import pytest

from scheduler.local import LocalScheduler
from tools.scheduler_tools import _build_scheduler_tools


WHEN = "2099-05-01T15:00:00+00:00"


def _scheduler(tmp_path):
    return LocalScheduler("destination-test", invoke_url="http://test", db_dir=tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("destination", [None, "origin", "home"])
async def test_schedule_destination_round_trips_and_survives_edits(tmp_path, destination):
    scheduler = _scheduler(tmp_path)
    tool = next(t for t in _build_scheduler_tools(scheduler) if t.name == "schedule_task")
    args = {"prompt": "Report progress", "when": WHEN, "state": {"session_id": "chat-a"}}
    if destination is not None:
        args["result_destination"] = destination
    assert (await tool.ainvoke(args)).startswith("Scheduled job ")
    scheduler = _scheduler(tmp_path)
    job = scheduler.list_jobs()[0]
    assert job.result_destination == (destination or "origin")
    assert job.origin_session == "chat-a"
    assert job.context_id == "chat-a"
    edited = scheduler.update_job(job.id, "Report updated progress", WHEN)
    assert edited.result_destination == job.result_destination
    assert scheduler.cancel_job(job.id)
    restored = scheduler.add_job(edited.prompt, edited.schedule, job_id=job.id, restore_state=edited)
    assert restored.result_destination == job.result_destination
    assert _scheduler(tmp_path).list_jobs()[0].result_destination == job.result_destination


def test_existing_database_without_destination_migrates(tmp_path):
    scheduler = _scheduler(tmp_path)
    job = scheduler.add_job("Legacy job", WHEN)
    with sqlite3.connect(scheduler.path) as db:
        db.execute("ALTER TABLE jobs DROP COLUMN result_destination")
    scheduler = _scheduler(tmp_path)
    assert scheduler.get_job(job.id).result_destination == "origin"
    with sqlite3.connect(scheduler.path) as db:
        assert db.execute("SELECT result_destination FROM jobs WHERE id = ?", (job.id,)).fetchone() == ("origin",)


@pytest.mark.asyncio
async def test_destination_is_part_of_duplicate_detection(tmp_path):
    scheduler = _scheduler(tmp_path)
    tool = next(t for t in _build_scheduler_tools(scheduler) if t.name == "schedule_task")
    args = {"prompt": "Report progress", "when": WHEN}
    assert (await tool.ainvoke(args)).startswith("Scheduled job ")
    args["result_destination"] = "home"
    assert (await tool.ainvoke(args)).startswith("Scheduled job ")
    assert (await tool.ainvoke(args)).startswith("Already scheduled as ")
    assert {j.result_destination for j in scheduler.list_jobs()} == {"origin", "home"}


@pytest.mark.asyncio
@pytest.mark.parametrize("destination", ["elsewhere", "", "HOME"])
async def test_invalid_destination_does_not_create_job(tmp_path, destination):
    scheduler = _scheduler(tmp_path)
    tool = next(t for t in _build_scheduler_tools(scheduler) if t.name == "schedule_task")
    result = await tool.ainvoke({"prompt": "Report", "when": WHEN, "result_destination": destination})
    assert result == 'Error: result_destination must be "origin" or "home".'
    with pytest.raises(ValueError, match='result_destination must be "origin" or "home"'):
        scheduler.add_job("Report", WHEN, result_destination=destination)
    assert scheduler.list_jobs() == []
