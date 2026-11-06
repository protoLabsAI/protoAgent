"""Copied and linked saves share PDF page validation and preview notes (#4761)."""

import pytest

from graph import components
from graph.components import extract_component, strip_component
from tests.test_artifact_linked import _arts, _fence, _load

pytestmark = pytest.mark.platform_sensitive

POSITIVE = "page must be a positive PDF page number (1-based), or 0 for no page hint."
PDF_ONLY = "page is only supported for a PDF. Omit page or use 0 for other files."
MISSING = "The PDF shows as text (no approved PDF preview verdict), so it can't open at a page."
REFUSED = "The PDF shows as text (the PDF is password-protected), so it can't open at a page."


@pytest.fixture
def art(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path / "store")
    components.set_plugin_components({mod._ref.ARTIFACT_REF: mod._ref.validate_artifact_ref})
    yield mod
    components.set_plugin_components(None)


@pytest.mark.parametrize("link", [False, True], ids=["copied", "linked"])
@pytest.mark.parametrize(
    "filename,page,verdict,accepted,refusal,note",
    [
        ("a.pdf", 2, {"render": True, "pages": 3}, 2, None, " — opens at page 2"),
        ("a.pdf", 3, {"render": True, "pages": 3}, 3, None, " — opens at page 3"),
        ("a.pdf", 9, {"render": True, "pages": 3}, 3, None, " — the PDF has 3 pages — opens at page 3"),
        ("a.pdf", -1, None, None, POSITIVE, ""),
        ("a.txt", 2, None, None, PDF_ONLY, ""),
        ("a.pdf", 2, {"render": False, "reason": "the PDF is password-protected"}, None, None, " — " + REFUSED),
        ("a.pdf", 9, {"render": False, "pages": 3}, None, None, " — " + MISSING),
        ("a.pdf", 2, None, None, None, " — " + MISSING),
        ("a.pdf", 2, {}, None, None, " — " + MISSING),
        ("a.pdf", 2, {"render": 1, "pages": 3}, None, None, " — " + MISSING),
        ("a.pdf", 0, None, None, None, ""),
        ("a.txt", 0, None, None, None, ""),
        ("a.pdf", None, None, None, None, ""),
    ],
)
def test_save_page_hint(art, monkeypatch, tmp_path, link, filename, page, verdict, accepted, refusal, note):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / filename).write_bytes(b"preview fixture")
    _fence(monkeypatch, art, workspace=ws)
    monkeypatch.setattr(art._pdfview, "preflight", lambda data: verdict)
    monkeypatch.setattr(art._preview, "_extract_preview", lambda *args: "extracted text")
    monkeypatch.setattr(art._preview, "_thumbnail", lambda *args: None)
    args = {"path": filename, "project": "workspace", "link": link}
    if page is not None:
        args["page"] = page
    out = art.save_file_artifact.invoke(args)
    if refusal:
        assert out == refusal
        assert _arts(art) == []
        assert extract_component(out) is None
        return

    assert ("Linked file artifact" if link else "Saved file artifact") in out
    saved, = _arts(art)
    props = extract_component(out)["props"]
    expected = {
        "artifact_id": saved["id"], "version": 1, "versions_total": 1,
        "title": filename, "kind": "file",
    }
    if accepted is not None:
        expected["page"] = accepted
    assert props == expected
    assert note in strip_component(out)
    if accepted is None:
        assert "opens at page" not in strip_component(out)
    if not note:
        assert "shows as text" not in strip_component(out)
        assert art._ref.ref_tail(saved) == art._ref.ref_tail(saved, page=0)


@pytest.mark.parametrize("kind,meta", [
    ("html", {"filename": "a.pdf", "mime": "application/pdf"}),
    ("file", None),
    ("file", {"filename": "a.txt", "mime": "text/plain"}),
])
def test_helper_refuses_other_artifacts(art, kind, meta):
    assert art._ref.page_hint(meta, kind, 1) == (None, PDF_ONLY, " — " + PDF_ONLY)
    assert art._ref.page_hint(meta, kind, -1) == (None, POSITIVE, " — " + POSITIVE)
    assert art._ref.page_hint(meta, kind, 0) == (None, None, "")


@pytest.mark.parametrize("filename,mime", [("a.PDF", "text/plain"), ("a.bin", "application/pdf")])
def test_helper_uses_pdf_filename_or_mime(art, filename, mime):
    meta = {"filename": filename, "mime": mime, "pdf": {"render": True, "pages": 2}}
    assert art._ref.page_hint(meta, "file", 1) == (1, None, " — opens at page 1")
