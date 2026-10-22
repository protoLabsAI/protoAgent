"""Serving LINKED file artifacts through the blob route (ADR 0092 D2 extension, #4370 SERVE half).

A linked version stores NO blob: only a fenced ``(project, project-relative path)`` reference plus
a size/mtime_ns/sha256 stamp. The blob route (``GET /api/plugins/artifact/artifact/{id}/blob``)
re-resolves that reference through the LIVE fs fence on EVERY request — never a stored absolute
path — so unregistering the project (or turning fs off) revokes access, then streams from a single
fd that is stamp-checked so substituted bytes are never served.

Loaded as a PACKAGE (so the plugin's relative imports resolve), reusing the plugin test helpers,
with the fs fence substituted over temp dirs via ``_links._registry`` (the module-level seam)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from tests.test_artifact_plugin import _app, _arts, _load

# symlink_to + tools.fs_tools (platform-branching) → platform-sensitive (test_platform_sensitive_marks.py)
pytestmark = pytest.mark.platform_sensitive


def _client(art):
    from fastapi.testclient import TestClient

    return TestClient(_app(art))


def _registry(**roots):
    """A real ``ProjectRegistry`` over the given temp roots (resolved, like the live fence)."""
    from tools.fs_tools import Project, ProjectRegistry

    return ProjectRegistry([Project(name=n, root=r.resolve()) for n, r in roots.items()])


def _empty_registry():
    """What ``live_project_registry`` returns when ``filesystem.enabled`` is false."""
    from tools.fs_tools import ProjectRegistry

    return ProjectRegistry([])


def _fence(monkeypatch, art, registry):
    monkeypatch.setattr(art._links, "_registry", lambda: registry)


def _put_linked(
    art,
    project_root: Path,
    rel: str,
    data: bytes,
    *,
    project: str = "proj",
    mime: str = "application/octet-stream",
    size: int | None = None,
    mtime_ns: int | None = None,
    sha: str | None = None,
) -> str:
    """Write ``data`` to ``project_root/rel`` and register a one-version LINKED ``file`` artifact
    pointing at it. The link stamp is taken from the file on disk unless overridden — the
    overrides drive the mismatch cases. Mirrors what the SAVE half (``_save_linked``) stores."""
    p = project_root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    st = p.stat()
    link = {
        "project": project,
        "path": Path(rel).as_posix(),
        "size": len(data) if size is None else size,
        "mtime_ns": st.st_mtime_ns if mtime_ns is None else mtime_ns,
        "sha256": hashlib.sha256(data).hexdigest() if sha is None else sha,
    }
    file_meta = {"mime": mime, "filename": Path(rel).name, "size": link["size"], "thumb": ""}
    v = art._store._new_version("preview", extra={"file": file_meta, "link": link})
    store = art._read_store()
    art_id = art._store._new_id()
    store["artifacts"].insert(
        0,
        {
            "id": art_id,
            "title": file_meta["filename"],
            "kind": "file",
            "linked": True,
            "versions": [v],
            "version_count": 1,
            "created": v["ts"],
            "updated": v["ts"],
        },
    )
    store["current"] = art_id
    art._store._write_store(store)
    return art_id


def _blob_url(aid: str) -> str:
    return f"/api/plugins/artifact/artifact/{aid}/blob"


def _stored_link(art, aid: str) -> dict:
    return art._store._find(art._read_store(), aid)["versions"][0]["link"]


# ── r1: a linked file over the 25 MB blob cap serves its exact bytes, with Range ──────────────


def test_linked_over_25mb_serves_exact_bytes_and_range(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    body = bytearray(b"L" * (26 * 1024 * 1024))  # 26 MB — over the 25 MB max_blob_kb cap
    body[1000:1010] = b"NEEDLE1234"  # a position-dependent marker so a wrong slice can't pass
    body[-5:] = b"TAILX"
    data = bytes(body)
    aid = _put_linked(art, proj, "big.bin", data)
    _fence(monkeypatch, art, _registry(proj=proj))
    c = _client(art)
    url = _blob_url(aid)

    full = c.get(url)
    assert full.status_code == 200 and full.content == data
    assert full.headers["accept-ranges"] == "bytes"
    assert full.headers["content-length"] == str(len(data))

    r = c.get(url, headers={"Range": "bytes=1000-1009"})
    assert r.status_code == 206 and r.content == b"NEEDLE1234"
    assert r.headers["content-range"] == f"bytes 1000-1009/{len(data)}"
    r = c.get(url, headers={"Range": "bytes=-5"})  # suffix
    assert r.status_code == 206 and r.content == b"TAILX"
    r = c.get(url, headers={"Range": f"bytes={len(data)}-"})  # past the end
    assert r.status_code == 416 and r.content == b""


def test_save_then_serve_round_trips(monkeypatch, tmp_path):
    """The two halves interoperate: the stamp the SAVE half writes is exactly what the SERVE half
    re-checks, so a save_file_artifact(link=True) download returns the on-disk bytes verbatim."""
    art = _load(monkeypatch, tmp_path)
    proj = tmp_path / "proj"
    (proj / "docs").mkdir(parents=True)
    data = bytes(range(256)) * 8  # 2048 bytes
    (proj / "docs" / "clip.bin").write_bytes(data)
    _fence(monkeypatch, art, _registry(proj=proj))

    out = art.save_file_artifact.invoke({"path": "docs/clip.bin", "project": "proj", "link": True})
    assert "Linked file artifact" in out
    a = _arts(art)[0]
    assert a["linked"] is True and "blob" not in a["versions"][0]
    c = _client(art)
    url = _blob_url(a["id"])
    assert c.get(url).content == data
    r = c.get(url, headers={"Range": "bytes=100-199"})
    assert r.status_code == 206 and r.content == data[100:200]


# ── r2: fence re-checked on every serve — unregister / fs-off revokes, re-register restores ────


def test_unregistered_or_fs_off_is_403_and_reregister_restores_200(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    data = b"hello linked world\n" * 20
    aid = _put_linked(art, proj, "doc.txt", data, mime="text/plain")
    c = _client(art)
    url = _blob_url(aid)

    _fence(monkeypatch, art, _registry(proj=proj))
    first = c.get(url)
    assert first.status_code == 200 and first.content == data

    # the project is no longer in the registry → 403 (its name is unknown there now)
    _fence(monkeypatch, art, _registry(other=proj))
    r = c.get(url)
    assert r.status_code == 403 and "no longer reachable" in r.json()["detail"]

    # fs disabled → live_project_registry() hands back an empty registry → 403
    _fence(monkeypatch, art, _empty_registry())
    assert c.get(url).status_code == 403

    # no fs toolset on this host at all → 403
    monkeypatch.setattr(art._links, "_registry", lambda: None)
    assert c.get(url).status_code == 403

    # re-registering the project restores access
    _fence(monkeypatch, art, _registry(proj=proj))
    restored = c.get(url)
    assert restored.status_code == 200 and restored.content == data


# ── r3: an in-project path replaced by a symlink pointing outside never serves the outside bytes


def test_symlink_escape_never_serves_outside_bytes(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    data = b"legit in-project bytes\n"
    aid = _put_linked(art, proj, "target.txt", data, mime="text/plain")
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"TOP-SECRET-OUTSIDE-THE-FENCE")

    # replace the linked file with a symlink escaping the project
    p = proj / "target.txt"
    p.unlink()
    try:
        p.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")

    _fence(monkeypatch, art, _registry(proj=proj))
    r = _client(art).get(_blob_url(aid))
    # the live fence follows the symlink, sees it escapes, and refuses (403); the stamp check is
    # the backstop if it ever resolved in-project (409) — either way the secret is never streamed
    assert r.status_code in (403, 409)
    assert b"SECRET" not in r.content
    assert r.content != b"TOP-SECRET-OUTSIDE-THE-FENCE"


# ── r4: a changed/missing file is 409 {"state": "changed_or_missing"} and sends no bytes ───────


def test_modified_same_size_same_mtime_is_409(monkeypatch, tmp_path):
    """The adversarial case: bytes swapped but length AND mtime restored, so only the sha256 of
    the open fd catches it."""
    art = _load(monkeypatch, tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    data = b"original contents, exact length matters\n"
    aid = _put_linked(art, proj, "doc.txt", data, mime="text/plain")
    stamp = _stored_link(art, aid)["mtime_ns"]

    tampered = b"SWAPPED contents, exact length matters!\n"
    assert len(tampered) == len(data)
    p = proj / "doc.txt"
    p.write_bytes(tampered)
    os.utime(p, ns=(stamp, stamp))  # restore mtime so size + mtime both still match the stamp

    _fence(monkeypatch, art, _registry(proj=proj))
    r = _client(art).get(_blob_url(aid))
    assert r.status_code == 409 and r.json()["detail"]["state"] == "changed_or_missing"
    assert tampered not in r.content


def test_changed_mtime_is_409(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    data = b"unchanged bytes\n"
    aid = _put_linked(art, proj, "doc.txt", data, mime="text/plain")
    stamp = _stored_link(art, aid)["mtime_ns"]

    os.utime(proj / "doc.txt", ns=(stamp + 1_000_000_000, stamp + 1_000_000_000))  # 1s later
    _fence(monkeypatch, art, _registry(proj=proj))
    r = _client(art).get(_blob_url(aid))
    assert r.status_code == 409 and r.json()["detail"]["state"] == "changed_or_missing"


def test_deleted_file_is_409(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    aid = _put_linked(art, proj, "gone.txt", b"here today\n", mime="text/plain")
    (proj / "gone.txt").unlink()

    _fence(monkeypatch, art, _registry(proj=proj))
    r = _client(art).get(_blob_url(aid))
    assert r.status_code == 409 and r.json()["detail"]["state"] == "changed_or_missing"
    assert b"here today" not in r.content  # no file bytes went out


# ── r5: the media-ticket route 415s for a linked version; GC/eviction leave linked files alone ─


def test_media_ticket_returns_415_for_linked_version(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    # even an image mime linked → 415 (served in place from the project, never via a media ticket)
    aid = _put_linked(art, proj, "pic.png", b"\x89PNG\r\n\x1a\n" + b"0" * 128, mime="image/png")
    _fence(monkeypatch, art, _registry(proj=proj))
    r = _client(art).post(f"/api/plugins/artifact/artifact/{aid}/media-ticket")
    assert r.status_code == 415


def test_gc_and_eviction_never_touch_a_linked_file(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    data = b"in-place bytes that must survive every sweep\n"
    aid = _put_linked(art, proj, "keep.txt", data, mime="text/plain")
    linked_file = proj / "keep.txt"

    # a normal (copied) artifact too, so blobs/ actually exists and GC has real work to do
    (proj / "copy.bin").write_bytes(b"copy me")
    _fence(monkeypatch, art, _registry(proj=proj))
    art.save_file_artifact.invoke({"path": "copy.bin", "project": "proj"})
    copied = next(a for a in _arts(art) if a["id"] != aid)
    cblob = art._blob_path(copied["id"], copied["versions"][0]["blob"])
    assert cblob.exists() and art._blob_root().exists()

    # a store write runs _evict + _gc_blobs — neither crashes on nor deletes the linked file
    art._store._write_store(art._read_store())
    assert linked_file.exists() and linked_file.read_bytes() == data
    assert cblob.exists()

    # _evict with the linked artifact present does not raise
    kept = art._store._evict(art._read_store()["artifacts"], 0)
    assert isinstance(kept, list)

    # even when everything is evicted, GC deletes nothing inside the project
    art._store._write_store({"artifacts": [], "current": None})
    assert linked_file.exists() and linked_file.read_bytes() == data


# ── the copied-blob path is unchanged (regression: the link branch only fires for linked) ──────


def test_copied_blob_still_served_unchanged(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    data = bytes(range(256)) * 4
    (proj / "clip.bin").write_bytes(data)
    _fence(monkeypatch, art, _registry(proj=proj))
    art.save_file_artifact.invoke({"path": "clip.bin", "project": "proj"})  # copy, not link
    a = _arts(art)[0]
    assert "link" not in a["versions"][0] and a["versions"][0]["blob"]

    c = _client(art)
    url = _blob_url(a["id"])
    assert c.get(url).content == data
    r = c.get(url, headers={"Range": "bytes=10-19"})
    assert r.status_code == 206 and r.content == data[10:20]
