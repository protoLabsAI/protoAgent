"""The console's folder picker reads the SERVER's filesystem (operator_api/browse_routes).

A browser-native picker can't produce these paths — it describes the client's machine,
which is frequently not the machine being configured — so the server lists its own
directories and the client walks them. Read-only: names and kinds, never contents.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from operator_api.browse_routes import register_browse_routes


def _client():
    app = FastAPI()
    register_browse_routes(app)
    return TestClient(app)


def _tree(tmp_path: Path) -> Path:
    (tmp_path / "alpha").mkdir()
    (tmp_path / "Beta").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "notes.txt").write_text("x")
    return tmp_path


def test_lists_directories_only_by_default(tmp_path):
    _tree(tmp_path)
    body = _client().get("/api/fs/browse", params={"path": str(tmp_path)}).json()
    assert [e["name"] for e in body["entries"]] == ["alpha", "Beta"], "dirs only, case-insensitive sort"
    assert all(e["kind"] == "dir" for e in body["entries"])
    assert body["path"] == str(tmp_path.resolve())
    assert body["parent"] == str(tmp_path.resolve().parent)


def test_files_flag_includes_files_after_dirs(tmp_path):
    """File-valued settings (checkpoint.db_path) need to pick a file, so `files=true`
    lists them — but directories still sort first, since you navigate by folder."""
    _tree(tmp_path)
    body = _client().get("/api/fs/browse", params={"path": str(tmp_path), "files": "true"}).json()
    assert [e["name"] for e in body["entries"]] == ["alpha", "Beta", "notes.txt"]
    assert body["entries"][-1]["kind"] == "file"


def test_hidden_entries_are_opt_in(tmp_path):
    _tree(tmp_path)
    visible = _client().get("/api/fs/browse", params={"path": str(tmp_path)}).json()
    assert ".hidden" not in [e["name"] for e in visible["entries"]]
    shown = _client().get("/api/fs/browse", params={"path": str(tmp_path), "hidden": "true"}).json()
    assert ".hidden" in [e["name"] for e in shown["entries"]]


def test_entry_paths_are_absolute_and_usable(tmp_path):
    """The picker hands an entry's `path` straight back as the saved setting, so it
    must be absolute — a name alone would resolve against the server's cwd ("/" under
    the desktop sidecar) if it ever round-tripped."""
    _tree(tmp_path)
    body = _client().get("/api/fs/browse", params={"path": str(tmp_path)}).json()
    for entry in body["entries"]:
        assert Path(entry["path"]).is_absolute()
        assert Path(entry["path"]).is_dir()


def test_traversal_normalizes_rather_than_escaping(tmp_path):
    """`..` is resolved, not rejected — walking up is the whole point of a browser, and
    there's nothing to escape: this endpoint is deliberately unfenced (you're choosing
    what to fence). It grants nothing the operator couldn't already type by hand."""
    _tree(tmp_path)
    body = _client().get("/api/fs/browse", params={"path": f"{tmp_path}/alpha/.."}).json()
    assert body["path"] == str(tmp_path.resolve())


def test_missing_and_non_directory_paths_are_refused(tmp_path):
    _tree(tmp_path)
    c = _client()
    missing = c.get("/api/fs/browse", params={"path": str(tmp_path / "nope")})
    assert missing.status_code == 404 and "no such folder" in missing.json()["detail"]
    a_file = c.get("/api/fs/browse", params={"path": str(tmp_path / "notes.txt")})
    assert a_file.status_code == 400 and "not a folder" in a_file.json()["detail"]


def test_blank_path_starts_at_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _tree(tmp_path)
    body = _client().get("/api/fs/browse").json()
    assert body["path"] == str(tmp_path.resolve())


def test_roots_offer_jump_off_points_and_drop_dead_ones(tmp_path, monkeypatch):
    """Roots seed the picker's first open. A configured work folder that no longer
    exists must NOT be offered — the picker would hand back a path the fs fence then
    skips, which is the exact failure this whole feature exists to prevent."""
    import runtime.state as rs

    monkeypatch.setenv("HOME", str(tmp_path))
    live = tmp_path / "alpha"
    live.mkdir()
    monkeypatch.setattr(
        rs.STATE,
        "graph_config",
        type("C", (), {"filesystem_projects": [
            {"name": "live", "path": str(live)},
            {"name": "gone", "path": str(tmp_path / "vanished")},
        ]})(),
        raising=False,
    )
    roots = _client().get("/api/fs/browse", params={"path": str(tmp_path)}).json()["roots"]
    labels = {r["label"] for r in roots}
    assert "live" in labels and "gone" not in labels
    assert all(Path(r["path"]).is_dir() for r in roots)
    assert len({r["path"] for r in roots}) == len(roots), "deduped"


def test_listing_is_capped(tmp_path, monkeypatch):
    """A browse of a huge directory is a UI convenience, not a data dump."""
    import operator_api.browse_routes as br

    monkeypatch.setattr(br, "_MAX_ENTRIES", 5)
    for i in range(12):
        (tmp_path / f"d{i:02d}").mkdir()
    body = _client().get("/api/fs/browse", params={"path": str(tmp_path)}).json()
    assert len(body["entries"]) == 5


def test_truncation_is_reported_not_swallowed(tmp_path, monkeypatch):
    """A silently truncated listing reads as "that's everything" — which is how you
    conclude a folder isn't there when it is. The flag lets the picker say so."""
    import operator_api.browse_routes as br

    monkeypatch.setattr(br, "_MAX_ENTRIES", 3)
    for i in range(5):
        (tmp_path / f"d{i}").mkdir()
    body = _client().get("/api/fs/browse", params={"path": str(tmp_path)}).json()
    assert body["truncated"] is True and len(body["entries"]) == 3

    (tmp_path / "solo").mkdir()
    small = _client().get("/api/fs/browse", params={"path": str(tmp_path / "solo")}).json()
    assert small["truncated"] is False
