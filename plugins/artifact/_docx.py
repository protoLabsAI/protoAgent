"""Page-preview preflight for ``.docx`` file artifacts — the server-side safety gate.

The panel renders a Word document as real pages with the vendored client-side renderer
(``vendor/docx-preview.min.js`` on top of ``vendor/jszip.min.js``, inside the no-same-origin
artifact sandbox). Before the panel ever hands it the bytes, ``save_file_artifact`` runs this
preflight once and stamps the verdict onto the version's ``file`` meta as
``docx: {render, reason, images}``. That stamp IS the per-version cache: a view never
re-inflates anything. The panel renders a document ONLY when the stamp says ``render: True``;
anything else (a refusal, or a version saved before the preflight existed) keeps the text card.

A .docx is a zip, so this reuses the slide preflight's bounded inflater (``_slides._inflate``):
every entry's raw stream is actually inflated in chunks against a per-entry and whole-archive
byte budget and a wall clock, independent of what the zip's directory claims, and the real size
and CRC must match the declared ones. Every entry is sniffed as an image from its header bytes
(not its name or folder — a renderer decodes whatever a relationship points at); the
renderer decodes every image it shows, so one over the per-image or whole-document pixel budget
refuses the preview (with a reason) rather than handing the browser a decompression bomb.

The shell mirrors these caps (``DOCX_CAPS`` in ``shell.js``, drift-guarded by a test) and the
frame bounds its own parse time on top.
"""

from __future__ import annotations

import io
import re
import time
import zipfile
from pathlib import Path

from . import _slides

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
DOCX_EXTS = {".docx"}

# ── caps (mirrored by DOCX_CAPS in shell.js) ─────────────────────────────────
MAX_BYTES = 40 * 1024 * 1024  # the .docx file itself
MAX_ENTRIES = 4000  # files in the archive
MAX_IMAGE_PIXELS = 50_000_000  # one image's width × height (≈ 200 MB of RGBA once decoded)
MAX_DOC_PIXELS = 400_000_000  # all images together (a report with ~30 phone photos fits)

_IMAGE_RE = re.compile(r"^word/media/[^/]+\.(png|gif|jpe?g|bmp|webp)$", re.IGNORECASE)


def is_docx(filename: str, mime: str) -> bool:
    """Does this file get the page renderer? By extension, or by the WordprocessingML mime."""
    return Path(filename or "").suffix.lower() in DOCX_EXTS or (mime or "").lower() == DOCX_MIME


def _verdict(render: bool, reason: str = "", images: int = 0) -> dict:
    return {"render": render, "reason": reason, "images": images}


def preflight(data: bytes) -> dict:
    """``{render, reason, images}`` for a .docx's bytes — ``render`` False names the cap it broke.
    Never raises: anything unreadable is a ``render: False`` verdict (the text card still shows)."""
    if len(data) > MAX_BYTES:
        return _verdict(
            False, f"file is {len(data) // (1024 * 1024)} MB — over the {MAX_BYTES // (1024 * 1024)} MB preview cap"
        )
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError, OSError):
        return _verdict(False, "not a readable .docx (bad zip)")
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > MAX_ENTRIES:
            return _verdict(False, f"{len(infos)} files in the archive — over the {MAX_ENTRIES} cap")
        for i in infos:  # cheap refusals from the directory first; the inflate pass is binding
            if i.file_size > _slides.MAX_ENTRY_BYTES:
                return _verdict(
                    False, f"{i.filename} inflates to {i.file_size // (1024 * 1024)} MB — over the per-file cap"
                )
        if sum(i.file_size for i in infos) > _slides.MAX_TOTAL_BYTES:
            return _verdict(False, f"archive inflates past the {_slides.MAX_TOTAL_BYTES // (1024 * 1024)} MB cap")
        if "word/document.xml" not in {i.filename for i in infos}:
            return _verdict(False, "no word/document.xml — not a Word document")
        deadline = time.monotonic() + _slides.MAX_SCAN_SECONDS
        total, images, pixels = 0, 0, 0
        try:
            for i in infos:
                size, head = _slides._inflate(data, i, _slides.MAX_TOTAL_BYTES - total, deadline, "document")
                total += size
                # By content, not name: a renderer decodes an image wherever the document's
                # relationships point, so a raster hidden under another name or folder counts too.
                dims = _slides.image_dims(head)
                if dims or _IMAGE_RE.match(i.filename):
                    images += 1
                    px = dims[0] * dims[1] if dims else 0
                    if px > MAX_IMAGE_PIXELS:
                        mp = MAX_IMAGE_PIXELS // 1_000_000
                        return _verdict(False, f"an image is {px // 1_000_000} MP (cap {mp} MP)", images)
                    pixels += px
                    if pixels > MAX_DOC_PIXELS:
                        mp = MAX_DOC_PIXELS // 1_000_000
                        return _verdict(False, f"its images total more than {mp} MP", images)
        except _slides._Refused as e:
            return _verdict(False, str(e), images)
        except Exception:  # noqa: BLE001 — zlib.error / a malformed header: not a file we'll parse
            return _verdict(False, "the zip is corrupt (an entry couldn't be inflated)", images)
    return _verdict(True, "", images)
