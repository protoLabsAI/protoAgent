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


def _client(notes):
    """A TestClient over just the DATA router (the gate is the host's job, not ours)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(notes._build_data_router(), prefix="/api/plugins/notes")
    return TestClient(app)


def test_get_note_exposes_a_version_token(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NOTES_DIR", str(tmp_path))
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    notes = _load_notes()
    notes.write_note.invoke({"content": "hello"})

    body = _client(notes).get("/api/plugins/notes/note").json()
    assert body["content"] == "hello"
    # Opaque + content-derived: same content ⇒ same token, no clock/mtime dependence.
    assert body["version"] == notes._version("hello")


def test_put_with_current_version_saves(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NOTES_DIR", str(tmp_path))
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    notes = _load_notes()
    c = _client(notes)
    notes.write_note.invoke({"content": "hello"})

    r = c.put("/api/plugins/notes/note", json={"content": "hello there", "base_version": notes._version("hello")})
    assert r.status_code == 200
    assert notes.read_note.invoke({}) == "hello there"
    # The response hands back the NEW version so the editor can keep saving without a re-GET.
    assert r.json()["version"] == notes._version("hello there")


def test_put_with_stale_version_conflicts_and_preserves_the_agents_write(tmp_path, monkeypatch) -> None:
    """The bug this guards: the operator is typing (so the editor's poll is suppressed),
    the agent's write_note lands, and the operator's next autosave clobbers it."""
    monkeypatch.setenv("NOTES_DIR", str(tmp_path))
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    notes = _load_notes()
    c = _client(notes)
    notes.write_note.invoke({"content": "hello"})
    stale = notes._version("hello")

    # The agent writes while the operator has the editor open.
    notes.write_note.invoke({"content": "agent findings"})

    r = c.put("/api/plugins/notes/note", json={"content": "operator edit", "base_version": stale})
    assert r.status_code == 409
    body = r.json()
    assert body["conflict"] is True
    # The agent's write survives on disk, and the 409 carries it back so the editor
    # can offer "Take theirs" without a second round-trip.
    assert notes.read_note.invoke({}) == "agent findings"
    assert body["content"] == "agent findings"
    assert body["version"] == notes._version("agent findings")


def test_put_without_base_version_force_overwrites(tmp_path, monkeypatch) -> None:
    """base_version is OPTIONAL — absence means force. Keeps write_note (a documented
    full overwrite) and any pre-guard client working unchanged."""
    monkeypatch.setenv("NOTES_DIR", str(tmp_path))
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    notes = _load_notes()
    c = _client(notes)
    notes.write_note.invoke({"content": "hello"})

    r = c.put("/api/plugins/notes/note", json={"content": "forced"})
    assert r.status_code == 200
    assert notes.read_note.invoke({}) == "forced"


def test_idempotent_rewrite_does_not_manufacture_a_conflict(tmp_path, monkeypatch) -> None:
    """Why the token is a content hash and not mtime: writing the same bytes twice
    leaves the version unchanged, so a re-save can't 409 against itself."""
    monkeypatch.setenv("NOTES_DIR", str(tmp_path))
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    notes = _load_notes()
    c = _client(notes)
    notes.write_note.invoke({"content": "same"})
    notes.write_note.invoke({"content": "same"})

    r = c.put("/api/plugins/notes/note", json={"content": "next", "base_version": notes._version("same")})
    assert r.status_code == 200


def test_views_send_base_version_and_never_fall_back_to_a_tokenless_fetch() -> None:
    """The kit fallback used to fetch WITHOUT the bearer — silently 401ing every save
    on a gated instance. A kit that won't load is a broken editor, not a degraded one."""
    notes = _load_notes()
    for html in (notes._EDITOR_HTML, notes._QUICK_HTML):
        assert "base_version" in html  # the guard is actually wired from the client
        assert "fetch(window.__base" not in html  # no tokenless shim
        # A failed initial GET leaves us with no base_version, which the server reads
        # as force-overwrite — so a view that never loaded must not save at all.
        assert "if(!loaded)" in html


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
