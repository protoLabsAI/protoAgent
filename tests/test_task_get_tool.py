"""Read the original scope of agent-created tasks without changing them."""

from types import SimpleNamespace

import pytest

from graph.config import LangGraphConfig
from runtime.acp_runtime import persona_doc
from runtime.operator_mcp_tools import operator_tools
from runtime.state import STATE
from tasks.store import TaskStore
from tools.scheduler_tools import _build_task_tools


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("TASKS_DB_PATH", raising=False)
    monkeypatch.delenv("BEADS_DB_PATH", raising=False)
    tasks = TaskStore(db_path=str(tmp_path / "tasks.db"))
    yield tasks
    tasks._conn.close()


def test_get_created_task_scope_and_fresh_metadata_without_mutation(store):
    tools = {t.name: t for t in _build_task_tools(store)}
    assert list(tools) == ["task_create", "task_list", "task_get", "task_update", "task_close"]
    tools["task_create"].invoke({
        "title": "Audit stale tasks", "description": "Read original scope.\nClose completed work.",
        "priority": 1, "issue_type": "chore", "links": ["bd-done", "bd-pending"],
        "state": {"session_id": "chat-audit"},
    })
    tools["task_update"].invoke({"issue_id": "task-1", "status": "waiting", "ask": "Approve closure?"})
    store.resolve_link("bd-done", "merged")
    before = store.get("task-1")
    assert tools["task_get"].invoke({"issue_id": "task-1"}) == (
        "Title: Audit stale tasks\nId: task-1\nStatus: waiting\nPriority: 1\nIssue type: chore\n"
        "Description: Read original scope.\nClose completed work.\nAsk: Approve closure?\n"
        "Links: bd-done✓, bd-pending\nOrigin session id: chat-audit\n"
        f"Created: {before['created_at']}\nUpdated: {before['updated_at']}"
    )
    assert store.get("task-1") == before
    assert tools["task_list"].invoke({}) == (
        "[waiting] task-1 (p1, chore) Audit stale tasks  links=bd-done✓,bd-pending"
    )
    tools["task_close"].invoke({"issue_id": "task-1"})
    assert "Status: closed" in tools["task_get"].invoke({"issue_id": "task-1"})


@pytest.mark.parametrize(("issue_id", "expected"), [
    ("", "Error: issue_id is required."),
    ("  ", "Error: issue_id is required."),
    ("task-999", "Error: no task task-999."),
])
def test_get_missing_id(store, issue_id, expected):
    get = next(t for t in _build_task_tools(store) if t.name == "task_get")
    assert get.invoke({"issue_id": issue_id}) == expected


def test_get_legacy_row_without_optional_metadata():
    row = {"id": "task-old", "title": "Legacy", "status": "open", "priority": 2, "issue_type": "task"}
    get = next(t for t in _build_task_tools(SimpleNamespace(get=lambda _: row)) if t.name == "task_get")
    assert get.invoke({"issue_id": "task-old"}) == (
        "Title: Legacy\nId: task-old\nStatus: open\nPriority: 2\nIssue type: task\nDescription: "
    )


def test_read_only_mcp_exposes_get_and_acp_names_it(store, monkeypatch):
    monkeypatch.delenv("PROTOAGENT_MCP_TRUST", raising=False)
    monkeypatch.setattr(STATE, "tasks_store", store)
    config = LangGraphConfig(operator_mcp_profile="read-only")
    exposed = {t.name: t for t in operator_tools(config)}
    assert {"task_list", "task_get"} <= exposed.keys()
    assert exposed["task_get"].invoke({"issue_id": "task-999"}) == "Error: no task task-999."
    assert "`task_get`" in persona_doc(config, exposed=set(exposed))
