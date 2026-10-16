"""PDF handling in ``fetch_url`` (#4294).

``fetch_url`` used to decode every non-HTML response as text, so a PDF URL
(NerdNotes, the Necromunda FAQ) came back as the raw ``%PDF-1.7`` byte
stream. These tests drive the live-network half of the tool through an
in-process httpx transport (same pattern as ``test_starter_tools.py``) and
pin the PDF contract:

- a real PDF — served as ``application/pdf`` OR sniffed from a ``%PDF-``
  body under another content-type — comes back as extracted text under the
  ``[200] <url>`` header,
- a corrupt / truncated PDF returns a readable ``Error:`` (never raises,
  never leaks ``%PDF`` bytes),
- a valid PDF with no extractable text returns a readable note.

The pure extractor (``_extract_text_from_pdf``) is also exercised directly.
"""

from __future__ import annotations

import pytest

from tools.lg_tools import _extract_text_from_pdf, fetch_url


def _make_pdf(text: str, *, draw: bool = True) -> bytes:
    """Build a minimal, valid single-page PDF whose content stream draws
    ``text``. The xref offsets are computed from the real byte positions so
    a strict parser (pypdf) accepts it. ``draw=False`` emits an empty content
    stream — a valid PDF with no extractable text (image-only/scanned proxy)."""
    header = b"%PDF-1.4\n"
    stream = b"BT /F1 24 Tf 72 700 Td (" + text.encode("latin-1") + b") Tj ET" if draw else b""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"
        ),
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    body = bytearray(header)
    offsets: list[int] = []
    for i, obj in enumerate(objs, start=1):
        offsets.append(len(body))
        body += str(i).encode() + b" 0 obj\n" + obj + b"\nendobj\n"
    xref_pos = len(body)
    n = len(objs) + 1
    body += b"xref\n0 " + str(n).encode() + b"\n"
    body += b"0000000000 65535 f \n"
    for off in offsets:
        body += ("%010d 00000 n \n" % off).encode()
    body += (
        b"trailer\n<< /Size " + str(n).encode() + b" /Root 1 0 R >>\n"
        b"startxref\n" + str(xref_pos).encode() + b"\n%%EOF"
    )
    return bytes(body)


def _mock_httpx(monkeypatch, handler):
    """Route fetch_url's AsyncClient through an in-process transport."""
    import httpx

    real = httpx.AsyncClient

    class _Client(real):
        def __init__(self, *a, **k):
            k["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **k)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)


# ── fetch_url end-to-end (mocked network) ────────────────────────────────────


@pytest.mark.asyncio
async def test_pdf_by_content_type_is_extracted(monkeypatch):
    """r1 — content-type application/pdf + a small valid PDF → the PDF's text
    under the `[200] <url>` header."""
    import httpx

    pdf = _make_pdf("Hello PDF World")

    def _handler(request):
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=pdf)

    _mock_httpx(monkeypatch, _handler)
    result = await fetch_url.ainvoke({"url": "https://example.com/doc.pdf"})
    assert result.startswith("[200] https://example.com/doc.pdf"), result
    assert "Hello PDF World" in result
    assert "%PDF" not in result


@pytest.mark.asyncio
async def test_pdf_sniffed_from_octet_stream_body(monkeypatch):
    """r2 — served as application/octet-stream but the body starts with `%PDF-`
    → still extracted, not decoded as raw bytes."""
    import httpx

    pdf = _make_pdf("Octet Stream Payload")
    assert pdf.startswith(b"%PDF-")

    def _handler(request):
        return httpx.Response(
            200, headers={"content-type": "application/octet-stream"}, content=pdf
        )

    _mock_httpx(monkeypatch, _handler)
    result = await fetch_url.ainvoke({"url": "https://example.com/mystery"})
    assert result.startswith("[200]"), result
    assert "Octet Stream Payload" in result
    assert "%PDF" not in result


@pytest.mark.asyncio
async def test_corrupt_pdf_returns_readable_error(monkeypatch):
    """r3 — a truncated PDF pypdf can't parse → a bare `Error:` string, no
    exception, and no `%PDF` bytes handed back to the model."""
    import httpx

    truncated = _make_pdf("This one gets cut off")[:40]
    assert truncated.startswith(b"%PDF-")

    def _handler(request):
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=truncated)

    _mock_httpx(monkeypatch, _handler)
    result = await fetch_url.ainvoke({"url": "https://example.com/truncated.pdf"})
    assert result.startswith("Error:"), result
    assert "could not extract text from PDF" in result
    assert "%PDF" not in result


@pytest.mark.asyncio
async def test_pdf_without_extractable_text_returns_note(monkeypatch):
    """r4 — a valid PDF with no text (image-only/scanned/encrypted proxy) →
    a readable note, not an empty body."""
    import httpx

    pdf = _make_pdf("", draw=False)

    def _handler(request):
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=pdf)

    _mock_httpx(monkeypatch, _handler)
    result = await fetch_url.ainvoke({"url": "https://example.com/scan.pdf"})
    assert result.startswith("[200]"), result
    assert "no extractable text" in result
    assert result.strip() != "[200] https://example.com/scan.pdf"


@pytest.mark.asyncio
async def test_html_path_unchanged(monkeypatch):
    """r5 — a text/html response still runs through the HTML extractor, not
    the PDF branch."""
    import httpx

    def _handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><body><main><p>Plain article body.</p></main></body></html>",
        )

    _mock_httpx(monkeypatch, _handler)
    result = await fetch_url.ainvoke({"url": "https://example.com/page"})
    assert result.startswith("[200]"), result
    assert "Plain article body." in result


# ── the pure extractor ───────────────────────────────────────────────────────


def test_extractor_returns_text_for_valid_pdf():
    out = _extract_text_from_pdf(_make_pdf("Direct Extractor Check"))
    assert "Direct Extractor Check" in out
    assert not out.startswith("Error:")


def test_extractor_errors_on_garbage_bytes():
    out = _extract_text_from_pdf(b"%PDF-1.7 not actually a pdf at all")
    assert out.startswith("Error:")
    assert "could not extract text from PDF" in out


def test_extractor_notes_empty_pdf():
    out = _extract_text_from_pdf(_make_pdf("", draw=False))
    assert out.startswith("Note:")
    assert "no extractable text" in out
