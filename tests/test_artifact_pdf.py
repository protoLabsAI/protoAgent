"""Page previews for .pdf file artifacts — the vendored pdf.js, preview selection, the
extracted-text fallback, and the safety caps (``plugins/artifact/_pdfview.py`` + ``shell.js``).

The shell's JS helpers (previewKind / pdfOk) are run under ``node`` where it's on PATH, so the
selection rules are checked as code, not just as strings; those cases skip cleanly without node."""

from __future__ import annotations

import io
import json
import re
import zlib

import pytest

from tests.test_artifact_plugin import ROOT, _app, _arts, _load
from tests.test_artifact_slides import _js, _js_function, _node

# node subprocesses → platform-sensitive (tests/test_platform_sensitive_marks.py)
pytestmark = pytest.mark.platform_sensitive


# ── real PDFs + hostile variants (built with pypdf, a core dep) ────────────────


def _pdf(pages: int = 2, *, content: bytes = b"BT /F1 12 Tf 72 720 Td (hello) Tj ET", password: str = "") -> bytes:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject

    w = PdfWriter()
    for _ in range(pages):
        page = w.add_blank_page(width=612, height=792)
        s = DecodedStreamObject()
        s.set_data(content)
        page[NameObject("/Contents")] = w._add_object(s)
    if password:
        w.encrypt(password)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _pdf_with_stream(raw: bytes, *, filt: str = "/FlateDecode", image: tuple[int, int] | None = None) -> bytes:
    """One page whose content stream (or an image XObject, when ``image`` is given) carries
    ``raw`` as its ENCODED data — so a bomb is the real compressed bytes, not a fake length."""
    from pypdf import PdfWriter
    from pypdf.generic import (
        DictionaryObject,
        NameObject,
        NumberObject,
        StreamObject,
    )

    w = PdfWriter()
    page = w.add_blank_page(width=612, height=792)
    s = StreamObject()
    s._data = raw
    s[NameObject("/Filter")] = NameObject(filt)
    if image is None:
        page[NameObject("/Contents")] = w._add_object(s)
    else:
        s[NameObject("/Type")] = NameObject("/XObject")
        s[NameObject("/Subtype")] = NameObject("/Image")
        s[NameObject("/Width")] = NumberObject(image[0])
        s[NameObject("/Height")] = NumberObject(image[1])
        s[NameObject("/ColorSpace")] = NameObject("/DeviceGray")
        s[NameObject("/BitsPerComponent")] = NumberObject(8)
        xo = DictionaryObject({NameObject("/Im0"): w._add_object(s)})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/XObject"): xo})
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


# ── the preflight ─────────────────────────────────────────────────────────────


def test_preflight_passes_a_plain_pdf(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    assert art._pdfview.preflight(_pdf(pages=3)) == {"render": True, "pages": 3}


def test_preflight_passes_an_image_within_caps(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    data = _pdf_with_stream(zlib.compress(b"\x80" * (64 * 64)), image=(64, 64))
    assert art._pdfview.preflight(data) == {"render": True, "pages": 1}


@pytest.mark.parametrize(
    "data,reason",
    [
        (b"not a pdf at all", "couldn't be read"),
        (b"%PDF-1.7\n", "couldn't be read"),
    ],
)
def test_preflight_refuses_unreadable_files(monkeypatch, tmp_path, data, reason):
    art = _load(monkeypatch, tmp_path)
    v = art._pdfview.preflight(data)
    assert v["render"] is False and reason in v["reason"]


def test_preflight_refuses_a_password_protected_pdf(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    v = art._pdfview.preflight(_pdf(password="hunter2"))
    assert v == {"render": False, "reason": "the PDF is password-protected"}


def test_preflight_refuses_an_oversized_file(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    monkeypatch.setattr(art._pdfview, "MAX_BYTES", 100)
    v = art._pdfview.preflight(_pdf())
    assert v["render"] is False and "preview cap" in v["reason"]


def test_preflight_refuses_too_many_pages(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    monkeypatch.setattr(art._pdfview, "MAX_PAGES", 2)
    v = art._pdfview.preflight(_pdf(pages=3))
    assert v == {"render": False, "reason": "3 pages is over the 2-page preview cap", "pages": 3}


def test_preflight_refuses_a_decompression_bomb_content_stream(monkeypatch, tmp_path):
    """A ~100 KB stream that inflates to 100 MB: measured by what it ACTUALLY inflates to — pypdf's
    bounded filters stop at their own limit, and our per-stream cap catches anything under it."""
    art = _load(monkeypatch, tmp_path)
    bomb = zlib.compress(b"\0" * (100 * 1024 * 1024), 9)
    assert len(bomb) < 200_000
    v = art._pdfview.preflight(_pdf_with_stream(bomb))
    assert v["render"] is False and ("safety cap" in v["reason"] or "once decoded" in v["reason"]), v


def test_preflight_charges_every_stream_to_one_budget(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    monkeypatch.setattr(art._pdfview, "MAX_TOTAL_BYTES", 50)
    v = art._pdfview.preflight(_pdf(pages=3, content=b"0 0 m 10 10 l S " * 2))  # 32 bytes a page
    assert v["render"] is False and "decode to more than" in v["reason"]


def test_preflight_refuses_a_giant_image_without_decoding_it(monkeypatch, tmp_path):
    """Pixel caps come from the image dictionary — a 100 MP image is refused before its data is
    touched (the stream here is tiny and would decode fine)."""
    art = _load(monkeypatch, tmp_path)
    v = art._pdfview.preflight(_pdf_with_stream(zlib.compress(b"\0"), image=(10_000, 10_000)))
    assert v["render"] is False and "MP" in v["reason"]


def test_preflight_has_a_wall_clock_budget(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    monkeypatch.setattr(art._pdfview, "MAX_SCAN_SECONDS", -1.0)
    v = art._pdfview.preflight(_pdf())
    assert v["render"] is False and "took longer than" in v["reason"]


@pytest.mark.parametrize(
    "name,mime,want",
    [
        ("report.pdf", "application/pdf", True),
        ("REPORT.PDF", "application/octet-stream", True),
        ("download", "application/pdf", True),  # no extension, but the mime
        ("deck.pptx", "application/vnd.ms-powerpoint", False),
        ("notes.txt", "text/plain", False),
    ],
)
def test_is_pdf_by_extension_or_mime(monkeypatch, tmp_path, name, mime, want):
    art = _load(monkeypatch, tmp_path)
    assert art._pdfview.is_pdf(name, mime) is want


# ── save_file_artifact stamps the verdict ──────────────────────────────────────


def test_save_file_artifact_stamps_the_pdf_verdict(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    f = tmp_path / "sheet.pdf"
    f.write_bytes(_pdf(pages=2))
    assert "Saved file artifact" in art.save_file_artifact.invoke({"path": str(f)})
    meta = _arts(art)[0]["versions"][0]["file"]
    assert meta["pdf"] == {"render": True, "pages": 2}
    assert "slides" not in meta


def test_save_file_artifact_stamps_a_refusal_too(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    f = tmp_path / "locked.pdf"
    f.write_bytes(_pdf(password="pw"))
    art.save_file_artifact.invoke({"path": str(f)})
    assert _arts(art)[0]["versions"][0]["file"]["pdf"]["render"] is False


def test_non_pdf_files_get_no_pdf_verdict(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    f = tmp_path / "notes.txt"
    f.write_text("hi", encoding="utf-8")
    art.save_file_artifact.invoke({"path": str(f)})
    assert "pdf" not in _arts(art)[0]["versions"][0]["file"]


# ── vendored pdf.js + its route ─────────────────────────────────────────────────


@pytest.mark.parametrize("name,marker", [("pdfjs.min.mjs", b"globalThis.pdfjsLib="), ("pdfjs-worker.min.mjs", b"globalThis.pdfjsWorker={WorkerMessageHandler}")])
def test_pdfjs_is_vendored_and_served_same_origin(monkeypatch, tmp_path, name, marker):
    from fastapi.testclient import TestClient

    art = _load(monkeypatch, tmp_path)
    assert name in art._VENDOR_FILES
    r = TestClient(_app(art)).get(f"/plugins/artifact/vendor/{name}")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]  # a module script needs a JS mime
    assert r.headers.get("access-control-allow-origin") == "*"  # SRI from the opaque sandbox
    assert marker in r.content  # the global the frame reads (lib) / the main-thread hook (worker)
    notices = (ROOT / "vendor" / "pdfjs.LICENSES.txt").read_text(encoding="utf-8")
    assert "Apache License" in notices and "pdfjs-dist 6.4.299" in notices


# ── preview selection ──────────────────────────────────────────────────────────


def test_shell_preview_kind_routes_pdfs_to_pages(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    cases = [
        ["report.pdf", "application/pdf"],
        ["REPORT.PDF", "application/octet-stream"],
        ["download", "application/pdf"],
        ["notes.txt", "text/plain"],
    ]
    got = _node(
        'var PPTX_MIME="x";'
        + _js_function(_js(art), "previewKind")
        + "console.log(JSON.stringify("
        + json.dumps(cases)
        + ".map(function(c){return previewKind(c[0],c[1]);})));"
    )
    assert got == ["pdf", "pdf", "pdf", "text"]
    for (name, mime), kind in zip(cases, got):
        assert (kind == "pdf") == art._pdfview.is_pdf(name, mime), name


def test_shell_honours_a_refused_pdf_preflight(monkeypatch, tmp_path):
    """pdfOk: ONLY a PDF the preflight cleared reaches pdf.js. A refusal — or a version with no
    verdict (saved before the preflight existed) — gets the extracted-text card."""
    art = _load(monkeypatch, tmp_path)
    js = _js(art)
    versions = [
        {"file": {"filename": "a.pdf", "mime": "application/pdf", "pdf": {"render": True}}},
        {"file": {"filename": "a.pdf", "mime": "application/pdf", "pdf": {"render": False, "reason": "bomb"}}},
        {"file": {"filename": "a.pdf", "mime": "application/pdf"}},
        {"file": {"filename": "a.pptx", "mime": "x", "pdf": {"render": True}}},
    ]
    got = _node(
        'var PPTX_MIME="x";'
        + _js_function(js, "previewKind")
        + _js_function(js, "pdfOk")
        + "console.log(JSON.stringify("
        + json.dumps(versions)
        + ".map(pdfOk)));"
    )
    assert got == [True, False, False, False]
    card = _js_function(js, "fileCard")
    assert "if(pdfOk(v)) return pdfDoc(v);" in card
    assert "Page preview unavailable" in card
    assert "saved before page previews" in card


# ── the frame ──────────────────────────────────────────────────────────────────


def test_pdf_frame_runs_under_a_nonce_csp_with_no_network_and_no_workers(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    src = _js_function(_js(art), "pdfDoc")
    assert "Content-Security-Policy" in src
    for directive in (
        "default-src 'none'",
        "script-src 'nonce-",
        "connect-src 'none'",
        "worker-src 'none'",
        "frame-src 'none'",
        "object-src 'none'",
    ):
        assert directive in src, directive
    assert "unsafe-eval" not in src and "script-src 'unsafe-inline'" not in src
    # both modules carry the nonce AND their SRI; the worker module loads first
    assert 'cdnModule("pdfjsWorker", nonce) + cdnModule("pdfjs", nonce)' in src
    mod = _js_function(_js(art), "cdnModule")
    assert 'type="module"' in mod and "integrity=" in mod and 'crossorigin="anonymous"' in mod
    assert "allow-same-origin" not in art._SHELL_HTML
    ctl = _js_function(_js(art), "artPdf")
    assert "</" not in ctl and "<!" not in ctl  # it rides a srcdoc <script>
    # no fetches from inside the frame: no wasm decoders, no streaming/range loads
    for opt in ("useWasm:false", "disableAutoFetch:true", "disableStream:true", "disableRange:true", "enableXfa:false"):
        assert opt in ctl, opt
    assert "W.pdfjsWorker" in ctl  # refuses to start if pdf.js would need a real Worker


def test_in_frame_failure_shows_the_extracted_text(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    js = _js(art)
    ctl = _js_function(js, "artPdf")
    assert "ol.open=true" in ctl and 'state:"failed"' in ctl
    assert "the PDF renderer didn't load" in ctl
    assert "caps.parseMs" in ctl
    assert "password-protected" in ctl
    assert '<details id="ol"><summary>Extracted text</summary>' in _js_function(js, "pdfDoc")
    assert "PDF_CAPS.watchdogMs" in js and "Page preview unavailable — rendering took too long" in js


def test_pdf_pages_are_drawn_lazily_and_capped(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    ctl = _js_function(_js(art), "artPdf")
    assert "IntersectionObserver" in ctl and "release(s)" in ctl  # off-screen canvases are freed
    assert "caps.maxCanvasPixels" in ctl  # whatever the zoom


def test_pdf_js_caps_mirror_python(monkeypatch, tmp_path):
    """PDF_CAPS in shell.js and the constants in _pdfview.py are one policy, two enforcers."""
    art = _load(monkeypatch, tmp_path)
    block = re.search(r"var PDF_CAPS=\{(.*?)\};", _js(art), re.S).group(1)
    caps = {k: int(v) for k, v in re.findall(r"(\w+):\s*(\d+)", block)}
    assert caps["maxBytes"] == art._pdfview.MAX_BYTES
    assert caps["maxPages"] == art._pdfview.MAX_PAGES
    assert 0 < caps["parseMs"] < caps["watchdogMs"]
    # and the shell refuses to even fetch an over-cap file
    assert "PDF_CAPS.maxBytes" in _js_function(_js(art), "pptxBytes")


def test_shell_feeds_bytes_to_the_frame_of_the_right_kind(monkeypatch, tmp_path):
    """One byte-feed serves both renderers: the context carries its kind, the frame's request
    must match it, and the reply goes out on that kind's channel."""
    art = _load(monkeypatch, tmp_path)
    js = _js(art)
    msg = _js_function(js, "pptxMessage")
    assert 'm.type!=="protoArtifact:"+ctx.kind' in msg
    assert '"protoArtifact:"+ctx.kind+":data"' in msg
    assert 'kind:pdfOk(v) ? "pdf" : "pptx"' in js
    assert 'm.type==="protoArtifact:pptx"||m.type==="protoArtifact:pdf"' in js
