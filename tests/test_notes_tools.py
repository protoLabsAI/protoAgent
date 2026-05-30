"""Tests for the project-notes agent tools — per-tab read/write permission gating."""

from __future__ import annotations

import pytest

from operator_api.notes import NotesService
from tools.notes_tools import _MAX_NOTE_HISTORY, notes_list, notes_read, notes_revert, notes_write


def _seed(tmp_path):
    ws = {
        "version": 1,
        "workspaceVersion": 0,
        "activeTabId": "t1",
        "tabOrder": ["t1", "t2"],
        "tabs": {
            "t1": {"id": "t1", "name": "Todo", "content": "buy milk",
                   "permissions": {"agentRead": True, "agentWrite": True}, "metadata": {}},
            "t2": {"id": "t2", "name": "Private", "content": "the secret",
                   "permissions": {"agentRead": False, "agentWrite": False}, "metadata": {}},
        },
    }
    NotesService().save_workspace(str(tmp_path), ws)
    return str(tmp_path)


@pytest.mark.asyncio
async def test_list_shows_tabs_and_permission_flags(tmp_path):
    proj = _seed(tmp_path)
    out = await notes_list.ainvoke({"project_path": proj})
    assert "Todo [read, write]" in out
    assert "Private [no-read, no-write]" in out


@pytest.mark.asyncio
async def test_read_named_readable_tab(tmp_path):
    proj = _seed(tmp_path)
    out = await notes_read.ainvoke({"tab": "todo", "project_path": proj})  # case-insensitive
    assert "buy milk" in out


@pytest.mark.asyncio
async def test_read_blocked_when_agentRead_off(tmp_path):
    proj = _seed(tmp_path)
    out = await notes_read.ainvoke({"tab": "Private", "project_path": proj})
    assert "the secret" not in out
    assert "isn't shared" in out.lower() or "agent read is off" in out.lower()


@pytest.mark.asyncio
async def test_read_all_excludes_non_readable(tmp_path):
    proj = _seed(tmp_path)
    out = await notes_read.ainvoke({"project_path": proj})
    assert "buy milk" in out
    assert "the secret" not in out


@pytest.mark.asyncio
async def test_write_appends_to_writable_tab(tmp_path):
    proj = _seed(tmp_path)
    out = await notes_write.ainvoke({"tab": "Todo", "content": "call mom", "project_path": proj})
    assert "Updated" in out
    reloaded = NotesService().load_workspace(proj)
    assert reloaded["tabs"]["t1"]["content"] == "buy milk\ncall mom"
    assert reloaded["tabs"]["t1"]["metadata"]["characterCount"] == len("buy milk\ncall mom")


@pytest.mark.asyncio
async def test_write_blocked_when_agentWrite_off(tmp_path):
    proj = _seed(tmp_path)
    out = await notes_write.ainvoke({"tab": "Private", "content": "x", "project_path": proj})
    assert "read-only" in out.lower()
    # content untouched
    assert NotesService().load_workspace(proj)["tabs"]["t2"]["content"] == "the secret"


@pytest.mark.asyncio
async def test_write_unknown_tab_errors(tmp_path):
    proj = _seed(tmp_path)
    out = await notes_write.ainvoke({"tab": "Nope", "content": "x", "project_path": proj})
    assert "no notes tab named" in out.lower()


# ── version history + revert ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_write_records_prior_version_for_undo(tmp_path):
    proj = _seed(tmp_path)
    await notes_write.ainvoke({"tab": "Todo", "content": "call mom", "project_path": proj})
    ws = NotesService().load_workspace(proj)
    history = ws["tabs"]["t1"]["metadata"]["history"]
    assert history and history[-1]["content"] == "buy milk"  # pre-write snapshot


@pytest.mark.asyncio
async def test_history_is_capped(tmp_path):
    proj = _seed(tmp_path)
    for i in range(_MAX_NOTE_HISTORY + 5):
        await notes_write.ainvoke({"tab": "Todo", "content": f"item {i}", "project_path": proj})
    ws = NotesService().load_workspace(proj)
    assert len(ws["tabs"]["t1"]["metadata"]["history"]) == _MAX_NOTE_HISTORY


@pytest.mark.asyncio
async def test_revert_restores_previous_version(tmp_path):
    proj = _seed(tmp_path)
    await notes_write.ainvoke({"tab": "Todo", "content": "call mom", "project_path": proj})
    # content is now "buy milk\ncall mom"; revert → back to "buy milk"
    out = await notes_revert.ainvoke({"tab": "Todo", "project_path": proj})
    assert "Reverted" in out
    ws = NotesService().load_workspace(proj)
    assert ws["tabs"]["t1"]["content"] == "buy milk"
    assert ws["tabs"]["t1"]["metadata"]["history"] == []  # rolled-past version dropped


@pytest.mark.asyncio
async def test_revert_multiple_steps(tmp_path):
    proj = _seed(tmp_path)
    await notes_write.ainvoke({"tab": "Todo", "content": "a", "project_path": proj})  # hist: ["buy milk"]
    await notes_write.ainvoke({"tab": "Todo", "content": "b", "project_path": proj})  # hist: ["buy milk", "buy milk\na"]
    out = await notes_revert.ainvoke({"tab": "Todo", "steps": 2, "project_path": proj})
    assert "2 version" in out
    assert NotesService().load_workspace(proj)["tabs"]["t1"]["content"] == "buy milk"


@pytest.mark.asyncio
async def test_revert_with_no_history(tmp_path):
    proj = _seed(tmp_path)
    out = await notes_revert.ainvoke({"tab": "Todo", "project_path": proj})
    assert "No earlier version" in out


@pytest.mark.asyncio
async def test_revert_blocked_when_agentWrite_off(tmp_path):
    proj = _seed(tmp_path)
    out = await notes_revert.ainvoke({"tab": "Private", "project_path": proj})
    assert "read-only" in out.lower()
