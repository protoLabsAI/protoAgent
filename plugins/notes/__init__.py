"""Notes plugin (ADR 0034 S4) — the first-class React reference plugin.

A single shared markdown note that BOTH the agent (via tools) and the operator
(via the `ui: react` console panel) read and write. The plugin owns its whole
vertical: storage, agent tools, and the UI's data route. No tabs, no undo, no
versioning — deliberately the basic notebook we actually want.
"""

from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.tools import tool

log = logging.getLogger("protoagent.plugins.notes")


def _note_path() -> Path:
    """The single note file, instance-scoped (ADR 0004). ``NOTES_DIR`` overrides
    the base; ``PROTOAGENT_INSTANCE`` adds a per-instance subdir."""
    base = Path(os.environ.get("NOTES_DIR") or (Path.home() / ".protoagent" / "notes"))
    inst = os.environ.get("PROTOAGENT_INSTANCE", "").strip()
    if inst:
        base = base / inst
    base.mkdir(parents=True, exist_ok=True)
    return base / "note.md"


def _read() -> str:
    try:
        return _note_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _write(content: str) -> None:
    """Atomic write so a crash mid-save never truncates the note."""
    path = _note_path()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _updated_at() -> str | None:
    try:
        ts = _note_path().stat().st_mtime
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except FileNotFoundError:
        return None


@tool
def read_note() -> str:
    """Read the shared notes document (markdown). Use it to recall what you or the
    operator have written. Returns the full note text (empty string if blank)."""
    return _read()


@tool
def write_note(content: str) -> str:
    """Replace the entire shared notes document with ``content`` (markdown). This
    OVERWRITES the note — read_note first if you mean to keep the existing text."""
    _write(content)
    return f"Note saved ({len(content)} chars)."


@tool
def append_note(text: str) -> str:
    """Append ``text`` to the shared notes document (markdown), on a new line.
    Use this to add an entry without disturbing what's already there."""
    cur = _read()
    sep = "" if (not cur or cur.endswith("\n")) else "\n"
    _write(f"{cur}{sep}{text}\n")
    return f"Appended {len(text)} chars to the note."


def _build_router():
    """The UI's data route — mounted under ``/api/plugins/notes`` so it inherits the
    operator bearer gate (P0 auth). The React panel reads/writes the note here."""
    from fastapi import APIRouter, Body

    router = APIRouter()

    @router.get("/note")
    async def _get() -> dict:
        return {"content": _read(), "updated_at": _updated_at()}

    @router.put("/note")
    async def _put(body: dict = Body(...)) -> dict:
        _write(str(body.get("content", "")))
        return {"ok": True, "updated_at": _updated_at()}

    return router


def register(registry) -> None:
    """Entry point — called once at load with a PluginRegistry."""
    registry.register_tools([read_note, write_note, append_note])
    # Mounted at /api/plugins/notes (gated) — not the default /plugins/notes.
    registry.register_router(_build_router(), prefix="/api/plugins/notes")
