"""Each explicitly named inbox job survives retries as one durable task row."""

import json
from contextlib import contextmanager

import pytest

from inbox.store import InboxStore
from runtime.state import STATE
from tasks.store import TaskStore
from tools.scheduler_tools import _build_task_tools


@pytest.fixture
def open_stores(tmp_path, monkeypatch):
    monkeypatch.delenv("TASKS_DB_PATH", raising=False)
    monkeypatch.delenv("BEADS_DB_PATH", raising=False)
    monkeypatch.setattr(STATE, "scheduler", None)
    monkeypatch.setattr(STATE, "background_mgr", None)

    @contextmanager
    def opened():
        inbox = InboxStore(str(tmp_path / "inbox.db"))
        tasks = TaskStore(str(tmp_path / "tasks.db"))
        monkeypatch.setattr(STATE, "inbox_store", inbox)
        try:
            yield inbox, tasks
        finally:
            tasks._conn.close()

    return opened


def create_task(tasks, context, **kwargs):
    create = next(tool for tool in _build_task_tools(tasks) if tool.name == "task_create")
    return create.invoke({"title": "Follow up", "state": {"session_id": context}, **kwargs})


def test_distinct_jobs_share_only_request_lineage(open_stores):
    with open_stores() as (inbox, tasks):
        item = inbox.add("Fix CI and write docs", priority="now")
        identity = inbox.execution_identity(item["id"])
        for key in ("fix-ci", "write-docs"):
            assert create_task(tasks, identity["context_id"], job_key=key).startswith("Created ")

    with open_stores() as (inbox, tasks):
        request = inbox.request_for_context(identity["context_id"])["request"]
        rows = tasks.list()
        assert len(rows) == 2
        assert len({row["id"] for row in rows}) == 2
        assert {row["promotion_key"] for row in rows} == {
            f"inbox:{identity['message_id']}:fix-ci",
            f"inbox:{identity['message_id']}:write-docs",
        }
        for row in rows:
            assert json.loads(row["request_ref"]) == {**request, "item_id": str(item["id"])}


@pytest.mark.parametrize(("first_key", "retry_key", "normalized"), [
    ("fix-ci", "fix-ci", "fix-ci"),
    ("  Fix \t CI\n", "fix ci", "fix-ci"),
    ("A" * 81, "a" * 80, "a" * 80),
])
def test_redelivered_job_reuses_persisted_task(open_stores, first_key, retry_key, normalized):
    with open_stores() as (inbox, tasks):
        item = inbox.add("Please investigate", priority="now")
        identity = inbox.execution_identity(item["id"])
        inbox.mark_delivered([item["id"]])
        assert create_task(tasks, identity["context_id"], job_key=first_key).startswith("Created ")
        original = tasks.list()[0]

    with open_stores() as (inbox, tasks):
        inbox.mark_pending([item["id"]])
        redelivery = inbox.execution_identity(item["id"])
        result = create_task(tasks, redelivery["context_id"], job_key=retry_key, title="Retry title")
        assert result.startswith(f"Reused existing task {original['id']} for this job:")
        assert "Created" not in result

    with open_stores() as (_, tasks):
        assert tasks.list() == [original]
        assert original["promotion_key"] == f"inbox:{identity['message_id']}:{normalized}"


@pytest.mark.parametrize(("correlated", "kwargs"), [
    (False, {"job_key": "fix-ci"}),
    (True, {}),
    (True, {"job_key": ""}),
    (True, {"job_key": " \t\n "}),
])
def test_without_request_or_job_key_each_call_creates_a_row(open_stores, correlated, kwargs):
    with open_stores() as (inbox, tasks):
        context = "job:unknown"
        if correlated:
            item = inbox.add("Please investigate", priority="now")
            context = inbox.execution_identity(item["id"])["context_id"]
        assert create_task(tasks, context, **kwargs).startswith("Created ")

    with open_stores() as (_, tasks):
        assert create_task(tasks, context, **kwargs).startswith("Created ")

    with open_stores() as (_, tasks):
        rows = tasks.list()
        assert len(rows) == 2
        assert len({row["id"] for row in rows}) == 2
        assert all(row["promotion_key"] == "" for row in rows)
        assert all(bool(row["request_ref"]) == correlated for row in rows)
