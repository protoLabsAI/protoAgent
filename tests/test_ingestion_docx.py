"""Word (.docx) ingestion — chat attachments, the Knowledge page, and ``knowledge_ingest``.

All three surfaces funnel through ``ingestion.extract_bytes``, so the extractor is tested
directly and then once per surface (the attach + ingest routes, and the ``ops.knowledge``
path the agent tool calls with a local file).

Real documents are generated with python-docx. It is NOT a core dependency: the frozen
desktop app bundles it (ADR 0092 D1) and CI installs it for this file (checks.yml), but a
plain server install may not have it — so every test that needs the library skips without
it, while the checks that must hold *without* it (legacy ``.doc`` refusal, the zip guard,
the missing-library → 501 path) build their inputs by hand and always run.
"""

from __future__ import annotations

import io
import sys
import types
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ingestion import (
    SUPPORTED_DESCRIPTION,
    SUPPORTED_EXTENSIONS,
    ExtractionError,
    MissingDependency,
    UnsupportedSource,
    extract_bytes,
    extract_url,
)
from ingestion import engine

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
# The OLE2 compound-file signature every legacy binary .doc starts with.
OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _docx_lib():
    return pytest.importorskip("docx")  # python-docx — bundled on desktop, installed in CI


def _save(document) -> bytes:
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def _add_hyperlink(paragraph, text: str, url: str) -> None:
    """python-docx has no public hyperlink API; build the run the way Word writes it.
    Resumes put the email / portfolio in exactly this shape."""
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    r_id = paragraph.part.relate_to(url, RT.HYPERLINK, is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), r_id)
    run = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = text
    run.append(t)
    link.append(run)
    paragraph._p.append(link)


def _wrap_in_content_control(paragraph) -> None:
    """Move a paragraph into a block-level ``w:sdt`` — how resume templates wrap sections."""
    from docx.oxml import OxmlElement

    sdt = OxmlElement("w:sdt")
    content = OxmlElement("w:sdtContent")
    paragraph._p.addprevious(sdt)
    content.append(paragraph._p)
    sdt.append(content)


def _resume_docx() -> bytes:
    docx = _docx_lib()
    d = docx.Document()
    section = d.sections[0]
    # Contact details in the page header — where most resume templates keep them. The
    # same text in a first-page header must not appear twice.
    section.header.paragraphs[0].text = "Jane Doe | jane@example.com | (555) 010-0199"
    section.different_first_page_header_footer = True
    section.first_page_header.paragraphs[0].text = "Jane Doe | jane@example.com | (555) 010-0199"
    section.footer.paragraphs[0].text = "References available on request"

    d.add_heading("Jane Doe", level=0)  # the "Title" style
    _add_hyperlink(d.add_paragraph("Portfolio: "), "janedoe.dev", "https://janedoe.dev")
    d.add_heading("Experience", level=1)
    d.add_heading("Staff Engineer, Acme", level=2)
    d.add_paragraph("Led the platform team", style="List Bullet")
    d.add_paragraph("Cut deploy time by 40%", style="List Bullet 2")
    table = d.add_table(rows=2, cols=2)
    for (row, col), text in {(0, 0): "Skill", (0, 1): "Level", (1, 0): "Python", (1, 1): "Expert"}.items():
        table.cell(row, col).text = text
    d.add_paragraph("Available immediately")
    _wrap_in_content_control(d.add_paragraph("Open to relocation"))
    return _save(d)


def _bare_zip(members: dict[str, bytes]) -> bytes:
    """A zip built without python-docx — for the checks that must run without it."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, payload in members.items():
            zf.writestr(name, payload)
    return buf.getvalue()


def _minimal_docx_zip() -> bytes:
    return _bare_zip({"[Content_Types].xml": b"<Types/>", "word/document.xml": b"<w:document/>"})


# ── the extractor ─────────────────────────────────────────────────────────────


def test_docx_keeps_headings_lists_tables_and_header_in_document_order():
    r = extract_bytes("resume.docx", _resume_docx(), DOCX_MIME)

    assert r.source_type == "docx" and r.title == "resume"
    lines = r.text.splitlines()
    expected_in_order = [
        "Jane Doe | jane@example.com | (555) 010-0199",  # header first: it sits above the body
        "# Jane Doe",  # Title → level-1 heading
        "Portfolio: janedoe.dev",  # hyperlink text kept
        "# Experience",
        "## Staff Engineer, Acme",
        "- Led the platform team",
        "  - Cut deploy time by 40%",  # List Bullet 2 → nested
        "Skill | Level",  # table rows, cell by cell
        "Python | Expert",
        "Available immediately",
        "Open to relocation",  # inside a content control
        "References available on request",  # footer last
    ]
    for line in expected_in_order:
        assert line in lines, f"missing {line!r} in:\n{r.text}"
    positions = [lines.index(line) for line in expected_in_order]
    assert positions == sorted(positions), r.text
    assert r.text.count("jane@example.com") == 1  # identical first-page header deduped


def test_docx_layout_table_cells_read_as_flowing_blocks():
    """Resumes use tables for LAYOUT (dates | description with bullets). Squashing a
    multi-paragraph cell onto one ``a | b`` row would lose the bullets, so such a row is
    read cell by cell instead."""
    docx = _docx_lib()
    d = docx.Document()
    table = d.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "2020 - 2024"
    right = table.cell(0, 1)
    right.paragraphs[0].text = "Acme Corp"
    right.add_paragraph("Shipped the billing rewrite", style="List Bullet")

    lines = extract_bytes("layout.docx", _save(d)).text.splitlines()

    assert lines == ["2020 - 2024", "Acme Corp", "- Shipped the billing rewrite"]


def test_docx_text_box_is_read_once_despite_its_legacy_fallback():
    """Resume templates put a sidebar (LinkedIn, phone) in a text box. Word writes it
    twice — a modern DrawingML shape and a VML ``mc:Fallback`` copy — and only one may
    reach the text. A soft line break inside a list item stays inside the item."""
    docx = _docx_lib()
    from docx.oxml import parse_xml

    d = docx.Document()
    box = (
        "<w:txbxContent><w:p><w:r><w:t>linkedin.com/in/jane</w:t></w:r></w:p></w:txbxContent>"
    )
    anchor = parse_xml(
        '<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
        ' xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'
        ' xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"'
        ' xmlns:v="urn:schemas-microsoft-com:vml">'
        "<w:r><w:t>Sidebar</w:t></w:r><w:r><mc:AlternateContent>"
        f'<mc:Choice Requires="wps"><w:drawing><wps:wsp><wps:txbx>{box}</wps:txbx></wps:wsp></w:drawing></mc:Choice>'
        f"<mc:Fallback><w:pict><v:shape><v:textbox>{box}</v:textbox></v:shape></w:pict></mc:Fallback>"
        "</mc:AlternateContent></w:r></w:p>"
    )
    d.element.body.insert(0, anchor)
    item = d.add_paragraph("Python", style="List Bullet")
    item.runs[0].add_break()
    item.add_run("8 years")

    text = extract_bytes("sidebar.docx", _save(d)).text

    assert text.count("linkedin.com/in/jane") == 1, text
    assert text.splitlines() == ["Sidebar", "linkedin.com/in/jane", "- Python", "  8 years"]


def test_docx_dispatches_on_content_type_without_an_extension():
    r = extract_bytes("upload", _resume_docx(), DOCX_MIME)
    assert r.source_type == "docx" and "## Staff Engineer, Acme" in r.text


def test_docx_extension_wins_over_a_generic_content_type():
    # Some browsers/OSes send .docx as application/octet-stream (or nothing at all).
    r = extract_bytes("Resume.DOCX", _resume_docx(), "application/octet-stream")
    assert r.source_type == "docx"


def test_docx_url_dispatches_on_content_type():
    data = _resume_docx()
    r = extract_url("https://example.com/files/resume", fetch=lambda _u: (data, DOCX_MIME))
    assert r.source_type == "docx" and "Python | Expert" in r.text


def test_valid_zip_that_is_not_a_word_document_is_an_extraction_error():
    _docx_lib()
    with pytest.raises(ExtractionError, match="could not parse DOCX"):
        extract_bytes("resume.docx", _minimal_docx_zip())


def test_docx_that_is_not_a_zip_is_an_extraction_error():
    with pytest.raises(ExtractionError, match="not a valid .docx"):
        extract_bytes("resume.docx", b"definitely not a zip archive")


def test_docx_without_python_docx_is_a_missing_dependency_naming_the_fix(monkeypatch):
    monkeypatch.setitem(sys.modules, "docx", None)  # import docx → ImportError
    with pytest.raises(MissingDependency) as exc:
        extract_bytes("resume.docx", _minimal_docx_zip(), DOCX_MIME)
    msg = str(exc.value)
    assert "python-docx" in msg and "pip install python-docx" in msg
    assert "PDF" in msg  # a workaround the operator can use right now


@pytest.mark.parametrize(
    ("name", "ctype"),
    [
        ("resume.doc", "application/msword"),
        ("resume.doc", None),
        ("upload", "application/msword"),
        ("resume.docx", DOCX_MIME),  # a legacy file renamed .docx — sniffed by its signature
    ],
)
def test_legacy_word_doc_is_rejected_with_a_resave_hint(name, ctype):
    with pytest.raises(UnsupportedSource, match=r"(?i)legacy.*\.doc.*\.docx"):
        extract_bytes(name, OLE2_MAGIC + b"\x00" * 504, ctype)


def test_password_protected_docx_is_not_mistaken_for_a_legacy_doc():
    """Office stores an encrypted .docx in the same OLE2 container as a legacy .doc, so
    the signature alone would send the operator off to re-save a file that is already a
    .docx. The encrypted package's stream name tells the two apart."""
    encrypted = OLE2_MAGIC + b"\x00" * 504 + "EncryptedPackage".encode("utf-16-le")
    with pytest.raises(UnsupportedSource, match="password-protected") as exc:
        extract_bytes("resume.docx", encrypted, DOCX_MIME)
    assert "legacy" not in str(exc.value)


def test_docx_that_inflates_past_the_cap_is_refused_before_parsing(monkeypatch):
    """A .docx is a zip: a few KB on the wire can declare gigabytes of XML. The declared
    uncompressed total is checked before python-docx is ever handed the bytes."""
    monkeypatch.setattr(engine, "_MAX_DOCX_UNCOMPRESSED_BYTES", 64 * 1024)
    data = _bare_zip({"[Content_Types].xml": b"<Types/>", "word/document.xml": b"\0" * (1024 * 1024)})
    assert len(data) < 16 * 1024  # tiny compressed — that is the whole trick
    fake = types.ModuleType("docx")
    fake.Document = lambda *_a, **_k: pytest.fail("python-docx was handed a zip bomb")
    monkeypatch.setitem(sys.modules, "docx", fake)

    with pytest.raises(ExtractionError, match="expands to"):
        extract_bytes("bomb.docx", data)


def test_docx_with_too_many_members_is_refused_before_parsing(monkeypatch):
    monkeypatch.setattr(engine, "_MAX_DOCX_MEMBERS", 3)
    data = _bare_zip({f"word/part{i}.xml": b"<x/>" for i in range(5)})
    fake = types.ModuleType("docx")
    fake.Document = lambda *_a, **_k: pytest.fail("python-docx was handed an oversized archive")
    monkeypatch.setitem(sys.modules, "docx", fake)

    with pytest.raises(ExtractionError, match="members"):
        extract_bytes("many.docx", data)


def test_supported_types_advertise_docx_but_not_legacy_doc():
    assert ".docx" in SUPPORTED_EXTENSIONS and ".doc" not in SUPPORTED_EXTENSIONS
    assert "docx" in SUPPORTED_DESCRIPTION.lower()
    # The 415 message is how the API reports what it accepts.
    with pytest.raises(UnsupportedSource) as exc:
        extract_bytes("blob.bin", b"\x00\x01\x02\x03")
    assert "docx" in str(exc.value).lower()


# ── the surfaces: chat attach, Knowledge ingest, and the agent's knowledge_ingest ──


class _KS:
    """Store double exposing add_document (returns N ids to mimic chunking)."""

    def __init__(self):
        self.docs: list[tuple[str, dict]] = []
        self._chunk_max_chars = 1200
        self._chunk_overlap_chars = 150
        self._chunk_min_chars = 200

    def add_document(self, content, **kw):
        self.docs.append((content, kw))
        return [101, 102]


def _client(monkeypatch, ks, *, budget=8000):
    import runtime.state as rs
    from operator_api.knowledge_routes import register_knowledge_routes

    monkeypatch.setattr(rs.STATE, "knowledge_store", ks, raising=False)
    monkeypatch.setattr(rs.STATE, "skills_index", None, raising=False)
    monkeypatch.setattr(
        rs.STATE, "graph_config", types.SimpleNamespace(knowledge_attach_inline_budget=budget), raising=False
    )
    app = FastAPI()
    register_knowledge_routes(app)
    return TestClient(app)


def test_chat_attach_inlines_a_docx_resume(monkeypatch):
    ks = _KS()
    c = _client(monkeypatch, ks)
    files = {"file": ("resume.docx", _resume_docx(), DOCX_MIME)}

    resp = c.post("/api/knowledge/attach", data={"session_id": "s1"}, files=files)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode"] == "inline" and body["source_type"] == "docx" and body["name"] == "resume.docx"
    ctx = body["context"]
    assert ctx.startswith("[Attached file: resume.docx]")
    assert "jane@example.com" in ctx and "## Staff Engineer, Acme" in ctx and "Python | Expert" in ctx
    assert ks.docs == []  # small → inlined, nothing indexed


def test_chat_attach_docx_without_python_docx_is_501_with_the_fix(monkeypatch):
    monkeypatch.setitem(sys.modules, "docx", None)
    c = _client(monkeypatch, _KS())
    files = {"file": ("resume.docx", _minimal_docx_zip(), DOCX_MIME)}

    resp = c.post("/api/knowledge/attach", data={"session_id": "s1"}, files=files)

    assert resp.status_code == 501, resp.text
    assert "pip install python-docx" in resp.json()["detail"]


def test_chat_attach_legacy_doc_is_415_with_a_resave_hint(monkeypatch):
    c = _client(monkeypatch, _KS())
    files = {"file": ("resume.doc", OLE2_MAGIC + b"\x00" * 504, "application/msword")}

    resp = c.post("/api/knowledge/attach", data={"session_id": "s1"}, files=files)

    assert resp.status_code == 415
    assert ".docx" in resp.json()["detail"] and "legacy" in resp.json()["detail"].lower()


def test_knowledge_page_ingest_accepts_a_docx(monkeypatch):
    ks = _KS()
    c = _client(monkeypatch, ks)
    files = {"file": ("resume.docx", _resume_docx(), DOCX_MIME)}

    body = c.post("/api/knowledge/ingest", files=files).json()

    assert body["enabled"] is True and body["source_type"] == "docx"
    content, kw = ks.docs[0]
    assert "# Experience" in content and kw["source"] == "resume.docx" and kw["source_type"] == "docx"


def test_knowledge_ingest_docx_without_python_docx_is_501(monkeypatch):
    monkeypatch.setitem(sys.modules, "docx", None)
    c = _client(monkeypatch, _KS())
    files = {"file": ("resume.docx", _minimal_docx_zip(), DOCX_MIME)}
    assert c.post("/api/knowledge/ingest", files=files).status_code == 501


async def test_knowledge_ingest_tool_path_reads_a_local_docx(tmp_path):
    """``knowledge_ingest`` hands the op a local PATH (no content type) — the extension
    alone has to route it."""
    from ops import OpContext
    from ops.knowledge import IngestSource, ingest

    path = tmp_path / "resume.docx"
    path.write_bytes(_resume_docx())
    ks = _KS()

    res = await ingest(IngestSource.from_path(str(path)), ctx=OpContext(knowledge_store=ks, graph_config=None))

    assert res.source_type == "docx" and res.title == "resume"
    assert "- Led the platform team" in ks.docs[0][0]
