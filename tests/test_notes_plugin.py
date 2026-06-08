"""Notes plugin (ADR 0034 S4) — the single-doc store + read/write/append tools."""
from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_notes():
    spec = importlib.util.spec_from_file_location(
        "notes_plugin_under_test", Path("plugins/notes/__init__.py")
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_notes_tools_roundtrip(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NOTES_DIR", str(tmp_path))
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    notes = _load_notes()

    # Empty to start.
    assert notes.read_note.invoke({}) == ""
    # write_note replaces.
    notes.write_note.invoke({"content": "hello"})
    assert notes.read_note.invoke({}) == "hello"
    # append_note adds on a new line.
    notes.append_note.invoke({"text": "world"})
    assert notes.read_note.invoke({}) == "hello\nworld\n"
    # write_note overwrites (no merge).
    notes.write_note.invoke({"content": "fresh"})
    assert notes.read_note.invoke({}) == "fresh"


def test_notes_storage_is_instance_scoped(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NOTES_DIR", str(tmp_path))
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "alpha")
    notes = _load_notes()
    notes.write_note.invoke({"content": "alpha-note"})
    assert (tmp_path / "alpha" / "note.md").read_text(encoding="utf-8") == "alpha-note"
