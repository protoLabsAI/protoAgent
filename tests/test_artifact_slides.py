"""Slide previews for .pptx file artifacts — the vendored renderer, preview selection, the
text-outline fallback, and the safety caps (``plugins/artifact/_slides.py`` + ``shell.js``).

The shell's JS helpers (previewKind / slidesOk / the in-frame image-header reader) are run
under ``node`` where it's on PATH, so the selection rules and the JS caps are checked as code,
not just as strings; those cases skip cleanly without node."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import shutil
import struct
import subprocess
import zipfile
import zlib

import pytest

from tests.test_artifact_plugin import ROOT, _app, _arts, _load

# node subprocesses → platform-sensitive (tests/test_platform_sensitive_marks.py)
pytestmark = pytest.mark.platform_sensitive

NODE = shutil.which("node")
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def _js(art) -> str:
    return art._SHELL_JS


def _js_function(js: str, name: str) -> str:
    """The source of top-level shell function ``name`` (brace-matched from its header)."""
    start = js.index(f"function {name}(")
    depth, i = 0, js.index("{", start)
    while True:
        c = js[i]
        depth += c == "{"
        depth -= c == "}"
        i += 1
        if depth == 0:
            return js[start:i]


def _node(src: str):
    if not NODE:
        pytest.skip("node not on PATH")
    out = subprocess.run([NODE, "-e", src], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# ── a minimal real deck + hostile variants ─────────────────────────────────────


def _png(w: int, h: int) -> bytes:
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\0" * 8)) + chunk(b"IEND", b"")


def _deck(extra: dict[str, bytes] | None = None, slides: int = 2) -> bytes:
    """The parts a preflight looks at — not a full OOXML package, just its shape."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("ppt/presentation.xml", "<p:presentation/>")
        for i in range(1, slides + 1):
            z.writestr(f"ppt/slides/slide{i}.xml", f"<p:sld>{i}</p:sld>")
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


# ── vendored renderer + its route ───────────────────────────────────────────────


def test_renderer_is_vendored_and_served_same_origin(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    art = _load(monkeypatch, tmp_path)
    assert "pptx-renderer.min.js" in art._VENDOR_FILES
    r = TestClient(_app(art)).get("/plugins/artifact/vendor/pptx-renderer.min.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert r.headers.get("access-control-allow-origin") == "*"  # SRI from the opaque sandbox
    assert "immutable" in r.headers.get("cache-control", "")
    assert b"PptxRenderer" in r.content[:200]  # the IIFE global the frame reads
    # its licence notices ship beside it
    notices = (ROOT / "vendor" / "pptx-renderer.LICENSES.txt").read_text(encoding="utf-8")
    for needle in ("Apache License", "Mozilla Public License", "JSZip", "ECharts"):
        assert needle in notices, needle


def test_renderer_sri_hash_matches_the_vendored_bytes(monkeypatch, tmp_path):
    """The LIB map pins the exact bytes — a re-vendor without a hash bump would refuse to load."""
    art = _load(monkeypatch, tmp_path)
    data = (ROOT / "vendor" / "pptx-renderer.min.js").read_bytes()
    want = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()
    assert f'"pptx-renderer.min.js",\n      "{want}"' in _js(art)


def test_slides_frame_runs_under_a_nonce_csp_with_no_network(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    src = _js_function(_js(art), "slidesDoc")
    assert "Content-Security-Policy" in src
    for directive in (
        "default-src 'none'",
        "script-src 'nonce-",
        "connect-src 'none'",
        "img-src blob: data:",
        "worker-src 'none'",
        "frame-src 'none'",
        "object-src 'none'",
    ):
        assert directive in src, directive
    assert "unsafe-eval" not in src and "script-src 'unsafe-inline'" not in src
    assert 'cdn("pptx", nonce)' in src  # the lib carries the nonce AND its SRI
    # Still the same sandbox model as every artifact: never same-origin.
    assert "allow-same-origin" not in art._SHELL_HTML
    # The controller rides a srcdoc <script>: no literal close tags inside it.
    ctl = _js_function(_js(art), "artSlides")
    assert "</" not in ctl and "<!" not in ctl


# ── preview selection by mime / extension ──────────────────────────────────────


@pytest.mark.parametrize(
    "name,mime,want",
    [
        ("deck.pptx", PPTX_MIME, True),
        ("DECK.PPTX", "application/octet-stream", True),
        ("download", PPTX_MIME, True),  # no extension, but the OOXML mime
        ("old.ppt", "application/vnd.ms-powerpoint", False),  # legacy binary: outline only
        ("report.docx", "application/msword", False),
        ("data.csv", "text/csv", False),
    ],
)
def test_is_slides_by_extension_or_mime(monkeypatch, tmp_path, name, mime, want):
    art = _load(monkeypatch, tmp_path)
    assert art._slides.is_slides(name, mime) is want


def test_shell_preview_kind_matches_python(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    js = _js(art)
    cases = [
        ["deck.pptx", PPTX_MIME],
        ["DECK.PPTX", "application/octet-stream"],
        ["download", PPTX_MIME],
        ["old.ppt", "application/vnd.ms-powerpoint"],
        ["t.csv", "text/csv"],
        ["n.md", "text/markdown"],
        ["x.json", "application/json"],
        ["r.docx", "application/msword"],
    ]
    got = _node(
        'var PPTX_MIME="'
        + PPTX_MIME
        + '";'
        + _js_function(js, "previewKind")
        + "console.log(JSON.stringify("
        + json.dumps(cases)
        + ".map(function(c){return previewKind(c[0],c[1]);})));"
    )
    assert got == ["slides", "slides", "slides", "text", "table", "md", "json", "text"]
    for (name, mime), kind in zip(cases, got):
        assert (kind == "slides") == art._slides.is_slides(name, mime), name


def test_shell_honours_a_refused_preflight(monkeypatch, tmp_path):
    """slidesOk: a deck the preflight refused gets the outline card; a deck with no verdict
    (saved before the preflight existed) still renders — the frame re-enforces the caps."""
    art = _load(monkeypatch, tmp_path)
    js = _js(art)
    versions = [
        {"file": {"filename": "a.pptx", "mime": PPTX_MIME, "slides": {"render": True}}},
        {"file": {"filename": "a.pptx", "mime": PPTX_MIME, "slides": {"render": False, "reason": "bomb"}}},
        {"file": {"filename": "a.pptx", "mime": PPTX_MIME}},
        {"file": {"filename": "a.csv", "mime": "text/csv"}},
    ]
    got = _node(
        'var PPTX_MIME="'
        + PPTX_MIME
        + '";'
        + _js_function(js, "previewKind")
        + _js_function(js, "slidesOk")
        + "console.log(JSON.stringify("
        + json.dumps(versions)
        + ".map(slidesOk)));"
    )
    assert got == [True, False, True, False]
    card = _js_function(js, "fileCard")
    assert "if(slidesOk(v)) return slidesDoc(v);" in card
    assert "Slide preview unavailable" in card


# ── fallback when parsing fails ────────────────────────────────────────────────


def test_unparseable_pptx_is_refused_and_keeps_its_outline_card(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    p = tmp_path / "broken.pptx"
    p.write_bytes(b"PK\x03\x04 not a zip at all")
    assert "Saved file artifact" in art.save_file_artifact.invoke({"path": str(p)})
    v = _arts(art)[0]["versions"][0]
    assert v["file"]["slides"]["render"] is False
    assert "bad zip" in v["file"]["slides"]["reason"]
    assert "no text preview" in v["code"].lower()  # the outline path still answers


def test_real_deck_gets_a_passing_verdict(monkeypatch, tmp_path):
    pptx = pytest.importorskip("pptx")
    art = _load(monkeypatch, tmp_path)
    prs = pptx.Presentation()
    for title in ("One", "Two", "Three"):
        s = prs.slides.add_slide(prs.slide_layouts[1])
        s.shapes.title.text = title
    prs.save(str(tmp_path / "deck.pptx"))
    art.save_file_artifact.invoke({"path": str(tmp_path / "deck.pptx")})
    meta = _arts(art)[0]["versions"][0]["file"]
    assert meta["slides"] == {"render": True, "reason": "", "count": 3, "big_images": 0}


def test_non_deck_files_carry_no_slides_verdict(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    f = tmp_path / "t.csv"
    f.write_text("a,b\n1,2", encoding="utf-8")
    art.save_file_artifact.invoke({"path": str(f)})
    assert "slides" not in _arts(art)[0]["versions"][0]["file"]


def test_in_frame_failure_shows_the_outline(monkeypatch, tmp_path):
    """The frame's own failure path (renderer missing, parse error, timeout, cap) opens the
    outline and reports up; the shell's watchdog swaps in the outline card for a frame that
    never answers."""
    art = _load(monkeypatch, tmp_path)
    js = _js(art)
    ctl = _js_function(js, "artSlides")
    assert "ol.open=true" in ctl and 'state:"failed"' in ctl
    assert "the slide renderer didn't load" in ctl
    assert "caps.parseMs" in ctl  # the frame's own time cap
    assert "pptxFallback(ctx" in js and "PPTX_CAPS.watchdogMs" in js
    # the outline is always in the slides page too (screen readers, and the failure view)
    assert '<details id="ol"><summary>Text outline</summary>' in _js_function(js, "slidesDoc")


# ── safety caps ───────────────────────────────────────────────────────────────


def test_preflight_passes_a_plain_deck(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    assert art._slides.preflight(_deck(slides=3)) == {"render": True, "reason": "", "count": 3, "big_images": 0}


def test_preflight_refuses_a_zip_bomb(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    s = art._slides
    # 40 MB of zeros: tiny on disk, over the per-entry cap and a ~1000:1 ratio
    v = s.preflight(_deck({"ppt/media/bomb.bin": b"\0" * (40 * 1024 * 1024)}))
    assert v["render"] is False and "per-file cap" in v["reason"]
    # under the per-entry cap, but still an absurd ratio
    v = s.preflight(_deck({"ppt/media/ratio.bin": b"\0" * (8 * 1024 * 1024)}))
    assert v["render"] is False and "zip bomb" in v["reason"]


def test_preflight_caps_total_inflation_and_entry_count(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    s = art._slides
    monkeypatch.setattr(s, "MAX_TOTAL_BYTES", 1024)
    assert "inflates to" in s.preflight(_deck({"ppt/notes.xml": b"x" * 4096}))["reason"]
    monkeypatch.setattr(s, "MAX_TOTAL_BYTES", 256 * 1024 * 1024)
    monkeypatch.setattr(s, "MAX_ENTRIES", 5)
    assert "files in the archive" in s.preflight(_deck(slides=6))["reason"]


def test_preflight_caps_file_size_and_slide_count(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    s = art._slides
    monkeypatch.setattr(s, "MAX_BYTES", 100)
    assert "preview cap" in s.preflight(_deck())["reason"]
    monkeypatch.setattr(s, "MAX_BYTES", 40 * 1024 * 1024)
    monkeypatch.setattr(s, "MAX_SLIDES", 2)
    v = s.preflight(_deck(slides=3))
    assert v["render"] is False and "slide preview cap" in v["reason"]


def test_preflight_refuses_non_decks(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    s = art._slides
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<w/>")
    assert "not a PowerPoint deck" in s.preflight(buf.getvalue())["reason"]
    assert "no slides" in s.preflight(_deck(slides=0))["reason"]


def test_preflight_counts_huge_images_without_decoding_them(monkeypatch, tmp_path):
    """A 30000×30000 PNG header (≈3.6 GB if decoded) is counted, not fatal — the frame swaps
    it for a placeholder; a normal image isn't counted."""
    art = _load(monkeypatch, tmp_path)
    v = art._slides.preflight(
        _deck({"ppt/media/image1.png": _png(30000, 30000), "ppt/media/image2.png": _png(800, 600)})
    )
    assert v["render"] is True and v["big_images"] == 1


def _image_samples():
    PIL = pytest.importorskip("PIL.Image")
    out = {}
    for fmt, ext in (("PNG", "png"), ("GIF", "gif"), ("JPEG", "jpg"), ("BMP", "bmp"), ("WEBP", "webp")):
        b = io.BytesIO()
        try:
            PIL.new("RGB", (123, 45), (200, 10, 10)).save(b, fmt)
        except (KeyError, OSError):  # a Pillow built without that codec
            continue
        out[ext] = b.getvalue()
    out["png-huge"] = _png(30000, 30000)
    return out


def test_image_header_dims_python_and_js_agree(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    samples = _image_samples()
    for name, data in samples.items():
        want = (30000, 30000) if name == "png-huge" else (123, 45)
        assert art._slides.image_dims(data[:65536]) == want, name
    assert art._slides.image_dims(b"not an image at all, just text bytes") is None
    # the in-frame reader (nested inside artSlides) is the same algorithm
    ctl = _js_function(_js(art), "artSlides")
    dims = _js_function(ctl, "dims")
    payload = {k: list(v[:4096]) for k, v in samples.items()}
    got = _node(
        dims + "var S=" + json.dumps(payload) + ";var o={};"
        "Object.keys(S).forEach(function(k){o[k]=dims(new Uint8Array(S[k]));});"
        "console.log(JSON.stringify(o));"
    )
    for name, wh in got.items():
        want = [30000, 30000] if name == "png-huge" else [123, 45]
        assert wh == want, name


def test_js_caps_mirror_python(monkeypatch, tmp_path):
    """PPTX_CAPS in shell.js and the constants in _slides.py are one policy, two enforcers."""
    art = _load(monkeypatch, tmp_path)
    s = art._slides
    block = re.search(r"var PPTX_CAPS=\{(.*?)\};", _js(art), re.S).group(1)
    caps = {k: int(v) for k, v in re.findall(r"(\w+):\s*(\d+)", block)}
    assert caps["maxBytes"] == s.MAX_BYTES
    assert caps["maxEntries"] == s.MAX_ENTRIES
    assert caps["maxEntryBytes"] == s.MAX_ENTRY_BYTES
    assert caps["maxTotalBytes"] == s.MAX_TOTAL_BYTES
    assert caps["maxImagePixels"] == s.MAX_IMAGE_PIXELS
    assert caps["maxSlides"] == s.MAX_SLIDES
    assert 0 < caps["parseMs"] < caps["watchdogMs"]
    # the frame hands every one of them to the renderer's zip parser / its own checks
    ctl = _js_function(_js(art), "artSlides")
    for key in (
        "maxEntries",
        "maxEntryUncompressedBytes",
        "maxTotalUncompressedBytes",
        "maxMediaBytes",
        "maxImagePixels",
        "maxSlides",
    ):
        assert key in ctl, key
    # and the shell refuses to even fetch an over-cap file
    assert "PPTX_CAPS.maxBytes" in _js_function(_js(art), "pptxBytes")
