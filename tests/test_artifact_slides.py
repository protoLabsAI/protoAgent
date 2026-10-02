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


def _sri_pins(js: str) -> dict[str, str]:
    """The shell's LIB map as {file: "sha512-…"} — parsed, so line endings/indent don't matter."""
    return dict(re.findall(r'\[\s*"([\w.-]+\.min\.js)"\s*,\s*"(sha512-[A-Za-z0-9+/=]+)"\s*\]', js))


def test_every_pinned_lib_is_served_byte_exact_to_its_sri(monkeypatch, tmp_path):
    """The SRI pin must equal the hash of the bytes the vendor route actually SERVES — on every
    platform. A checkout that rewrites line endings (Windows autocrlf) would change the bytes and
    the sandbox would refuse the script; .gitattributes marks vendor/ -text so it can't."""
    from fastapi.testclient import TestClient

    art = _load(monkeypatch, tmp_path)
    pins = _sri_pins(_js(art))
    assert set(pins) == {
        "mermaid.min.js",
        "react.production.min.js",
        "react-dom.production.min.js",
        "babel.min.js",
        "pptx-renderer.min.js",
    }
    c = TestClient(_app(art))
    for name, pin in pins.items():
        served = c.get(f"/plugins/artifact/vendor/{name}").content
        assert served == (ROOT / "vendor" / name).read_bytes(), name  # served as-is
        assert pin == "sha512-" + base64.b64encode(hashlib.sha512(served).digest()).decode(), name


def test_vendored_libs_are_checked_out_byte_exact():
    """The guard behind the test above: git must never apply eol conversion to vendor/."""
    attrs = (ROOT.parent.parent / ".gitattributes").read_text(encoding="utf-8")
    assert re.search(r"^plugins/artifact/vendor/\*\*\s+.*-text", attrs, re.M), attrs


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
    """slidesOk: ONLY a deck the preflight cleared reaches the renderer. A refusal — or a version
    with no verdict (saved before the preflight existed) — gets the outline card, so the frame
    never parses bytes the server hasn't inflated under budget."""
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
    assert got == [True, False, False, False]
    card = _js_function(js, "fileCard")
    assert "if(slidesOk(v)) return slidesDoc(v);" in card
    assert "Slide preview unavailable" in card
    assert "saved before slide previews" in card  # the no-verdict case says why


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
    assert "inflates past" in s.preflight(_deck({"ppt/notes.xml": b"x" * 4096}))["reason"]
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
    assert caps["maxDeckPixels"] == s.MAX_DECK_PIXELS
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
    assert "maxConcurrency:1" in ctl  # sequential inflate: the first cap hit stops the rest
    # and the shell refuses to even fetch an over-cap file
    assert "PPTX_CAPS.maxBytes" in _js_function(_js(art), "pptxBytes")


# ── lying-header bombs (security review of #4019) ─────────────────────────────


def _lie(data: bytes, name: str, size: int, crc: int | None = None) -> bytes:
    """Patch ``name``'s declared uncompressed size (and optionally CRC) in BOTH the local and
    the central-directory header — what a hostile zip does to slip past size checks."""
    out = bytearray(data)
    needle = name.encode()
    i = 0
    while (i := out.find(needle, i)) >= 0:
        if out[i - 30 : i - 26] == b"PK\x03\x04":  # local header: crc @14, usize @22
            if crc is not None:
                struct.pack_into("<I", out, i - 30 + 14, crc)
            struct.pack_into("<I", out, i - 30 + 22, size)
        if out[i - 46 : i - 42] == b"PK\x01\x02":  # central dir: crc @16, usize @24
            if crc is not None:
                struct.pack_into("<I", out, i - 46 + 16, crc)
            struct.pack_into("<I", out, i - 46 + 24, size)
        i += 1
    return bytes(out)


def _lying_bomb(entries: int = 4, mb: int = 48, crc_too: bool = False) -> bytes:
    zeros = b"\0" * (mb * 1024 * 1024)
    data = _deck({f"ppt/media/b{k}.bin": zeros for k in range(entries)})
    crc = zlib.crc32(b"\0" * 100) if crc_too else None
    for k in range(entries):
        data = _lie(data, f"ppt/media/b{k}.bin", 100, crc)
    return data


def test_preflight_measures_what_a_lying_header_bomb_really_inflates_to(monkeypatch, tmp_path):
    """The review's repro, scaled down: every entry claims 100 bytes but holds 48 MB of zeros.
    The directory looks harmless; the preflight inflates the real stream under budget and
    refuses it — without ever holding more than a chunk of it in memory."""
    import tracemalloc

    art = _load(monkeypatch, tmp_path)
    data = _lying_bomb()
    assert all(i.file_size <= 100 for i in zipfile.ZipFile(io.BytesIO(data)).infolist())  # it lies
    tracemalloc.start()
    v = art._slides.preflight(data)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert v["render"] is False and "per-file cap" in v["reason"]
    assert peak < 48 * 1024 * 1024  # bounded by the per-entry cap, never the whole bomb


def test_preflight_refuses_a_lie_even_when_the_crc_is_forged(monkeypatch, tmp_path):
    """Declared size AND CRC forged to match the first 100 bytes (so Python's own ZipExtFile,
    which stops at the declared size, would accept it): the raw stream still inflates past it."""
    art = _load(monkeypatch, tmp_path)
    v = art._slides.preflight(_lying_bomb(entries=1, crc_too=True))
    assert v["render"] is False and "per-file cap" in v["reason"]
    # under the caps, a size/CRC mismatch is still a tampered zip, not a deck
    small = _lie(_deck({"ppt/media/a.bin": b"\x01" * 5000}), "ppt/media/a.bin", 100, zlib.crc32(b"\x01" * 100))
    v = art._slides.preflight(small)
    assert v["render"] is False and "declared size/CRC" in v["reason"]


def test_lying_bomb_saved_as_an_artifact_gets_the_outline(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    p = tmp_path / "bomb.pptx"
    p.write_bytes(_lying_bomb(entries=2))
    art.save_file_artifact.invoke({"path": str(p)})
    meta = _arts(art)[0]["versions"][0]["file"]
    assert meta["slides"]["render"] is False  # → slidesOk false → the outline card, no frame parse


def test_preflight_refuses_truncated_and_corrupt_streams(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    good = _deck({"ppt/media/a.bin": bytes(range(256)) * 200})
    assert art._slides.preflight(good)["render"] is True
    assert art._slides.preflight(good[: len(good) // 2])["render"] is False  # cut mid-archive
    # flip bytes inside the deflate stream: inflate error or CRC mismatch, never a pass
    i = good.index(b"ppt/media/a.bin") + len("ppt/media/a.bin") + 40
    bad = good[:i] + bytes(b ^ 0x5A for b in good[i : i + 64]) + good[i + 64 :]
    assert art._slides.preflight(bad)["render"] is False


def test_honest_decks_still_render(monkeypatch, tmp_path):
    """The inflate-everything pass must not cost honest decks anything: a deck with real,
    incompressible media near the caps passes, and quickly."""
    import os
    import time

    art = _load(monkeypatch, tmp_path)
    media = {f"ppt/media/image{k}.png": _png(1920, 1080) + os.urandom(2 * 1024 * 1024) for k in range(8)}
    t = time.monotonic()
    v = art._slides.preflight(_deck(media, slides=40))
    assert v == {"render": True, "reason": "", "count": 40, "big_images": 0}
    assert time.monotonic() - t < 5


def test_deck_pixel_budget_placeholders_the_overflow(monkeypatch, tmp_path):
    """Many images just under the per-image cap still add up: past MAX_DECK_PIXELS the rest are
    counted for placeholders (the frame applies the same running budget)."""
    art = _load(monkeypatch, tmp_path)
    media = {f"ppt/media/image{k}.png": _png(7000, 7000) for k in range(5)}  # 49 MP each
    v = art._slides.preflight(_deck(media))
    assert v["render"] is True and v["big_images"] == 2  # 3 × 49 MP fit in 150 MP
