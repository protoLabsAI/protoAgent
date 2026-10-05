"""PDF-preview preflight for ``.pdf`` file artifacts — the server-side safety gate.

The panel renders a PDF as real pages with the vendored pdf.js (``vendor/pdfjs.min.mjs`` +
``vendor/pdfjs-worker.min.mjs``, run on the frame's main thread inside the no-same-origin
artifact sandbox — no Worker, no network). Before the panel ever hands it the bytes,
``save_file_artifact`` runs this preflight once and stamps the verdict onto the version's
``file`` meta as ``pdf: {render, reason, pages}``. That stamp IS the per-version cache: a view
never re-parses anything here. The panel renders a PDF ONLY when the stamp says
``render: True``; anything else (a refusal, or a version saved before the preflight existed)
keeps the extracted-text card.

pdf.js has no decompression limits of its own, so every stream the renderer will decode is
decoded HERE first, through pypdf's bounded filters (``LimitReachedError`` on a bomb), against
a per-stream and a whole-document byte budget and a wall-clock budget: every page's content
streams, its image and form XObjects (forms recursively), and its embedded font programs.
Image pixel dimensions are read from the image dictionaries (no decode) and capped per image
and per page. Password-protected files (no empty user password), unreadable files, page counts
over the cap and anything over a budget are ``render: False`` with a human reason.

The shell mirrors these caps (``PDF_CAPS`` in ``shell.js``, drift-guarded by a test) and the
frame enforces its own on top: a parse budget, lazy page rendering, and a canvas pixel cap.
"""

from __future__ import annotations

import io
import time
from pathlib import Path

PDF_MIME = "application/pdf"
PDF_EXTS = {".pdf"}

# ── caps (mirrored by PDF_CAPS in shell.js) ──────────────────────────────────
MAX_BYTES = 40 * 1024 * 1024  # the .pdf file itself
MAX_PAGES = 2000
MAX_STREAM_BYTES = 64 * 1024 * 1024  # one decoded stream
MAX_TOTAL_BYTES = 512 * 1024 * 1024  # every decoded stream together
MAX_IMAGE_PIXELS = 50_000_000  # one image's width × height (≈ 200 MB of RGBA once decoded)
MAX_PAGE_PIXELS = 150_000_000  # all images drawn on one page together
MAX_SCAN_SECONDS = 15.0  # wall clock for the whole document (a real 40 MB report takes ~1 s)
_MAX_FORM_DEPTH = 12  # nested form XObjects; real documents use 1–3


class _Refuse(Exception):
    pass


def is_pdf(filename: str, mime: str) -> bool:
    """Does this file get the PDF renderer? By extension, or by the PDF mime."""
    return Path(filename or "").suffix.lower() in PDF_EXTS or (mime or "").lower() == PDF_MIME


class _Budget:
    def __init__(self) -> None:
        self.total = 0
        self.deadline = time.monotonic() + MAX_SCAN_SECONDS
        self.seen: set[int] = set()

    def tick(self) -> None:
        if time.monotonic() > self.deadline:
            raise _Refuse(f"checking it took longer than {int(MAX_SCAN_SECONDS)} s")

    def decode(self, stream, what: str) -> None:
        """Decode one stream through pypdf's bounded filters and charge it to the budget."""
        from pypdf.errors import LimitReachedError

        key = id(stream)
        ref = getattr(stream, "indirect_reference", None)
        if ref is not None:
            key = hash((ref.idnum, ref.generation))
        if key in self.seen:
            return
        self.seen.add(key)
        self.tick()
        try:
            data = stream.get_data()
        except LimitReachedError:
            raise _Refuse(f"a {what} stream inflates past the safety cap") from None
        except NotImplementedError:
            return  # a filter pypdf can't decode (JBIG2 / JPX images) — pdf.js draws it or leaves it blank
        except Exception as e:  # noqa: BLE001 — a corrupt stream; pdf.js may still cope, but we can't vouch for it
            raise _Refuse(f"a {what} stream is damaged ({type(e).__name__})") from None
        n = len(data or b"")
        if n > MAX_STREAM_BYTES:
            raise _Refuse(f"a {what} stream is {n // 1048576} MB once decoded (cap {MAX_STREAM_BYTES // 1048576} MB)")
        self.total += n
        if self.total > MAX_TOTAL_BYTES:
            raise _Refuse(f"its streams decode to more than {MAX_TOTAL_BYTES // 1048576} MB")


def _int(obj, key: str) -> int:
    try:
        return int(obj.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _resources(res, budget: _Budget, page_pixels: list[int], depth: int) -> None:
    """Walk a resource dictionary: images (dims + data), forms (recursively), font programs."""
    if res is None:
        return
    res = res.get_object()
    xobjects = res.get("/XObject")
    if xobjects is not None:
        for _name, ref in xobjects.get_object().items():
            budget.tick()
            xo = ref.get_object()
            sub = xo.get("/Subtype")
            if sub == "/Image":
                px = _int(xo, "/Width") * _int(xo, "/Height")
                if px > MAX_IMAGE_PIXELS:
                    raise _Refuse(f"an image is {px // 1_000_000} MP (cap {MAX_IMAGE_PIXELS // 1_000_000} MP)")
                page_pixels[0] += px
                if page_pixels[0] > MAX_PAGE_PIXELS:
                    raise _Refuse(f"one page draws more than {MAX_PAGE_PIXELS // 1_000_000} MP of images")
                budget.decode(xo, "image")
                smask = xo.get("/SMask")
                if smask is not None:
                    budget.decode(smask.get_object(), "image mask")
            elif sub == "/Form":
                if depth >= _MAX_FORM_DEPTH:
                    raise _Refuse("its drawing instructions nest too deeply")
                budget.decode(xo, "drawing")
                _resources(xo.get("/Resources"), budget, page_pixels, depth + 1)
    fonts = res.get("/Font")
    if fonts is not None:
        for _name, ref in fonts.get_object().items():
            budget.tick()
            font = ref.get_object()
            descs = [font.get("/FontDescriptor")]
            for df in font.get("/DescendantFonts") or []:
                descs.append(df.get_object().get("/FontDescriptor"))
            for desc in descs:
                if desc is None:
                    continue
                desc = desc.get_object()
                for key in ("/FontFile", "/FontFile2", "/FontFile3"):
                    if key in desc:
                        budget.decode(desc[key].get_object(), "font")


def preflight(data: bytes) -> dict:
    """The render verdict for one PDF: ``{"render": True, "pages": n}`` or
    ``{"render": False, "reason": "...", "pages": n?}``."""
    if len(data) > MAX_BYTES:
        return {"render": False, "reason": f"the file is over the {MAX_BYTES // 1048576} MB preview cap"}
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover — pypdf is a core dependency
        return {"render": False, "reason": "the PDF checker isn't installed"}
    pages = None
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            try:
                ok = reader.decrypt("")
            except Exception:  # noqa: BLE001
                ok = 0
            if not ok:
                return {"render": False, "reason": "the PDF is password-protected"}
        pages = len(reader.pages)
        if pages == 0:
            return {"render": False, "reason": "the PDF has no pages", "pages": 0}
        if pages > MAX_PAGES:
            return {"render": False, "reason": f"{pages} pages is over the {MAX_PAGES}-page preview cap", "pages": pages}
        budget = _Budget()
        for page in reader.pages:
            budget.tick()
            contents = page.get("/Contents")
            if contents is not None:
                contents = contents.get_object()
                for part in contents if isinstance(contents, list) else [contents]:
                    budget.decode(part.get_object(), "page")
            _resources(page.get("/Resources"), budget, [0], 0)
    except _Refuse as r:
        out = {"render": False, "reason": str(r)}
        if pages is not None:
            out["pages"] = pages
        return out
    except Exception as e:  # noqa: BLE001 — anything pypdf can't parse, we can't vouch for
        out = {"render": False, "reason": f"the PDF couldn't be read ({type(e).__name__})"}
        if pages is not None:
            out["pages"] = pages
        return out
    return {"render": True, "pages": pages}
