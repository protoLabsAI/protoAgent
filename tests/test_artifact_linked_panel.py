"""Panel view for LINKED file artifacts (#4370 PANEL half, the last slice; ADR 0092 D2 extension).

A linked version (``save_file_artifact(link=True)``) stores no blob: its bytes are the LIVE project
file, served in place through the blob route and re-checked against the fs fence on every serve. The
panel trusts ONLY the server's per-serve verdict — 200 renders, 403 means the fence failed, 409
means the on-disk bytes changed or the file is gone. It never resolves a path itself.

The shell's JS is exercised the way ``tests/test_artifact_pdf.py`` does it: helpers (``linkBanner``
/ ``linkStatusMsg`` / ``pdfOk`` / ``fileCard``) run under ``node`` where it's on PATH, so the code
paths are checked as code, not just as strings; those cases skip cleanly without node. The rest are
source assertions on the exact 403/409 wording and the wiring that routes them."""

from __future__ import annotations

import json
import re

import pytest

from tests.test_artifact_plugin import _load
from tests.test_artifact_slides import _js, _js_function, _node

# node subprocesses → platform-sensitive (tests/test_platform_sensitive_marks.py)
pytestmark = pytest.mark.platform_sensitive

# The two card messages, verbatim — the whole point of r2/r3 is this exact wording.
CHANGED_MSG = "This file changed or is missing since it was linked. Re-link it to view the current file."
GONE_MSG = "This linked file is no longer reachable (its project was unregistered or the path left the project)."

# A linked PDF over the 25 MB copy cap but under the 40 MB page-preview cap (r1): 26 MB.
_LINKED_PDF = {
    "file": {"filename": "big.pdf", "mime": "application/pdf", "size": 26 * 1024 * 1024,
             "pdf": {"render": True, "pages": 40}},
    "link": {"project": "app", "path": "docs/big.pdf", "size": 26 * 1024 * 1024},
    "code": "EXTRACTED PREVIEW TEXT",
}


def _const_lines(js: str, needle: str) -> str:
    return "\n".join(line for line in js.splitlines() if needle in line)


def _run_filecard(js: str, version: dict, note: str) -> str:
    """Render ``fileCard(version, note)`` under node with minimal DOM stubs (no browser)."""
    stubs = (
        "var document={documentElement:{}};"
        'function getComputedStyle(){return {getPropertyValue:function(){return "";}};}'
        'var PPTX_MIME="x";var DOCX_MIME="y";'
    )
    deps = (
        _const_lines(js, "var TRUNC_MARK=")
        + _js_function(js, "esc")
        + _js_function(js, "fmtSize")
        + _js_function(js, "previewKind")
        + _js_function(js, "stripTrunc")
        + _js_function(js, "linkOf")
        + _js_function(js, "linkBanner")
        + _js_function(js, "fileCard")
    )
    return _node(
        stubs + deps
        + "var v=" + json.dumps(version) + ";"
        + "console.log(JSON.stringify(fileCard(v, " + json.dumps(note) + ")));"
    )


# ── the read-only banner (r1) ───────────────────────────────────────────────────


def test_link_banner_names_the_project_path_and_reads_read_only(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    js = _js(art)
    got = _node(
        _js_function(js, "esc") + _js_function(js, "linkOf") + _js_function(js, "linkBanner")
        + "console.log(JSON.stringify(["
        + '  linkBanner({link:{project:"app",path:"docs/big.pdf"}},"#fff","#999","#ccc"),'
        + '  linkBanner({file:{}},"#fff","#999","#ccc")'
        + "]));"
    )
    linked, plain = got
    assert "Linked" in linked and "app/docs/big.pdf" in linked and "read-only" in linked
    assert plain == ""  # a copied (non-linked) version gets no banner


def test_file_card_and_every_page_view_inject_the_banner(monkeypatch, tmp_path):
    """The label rides the generic file card AND each real-page/slide renderer, so a linked PDF
    that renders pages still shows it's a read-only reference."""
    art = _load(monkeypatch, tmp_path)
    js = _js(art)
    for name in ("fileCard", "pdfDoc", "slidesDoc", "docxDoc"):
        assert "linkBanner(" in _js_function(js, name), name


def test_linked_pdf_over_25mb_renders_pages_with_the_banner(monkeypatch, tmp_path):
    """r1: a linked PDF with a cleared preflight routes to the page renderer (pdfOk → pdfDoc),
    the feed cap is the 40 MB page cap (not the 25 MB copy cap), and pdfDoc carries the banner."""
    art = _load(monkeypatch, tmp_path)
    js = _js(art)
    ok = _node(
        'var PPTX_MIME="x";var DOCX_MIME="y";'
        + _js_function(js, "previewKind") + _js_function(js, "pdfOk")
        + "console.log(JSON.stringify(pdfOk(" + json.dumps(_LINKED_PDF) + ")));"
    )
    assert ok is True  # a cleared verdict → the page renderer, link or not
    card = _js_function(js, "fileCard")
    assert "if(pdfOk(v)) return pdfDoc(v);" in card  # linked pdf → real pages
    assert "linkBanner(v," in _js_function(js, "pdfDoc")  # the banner on the page view
    # the byte feed caps on the 40 MB page cap, which a 26 MB linked file is under
    block = re.search(r"var PDF_CAPS=\{(.*?)\};", js, re.S).group(1)
    max_bytes = int(re.search(r"maxBytes:\s*(\d+)", block).group(1))
    assert max_bytes == 41943040 and _LINKED_PDF["file"]["size"] < max_bytes
    assert "feedCaps(ctx.kind).maxBytes" in _js_function(js, "pptxBytes")


# ── 409 / 403 → a clear card (r2, r3) ───────────────────────────────────────────


def test_status_messages_map_409_to_changed_and_403_to_gone(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    js = _js(art)
    got = _node(
        _const_lines(js, "LINK_CHANGED_MSG=") + _const_lines(js, "LINK_GONE_MSG=")
        + _js_function(js, "linkStatusMsg")
        + "console.log(JSON.stringify([linkStatusMsg(409),linkStatusMsg(403),linkStatusMsg(200)]));"
    )
    assert got == [CHANGED_MSG, GONE_MSG, ""]
    # the exact wording lives in the shell (acceptance criteria r2/r3)
    assert CHANGED_MSG in js and GONE_MSG in js


def test_409_card_shows_the_message_over_extracted_text_and_draws_no_pages(monkeypatch, tmp_path):
    """r2: the changed/missing card carries the message and the stored extracted preview, and —
    because a note forces the text card — pdf.js never runs, so no pages are drawn."""
    art = _load(monkeypatch, tmp_path)
    out = _run_filecard(_js(art), _LINKED_PDF, CHANGED_MSG)
    assert CHANGED_MSG in out
    assert "Extracted text" in out and "EXTRACTED PREVIEW TEXT" in out  # the stored preview, below
    assert "Linked" in out and "app/docs/big.pdf" in out  # still labelled read-only
    # no pages: none of the page renderer's markers appear (stale bytes are never shown)
    for marker in ("pdfjsLib", "artPdf", "Rendering pages"):
        assert marker not in out, marker


def test_403_card_shows_the_not_reachable_message(monkeypatch, tmp_path):
    """r3: a 403 renders the 'no longer reachable' card, same shape as the 409 one."""
    art = _load(monkeypatch, tmp_path)
    out = _run_filecard(_js(art), _LINKED_PDF, GONE_MSG)
    assert GONE_MSG in out
    assert "EXTRACTED PREVIEW TEXT" in out
    for marker in ("pdfjsLib", "artPdf", "Rendering pages"):
        assert marker not in out, marker


def test_byte_feed_routes_a_linked_403_or_409_to_the_fallback_card(monkeypatch, tmp_path):
    """The blob feed maps a linked version's 403/409 to the shell's own card (fileCard over the
    extracted text), NOT an in-frame error — so the renderer never draws unverifiable bytes."""
    art = _load(monkeypatch, tmp_path)
    js = _js(art)
    feed = _js_function(js, "pptxBytes")
    assert "linkOf(ctx.v) ? linkStatusMsg(r.status)" in feed
    assert "le.fallback=true" in feed  # a fallback-flagged error, not a generic fetch failure
    msg = _js_function(js, "pptxMessage")
    assert "if(err && err.fallback){ pptxFallback(ctx, String(err.message)); return; }" in msg
    assert "fileCard(ctx.v, note)" in _js_function(js, "pptxFallback")  # the card + stored text
    # a non-linked fetch failure still falls through to the generic message
    assert 'throw new Error("the file couldn\'t be fetched ("+r.status+")")' in feed


# ── Download uses the same route and the same two messages (r2, r3) ─────────────


def test_download_flashes_the_same_two_messages_on_403_409(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    js = _js(art)
    # the download handler reads the blob route (same route as the feed) and maps linked 403/409
    assert "(!r.ok && linkOf(v)) ? linkStatusMsg(r.status)" in js
    assert 'if(lm){ dlFlash("Unavailable", lm); return; }' in js  # the message on the aria-live flash
    # the generic failure path is untouched — any other non-2xx still flashes "Download failed"
    assert 'catch(e){ dlFlash("Failed","Download failed"); }' in js
    # the route the download uses is the gated blob route (ADR 0092 D2)
    assert '"/api/plugins/artifact/artifact/"+encodeURIComponent(a.id)+"/blob?version="' in js


# ── revision affordances hidden for a linked artifact (r4) ──────────────────────


def test_version_controls_are_hidden_for_a_linked_artifact(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    js = _js(art)
    render = _js_function(js, "render")
    assert '$vnav.style.display = a.linked ? "none" : "";' in render  # the version stepper is gone
    # the stepper markup is a single #vnav span wrapping prev / label / next
    html = art._SHELL_HTML
    assert 'id="vnav"' in html
    vnav = html[html.index('id="vnav"'): html.index('id="vnav"') + 400]
    for ctrl in ('id="vprev"', 'id="vlabel"', 'id="vnext"'):
        assert ctrl in vnav, ctrl
    # editing is already hidden for any file artifact (not re-enabled for linked)
    assert '$edit.style.display = a.kind==="file" ? "none"' in render
