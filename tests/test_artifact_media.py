"""Inline image/video for `file` artifacts (#4257) — HTTP Range on the blob routes, the media
ticket + PUBLIC media route (``plugins/artifact/_media.py``), the shell's preview selection and
media frame, and the web-storage guard every artifact frame runs first.

The shell's JS is run under ``node`` where it's on PATH (skips cleanly without it)."""

from __future__ import annotations

import json
import re

import pytest

from tests.test_artifact_plugin import _app, _arts, _load
from tests.test_artifact_slides import _js, _js_function, _node

# node subprocesses → platform-sensitive (tests/test_platform_sensitive_marks.py)
pytestmark = pytest.mark.platform_sensitive

_VIDEO = bytes(range(256)) * 40  # 10240 bytes standing in for an MP4 (the routes never parse it)


def _save(art, tmp_path, name: str, data: bytes, artifact_id: str = "") -> str:
    f = tmp_path / name
    f.write_bytes(data)
    art.save_file_artifact.invoke({"path": str(f), "artifact_id": artifact_id})
    return _arts(art)[0]["id"]


def _client(art):
    from fastapi.testclient import TestClient

    return TestClient(_app(art))


# ── Range on the gated blob route ─────────────────────────────────────────────


def test_blob_route_serves_byte_ranges(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    aid = _save(art, tmp_path, "clip.mp4", _VIDEO)
    c = _client(art)
    url = f"/api/plugins/artifact/artifact/{aid}/blob"
    full = c.get(url)
    assert full.status_code == 200 and full.content == _VIDEO
    assert full.headers["accept-ranges"] == "bytes"
    r = c.get(url, headers={"Range": "bytes=100-199"})
    assert r.status_code == 206 and r.content == _VIDEO[100:200]
    assert r.headers["content-range"] == f"bytes 100-199/{len(_VIDEO)}"
    assert r.headers["content-length"] == "100"
    r = c.get(url, headers={"Range": "bytes=10000-"})  # open-ended
    assert r.status_code == 206 and r.content == _VIDEO[10000:]
    r = c.get(url, headers={"Range": "bytes=-40"})  # suffix
    assert r.status_code == 206 and r.content == _VIDEO[-40:]
    r = c.get(url, headers={"Range": "bytes=10000-999999"})  # end clamps to the file
    assert r.status_code == 206 and r.content == _VIDEO[10000:]
    r = c.get(url, headers={"Range": f"bytes={len(_VIDEO)}-"})  # past the end
    assert r.status_code == 416 and r.headers["content-range"] == f"bytes */{len(_VIDEO)}" and r.content == b""
    r = c.get(url, headers={"Range": "bytes=0-1,5-6"})  # multi-range → the whole body is legal
    assert r.status_code == 200 and r.content == _VIDEO


@pytest.mark.parametrize(
    "header,want",
    [
        (None, None),
        ("", None),
        ("bytes=0-9", (0, 9)),
        ("bytes=5-", (5, 99)),
        ("bytes=-10", (90, 99)),
        ("bytes=-500", (0, 99)),
        ("bytes=50-10", False),
        ("bytes=100-", False),
        ("bytes=-0", False),
        ("bytes=x-y", False),
        ("bytes=7", False),
        ("items=0-9", None),
        ("bytes=0-1,4-5", None),
    ],
)
def test_parse_range(monkeypatch, tmp_path, header, want):
    art = _load(monkeypatch, tmp_path)
    assert art._media.parse_range(header, 100) == want


# ── the media ticket + public media route ─────────────────────────────────────


def _ticket(c, aid: str, version: int = 0):
    return c.post(f"/api/plugins/artifact/artifact/{aid}/media-ticket" + (f"?version={version}" if version else ""))


def test_media_route_streams_a_ticketed_video_with_ranges(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    aid = _save(art, tmp_path, "clip.mp4", _VIDEO)
    c = _client(art)
    t = _ticket(c, aid)
    assert t.status_code == 200
    body = t.json()
    assert body["version"] == 1 and body["path"].startswith(f"/plugins/artifact/media/{aid}?version=1&t=")
    r = c.get(body["path"])
    assert r.status_code == 200 and r.content == _VIDEO
    assert r.headers["content-type"] == "video/mp4"
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["content-disposition"].startswith("inline;")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in r.headers["content-security-policy"]
    r = c.get(body["path"], headers={"Range": "bytes=2048-4095"})  # a seek
    assert r.status_code == 206 and r.content == _VIDEO[2048:4096]
    assert c.head(body["path"]).status_code == 200


def test_media_route_refuses_without_a_matching_ticket(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    aid = _save(art, tmp_path, "clip.mp4", _VIDEO)
    aid = _save(art, tmp_path, "clip.mp4", _VIDEO[::-1], artifact_id=aid)  # version 2
    c = _client(art)
    tok = _ticket(c, aid, 1).json()["ticket"]
    base = f"/plugins/artifact/media/{aid}"
    assert c.get(f"{base}?version=1&t={tok}").content == _VIDEO
    assert c.get(f"{base}?version=2&t={tok}").status_code == 404  # bound to version 1
    assert c.get(f"{base}?version=1").status_code == 404  # no ticket
    assert c.get(f"{base}?version=1&t=forged").status_code == 404
    assert c.get(f"{base}?t={tok}").status_code == 404  # an explicit version is required
    other = _save(art, tmp_path, "other.mp4", _VIDEO)
    assert c.get(f"/plugins/artifact/media/{other}?version=1&t={tok}").status_code == 404  # bound to the id
    latest = _ticket(c, aid).json()
    assert latest["version"] == 2  # absent → the latest, pinned at mint time
    assert c.get(latest["path"]).content == _VIDEO[::-1]


def test_media_ticket_expires(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    aid = _save(art, tmp_path, "pic.png", b"\x89PNG\r\n\x1a\nnot-really")
    c = _client(art)
    path = _ticket(c, aid).json()["path"]
    assert c.get(path).status_code == 200
    monkeypatch.setattr(art._media, "TTL_S", -1)
    assert c.get(path).status_code == 404


def test_media_ticket_only_for_inline_media(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    c = _client(art)
    csv = _save(art, tmp_path, "r.csv", b"a,b\n1,2\n")
    assert _ticket(c, csv).status_code == 415
    page = _save(art, tmp_path, "evil.html", b"<script>alert(1)</script>")
    assert _ticket(c, page).status_code == 415
    assert _ticket(c, "a-nope-000000").status_code == 404
    art.show_artifact.invoke({"kind": "html", "code": "<p>x</p>"})
    assert _ticket(c, _arts(art)[0]["id"]).status_code == 404  # not a file artifact
    vid = _save(art, tmp_path, "v.webm", _VIDEO)
    assert _ticket(c, vid, 9).status_code == 404  # out-of-range version


@pytest.mark.parametrize(
    "name,mime,kind,served",
    [
        ("a.png", "image/png", "image", "image/png"),
        ("a.JPG", "image/jpeg", "image", "image/jpeg"),
        ("a.webp", "application/octet-stream", "image", "image/webp"),
        ("a.gif", "image/gif", "image", "image/gif"),
        ("a.svg", "image/svg+xml", "image", "image/svg+xml"),
        ("a.mp4", "video/mp4", "video", "video/mp4"),
        ("a.webm", "video/webm", "video", "video/webm"),
        ("download", "video/mp4", "video", "video/mp4"),
        ("a.mov", "video/quicktime", None, None),
        ("a.html", "text/html", None, None),
        ("a.csv", "text/csv", None, None),
    ],
)
def test_media_allowlist_and_shell_mirror(monkeypatch, tmp_path, name, mime, kind, served):
    art = _load(monkeypatch, tmp_path)
    meta = {"filename": name, "mime": mime}
    assert art._media.media_kind(meta) == kind
    assert art._media.media_type(meta) == served
    got = _node(
        'var PPTX_MIME="x";var DOCX_MIME="y";'
        + _js_function(_js(art), "previewKind")
        + f"console.log(JSON.stringify(previewKind({json.dumps(name)},{json.dumps(mime)})));"
    )
    assert (got if got in ("image", "video") else None) == kind


# ── the shell's media frame ───────────────────────────────────────────────────


def test_file_card_routes_media_to_the_media_frame(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    js = _js(art)
    card = _js_function(js, "fileCard")
    assert 'if((pk==="image"||pk==="video") && !note) return mediaDoc(v, pk);' in card
    doc = _js_function(js, "mediaDoc")
    assert '<video id="m" controls preload="metadata" playsinline' in doc
    assert '<img id="m"' in doc and 'type="checkbox" id="z"' in doc  # click toggles full size
    # nonce CSP: media/img from this origin only, no fetch/XHR, no workers/frames/objects
    for directive in (
        "default-src 'none'",
        "script-src 'nonce-",
        "media-src \"+src+\"",
        "img-src \"+src+\" data:",
        "connect-src 'none'",
        "worker-src 'none'",
        "frame-src 'none'",
        "object-src 'none'",
    ):
        assert directive in doc, directive
    assert "unsafe-eval" not in doc and "script-src 'unsafe-inline'" not in doc
    assert "allow-same-origin" not in art._SHELL_HTML  # the nested frame stays an opaque sandbox
    ctl = _js_function(js, "artMedia")
    assert "</" not in ctl and "<!" not in ctl  # rides a srcdoc <script>
    assert 'state:"need"' in ctl and "e.source!==W.parent" in ctl
    feed = _js_function(js, "mediaMessage")
    assert "/media-ticket?version=" in feed and "kit.apiFetch" in feed
    assert 'path.indexOf("/plugins/artifact/media/")!==0' in feed
    assert js.count("mediaReset(a, v, vi);") == 2  # panel + embed renders both arm the feed
    assert 'if(m.type==="protoArtifact:media"){ mediaMessage(m); return; }' in js


def test_media_frame_builds_in_node(monkeypatch, tmp_path):
    """mediaDoc runs as code: one nonce on both the CSP and the script, no stray `</script>`."""
    art = _load(monkeypatch, tmp_path)
    js = _js(art)
    prelude = (
        'var location={origin:"http://127.0.0.1:7870"};'
        "var document={documentElement:{}};"
        'function getComputedStyle(){return {getPropertyValue:function(){return "";}};}'
        'var crypto={getRandomValues:function(a){for(var i=0;i<a.length;i++)a[i]=i*7;return a;}};'
        'function btoa(s){return Buffer.from(s,"binary").toString("base64");}'
    )
    fns = "".join(_js_function(js, n) for n in ("esc", "fmtSize", "cspNonce", "artMedia", "mediaDoc"))
    out = _node(
        prelude
        + fns
        + 'console.log(JSON.stringify([mediaDoc({file:{filename:"c.mp4",mime:"video/mp4",size:7952178}},"video"),'
        + 'mediaDoc({file:{filename:"p\\"x.png",mime:"image/png",size:10}},"image")]));'
    )
    vid, img = out
    nonce = re.search(r"script-src 'nonce-([A-Za-z0-9]+)'", vid).group(1)
    assert f'<script nonce="{nonce}">' in vid
    assert "media-src http://127.0.0.1:7870;" in vid
    assert "7.6 MB" in vid and "<video" in vid
    assert vid.count("</script>") == 1
    assert 'alt="p&quot;x.png"' in img and "<video" not in img


# ── the web-storage guard ─────────────────────────────────────────────────────


def test_every_base_frame_guards_web_storage(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    js = _js(art)
    assert "+ STOREBOOT + SHIM + ERRBOOT;" in _js_function(js, "base")
    m = re.search(r"var STOREBOOT = ([\s\S]*?\);<\\/script>';)", js)
    assert m, "STOREBOOT not found"
    # A sandboxed (opaque-origin) frame: touching either storage throws. After the guard both
    # are working in-memory stores; a frame where storage works is left alone.
    got = _node(
        "var STOREBOOT = " + m.group(1) + ";"
        "var body=STOREBOOT.replace('<script>','').replace('<\\/script>','');"
        "function sandboxed(){var w={};['localStorage','sessionStorage'].forEach(function(n){"
        "Object.defineProperty(w,n,{configurable:true,get:function(){throw new Error('SecurityError');}});});return w;}"
        "var w=sandboxed();new Function('window',body)(w);"
        "w.localStorage.setItem('k',1);w.sessionStorage.setItem('s','v');"
        "var real={getItem:function(){return 'real';}};var ok={localStorage:real,sessionStorage:real};"
        "new Function('window',body)(ok);"
        "console.log(JSON.stringify([w.localStorage.getItem('k'),w.localStorage.length,w.localStorage.key(0),"
        "w.localStorage.getItem('none'),w.sessionStorage.getItem('s'),ok.localStorage===real]));"
    )
    assert got == ["1", 1, "k", None, "v", True]
