"""Project-notes tools — let the agent read/write the operator console's Notes
panel tabs, respecting each tab's per-tab ``agentRead`` / ``agentWrite``
permission toggles.

The Notes panel persists a workspace at ``<project>/.automaker/notes/workspace.json``
(see ``operator_api/notes.py``). Each tab carries
``permissions: {agentRead, agentWrite}`` — the operator decides which tabs the
agent may see or edit. These tools are the bridge that makes those toggles
mean something; without them the agent can't see the operator's notes at all
(it would otherwise confuse them with its private ``memory_*`` store).

The project defaults to the server's working directory (the repo root the Notes
panel shows by default); pass ``project_path`` to target another project.
"""

from __future__ import annotations

import os
import time

from langchain_core.tools import tool

from operator_api.notes import NotesService

_notes = NotesService()


def _project(project_path: str) -> str:
    return project_path.strip() or os.getcwd()


def _find_tab(workspace: dict, name: str):
    """Return (tab_id, tab) matching ``name`` (case-insensitive), or (None, None)."""
    target = name.strip().lower()
    for tab_id, tab in (workspace.get("tabs") or {}).items():
        if str(tab.get("name", "")).strip().lower() == target:
            return tab_id, tab
    return None, None


@tool
async def notes_list(project_path: str = "") -> str:
    """List the operator's Notes panel tabs and which ones the agent may
    read/write. Use this to discover the operator's notes (e.g. a "Todo" tab)
    before reading them. These are the human-curated notes in the console's
    Notes panel — distinct from your private ``memory_*`` store.

    Args:
        project_path: Project whose notes to list. Defaults to the current
            project (the repo root shown in the Notes panel).
    """
    ws = _notes.load_workspace(_project(project_path))
    tabs = ws.get("tabs") or {}
    if not tabs:
        return "No notes tabs exist for this project."
    order = ws.get("tabOrder") or list(tabs.keys())
    lines = [f"{len(tabs)} notes tab(s):"]
    for tid in order:
        tab = tabs.get(tid)
        if not tab:
            continue
        perms = tab.get("permissions") or {}
        flags = []
        flags.append("read" if perms.get("agentRead") else "no-read")
        flags.append("write" if perms.get("agentWrite") else "no-write")
        chars = len(str(tab.get("content") or ""))
        lines.append(f"- {tab.get('name', '(unnamed)')} [{', '.join(flags)}] — {chars} chars")
    return "\n".join(lines)


@tool
async def notes_read(tab: str = "", project_path: str = "") -> str:
    """Read the operator's Notes panel tab content (only tabs the operator has
    marked agent-readable). Use this when the operator asks what's in their
    notes / a specific tab (e.g. "what's on my Todo tab?").

    Args:
        tab: Tab name to read (case-insensitive). Leave blank to read every
            agent-readable tab.
        project_path: Project whose notes to read. Defaults to the current project.
    """
    ws = _notes.load_workspace(_project(project_path))
    tabs = ws.get("tabs") or {}

    if tab.strip():
        tid, found = _find_tab(ws, tab)
        if found is None:
            return f"No notes tab named {tab!r}. Use notes_list to see available tabs."
        if not (found.get("permissions") or {}).get("agentRead"):
            return f"The {found.get('name')!r} tab isn't shared with the agent (Agent read is off)."
        content = str(found.get("content") or "")
        return f"# {found.get('name')}\n\n{content or '(empty)'}"

    # No tab specified → all readable tabs.
    order = ws.get("tabOrder") or list(tabs.keys())
    readable = [
        tabs[tid] for tid in order
        if tabs.get(tid) and (tabs[tid].get("permissions") or {}).get("agentRead")
    ]
    if not readable:
        return "No notes tabs are shared with the agent (no tab has Agent read enabled)."
    blocks = [f"# {t.get('name')}\n\n{str(t.get('content') or '') or '(empty)'}" for t in readable]
    return "\n\n---\n\n".join(blocks)


@tool
async def notes_write(tab: str, content: str, mode: str = "append", project_path: str = "") -> str:
    """Write to an operator Notes panel tab (only tabs the operator has marked
    agent-writable). Use this to record something into the operator's notes
    (e.g. add an item to their Todo tab).

    Args:
        tab: Tab name to write (case-insensitive). Must already exist.
        content: Text to write.
        mode: "append" (default — add to the end on a new line) or "replace".
        project_path: Project whose notes to write. Defaults to the current project.
    """
    proj = _project(project_path)
    ws = _notes.load_workspace(proj)
    tid, found = _find_tab(ws, tab)
    if found is None:
        return f"No notes tab named {tab!r}. Use notes_list to see available tabs."
    if not (found.get("permissions") or {}).get("agentWrite"):
        return f"The {found.get('name')!r} tab is read-only for the agent (Agent write is off)."

    existing = str(found.get("content") or "")
    if mode == "replace":
        new_content = content
    else:
        new_content = f"{existing}\n{content}" if existing else content

    meta = found.setdefault("metadata", {})
    _push_history(meta, existing)  # snapshot the pre-write content for undo
    found["content"] = new_content
    meta["updatedAt"] = int(time.time() * 1000)
    meta["characterCount"] = len(new_content)
    meta["wordCount"] = len(new_content.split())
    ws["workspaceVersion"] = int(ws.get("workspaceVersion", 0)) + 1

    try:
        _notes.save_workspace(proj, ws)
    except Exception as e:  # noqa: BLE001 — surface a readable tool error
        return f"Error: could not save notes: {e}"
    return f"Updated the {found.get('name')!r} tab ({mode}). It now has {len(new_content)} chars."


@tool
async def notes_revert(tab: str, steps: int = 1, project_path: str = "") -> str:
    """Undo recent writes to a Notes tab, restoring an earlier version.

    Use when asked to undo/revert a change to a tab. Reverts ``steps`` versions
    back (default 1) from the per-tab history that ``notes_write`` records.

    Args:
        tab: Tab name (case-insensitive).
        steps: How many versions to roll back (default 1).
        project_path: Project whose notes to revert. Defaults to the current project.
    """
    proj = _project(project_path)
    ws = _notes.load_workspace(proj)
    _tid, found = _find_tab(ws, tab)
    if found is None:
        return f"No notes tab named {tab!r}. Use notes_list to see available tabs."
    if not (found.get("permissions") or {}).get("agentWrite"):
        return f"The {found.get('name')!r} tab is read-only for the agent (Agent write is off)."

    meta = found.setdefault("metadata", {})
    history = meta.get("history") or []
    if not history:
        return f"No earlier version of the {found.get('name')!r} tab to revert to."

    steps = max(1, min(steps, len(history)))
    restored = history[-steps]["content"]
    meta["history"] = history[:-steps]  # drop the versions we rolled past
    found["content"] = restored
    meta["updatedAt"] = int(time.time() * 1000)
    meta["characterCount"] = len(restored)
    meta["wordCount"] = len(restored.split())
    ws["workspaceVersion"] = int(ws.get("workspaceVersion", 0)) + 1

    try:
        _notes.save_workspace(proj, ws)
    except Exception as e:  # noqa: BLE001
        return f"Error: could not save notes: {e}"
    return f"Reverted the {found.get('name')!r} tab {steps} version(s) back ({len(restored)} chars)."


# Keep a short undo history per tab (newest last), capped.
_MAX_NOTE_HISTORY = 10


def _push_history(meta: dict, prev_content: str) -> None:
    history = meta.setdefault("history", [])
    history.append({"content": prev_content, "at": int(time.time() * 1000)})
    if len(history) > _MAX_NOTE_HISTORY:
        del history[: len(history) - _MAX_NOTE_HISTORY]


def get_notes_tools() -> list:
    """The project-notes tools, gated per-tab by the operator's read/write toggles."""
    return [notes_list, notes_read, notes_write, notes_revert]
