"""Inbox-fired tasks keep durable request provenance without deduplicating work."""

import json

import pytest

from inbox.store import InboxStore
from runtime.state import STATE
from tasks.store import TaskStore
from tools.scheduler_tools import _build_task_tools


@pytest.fixture
def stores(tmp_path, monkeypatch):
    monkeypatch.delenv("TASKS_DB_PATH", raising=False)
    monkeypatch.delenv("BEADS_DB_PATH", raising=False)
    inbox = InboxStore(str(tmp_path / "inbox.db"))
    tasks = TaskStore(str(tmp_path / "tasks.db"))
    monkeypatch.setattr(STATE, "inbox_store", inbox)
    monkeypatch.setattr(STATE, "scheduler", None)
    monkeypatch.setattr(STATE, "background_mgr", None)
    yield inbox, tasks
    tasks._conn.close()


def create_task(tasks, context):
    create = next(tool for tool in _build_task_tools(tasks) if tool.name == "task_create")
    result = create.invoke({"title": "Follow up", "state": {"session_id": context}})
    assert result.startswith("Created ")


@pytest.mark.parametrize("accepted", [False, True])
def test_inbox_request_survives_redelivery_and_reopened_stores(stores, tmp_path, monkeypatch, accepted):
    inbox, tasks = stores
    requester = {"caller": "peer-a", "caller_kind": "agent", "on_behalf_of": "Josh"}
    item = inbox.add("Please investigate", priority="now", origin=requester,
                     origin_session="chat-original", dedup_key="request-1")
    identity = inbox.execution_identity(item["id"])
    request = {"kind": "inbox", "item_id": item["id"],
               "context_id": identity["context_id"], "message_id": identity["message_id"]}
    if accepted:
        inbox.record_execution_acceptance(item["id"], "accepted-task")
        request["task_id"] = "accepted-task"
    expected = {"request": request, "requester": requester}
    assert inbox.request_for_context(identity["context_id"]) == expected
    inbox.mark_delivered([item["id"]])
    create_task(tasks, identity["context_id"])
    first = tasks.list()[0]
    tasks._conn.close()

    reopened_inbox = InboxStore(str(tmp_path / "inbox.db"))
    reopened_tasks = TaskStore(str(tmp_path / "tasks.db"))
    monkeypatch.setattr(STATE, "inbox_store", reopened_inbox)
    try:
        assert reopened_tasks.get(first["id"]) == first
        reopened_inbox.mark_pending([item["id"]])
        redelivery = reopened_inbox.execution_identity(item["id"])
        assert redelivery["context_id"] == identity["context_id"]
        assert redelivery["message_id"] == identity["message_id"]
        assert reopened_inbox.request_for_context(redelivery["context_id"]) == expected
        create_task(reopened_tasks, redelivery["context_id"])
        rows = reopened_tasks.list()
        assert len(rows) == 2
        assert len({row["id"] for row in rows}) == 2
        for row in rows:
            assert json.loads(row["request_ref"]) == {**request, "item_id": str(item["id"])}
            assert json.loads(row["requester"]) == requester
            assert row["promotion_key"] == ""
            assert row["session_id"] == "chat-original"
    finally:
        reopened_tasks._conn.close()


@pytest.mark.parametrize("context", ["", "job:unknown", "background:unknown", "chat-a", "peer-context"])
def test_unrelated_context_has_no_request(stores, context):
    inbox, tasks = stores
    assert inbox.request_for_context(context) == {}
    create_task(tasks, context)
    row = tasks.list()[0]
    assert row["request_ref"] == row["requester"] == row["promotion_key"] == ""


@pytest.mark.parametrize("condition", ["no_metadata", "missing_item", "missing_store", "failed_read"])
def test_missing_provenance_does_not_invent_requester(stores, monkeypatch, condition):
    inbox, tasks = stores
    item = inbox.add("work", priority="now", source="not-a-requester")
    context = inbox.execution_identity(item["id"])["context_id"]
    if condition == "missing_item":
        db = inbox._connect()
        try:
            db.execute("DELETE FROM inbox WHERE id = ?", (item["id"],))
            db.commit()
        finally:
            db.close()
        assert inbox.request_for_context(context) == {}
    elif condition == "missing_store":
        monkeypatch.setattr(STATE, "inbox_store", None)
    elif condition == "failed_read":
        def broken():
            raise OSError("store unavailable")
        monkeypatch.setattr(inbox, "_connect", broken)
    else:
        assert inbox.request_for_context(context)["requester"] == {}
    create_task(tasks, context)
    row = tasks.list()[0]
    assert row["requester"] == ""
    if condition == "no_metadata":
        assert json.loads(row["request_ref"])["item_id"] == str(item["id"])
    else:
        assert row["request_ref"] == ""
