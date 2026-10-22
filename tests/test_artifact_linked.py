"""Tests for LINKED file artifacts — save_file_artifact(link=True) (ADR 0092 D2 extension, #4370).

A linked version stores NO blob: only a fenced (project, project-relative path) reference plus a
size/mtime/sha256 stamp. The file must live inside a registered filesystem project (it's served
in place from there); a path outside every project, or a symlink escaping one, is refused. Linked
artifacts are read-only — no artifact_id, no revisions.

Loaded the same way as test_artifact_plugin.py (as a PACKAGE, so relative imports resolve), with a
registry substituted over temp dirs."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.platform_sensitive

ROOT = Path(__file__).resolve().parent.parent / "plugins" / "artifact"


def _load(monkeypatch, tmp_path):
    """Fresh package bound to an isolated ARTIFACT_DIR (see test_artifact_plugin._load)."""
    monkeypatch.setenv("ARTIFACT_DIR", str(tmp_path))
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    for k in [k for k in sys.modules if k.startswith("artifact_under_test")]:
        del sys.modules[k]
    spec = importlib.util.spec_from_file_location(
        "artifact_under_test", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["artifact_under_test"] = mod
    spec.loader.exec_module(mod)
    mod._render_status._RENDER_WAIT_MS = 0
    return mod


def _arts(art):
    return art._read_store()["artifacts"]


def _fence(monkeypatch, art, **roots):
    """Substitute the fs fence with a registry over the given temp roots (matches the helper in
    test_artifact_plugin.py)."""
    from tools.fs_tools import Project, ProjectRegistry

    reg = ProjectRegistry([Project(name=n, root=r.resolve()) for n, r in roots.items()])
    monkeypatch.setattr(art._links, "_registry", lambda: reg)


# ── r1: a 31 MB PDF links at the default blob cap, stores a reference, writes no blob ────────


def test_link_true_stores_a_reference_and_no_blob(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)  # default max_blob_kb (25 MB)
    ws = tmp_path / "ws"
    (ws / "docs").mkdir(parents=True)
    f = ws / "docs" / "big.pdf"
    payload = b"%PDF-1.4\n" + b"0" * (31 * 1024 * 1024)  # 31 MB — over the 25 MB blob cap
    f.write_bytes(payload)
    _fence(monkeypatch, art, workspace=ws)

    out = art.save_file_artifact.invoke({"path": "docs/big.pdf", "link": True, "title": "Source"})
    assert "Linked file artifact" in out

    a = _arts(art)[0]
    assert a["kind"] == "file" and a["linked"] is True and a["title"] == "Source"
    v = a["versions"][0]
    # a reference, not a copy: no sidecar blob token on the version
    assert "blob" not in v
    link = v["link"]
    assert link["project"] == "workspace"
    assert link["path"] == "docs/big.pdf" and not Path(link["path"]).is_absolute()
    assert link["size"] == len(payload)
    assert link["sha256"] == hashlib.sha256(payload).hexdigest()
    assert isinstance(link["mtime_ns"], int)
    # the usual `file` meta + the pdf preflight are built just like a copied save
    assert v["file"]["filename"] == "big.pdf" and v["file"]["size"] == len(payload)
    assert "pdf" in v["file"]
    # NO bytes were written under the store's blobs/ dir — the dir was never even created
    assert not art._blob_root().exists()
    assert not (tmp_path / "blobs").exists()


def test_link_true_from_an_absolute_path_inside_a_project(monkeypatch, tmp_path):
    """An absolute path with no `project` still links, with the containing project recovered and
    the stored path made project-relative (and posix)."""
    art = _load(monkeypatch, tmp_path)
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    f = ws / "sub" / "data.csv"
    f.write_text("a,b\n1,2\n", encoding="utf-8")
    _fence(monkeypatch, art, workspace=ws)

    out = art.save_file_artifact.invoke({"path": str(f), "link": True})
    assert "Linked file artifact" in out
    link = _arts(art)[0]["versions"][0]["link"]
    assert link["project"] == "workspace" and link["path"] == "sub/data.csv"


# ── r2: a file outside every project, or escaping one via a symlink, is refused ───────────────


def test_link_true_absolute_path_outside_every_project_is_refused(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("free", encoding="utf-8")
    _fence(monkeypatch, art, workspace=ws)

    out = art.save_file_artifact.invoke({"path": str(outside), "link": True})
    assert "outside every project" in out
    assert _arts(art) == []


def test_link_true_in_project_symlink_escape_is_refused(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("nope", encoding="utf-8")
    try:
        (ws / "link.txt").symlink_to(secret)
        (ws / "dirlink").symlink_to(tmp_path, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    _fence(monkeypatch, art, workspace=ws)

    # with `project` given, the existing fence (_project_relative) refuses the escape
    for p in (ws / "link.txt", ws / "dirlink" / "secret.txt"):
        out = art.save_file_artifact.invoke({"path": str(p), "project": "workspace", "link": True})
        assert "outside project" in out, out
    # and with no `project`, _project_for refuses it just the same (the resolved target is
    # outside every root)
    out = art.save_file_artifact.invoke({"path": str(ws / "link.txt"), "link": True})
    assert "outside every project" in out, out
    assert _arts(art) == []


# ── r3: a file over max_linked_kb is refused with a message naming the setting ────────────────


def test_link_true_over_max_linked_kb_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("ARTIFACT_MAX_LINKED_KB", "1")  # 1 KB cap
    art = _load(monkeypatch, tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "big.bin").write_bytes(b"x" * 4096)  # 4 KB > 1 KB
    _fence(monkeypatch, art, workspace=ws)

    out = art.save_file_artifact.invoke({"path": "big.bin", "link": True})
    assert "too large to link" in out.lower() and "max_linked_kb" in out
    assert _arts(art) == []


# ── r4: a linked artifact is read-only — no artifact_id with link, no revisions ───────────────


def test_link_true_with_artifact_id_is_refused(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("a", encoding="utf-8")
    _fence(monkeypatch, art, workspace=ws)

    out = art.save_file_artifact.invoke({"path": "a.txt", "artifact_id": "a-123", "link": True})
    assert "read-only" in out and "artifact_id" in out
    assert _arts(art) == []  # refused before any resolve/read


def test_revising_a_linked_artifact_is_refused(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "doc.txt").write_text("body", encoding="utf-8")
    _fence(monkeypatch, art, workspace=ws)
    art.save_file_artifact.invoke({"path": "doc.txt", "link": True})
    aid = _arts(art)[0]["id"]

    out = art.save_file_artifact.invoke({"path": "doc.txt", "artifact_id": aid})  # no link → revise
    assert "read-only" in out and "no revisions" in out
    a = _arts(art)[0]
    assert a["id"] == aid and len(a["versions"]) == 1  # unchanged, no second version


# ── r5: without link, the 25 MB blob cap and its message are unchanged (regression) ───────────


def test_without_link_oversized_file_still_refused_with_blob_message(monkeypatch, tmp_path):
    monkeypatch.setenv("ARTIFACT_MAX_BLOB_KB", "1")  # 1 KB cap (stands in for the 25 MB default)
    art = _load(monkeypatch, tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "big.bin").write_bytes(b"x" * 4096)  # 4 KB > 1 KB
    _fence(monkeypatch, art, workspace=ws)

    out = art.save_file_artifact.invoke({"path": "big.bin"})  # link defaults to False
    assert "too large" in out.lower() and "max_blob_kb" in out
    assert "to link" not in out  # today's blob message, not the linked one
    assert _arts(art) == []


def test_without_link_a_normal_save_still_writes_a_blob(monkeypatch, tmp_path):
    """Sanity: the default (copy) path is unchanged — a blob is written, no `link`/`linked`."""
    art = _load(monkeypatch, tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "notes.txt").write_text("hello", encoding="utf-8")
    _fence(monkeypatch, art, workspace=ws)

    art.save_file_artifact.invoke({"path": "notes.txt"})
    a = _arts(art)[0]
    assert a["kind"] == "file" and "linked" not in a
    v = a["versions"][0]
    assert v["blob"] and "link" not in v
    assert art._blob_path(a["id"], v["blob"]).exists()


def _linked_client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from tests.test_artifact_plugin import _app

    art = _load(monkeypatch, tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    source = ws / "movie.mp4"
    payload = bytes(range(256)) * 8
    source.write_bytes(payload)
    _fence(monkeypatch, art, workspace=ws)
    art.save_file_artifact.invoke({"path": "movie.mp4", "link": True})
    aid = _arts(art)[0]["id"]
    return art, TestClient(_app(art)), source, payload, f"/api/plugins/artifact/artifact/{aid}"


def test_linked_download_and_media_preview(monkeypatch, tmp_path):
    art, client, source, payload, url = _linked_client(monkeypatch, tmp_path)
    with client:
        response = client.get(url + "/blob")
        assert response.status_code == 200 and response.content == payload
        assert 'filename="movie.mp4"' in response.headers["content-disposition"]
        response = client.get(url + "/blob?version=1", headers={"Range": "bytes=5-19"})
        assert response.status_code == 206 and response.content == payload[5:20]
        assert client.get(url + "/blob?version=2").status_code == 404
        ticket = client.post(url + "/media-ticket")
        assert ticket.status_code == 200
        media_url = ticket.json()["path"]
        assert client.get(media_url).content == payload
        response = client.get(media_url, headers={"Range": "bytes=20-29"})
        assert response.status_code == 206 and response.content == payload[20:30]
        response = client.head(media_url)
        assert response.status_code == 200 and response.content == b""
        assert response.headers["content-length"] == str(len(payload))
    assert source.read_bytes() == payload
    assert not art._blob_root().exists()


@pytest.mark.parametrize("change", ["deleted", "changed", "unregistered", "escape", "cap"])
def test_linked_routes_recheck_source_and_live_fence(monkeypatch, tmp_path, change):
    art, client, source, payload, url = _linked_client(monkeypatch, tmp_path)
    with client:
        ticket = client.post(url + "/media-ticket").json()["path"]
        if change == "deleted":
            source.unlink()
        elif change == "changed":
            # Even same-size bytes with a restored timestamp cannot reuse the saved stamp.
            import os

            stamp = source.stat()
            source.write_bytes(b"x" * len(payload))
            os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        elif change == "unregistered":
            _fence(monkeypatch, art)
        elif change == "cap":
            monkeypatch.setenv("ARTIFACT_MAX_LINKED_KB", "1")
        else:
            outside = tmp_path / "outside.mp4"
            outside.write_bytes(payload)
            source.unlink()
            try:
                source.symlink_to(outside)
            except (OSError, NotImplementedError):
                pytest.skip("symlinks unavailable")
        assert client.get(url + "/blob").status_code == 404
        assert client.post(url + "/media-ticket").status_code == 404
        assert client.get(ticket).status_code == 404
