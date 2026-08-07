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


def test_editor_renders_markdown_inline_with_no_preview_toggle() -> None:
    """The editor renders as you type — no edit↔preview mode to flip between."""
    html = _load_notes()._EDITOR_HTML
    assert 'id="toggle"' not in html and ">Preview<" not in html
    assert "livePreview" in html  # the marker-hiding ViewPlugin
    # Moving the caret is what reveals/re-hides markers, so selection changes MUST
    # rebuild decorations — on docChanged alone the markers never come back.
    assert "u.selectionSet" in html


def test_editor_is_offline_and_the_marked_cdn_is_gone() -> None:
    """`network: []` in the manifest was a lie while the preview pulled marked off
    cdnjs. Every asset the page loads is now same-origin."""
    html = _load_notes()._EDITOR_HTML
    assert "cdnjs" not in html and "marked" not in html
    assert "http://" not in html and "https://" not in html  # nothing loads off-box
    assert '<script type="importmap">' in html


def test_import_map_covers_the_whole_vendored_closure() -> None:
    """Every bare specifier the vendored bundles emit must be in the import map, and
    each must map to exactly ONE file: CM6 compares Facets/EditorState by reference, so
    a duplicate @codemirror/state makes markdown()'s extensions unrecognizable to the
    other copy's EditorView. A missing entry is a bare-specifier resolution error."""
    import json
    import re
    from pathlib import Path

    notes = _load_notes()
    raw = re.search(r'<script type="importmap">\s*(\{.*?\})\s*</script>', notes._EDITOR_HTML, re.S)
    assert raw, "no import map in the editor page"
    imports = json.loads(raw.group(1))["imports"]

    vendor = Path("plugins/notes/vendor")
    # Every mapped target is allowlisted AND actually on disk (a typo here is a 404 at
    # runtime, which the static four-rules checks would never catch).
    for spec, target in imports.items():
        name = target.rsplit("/", 1)[-1]
        assert name in notes._VENDOR_FILES, f"{spec} → {name} is not allowlisted"
        assert (vendor / name).is_file(), f"{spec} → {name} is not vendored"
    # One package, one file — no package resolves to two different modules.
    assert len(set(imports.values())) == len(imports)

    # And the closure is CLOSED: nothing any bundle imports is left unmapped. This
    # catches BOTH bare specifiers ("@codemirror/state") and esm.sh's absolute-path
    # polyfill injections ("/node/process.mjs") — the latter is not hypothetical:
    # @lezer/lr's bundle imports it for `process.env.LOG`, and an unmapped import 404s,
    # aborts the module graph, and leaves the editor as a silent empty box with nothing
    # thrown on the page. Anything unresolvable must fail HERE, not in a browser.
    needed = set()
    for f in vendor.glob("*.mjs"):
        src = f.read_text(encoding="utf-8")
        for pat in (r'from\s*"([@/][^"]+)"', r'import\s*"([@/][^"]+)"', r'import\s+\S+\s+from\s*"([@/][^"]+)"'):
            needed |= set(re.findall(pat, src))
    # Relative specifiers resolve on their own; only bare/absolute ones need mapping.
    needed = {s for s in needed if not s.startswith(".")}
    assert needed <= set(imports), f"unmapped specifiers: {sorted(needed - set(imports))}"


def test_editor_css_outranks_codemirrors_base_theme() -> None:
    """CM injects `.ͼ1 .cm-scroller{font-family:monospace}` — a GENERATED class, so
    two-class specificity. A bare `.cm-scroller` rule (one class) loses to it wherever
    our stylesheet sits, and the editor renders monospace with the DS font silently
    ignored. Our rules must stay scoped under `.cm-editor` to match it."""
    html = _load_notes()._EDITOR_HTML
    css = html.split("<style>")[1].split("</style>")[0]
    for hook in (".cm-scroller", ".cm-content", ".cm-md-h1"):
        assert f".cm-editor {hook}" in css, f"{hook} must be scoped under .cm-editor"


def test_vendor_route_is_allowlisted_against_path_traversal(tmp_path, monkeypatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setenv("NOTES_DIR", str(tmp_path))
    notes = _load_notes()
    app = FastAPI()
    app.include_router(notes._build_view_router(), prefix="/plugins/notes")
    c = TestClient(app)

    r = c.get("/plugins/notes/vendor/state.mjs")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/javascript")

    # The allowlist IS the traversal defence — the route joins the name onto vendor/.
    for bad in ("../__init__.py", "../../../etc/passwd", "nope.mjs"):
        assert c.get(f"/plugins/notes/vendor/{bad}").status_code == 404


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


# ── History (#2022) ──────────────────────────────────────────────────────────
#
# The load-bearing test in here is `test_an_editing_burst_collapses_to_one_version`.
# The obvious implementation — archive on every write — is silently destructive with
# this plugin's 700ms autosave: a minute of typing mints ~85 versions and evicts the
# note's real history from any cap. If that test is ever "simplified" away, the feature
# is back to shredding the thing it exists to protect.


def _hist(notes):
    return sorted(p.name for p in notes._history_dir().glob("*.md"))


def _age_history(notes, seconds: int) -> None:
    """Rewrite each snapshot's filename stamp to `seconds` ago, so the coalescing window
    can be tested without sleeping. Exercises _parse_id's real format as a side effect."""
    from datetime import datetime, timedelta, timezone

    for p in sorted(notes._history_dir().glob("*.md")):
        _, _, by = notes._parse_id(p)
        old = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        stamp = old.strftime("%Y%m%dT%H%M%S.%fZ")
        p.rename(p.with_name(f"{stamp}-{by}-{p.stem.split('-')[-1]}.md"))


def test_write_note_archives_the_outgoing_text(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NOTES_DIR", str(tmp_path))
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    notes = _load_notes()

    notes.write_note.invoke({"content": "first"})
    assert _hist(notes) == []  # nothing to lose yet — history isn't seeded with the empty note

    _age_history(notes, 9999)
    notes.write_note.invoke({"content": "second"})
    versions = notes._list_versions()
    assert len(versions) == 1
    assert notes._read_version(versions[0]["id"]) == "first"  # the OUTGOING text
    assert notes.read_note.invoke({}) == "second"


def test_an_editing_burst_collapses_to_one_version(tmp_path, monkeypatch) -> None:
    """The autosave case. Successive same-author writes inside the window share a version,
    and the one kept is the state BEFORE the burst — that's what you'd want back."""
    monkeypatch.setenv("NOTES_DIR", str(tmp_path))
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    notes = _load_notes()

    notes.write_note.invoke({"content": "ORIGINAL"})
    _age_history(notes, 9999)
    for text in ("typing a", "typing ab", "typing abc", "typing abcd"):
        notes.write_note.invoke({"content": text})

    versions = notes._list_versions()
    assert len(versions) == 1, f"autosave burst should collapse, got {len(versions)}"
    assert notes._read_version(versions[0]["id"]) == "ORIGINAL"
    assert notes.read_note.invoke({}) == "typing abcd"


def test_the_window_expiring_cuts_a_new_version(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NOTES_DIR", str(tmp_path))
    monkeypatch.setenv("NOTES_COALESCE_SECONDS", "300")
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    notes = _load_notes()

    notes.write_note.invoke({"content": "one"})
    notes.write_note.invoke({"content": "two"})  # archives "one"
    assert len(notes._list_versions()) == 1
    _age_history(notes, 600)  # window passed
    notes.write_note.invoke({"content": "three"})  # archives "two" as its own version
    assert len(notes._list_versions()) == 2


def test_a_change_of_author_always_cuts_a_version(tmp_path, monkeypatch) -> None:
    """Agent-clobbers-operator is the case most worth undoing, so it must survive the
    coalescing window rather than be swallowed by it."""
    monkeypatch.setenv("NOTES_DIR", str(tmp_path))
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    notes = _load_notes()
    c = _client(notes)

    notes.write_note.invoke({"content": "agent one"})
    notes.write_note.invoke({"content": "agent two"})  # archives "agent one" (by agent)
    # Operator saves immediately after — same window, different author.
    c.put("/api/plugins/notes/note", json={"content": "operator edit"})

    versions = notes._list_versions()
    assert [v["by"] for v in versions] == ["operator", "agent"]  # newest first
    assert notes._read_version(versions[0]["id"]) == "agent two"  # the clobbered text


def test_coalesce_seconds_zero_archives_every_write(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NOTES_DIR", str(tmp_path))
    monkeypatch.setenv("NOTES_COALESCE_SECONDS", "0")
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    notes = _load_notes()

    for text in ("a", "b", "c", "d"):
        notes.write_note.invoke({"content": text})
    assert len(notes._list_versions()) == 3  # a, b, c archived; d is current


def test_an_identical_rewrite_does_not_pile_up(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NOTES_DIR", str(tmp_path))
    monkeypatch.setenv("NOTES_COALESCE_SECONDS", "0")
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    notes = _load_notes()

    notes.write_note.invoke({"content": "same"})
    notes.write_note.invoke({"content": "same"})
    notes.write_note.invoke({"content": "same"})
    assert len(notes._list_versions()) <= 1


def test_retention_prunes_the_oldest(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NOTES_DIR", str(tmp_path))
    monkeypatch.setenv("NOTES_COALESCE_SECONDS", "0")
    monkeypatch.setenv("NOTES_MAX_VERSIONS", "3")
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    notes = _load_notes()

    for i in range(8):
        notes.write_note.invoke({"content": f"v{i}"})
    versions = notes._list_versions()
    assert len(versions) == 3
    # Newest-first, and it's the OLDEST that got dropped.
    assert [notes._read_version(v["id"]) for v in versions] == ["v6", "v5", "v4"]


def test_read_note_can_target_a_version(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NOTES_DIR", str(tmp_path))
    monkeypatch.setenv("NOTES_COALESCE_SECONDS", "0")
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    notes = _load_notes()

    notes.write_note.invoke({"content": "old text"})
    notes.write_note.invoke({"content": "new text"})
    vid = notes._list_versions()[0]["id"]
    assert notes.read_note.invoke({"version": vid}) == "old text"
    assert notes.read_note.invoke({}) == "new text"
    # An unknown id explains itself instead of returning "" (which reads as an empty note).
    assert "No such note version" in notes.read_note.invoke({"version": "nope"})


def test_a_version_id_cannot_escape_the_history_dir(tmp_path, monkeypatch) -> None:
    """The id comes from agent/operator input and is matched against known filenames,
    never joined onto a path."""
    monkeypatch.setenv("NOTES_DIR", str(tmp_path))
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    notes = _load_notes()
    (tmp_path / "secret.md").write_text("SECRET", encoding="utf-8")

    for attempt in ("../secret", "../../secret", "/etc/passwd", "../secret.md"):
        assert notes._read_version(attempt) is None
        assert "No such note version" in notes.read_note.invoke({"version": attempt})


def test_restore_is_itself_undoable(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NOTES_DIR", str(tmp_path))
    monkeypatch.setenv("NOTES_COALESCE_SECONDS", "0")
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    notes = _load_notes()

    notes.write_note.invoke({"content": "wanted"})
    notes.write_note.invoke({"content": "clobbered it"})
    vid = notes._list_versions()[0]["id"]

    notes.restore_note_version.invoke({"version": vid})
    assert notes.read_note.invoke({}) == "wanted"
    # The text the restore replaced is archived too — nothing is lost by recovering.
    assert any(notes._read_version(v["id"]) == "clobbered it" for v in notes._list_versions())


def test_diff_compares_a_version_with_the_current_note(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NOTES_DIR", str(tmp_path))
    monkeypatch.setenv("NOTES_COALESCE_SECONDS", "0")
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    notes = _load_notes()

    notes.write_note.invoke({"content": "line one\nline two"})
    notes.write_note.invoke({"content": "line one\nline TWO"})
    vid = notes._list_versions()[0]["id"]

    out = notes.diff_note_version.invoke({"version": vid})
    assert "-line two" in out and "+line TWO" in out


def test_history_routes_list_read_and_restore(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NOTES_DIR", str(tmp_path))
    monkeypatch.setenv("NOTES_COALESCE_SECONDS", "0")
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    notes = _load_notes()
    c = _client(notes)

    notes.write_note.invoke({"content": "original"})
    notes.write_note.invoke({"content": "replaced"})

    listed = c.get("/api/plugins/notes/versions").json()["versions"]
    assert len(listed) == 1 and listed[0]["snippet"] == "original"

    got = c.get(f"/api/plugins/notes/versions/{listed[0]['id']}").json()
    assert got["content"] == "original" and "-original" in got["diff"]

    assert c.get("/api/plugins/notes/versions/nope").status_code == 404
    assert c.post("/api/plugins/notes/versions/nope/restore").status_code == 404

    r = c.post(f"/api/plugins/notes/versions/{listed[0]['id']}/restore")
    assert r.status_code == 200 and r.json()["content"] == "original"
    assert notes.read_note.invoke({}) == "original"


def test_a_broken_history_dir_never_blocks_a_save(tmp_path, monkeypatch) -> None:
    """History is best-effort: losing it is bad, but failing the write would be worse."""
    monkeypatch.setenv("NOTES_DIR", str(tmp_path))
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    notes = _load_notes()
    notes.write_note.invoke({"content": "before"})

    def _boom():
        raise OSError("history dir unavailable")

    monkeypatch.setattr(notes, "_history_dir", _boom)
    notes.write_note.invoke({"content": "after"})  # must not raise
    assert notes.read_note.invoke({}) == "after"


def test_history_ui_is_wired_and_four_rules_compliant() -> None:
    """The history drawer must reach its data the same slug-aware, authed way as the
    rest of the page — a bare fetch() here would 401 on a gated instance and hit the
    HOST rather than the member through the fleet proxy."""
    html = _load_notes()._EDITOR_HTML
    assert 'id="histbtn"' in html and 'id="histwrap"' in html
    assert 'apiFetch("/api/plugins/notes/versions")' in html
    assert 'apiFetch("/api/plugins/notes/versions/"' in html
    # Version ids are interpolated into a URL and into markup — encode + escape both.
    assert "encodeURIComponent(" in html
    assert "function esc(" in html
    # Still no hand-rolled colour: the drawer and the diff are --pl-* tokens only.
    assert "#histwrap{position:absolute" in html
    for hexish in ("#0a0a0c", "#fff;", "#000;"):
        assert hexish not in html
