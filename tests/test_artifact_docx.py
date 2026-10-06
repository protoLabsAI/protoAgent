"""Page previews for .docx file artifacts (vendored docx-preview on JSZip) and the typed table
previews for .csv/.tsv/.xlsx — preview selection, the save-time preflight, the frame's lockdown,
and the table helpers (run under ``node`` where it's on PATH; those cases skip without node)."""

from __future__ import annotations

import io
import json
import re
import struct
import zipfile
import zlib

import pytest

from tests.test_artifact_plugin import ROOT, _app, _arts, _load
from tests.test_artifact_slides import _js, _js_function, _node

# node subprocesses → platform-sensitive (tests/test_platform_sensitive_marks.py)
pytestmark = pytest.mark.platform_sensitive

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _png(w: int, h: int) -> bytes:
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\0" * 8)) + chunk(b"IEND", b"")


def _real_docx(tmp_path) -> bytes:
    from docx import Document  # core dep

    d = Document()
    d.add_heading("Quarterly report", 1)
    d.add_paragraph("Body text with ").add_run("bold").bold = True
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text, t.cell(1, 0).text, t.cell(1, 1).text = "a", "b", "1", "2"
    p = tmp_path / "real.docx"
    d.save(str(p))
    return p.read_bytes()


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n, b in entries.items():
            z.writestr(n, b)
    return buf.getvalue()


# ── preflight ──────────────────────────────────────────────────────────────────


def test_preflight_passes_a_real_document(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    v = art._docx.preflight(_real_docx(tmp_path))
    # python-docx's template carries docProps/thumbnail.jpeg — measured by content like any image
    assert v == {"render": True, "reason": "", "images": 1}


def test_preflight_counts_images_within_budget(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    data = _zip({"word/document.xml": b"<w:document/>", "word/media/image1.png": _png(640, 480)})
    assert art._docx.preflight(data) == {"render": True, "reason": "", "images": 1}


@pytest.mark.parametrize(
    "data,reason",
    [
        (b"not a zip at all", "bad zip"),
        (_zip({"ppt/presentation.xml": b"<p/>"}), "not a Word document"),
    ],
)
def test_preflight_refuses_non_documents(monkeypatch, tmp_path, data, reason):
    art = _load(monkeypatch, tmp_path)
    v = art._docx.preflight(data)
    assert v["render"] is False and reason in v["reason"]


def test_preflight_refuses_a_giant_image_without_decoding_it(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    data = _zip({"word/document.xml": b"<w:document/>", "word/media/huge.png": _png(10_000, 10_000)})
    v = art._docx.preflight(data)
    assert v["render"] is False and "MP" in v["reason"]


def test_preflight_has_a_whole_document_pixel_budget(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    monkeypatch.setattr(art._docx, "MAX_DOC_PIXELS", 500_000)
    imgs = {f"word/media/i{n}.png": _png(640, 480) for n in range(3)}  # ~0.92 MP together
    v = art._docx.preflight(_zip({"word/document.xml": b"<w:document/>", **imgs}))
    assert v["render"] is False and "images total" in v["reason"]


def test_preflight_refuses_a_zip_bomb_by_what_it_actually_inflates(monkeypatch, tmp_path):
    """The per-entry cap is enforced on the real inflated bytes (shared with the slide preflight)."""
    art = _load(monkeypatch, tmp_path)
    monkeypatch.setattr(art._slides, "MAX_ENTRY_BYTES", 1024 * 1024)
    data = _zip({"word/document.xml": b"<w:document>" + b" " * (4 * 1024 * 1024) + b"</w:document>"})
    v = art._docx.preflight(data)
    assert v["render"] is False and ("per-file cap" in v["reason"] or "compression ratio" in v["reason"]), v


def test_preflight_refuses_an_oversized_file(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    monkeypatch.setattr(art._docx, "MAX_BYTES", 100)
    v = art._docx.preflight(_real_docx(tmp_path))
    assert v["render"] is False and "preview cap" in v["reason"]


@pytest.mark.parametrize(
    "name,mime,want",
    [
        ("report.docx", DOCX_MIME, True),
        ("REPORT.DOCX", "application/octet-stream", True),
        ("download", DOCX_MIME, True),
        ("old.doc", "application/msword", False),  # legacy binary Word: text card only
        ("deck.pptx", "x", False),
    ],
)
def test_is_docx_by_extension_or_mime(monkeypatch, tmp_path, name, mime, want):
    art = _load(monkeypatch, tmp_path)
    assert art._docx.is_docx(name, mime) is want


def test_save_file_artifact_stamps_the_docx_verdict(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    f = tmp_path / "report.docx"
    f.write_bytes(_real_docx(tmp_path))
    assert "Saved file artifact" in art.save_file_artifact.invoke({"path": str(f)})
    meta = _arts(art)[0]["versions"][0]["file"]
    assert meta["docx"] == {"render": True, "reason": "", "images": 1}  # the template's thumbnail
    assert "pdf" not in meta and "slides" not in meta


# ── vendored renderer ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("name,marker", [("jszip.min.js", b"JSZip"), ("docx-preview.min.js", b".docx={}")])
def test_renderer_is_vendored_and_served_same_origin(monkeypatch, tmp_path, name, marker):
    from fastapi.testclient import TestClient

    art = _load(monkeypatch, tmp_path)
    assert name in art._VENDOR_FILES
    r = TestClient(_app(art)).get(f"/plugins/artifact/vendor/{name}")
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "*"
    assert marker in r.content
    notices = (ROOT / "vendor" / "docx-preview.LICENSES.txt").read_text(encoding="utf-8")
    assert "Apache License" in notices and "Permission is hereby granted" in notices  # Apache-2.0 + MIT


# ── preview selection + the frame ─────────────────────────────────────────────


def test_shell_routes_docx_and_xlsx(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    cases = [
        ["report.docx", DOCX_MIME],
        ["REPORT.DOCX", "application/octet-stream"],
        ["download", DOCX_MIME],
        ["old.doc", "application/msword"],
        ["book.xlsx", "x"],
        ["data.csv", "text/csv"],
    ]
    got = _node(
        'var PPTX_MIME="p";var DOCX_MIME="' + DOCX_MIME + '";'
        + _js_function(_js(art), "previewKind")
        + "console.log(JSON.stringify("
        + json.dumps(cases)
        + ".map(function(c){return previewKind(c[0],c[1]);})));"
    )
    assert got == ["docx", "docx", "docx", "text", "sheets", "table"]
    for (name, mime), kind in zip(cases, got):
        assert (kind == "docx") == art._docx.is_docx(name, mime), name


def test_shell_honours_a_refused_docx_preflight(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    js = _js(art)
    versions = [
        {"file": {"filename": "a.docx", "mime": DOCX_MIME, "docx": {"render": True}}},
        {"file": {"filename": "a.docx", "mime": DOCX_MIME, "docx": {"render": False, "reason": "bomb"}}},
        {"file": {"filename": "a.docx", "mime": DOCX_MIME}},
        {"file": {"filename": "a.pdf", "mime": "application/pdf", "docx": {"render": True}}},
    ]
    got = _node(
        'var PPTX_MIME="p";var DOCX_MIME="' + DOCX_MIME + '";'
        + _js_function(js, "previewKind")
        + _js_function(js, "docxOk")
        + "console.log(JSON.stringify("
        + json.dumps(versions)
        + ".map(docxOk)));"
    )
    assert got == [True, False, False, False]
    card = _js_function(js, "fileCard")
    assert "if(docxOk(v)) return docxDoc(v);" in card
    assert "saved before document previews" in card


def test_docx_frame_is_locked_down(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    js = _js(art)
    src = _js_function(js, "docxDoc")
    for directive in ("default-src 'none'", "script-src 'nonce-", "connect-src 'none'", "worker-src 'none'", "frame-src 'none'"):
        assert directive in src, directive
    assert "unsafe-eval" not in src
    assert 'cdn("jszip", nonce) + cdn("docxPreview", nonce)' in src  # JSZip first: docx-preview reads window.JSZip
    ctl = _js_function(js, "artDocx")
    assert "</" not in ctl and "<!" not in ctl
    assert "renderAltChunks:false" in ctl  # an altChunk is raw embedded HTML — never rendered
    assert "renderComments:false" in ctl
    assert "tidy(doc)" in ctl  # only http(s) / in-document links keep an href
    # sharp text on WKWebView: CSS zoom (re-layout), never a scaling transform (#1517)
    assert "style.zoom" in ctl and "style.transform" not in ctl and "scale(" not in ctl
    assert "caps.parseMs" in ctl and "the document renderer didn't load" in ctl


def test_docx_caps_mirror_python(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    block = re.search(r"var DOCX_CAPS=\{(.*?)\};", _js(art), re.S).group(1)
    caps = {k: int(v) for k, v in re.findall(r"(\w+):\s*(\d+)", block)}
    assert caps["maxBytes"] == art._docx.MAX_BYTES
    assert 0 < caps["parseMs"] < caps["watchdogMs"]
    feed = _js_function(_js(art), "feedCaps")
    assert 'kind==="docx" ? DOCX_CAPS' in feed


# ── tables: csv / tsv / xlsx ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,want",
    [
        ("a,b,c\n1,2,3", ","),
        ("a;b;c\n1;2;3", ";"),  # European Excel export
        ("a\tb\tc\n1\t2\t3", "\t"),
        ("a|b|c\n1|2|3", "|"),
        ('"x;y",b,c\n1,2,3', ","),  # a ; inside quotes doesn't count
        ("\n\nname,qty\nwidget,3", ","),
    ],
)
def test_csv_delimiter_is_sniffed(monkeypatch, tmp_path, text, want):
    art = _load(monkeypatch, tmp_path)
    got = _node(_js_function(_js(art), "sniffDelim") + "console.log(JSON.stringify(sniffDelim(" + json.dumps(text) + ")));")
    assert got == want


def test_csv_table_right_aligns_numeric_columns(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    js = _js(art)
    rows = [["item", "qty", "price", "note"], ["Lasgun", "3", "$15.00", ""], ["Autogun", "12", "(1,200)", "x"]]
    got = _node(
        'function esc(s){return String(s);}var TABLE_MAX_ROWS=500;'
        + _js_function(js, "dsvTable")
        + "var t=dsvTable("
        + json.dumps(rows)
        + ",false);console.log(JSON.stringify([t.label, (t.html.match(/<th class=\"n\">/g)||[]).length, (t.html.match(/<td class=\"n\">/g)||[]).length]));"
    )
    assert got == ["4 columns × 2 rows", 2, 4]  # qty + price numeric; item + note not


def test_csv_strips_a_byte_order_mark(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    card = _js_function(_js(art), "fileCard")
    assert "replace(/^\\uFEFF/" in card


def test_xlsx_preview_is_csv_per_sheet(monkeypatch, tmp_path):
    openpyxl = pytest.importorskip("openpyxl")  # bundled in the desktop app; optional on a server
    art = _load(monkeypatch, tmp_path)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Gangs"
    ws.append(["name", "credits", "note"])
    ws.append(["Goliath", 1000, "big, strong"])
    wb.create_sheet("Empty").append(["only"])
    p = tmp_path / "book.xlsx"
    wb.save(str(p))
    lines = art._preview._extract_xlsx(p).splitlines()
    assert lines[0] == "### sheet: Gangs"
    assert lines[2] == 'Goliath,1000,"big, strong"'  # a comma inside a cell stays one cell (CSV quoting)
    assert "### sheet: Empty" in lines


def test_shell_splits_the_xlsx_preview_into_one_table_per_sheet(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    preview = '### sheet: Gangs\nname,credits,note\nGoliath,1000,"big, strong"\n### sheet: Empty\nonly'
    got = _node(
        _js_function(_js(art), "splitSheets") + "console.log(JSON.stringify(splitSheets(" + json.dumps(preview) + ")));"
    )
    assert [s["name"] for s in got] == ["Gangs", "Empty"]
    assert got[0]["csv"].splitlines()[1] == 'Goliath,1000,"big, strong"'
    assert got[1]["csv"] == "only"


def test_word_symbol_font_bullets_become_real_unicode(monkeypatch, tmp_path):
    """Word stores bullets as private-use code points for the Symbol/Wingdings fonts (U+F0B7 …),
    which browsers don't have — they'd draw as boxes. The frame swaps them after rendering."""
    art = _load(monkeypatch, tmp_path)
    ctl = _js_function(_js(art), "artDocx")
    assert "fixGlyphs(dstyle); fixGlyphs(doc);" in ctl
    m = re.search(r"var GLYPHS=(\{.*?\});", ctl, re.S)
    glyphs = json.loads(m.group(1).replace("\\\\", "\\"))
    assert glyphs[""] == "•"  # Symbol bullet → •
    assert glyphs[""] == "▪"  # Wingdings square → ▪


def test_a_dash_does_not_stop_a_column_reading_as_numbers(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    rows = [["Weapon", "Str", "AP"], ["Lasgun", "3", "-"], ["Meltagun", "8", "-4"]]
    got = _node(
        'function esc(s){return String(s);}var TABLE_MAX_ROWS=500;'
        + _js_function(_js(art), "dsvTable")
        + "var t=dsvTable(" + json.dumps(rows) + ",false);"
        + "console.log(JSON.stringify((t.html.match(/<th class=\"n\">/g)||[]).length));"
    )
    assert got == 2  # Str and AP


def test_preflight_measures_images_by_content_not_name(monkeypatch, tmp_path):
    """A raster hidden outside word/media/ or without an image extension is still measured."""
    art = _load(monkeypatch, tmp_path)
    data = _zip({"word/document.xml": b"<w:document/>", "customXml/blob.bin": _png(10_000, 10_000)})
    v = art._docx.preflight(data)
    assert v["render"] is False and "MP" in v["reason"]


def test_a_cell_that_looks_like_a_sheet_marker_cannot_split_a_sheet(monkeypatch, tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    art = _load(monkeypatch, tmp_path)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Real"
    ws.append(["### sheet: Fake"])
    ws.append(["after"])
    p = tmp_path / "trap.xlsx"
    wb.save(str(p))
    preview = art._preview._extract_xlsx(p)
    got = _node(_js_function(_js(art), "splitSheets") + "console.log(JSON.stringify(splitSheets(" + json.dumps(preview) + ")));")
    assert [s["name"] for s in got] == ["Real"]
    assert got[0]["csv"].splitlines()[0] == '"### sheet: Fake"'
