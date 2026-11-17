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
        # Two distinct jobs → two keys, each scoped to the request ITEM (not message_id).
        assert {row["promotion_key"] for row in rows} == {
            f"inbox:{item['id']}:fix-ci",
            f"inbox:{item['id']}:write-docs",
        }
        # Both rows carry the same request lineage; the request is never its own row.
        for row in rows:
            assert json.loads(row["request_ref"]) == {**request, "item_id": str(item["id"])}


def test_same_job_key_under_different_requests_stays_separate(open_stores):
    """Scoping the key to the durable request item (not the bare job_key) keeps distinct
    requests apart: the same "fix-ci" fired for two different inbox items is two rows, never
    folded into one — the key never guesses equality from the job name alone."""
    with open_stores() as (inbox, tasks):
        first = inbox.add("Fix CI in repo A", priority="now")
        second = inbox.add("Fix CI in repo B", priority="now")
        ctx_a = inbox.execution_identity(first["id"])["context_id"]
        ctx_b = inbox.execution_identity(second["id"])["context_id"]
        assert create_task(tasks, ctx_a, job_key="fix-ci").startswith("Created ")
        assert create_task(tasks, ctx_b, job_key="fix-ci").startswith("Created ")

    with open_stores() as (_, tasks):
        rows = tasks.list()
        assert len(rows) == 2
        assert {row["promotion_key"] for row in rows} == {
            f"inbox:{first['id']}:fix-ci",
            f"inbox:{second['id']}:fix-ci",
        }


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
        assert original["promotion_key"] == f"inbox:{item['id']}:{normalized}"


def test_retired_execution_still_reuses_the_one_task(open_stores):
    """The decisive retry case: a turn that ends failed/canceled retires the execution
    identity (ADR 0129; server/a2a.py _restore_inbox_item), so the item's next delivery runs
    under a FRESH message_id and context_id. The job's single row must still be reused —
    which it is only because the key is scoped to the durable request item, not the retired
    execution's message_id."""
    with open_stores() as (inbox, tasks):
        item = inbox.add("Please investigate", priority="now")
        first = inbox.execution_identity(item["id"])
        inbox.mark_delivered([item["id"]])
        assert create_task(tasks, first["context_id"], job_key="fix-ci").startswith("Created ")
        original = tasks.list()[0]

    with open_stores() as (inbox, tasks):
        # The fired turn settled terminally: restore the item and retire its identity, in the
        # order server/a2a.py _restore_inbox_item does.
        inbox.mark_pending([item["id"]])
        inbox.retire_execution(item["id"])
        retry = inbox.execution_identity(item["id"])
        # A genuinely new execution identity — both the context and the message id changed,
        # exactly the situation under which message_id keying would duplicate the row.
        assert retry["message_id"] != first["message_id"]
        assert retry["context_id"] != first["context_id"]
        result = create_task(tasks, retry["context_id"], job_key="fix-ci", title="Retry title")
        assert result.startswith(f"Reused existing task {original['id']} for this job:")
        assert "Created" not in result

    with open_stores() as (_, tasks):
        assert tasks.list() == [original]
        assert original["promotion_key"] == f"inbox:{item['id']}:fix-ci"


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
