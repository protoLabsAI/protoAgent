"""Source → text extractors for the ingestion engine (ADR 0021).

Pure-Python, dependency-light. Network (URL fetch) and optional deps (pypdf,
python-docx, youtube-transcript-api) are isolated to their own extractors so the
parsing helpers (HTML→text, YouTube-id parsing, decode) stay unit-testable offline.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlparse

log = logging.getLogger(__name__)

# A page fetched from the web shouldn't be allowed to be unbounded. Generous —
# media (audio/video) URLs are larger than HTML; the operator route is trusted.
_MAX_FETCH_BYTES = 100 * 1024 * 1024  # 100 MB
_FETCH_TIMEOUT_S = 60.0
_FETCH_UA = "protoAgent/0.1 (+https://github.com/protoLabsAI/protoAgent)"
# ffmpeg audio-extraction (video → audio) is bounded so a pathological file can't
# wedge an ingest thread.
_FFMPEG_TIMEOUT_S = 600.0
# Cap redirect chains so a fetched URL can't bounce us toward an internal target
# after the initial egress check — every hop is re-checked. See _http_fetch.
_MAX_REDIRECTS = 5


class IngestionError(Exception):
    """Base for ingestion failures (kept distinct so routes can map to HTTP codes)."""


class UnsupportedSource(IngestionError):
    """The file type / URL isn't something we know how to extract."""


class ExtractionError(IngestionError):
    """The source is a known type but yielded no usable text."""


class MissingDependency(IngestionError):
    """A format needs an optional package that isn't installed."""


class SourceTooLarge(ExtractionError):
    """The source would cost more to extract than the engine allows (routes answer 413)."""


@dataclass
class ExtractResult:
    """Extracted text plus light provenance for the knowledge chunk."""

    text: str
    title: str | None = None
    source_type: str = "text"
    meta: dict = field(default_factory=dict)


# Extension → kind. content_type sniffing supplements this for URL/upload paths.
_TEXT_EXTS = {".txt", ".text", ".log", ".rst", ".csv", ".tsv"}
_MD_EXTS = {".md", ".markdown", ".mdown", ".mkd", ".mdx"}
_HTML_EXTS = {".html", ".htm", ".xhtml"}
_PDF_EXTS = {".pdf"}
# Word → text via python-docx (lazy; NOT a core dep — the desktop bundles it, a bare
# server may not have it, and a missing lib is a MissingDependency the routes map to 501).
_DOCX_EXTS = {".docx"}
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
# Legacy binary Word (an OLE2 compound file) — nothing here reads it; refuse with the fix.
_LEGACY_DOC_EXTS = {".doc"}
_LEGACY_DOC_MIME = "application/msword"
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
# A .docx is a zip of XML parts, and neither its upload size nor its declared sizes bound
# what opening it costs. python-docx reads each part with ZipFile.read(), which inflates a
# member in ONE call (deflate up to 2 GiB, bzip2/LZMA without limit) and only then cuts it
# to the declared size — so a header that lies about its size, carrying the CRC of the
# kept prefix, passes every zipfile check. lxml then builds ~130 bytes of tree per node.
# So _repack_docx inflates the text parts itself, streaming and capped, and python-docx
# only ever opens that re-packed copy. Budgets are measured from real documents: a résumé
# is ~0.1 MiB of XML scoring ~8k on the node budget below; a 300-page report ~4 MiB, ~350k.
_MAX_DOCX_XML_BYTES = 12 * 1024 * 1024  # inflated XML, all parts together
# Tree cost, bounded from the bytes: every element needs a '<' and every attribute an '=',
# and lxml spends about twice as much on an attribute as on an element — so '<' + 2×'='
# over-counts the tree without creating a Python object per node (an XML pre-parser would
# itself materialise a million-attribute tag). A 300-page report scores ~350k.
_MAX_DOCX_XML_NODES = 1_000_000
_MAX_DOCX_MEMBERS = 5000
_DOCX_READ_CHUNK = 64 * 1024
# Audio → transcribed directly via the gateway STT endpoint.
_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".oga", ".opus", ".aac", ".wma", ".aiff", ".aif"}
# Video → audio track extracted with ffmpeg, then transcribed.
_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpeg", ".mpg", ".wmv"}
# Image → described by a vision model (gateway), gated on an injected describe fn. Not in
# SUPPORTED_EXTENSIONS: support is conditional (needs knowledge.image_describe_model), so a
# bare extractor without a describe fn still rejects images with a clear message.
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif", ".heic", ".heif"}

SUPPORTED_EXTENSIONS = sorted(
    _TEXT_EXTS | _MD_EXTS | _HTML_EXTS | _PDF_EXTS | _DOCX_EXTS | _AUDIO_EXTS | _VIDEO_EXTS
)
SUPPORTED_DESCRIPTION = "text, Markdown, HTML, PDF, Word (.docx), audio + video files, and web/YouTube URLs"


# ── decoding / parsing helpers (pure) ────────────────────────────────────────


def _decode(data: bytes) -> str:
    """Best-effort text decode: UTF-8 (BOM-aware) then latin-1 as a last resort
    (latin-1 maps every byte, so it never raises — keeps ingest resilient)."""
    if isinstance(data, str):
        return data
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


def _looks_textual(data: bytes) -> bool:
    """Heuristic for an unknown-extension upload: decodes as UTF-8 and has no NULs
    → treat as plain text; otherwise it's binary we don't handle."""
    if b"\x00" in data[:8192]:
        return False
    try:
        data[:8192].decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def html_to_text(data: bytes | str) -> str:
    """Strip an HTML document to readable text (BeautifulSoup — a core dep).

    Drops script/style/nav/footer/aside chrome and collapses whitespace. Not a
    full readability model (that's a later upgrade), but enough that an article's
    body dominates the chunked text."""
    from bs4 import BeautifulSoup

    html = _decode(data) if isinstance(data, bytes) else data
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template", "svg", "nav", "footer", "aside", "form"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    # Collapse runs of blank lines / trailing spaces the tag soup leaves behind.
    lines = [ln.strip() for ln in text.splitlines()]
    out: list[str] = []
    blank = False
    for ln in lines:
        if ln:
            out.append(ln)
            blank = False
        elif not blank:
            out.append("")
            blank = True
    return "\n".join(out).strip()


def html_title(data: bytes | str) -> str | None:
    from bs4 import BeautifulSoup

    html = _decode(data) if isinstance(data, bytes) else data
    soup = BeautifulSoup(html, "html.parser")
    if soup.title and soup.title.string:
        return soup.title.string.strip() or None
    h1 = soup.find("h1")
    if h1:
        t = h1.get_text(strip=True)
        return t or None
    return None


_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}


def youtube_id(url: str) -> str | None:
    """Extract an 11-char YouTube video id from any of its URL shapes
    (watch?v=, youtu.be/, /shorts/, /embed/, /live/), else None."""
    try:
        u = urlparse(url.strip())
    except (ValueError, AttributeError):
        return None
    if (u.hostname or "").lower() not in _YT_HOSTS:
        return None
    if u.hostname and u.hostname.lower().endswith("youtu.be"):
        cand = u.path.lstrip("/").split("/")[0]
        return cand if _valid_yt_id(cand) else None
    if u.path == "/watch":
        cand = (parse_qs(u.query).get("v") or [""])[0]
        return cand if _valid_yt_id(cand) else None
    for prefix in ("/shorts/", "/embed/", "/live/", "/v/"):
        if u.path.startswith(prefix):
            cand = u.path[len(prefix) :].split("/")[0]
            return cand if _valid_yt_id(cand) else None
    return None


def _valid_yt_id(cand: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{11}", cand or ""))


# ── format extractors ────────────────────────────────────────────────────────


def _extract_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise MissingDependency("PDF ingestion needs the 'pypdf' package (pip install pypdf).") from exc
    import io

    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [(p.extract_text() or "").strip() for p in reader.pages]
    except Exception as exc:  # noqa: BLE001 — pypdf raises a zoo of errors on bad PDFs
        raise ExtractionError(f"could not parse PDF: {exc}") from exc
    return "\n\n".join(p for p in pages if p)


_LEGACY_DOC_HINT = (
    "legacy Word .doc files aren't supported — re-save it as .docx "
    "(Word: File ▸ Save As ▸ Word Document) or export it to PDF, then attach that"
)
_ENCRYPTED_DOCX_HINT = (
    "this Word document is password-protected — remove the password "
    "(Word: File ▸ Info ▸ Protect Document) or export it to PDF, then attach that"
)
_MACRO_OR_TEMPLATE_HINT = (
    "macro-enabled and template Word files (.docm/.dotx/.dotm) aren't accepted — save it as a "
    "regular .docx (Word: File ▸ Save As ▸ Word Document) or export it to PDF, then attach that"
)
# Word's other package flavours: macro-enabled documents and (macro-enabled) templates.
_WORD_VARIANT_EXTS = {".docm", ".dotx", ".dotm"}
_WORD_VARIANT_MIMES = {
    "application/vnd.ms-word.document.macroenabled.12",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.template",
    "application/vnd.ms-word.template.macroenabled.12",
}
# A password-protected .docx isn't a zip at all: Office wraps the encrypted package in the
# same OLE2 container a legacy .doc uses, as a stream with this (UTF-16) name.
_ENCRYPTED_OOXML_MARKER = "EncryptedPackage".encode("utf-16-le")


def _ole_word_refusal(data: bytes) -> UnsupportedSource:
    """The right refusal for an OLE2 Word file: encrypted .docx, else legacy .doc."""
    return UnsupportedSource(_ENCRYPTED_DOCX_HINT if _ENCRYPTED_OOXML_MARKER in data else _LEGACY_DOC_HINT)
_DOCX_MISSING_HINT = (
    "Word (.docx) files need the 'python-docx' package, which isn't installed in this "
    "server's Python — install it there (pip install python-docx) and retry, or export "
    "the document to PDF and attach that instead"
)

# WordprocessingML tags, in the Clark notation lxml reports (the walk below reads the raw
# XML so it behaves the same on every python-docx version — 0.8's ``paragraph.text``
# dropped hyperlink runs, which is where a resume keeps its email address).
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_W_P, _W_TBL, _W_TR, _W_TC = f"{_W}p", f"{_W}tbl", f"{_W}tr", f"{_W}tc"
_W_SDT, _W_SDT_CONTENT, _W_CUSTOM_XML = f"{_W}sdt", f"{_W}sdtContent", f"{_W}customXml"
_W_T, _W_TAB, _W_BR, _W_CR = f"{_W}t", f"{_W}tab", f"{_W}br", f"{_W}cr"
_W_NB_HYPHEN, _W_TXBX_CONTENT = f"{_W}noBreakHyphen", f"{_W}txbxContent"
_W_PPR, _W_PSTYLE, _W_NUMPR, _W_ILVL, _W_NUMID, _W_VAL = (
    f"{_W}pPr",
    f"{_W}pStyle",
    f"{_W}numPr",
    f"{_W}ilvl",
    f"{_W}numId",
    f"{_W}val",
)
# Subtrees whose text isn't on the page: formatting properties, tracked deletions and the
# moved-from copy, and a shape's legacy (VML) fallback that repeats its modern rendering.
_DOCX_SKIP = frozenset(
    {
        _W_PPR,
        f"{_W}rPr",
        f"{_W}sdtPr",
        f"{_W}sdtEndPr",
        f"{_W}del",
        f"{_W}moveFrom",
        "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback",
    }
)
_HEADING_STYLE_RE = re.compile(r"heading\s*([1-9])")
_LIST_STYLE_RE = re.compile(r"list (?:bullet|number)(?:\s*([1-9]))?")


_DOCX_TOO_BIG = (
    "this Word document is too large or too complex to read here ({detail}) — "
    "split it, or export it to PDF, then attach that"
)


def _repack_docx(data: bytes):
    """Re-pack an untrusted .docx into an in-memory zip (a ``BytesIO``) that python-docx
    can open without unbounded work. Nothing python-docx does touches the original.

    Each XML part is inflated here by STREAMING reads: ``ZipExtFile.read(n)`` inflates at
    most ``n`` bytes per step for stored/deflate members and stops at the declared size,
    so a lying header yields only the bytes it declared. Every chunk counts against the
    byte and node budgets, and only what was read is written back, under honest headers.
    Parts with no text in them (images, fonts, embedded objects) go back EMPTY and are
    never inflated. bzip2/LZMA members — which zipfile inflates without a bound even
    when read in chunks, and which Word never writes — are refused outright."""
    import io
    import zipfile
    import zlib

    if len(data) > _MAX_FETCH_BYTES:
        raise SourceTooLarge(f"document too large ({len(data)} bytes > {_MAX_FETCH_BYTES})")
    # zipfile builds a ZipInfo for every central-directory record before anything can be
    # counted, and each record starts with this signature — so count them first.
    if data.count(b"PK\x01\x02") > _MAX_DOCX_MEMBERS:
        raise SourceTooLarge(_DOCX_TOO_BIG.format(detail=f"more than {_MAX_DOCX_MEMBERS} archive members"))
    try:
        src = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError, OSError, EOFError) as exc:
        raise ExtractionError(f"not a valid .docx file (a .docx is a zip archive): {exc}") from exc

    out = io.BytesIO()
    bytes_left, nodes_left = _MAX_DOCX_XML_BYTES, _MAX_DOCX_XML_NODES
    over_bytes = _DOCX_TOO_BIG.format(detail=f"its text runs past {_MAX_DOCX_XML_BYTES // (1024 * 1024)} MB of XML")
    over_nodes = _DOCX_TOO_BIG.format(detail="more XML elements and attributes than it can hold")
    names: set[str] = set()
    with src, zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as dst:
        for info in src.infolist():
            name = info.filename
            if name in names:
                raise ExtractionError(f"not a valid .docx file: archive member {name!r} appears twice")
            names.add(name)
            if info.is_dir():
                continue
            if not name.lower().endswith((".xml", ".rels")):
                dst.writestr(name, b"")  # no text in it — never inflated
                continue
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED) or info.flag_bits & 0x1:
                raise ExtractionError(
                    f"not a valid .docx file: part {name!r} uses zip method {info.compress_type}"
                    f"{' with encryption' if info.flag_bits & 0x1 else ''} (Word writes only stored or deflate)"
                )
            if info.file_size > bytes_left:  # declared: refuse before reading a byte
                raise SourceTooLarge(over_bytes)
            chunks: list[bytes] = []
            try:
                with src.open(info) as part:
                    while chunk := part.read(_DOCX_READ_CHUNK):
                        bytes_left -= len(chunk)
                        nodes_left -= chunk.count(b"<") + 2 * chunk.count(b"=")
                        if bytes_left < 0:
                            raise SourceTooLarge(over_bytes)
                        if nodes_left < 0:
                            raise SourceTooLarge(over_nodes)
                        chunks.append(chunk)
            except (zipfile.BadZipFile, zlib.error, EOFError, OSError, ValueError, NotImplementedError) as exc:
                raise ExtractionError(f"could not parse DOCX: part {name!r} is damaged: {exc}") from exc
            body = b"".join(chunks)
            if b"<!DOCTYPE" in body:  # Word never writes one; refusing it leaves no entity tricks to play
                raise ExtractionError(f"could not parse DOCX: part {name!r} declares a DTD, which Word never writes")
            dst.writestr(name, body)
    out.seek(0)
    return out, names


def _not_a_word_document(names: set[str], detail: str = "") -> UnsupportedSource:
    """Name the package a renamed Office/OpenDocument file actually is, instead of
    surfacing python-docx's error about a part it didn't expect."""
    lowered = {n.lower() for n in names}
    if any(n.startswith("xl/") for n in lowered):
        return UnsupportedSource(
            "this file is an Excel workbook (.xlsx), not a Word document — export it to PDF or CSV and attach that"
        )
    if any(n.startswith("ppt/") for n in lowered):
        return UnsupportedSource(
            "this file is a PowerPoint deck (.pptx), not a Word document — export it to PDF and attach that"
        )
    if "mimetype" in lowered:
        return UnsupportedSource(
            "this file is an OpenDocument file, not a Word document — save it as .docx or export it to PDF"
        )
    return UnsupportedSource(
        "this file isn't a Word document" + (f" ({detail})" if detail else "")
        + " — open it in Word and save it as .docx, or export it to PDF"
    )


def _docx_style_kinds(document) -> tuple[dict[str, int], dict[str, int]]:
    """``(headings, lists)``: paragraph style id → heading level (1-9) / list level (0-8).
    Matched on the style NAME (``Heading 2``, ``List Bullet 2``) — ids are localized in
    non-English templates, names of built-in styles are not. A custom style that carries
    its own numbering counts as a list."""
    headings: dict[str, int] = {}
    lists: dict[str, int] = {}
    for style in document.styles:
        try:
            sid = style.style_id
            name = (style.name or "").strip().lower()
            numbered = style.element.find(f"{_W_PPR}/{_W_NUMPR}") is not None
        except Exception:  # noqa: BLE001 — one odd style definition never sinks the document
            continue
        if not sid:
            continue
        heading = _HEADING_STYLE_RE.fullmatch(name)
        listed = _LIST_STYLE_RE.fullmatch(name)
        if name == "title":
            headings[sid] = 1
        elif heading:
            headings[sid] = int(heading.group(1))
        elif listed:
            lists[sid] = int(listed.group(1) or 1) - 1
        elif numbered:
            lists[sid] = 0
    return headings, lists


def _docx_inline_text(el, kinds, spill: list[str]) -> str:
    """The visible text of one paragraph's runs (hyperlinks, fields, inline content
    controls and tracked insertions included). A text box anchored in the paragraph is
    its own little document: its blocks go to ``spill``, emitted after the paragraph."""
    parts: list[str] = []
    for child in el:
        tag = child.tag
        if not isinstance(tag, str) or tag in _DOCX_SKIP:  # comments/PIs have a non-str tag
            continue
        if tag == _W_T:
            parts.append(child.text or "")
        elif tag == _W_TAB:
            parts.append("\t")
        elif tag in (_W_BR, _W_CR):
            parts.append("\n")
        elif tag == _W_NB_HYPHEN:
            parts.append("-")
        elif tag == _W_TXBX_CONTENT:
            _docx_blocks(child, kinds, spill)
        else:
            parts.append(_docx_inline_text(child, kinds, spill))
    return "".join(parts)


def _docx_paragraph(p, kinds, out: list[str]) -> None:
    headings, lists = kinds
    spill: list[str] = []
    text = _docx_inline_text(p, kinds, spill).strip()
    style_id, num_level = "", None
    ppr = p.find(_W_PPR)
    if ppr is not None:
        pstyle = ppr.find(_W_PSTYLE)
        style_id = (pstyle.get(_W_VAL) if pstyle is not None else "") or ""
        numpr = ppr.find(_W_NUMPR)
        if numpr is not None:
            num_id = numpr.find(_W_NUMID)
            ilvl = numpr.find(_W_ILVL)
            # numId 0 is Word's explicit "no numbering here" (overrides a list style).
            num_level = -1 if num_id is not None and num_id.get(_W_VAL) == "0" else _int_attr(ilvl)
    if text:
        list_level = lists.get(style_id) if num_level is None else (num_level if num_level >= 0 else None)
        if style_id in headings:
            if out and out[-1]:
                out.append("")  # a blank line before a heading, as Markdown reads it
            out.append("#" * min(headings[style_id], 6) + " " + " ".join(text.split()))
        elif list_level is not None:
            indent = "  " * min(list_level, 8)
            out.append(f"{indent}- " + text.replace("\n", f"\n{indent}  "))  # a soft break stays in the item
        else:
            out.append(text)
    out.extend(spill)


def _int_attr(el) -> int:
    try:
        return max(0, int(el.get(_W_VAL))) if el is not None else 0
    except (TypeError, ValueError):
        return 0


def _docx_children(el, tag: str):
    """Direct ``tag`` children of ``el``, looking through the content-control / custom-XML
    wrappers Word may put around rows and cells."""
    for child in el:
        if child.tag == tag:
            yield child
        elif child.tag == _W_SDT:
            content = child.find(_W_SDT_CONTENT)
            if content is not None:
                yield from _docx_children(content, tag)
        elif child.tag == _W_CUSTOM_XML:
            yield from _docx_children(child, tag)


def _docx_table(tbl, kinds, out: list[str]) -> None:
    """A data table reads row by row as ``a | b | c``. A LAYOUT table (resumes: dates in
    one column, a job's bullets in the next) has multi-paragraph cells that one pipe row
    would flatten, so such a row is read cell by cell, keeping headings and bullets."""
    rows = []
    for tr in _docx_children(tbl, _W_TR):
        cells = []
        for tc in _docx_children(tr, _W_TC):
            lines: list[str] = []
            _docx_blocks(tc, kinds, lines)
            cells.append([ln for ln in lines if ln.strip()])
        rows.append(cells)
    if out and out[-1]:
        out.append("")
    for cells in rows:
        if not any(cells):
            continue
        if all(len(c) <= 1 for c in cells):
            out.append(" | ".join(" ".join(c[0].split()) if c else "" for c in cells))
        else:
            for c in cells:
                out.extend(c)
    out.append("")


def _docx_blocks(container, kinds, out: list[str]) -> None:
    """Paragraphs and tables of a story (body, cell, header, footer, text box) in order."""
    for child in container:
        tag = child.tag
        if tag == _W_P:
            _docx_paragraph(child, kinds, out)
        elif tag == _W_TBL:
            _docx_table(child, kinds, out)
        elif tag == _W_SDT:
            content = child.find(_W_SDT_CONTENT)
            if content is not None:
                _docx_blocks(content, kinds, out)
        elif tag == _W_CUSTOM_XML:
            _docx_blocks(child, kinds, out)


def _docx_story(lines: list[str]) -> str:
    """Join a story's lines, collapsing the blank runs tables/headings leave behind."""
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _extract_docx(data: bytes) -> str:
    """Word → markdown-ish text: header, body, footer. Headings become ``#`` lines, list
    items ``- `` (indented by level), tables row by row — all in document order."""
    if data[:8] == _OLE2_MAGIC:  # a password-protected .docx, or a legacy .doc renamed
        raise _ole_word_refusal(data)
    package, names = _repack_docx(data)  # bounded; python-docx never sees the original zip
    try:
        import docx  # python-docx
        from docx.opc.constants import RELATIONSHIP_TYPE as RT
    except ImportError as exc:
        raise MissingDependency(_DOCX_MISSING_HINT) from exc

    try:
        document = docx.Document(package)
    except Exception as exc:  # noqa: BLE001 — translate python-docx's view of a foreign package
        message = str(exc).lower()
        if "macroenabled" in message or ".template" in message:  # its "not a Word file" names the type
            raise UnsupportedSource(_MACRO_OR_TEMPLATE_HINT) from exc
        if "is not a word file" in message or not any(n.lower().startswith("word/") for n in names):
            raise _not_a_word_document(names) from exc
        raise ExtractionError(f"could not parse DOCX: {exc}") from exc
    try:
        kinds = _docx_style_kinds(document)
        body = document.element.find(f"{_W}body")
        body_lines: list[str] = []
        if body is not None:
            _docx_blocks(body, kinds, body_lines)
        # Headers/footers (resumes keep contact details there) via the document part's
        # relationships: every variant (default / first-page / even) once, deduped by text.
        margins: dict[str, list[str]] = {RT.HEADER: [], RT.FOOTER: []}
        for rel in list(document.part.rels.values()):
            if rel.is_external or rel.reltype not in margins:
                continue
            part = rel.target_part
            element = getattr(part, "element", None)
            if element is None:
                from docx.oxml import parse_xml

                element = parse_xml(part.blob)
            lines: list[str] = []
            _docx_blocks(element, kinds, lines)
            story = _docx_story(lines)
            if story and story not in margins[rel.reltype]:
                margins[rel.reltype].append(story)
    except Exception as exc:  # noqa: BLE001 — python-docx / lxml raise a zoo of errors on bad files
        raise ExtractionError(f"could not parse DOCX: {exc}") from exc
    stories = [*margins[RT.HEADER], _docx_story(body_lines), *margins[RT.FOOTER]]
    return "\n\n".join(s for s in stories if s)


def _snippet_text(snippet) -> str:
    """Read a transcript snippet's text across api versions: 1.x yields snippet
    objects with a ``.text`` attribute; the legacy 0.6 API yielded ``{"text": …}``."""
    if hasattr(snippet, "text"):
        return (snippet.text or "").strip()
    if isinstance(snippet, dict):
        return (snippet.get("text") or "").strip()
    return ""


def _youtube_transcript(video_id: str) -> str:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError as exc:
        raise MissingDependency(
            "YouTube ingestion needs the 'youtube-transcript-api' package (pip install youtube-transcript-api)."
        ) from exc
    try:
        # 1.x instance API (`fetch`); FetchedTranscript is iterable of snippets.
        fetched = YouTubeTranscriptApi().fetch(video_id)
    except Exception as exc:  # noqa: BLE001 — no captions / disabled / unavailable
        raise ExtractionError(f"no transcript available for video {video_id}: {exc}") from exc
    parts = [_snippet_text(s) for s in fetched]
    return " ".join(p for p in parts if p)


def _audio_from_video(data: bytes, suffix: str) -> bytes:
    """Extract a video's audio track to MP3 with ffmpeg, for transcription.

    ffmpeg is a system binary (not a pip dep); a missing one is a friendly
    MissingDependency, not a crash."""
    import os
    import shutil
    import subprocess
    import tempfile

    if not shutil.which("ffmpeg"):
        raise MissingDependency("video ingestion needs ffmpeg on PATH (brew install ffmpeg / apt install ffmpeg)")
    src = tempfile.NamedTemporaryFile(suffix=suffix or ".mp4", delete=False)
    out_path = src.name + ".mp3"
    try:
        src.write(data)
        src.flush()
        src.close()
        subprocess.run(
            ["ffmpeg", "-y", "-i", src.name, "-vn", "-acodec", "libmp3lame", "-q:a", "4", out_path],
            check=True,
            capture_output=True,
            timeout=_FFMPEG_TIMEOUT_S,
        )
        with open(out_path, "rb") as f:
            return f.read()
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or b"").decode("utf-8", "replace")[-400:]
        raise ExtractionError(f"ffmpeg could not extract audio: {tail}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ExtractionError("ffmpeg timed out extracting audio") from exc
    finally:
        for p in (src.name, out_path):
            try:
                os.unlink(p)
            except OSError:
                pass


def _transcribe_media(data: bytes, filename: str, transcribe, *, video: bool) -> str:
    """Turn audio/video bytes into text via the injected transcribe fn (gateway
    STT). Video is run through ffmpeg first to pull the audio track."""
    if transcribe is None:
        raise MissingDependency(
            "audio/video ingestion needs a transcription model — set "
            "knowledge.transcribe_model and a gateway that serves it (e.g. whisper-1)"
        )
    audio, name = data, filename
    if video:
        audio = _audio_from_video(data, Path(filename or "").suffix or ".mp4")
        name = (Path(filename or "audio").stem or "audio") + ".mp3"
    try:
        text = transcribe(audio, name)
    except IngestionError:
        raise
    except Exception as exc:  # noqa: BLE001 — gateway/transport error → clean failure
        raise ExtractionError(f"transcription failed: {exc}") from exc
    return text or ""


# ── public entry points ──────────────────────────────────────────────────────


def _describe_image(data: bytes, filename: str, mime: str, describe) -> str:
    """Turn image bytes into a text description via the injected describe fn (a gateway
    vision model). Lets a text-only chat model "see" an attached screenshot (#1381)."""
    if describe is None:
        raise UnsupportedSource(
            "this model can't see images — set knowledge.image_describe_model to a "
            "vision-capable gateway model to attach screenshots, or switch to a vision model"
        )
    try:
        text = describe(data, mime, filename or "image")
    except IngestionError:
        raise
    except Exception as exc:  # noqa: BLE001 — gateway/transport error → clean failure
        raise ExtractionError(f"image description failed: {exc}") from exc
    return text or ""


def extract_bytes(
    filename: str,
    data: bytes,
    content_type: str | None = None,
    *,
    transcribe=None,
    describe=None,
) -> ExtractResult:
    """Extract text from an uploaded file's bytes, dispatched by extension then
    content-type. ``filename`` provides the extension + a default title.
    ``transcribe`` (bytes, filename) -> text powers audio/video (gateway STT);
    ``describe`` (bytes, mime, filename) -> text powers images (gateway vision)."""
    ext = Path(filename or "").suffix.lower()
    ct = (content_type or "").split(";")[0].strip().lower()
    title = (Path(filename).stem if filename else None) or None

    if ext in _PDF_EXTS or ct == "application/pdf":
        text, source_type = _extract_pdf(data), "pdf"
    elif ext in _WORD_VARIANT_EXTS or ct in _WORD_VARIANT_MIMES:
        raise UnsupportedSource(_MACRO_OR_TEMPLATE_HINT)
    elif ext in _DOCX_EXTS or ct == _DOCX_MIME:
        text, source_type = _extract_docx(data), "docx"
    elif ext in _LEGACY_DOC_EXTS or ct == _LEGACY_DOC_MIME:
        raise _ole_word_refusal(data)
    elif ext in _HTML_EXTS or "html" in ct:
        text, source_type = html_to_text(data), "html"
    elif ext in _MD_EXTS:
        text, source_type = _decode(data), "markdown"
    elif ext in _AUDIO_EXTS or ct.startswith("audio/"):
        text, source_type = _transcribe_media(data, filename, transcribe, video=False), "audio"
    elif ext in _VIDEO_EXTS or ct.startswith("video/"):
        text, source_type = _transcribe_media(data, filename, transcribe, video=True), "video"
    elif ext in _IMAGE_EXTS or ct.startswith("image/"):
        text, source_type = _describe_image(data, filename, ct or f"image/{(ext[1:] or 'png')}", describe), "image"
    elif ext in _TEXT_EXTS or ct.startswith("text/"):
        text, source_type = _decode(data), "text"
    elif not ext and not ct and _looks_textual(data):
        text, source_type = _decode(data), "text"
    else:
        raise UnsupportedSource(f"unsupported file type {ext or ct or 'unknown'!r}; supported: {SUPPORTED_DESCRIPTION}")

    if not text.strip():
        raise ExtractionError("no extractable text in the file")
    return ExtractResult(text=text, title=title, source_type=source_type, meta={"filename": filename})


def extract_url(url: str, *, fetch=None, transcribe=None) -> ExtractResult:
    """Extract text from a web URL. YouTube links resolve to their transcript;
    everything else is fetched and dispatched by content-type (HTML/PDF/text/
    audio/video). Audio/video URLs are transcribed via the gateway STT fn.

    ``fetch`` is an injection seam for tests: a callable ``(url) -> (bytes,
    content_type)``. Defaults to an httpx GET (bounded size + timeout)."""
    url = (url or "").strip()
    if not url:
        raise UnsupportedSource("empty URL")

    vid = youtube_id(url)
    if vid:
        text = _youtube_transcript(vid)
        if not text.strip():
            raise ExtractionError(f"empty transcript for video {vid}")
        return ExtractResult(
            text=text, title=f"YouTube transcript ({vid})", source_type="youtube", meta={"url": url, "video_id": vid}
        )

    data, ct = (fetch or _http_fetch)(url)
    ct = (ct or "").split(";")[0].strip().lower()
    url_ext = Path(urlparse(url).path).suffix.lower()

    if "pdf" in ct or url_ext in _PDF_EXTS:
        text, source_type, title = _extract_pdf(data), "pdf", url
    elif ct in _WORD_VARIANT_MIMES or url_ext in _WORD_VARIANT_EXTS:
        raise UnsupportedSource(_MACRO_OR_TEMPLATE_HINT)
    elif ct == _DOCX_MIME or url_ext in _DOCX_EXTS:
        text, source_type, title = _extract_docx(data), "docx", url
    elif ct == _LEGACY_DOC_MIME or url_ext in _LEGACY_DOC_EXTS:
        raise _ole_word_refusal(data)
    elif ct.startswith("audio/") or url_ext in _AUDIO_EXTS:
        name = _media_filename(url, url_ext, ".mp3")
        text, source_type, title = _transcribe_media(data, name, transcribe, video=False), "audio", url
    elif ct.startswith("video/") or url_ext in _VIDEO_EXTS:
        name = _media_filename(url, url_ext, ".mp4")
        text, source_type, title = _transcribe_media(data, name, transcribe, video=True), "video", url
    elif "html" in ct or not ct:
        text = html_to_text(data)
        title = html_title(data) or url
        source_type = "html"
    elif ct.startswith("text/"):
        text, source_type, title = _decode(data), "text", url
    else:
        raise UnsupportedSource(f"unsupported content-type {ct!r} at {url}")

    if not text.strip():
        raise ExtractionError(f"no extractable text at {url}")
    return ExtractResult(text=text, title=title, source_type=source_type, meta={"url": url, "content_type": ct})


def _media_filename(url: str, url_ext: str, default_ext: str) -> str:
    """A filename WITH an extension for a media URL — so ffmpeg/STT see the
    format. Uses the URL's basename when it has one, else ``media<ext>``."""
    name = Path(urlparse(url).path).name
    if name and Path(name).suffix:
        return name
    return f"media{url_ext or default_ext}"


def _http_fetch(url: str) -> tuple[bytes, str]:
    import httpx

    from security import egress

    # SSRF guard: ingestion fetches an operator/user-supplied URL server-side and
    # persists the response, so it gets the same destination policy as the
    # ``fetch_url`` tool — reject private/loopback/link-local/cloud-metadata hosts
    # (unless egress-allowlisted), and re-check every redirect hop manually so a
    # public URL can't 30x-bounce us onto an internal target.
    err = egress.check_url(url)
    if err:
        raise UnsupportedSource(err)
    with httpx.Client(follow_redirects=False, timeout=_FETCH_TIMEOUT_S, headers={"User-Agent": _FETCH_UA}) as client:
        for _ in range(_MAX_REDIRECTS + 1):
            resp = client.get(url)
            if resp.is_redirect:
                url = str(resp.url.join(resp.headers.get("location", "")))
                err = egress.check_url(url)
                if err:
                    raise UnsupportedSource(err)
                continue
            resp.raise_for_status()
            content = resp.content
            if len(content) > _MAX_FETCH_BYTES:
                raise ExtractionError(f"document too large ({len(content)} bytes > {_MAX_FETCH_BYTES})")
            return content, resp.headers.get("content-type", "")
    raise ExtractionError(f"too many redirects (> {_MAX_REDIRECTS}) fetching {url}")
