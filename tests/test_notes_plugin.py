"""Notes plugin (ADR 0034 S4) — the single-doc store + read/write/append tools."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_notes():
    spec = importlib.util.spec_from_file_location("notes_plugin_under_test", Path("plugins/notes/__init__.py"))
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


def test_editor_view_follows_the_four_rules() -> None:
    """The served editor page must stay four-rules compliant
    (docs/how-to/build-a-plugin-view.md): kit css+js linked slug-aware (rule 4),
    data fetched via the kit's authed apiFetch (rules 2+3), and no hand-rolled
    theme map (the pre-kit page carried a hex :root token block that ignored
    the operator's theme)."""
    html = _load_notes()._EDITOR_HTML
    # Rule 4 — the same-origin DS kit, base-prefixed by hand (loads before the kit exists).
    assert "/_ds/plugin-kit.css" in html
    assert "/_ds/plugin-kit.js" in html
    assert 'location.pathname.split("/plugins/")[0]' in html  # slug-aware base
    # Rules 2+3 — gated data path via the kit's slug-aware authed fetch.
    assert 'apiFetch("/api/plugins/notes/note"' in html
    assert "initPluginView" in html
    # The kit is an ES MODULE — it must load via dynamic import from a module
    # script, never a classic <script src> (SyntaxError: Unexpected token 'export').
    assert 'type="module"' in html
    assert 'import(window.__base + "/_ds/plugin-kit.js")' in html
    # No hand-rolled theme: hex colors and a bespoke handshake listener are the
    # antipattern the kit replaces.
    assert "#0a0a0c" not in html
    assert 'addEventListener("message"' not in html


def test_quick_palette_view_follows_the_four_rules() -> None:
    """The compact palette page (ADR 0057 — `palette: { path: /quick }`) must stay
    four-rules compliant too: it's the same sandboxed-iframe contract as the editor."""
    html = _load_notes()._QUICK_HTML
    assert "/_ds/plugin-kit.css" in html
    assert "/_ds/plugin-kit.js" in html
    assert 'location.pathname.split("/plugins/")[0]' in html  # slug-aware base
    assert 'apiFetch("/api/plugins/notes/note"' in html  # gated data via the kit
    assert "initPluginView" in html
    assert 'type="module"' in html
    assert 'import(window.__base + "/_ds/plugin-kit.js")' in html  # ESM dynamic import
    assert "#0a0a0c" not in html
    assert 'addEventListener("message"' not in html
