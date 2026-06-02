"""Tests for the notes agent tools — per-tab read/write permission gating.

Notes are agent-global (one instance-scoped workspace, no project_path), so we
point the module-level ``_notes`` service at a tmp workspace per test.
"""

from __future__ import annotations

import pytest

from operator_api.notes import NotesService
from tools import notes_tools
from tools.notes_tools import _MAX_NOTE_HISTORY, notes_list, notes_read, notes_revert, notes_write


@pytest.fixture
def notes(tmp_path, monkeypatch):
    """A tmp-scoped notes service wired into the tools' module global."""
    service = NotesService(path=str(tmp_path / "notes" / "workspace.json"))
    monkeypatch.setattr(notes_tools, "_notes", service)
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
    service.save_workspace(ws)
    return service


@pytest.mark.asyncio
async def test_list_shows_tabs_and_permission_flags(notes):
    out = await notes_list.ainvoke({})
    assert "Todo [read, write]" in out
    assert "Private [no-read, no-write]" in out


@pytest.mark.asyncio
async def test_read_named_readable_tab(notes):
    out = await notes_read.ainvoke({"tab": "todo"})  # case-insensitive
    assert "buy milk" in out


@pytest.mark.asyncio
async def test_read_blocked_when_agentRead_off(notes):
    out = await notes_read.ainvoke({"tab": "Private"})
    assert "the secret" not in out
    assert "isn't shared" in out.lower() or "agent read is off" in out.lower()


@pytest.mark.asyncio
async def test_read_all_excludes_non_readable(notes):
    out = await notes_read.ainvoke({})
    assert "buy milk" in out
    assert "the secret" not in out


@pytest.mark.asyncio
async def test_write_appends_to_writable_tab(notes):
    out = await notes_write.ainvoke({"tab": "Todo", "content": "call mom"})
    assert "Updated" in out
    reloaded = notes.load_workspace()
    assert reloaded["tabs"]["t1"]["content"] == "buy milk\ncall mom"
    assert reloaded["tabs"]["t1"]["metadata"]["characterCount"] == len("buy milk\ncall mom")


@pytest.mark.asyncio
async def test_write_blocked_when_agentWrite_off(notes):
    out = await notes_write.ainvoke({"tab": "Private", "content": "x"})
    assert "read-only" in out.lower()
    # content untouched
    assert notes.load_workspace()["tabs"]["t2"]["content"] == "the secret"


@pytest.mark.asyncio
async def test_write_unknown_tab_errors(notes):
    out = await notes_write.ainvoke({"tab": "Nope", "content": "x"})
    assert "no notes tab named" in out.lower()


# ── version history + revert ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_write_records_prior_version_for_undo(notes):
    await notes_write.ainvoke({"tab": "Todo", "content": "call mom"})
    ws = notes.load_workspace()
    history = ws["tabs"]["t1"]["metadata"]["history"]
    assert history and history[-1]["content"] == "buy milk"  # pre-write snapshot


@pytest.mark.asyncio
async def test_history_is_capped(notes):
    for i in range(_MAX_NOTE_HISTORY + 5):
        await notes_write.ainvoke({"tab": "Todo", "content": f"item {i}"})
    ws = notes.load_workspace()
    assert len(ws["tabs"]["t1"]["metadata"]["history"]) == _MAX_NOTE_HISTORY


@pytest.mark.asyncio
async def test_revert_restores_previous_version(notes):
    await notes_write.ainvoke({"tab": "Todo", "content": "call mom"})
    # content is now "buy milk\ncall mom"; revert → back to "buy milk"
    out = await notes_revert.ainvoke({"tab": "Todo"})
    assert "Reverted" in out
    ws = notes.load_workspace()
    assert ws["tabs"]["t1"]["content"] == "buy milk"
    assert ws["tabs"]["t1"]["metadata"]["history"] == []  # rolled-past version dropped


@pytest.mark.asyncio
async def test_revert_multiple_steps(notes):
    await notes_write.ainvoke({"tab": "Todo", "content": "a"})  # hist: ["buy milk"]
    await notes_write.ainvoke({"tab": "Todo", "content": "b"})  # hist: ["buy milk", "buy milk\na"]
    out = await notes_revert.ainvoke({"tab": "Todo", "steps": 2})
    assert "2 version" in out
    assert notes.load_workspace()["tabs"]["t1"]["content"] == "buy milk"


@pytest.mark.asyncio
async def test_revert_with_no_history(notes):
    out = await notes_revert.ainvoke({"tab": "Todo"})
    assert "No earlier version" in out


@pytest.mark.asyncio
async def test_revert_blocked_when_agentWrite_off(notes):
    out = await notes_revert.ainvoke({"tab": "Private"})
    assert "read-only" in out.lower()
