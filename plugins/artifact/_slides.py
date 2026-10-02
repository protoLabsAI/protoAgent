"""Slide-preview preflight for ``.pptx`` file artifacts — the server-side safety gate.

The panel renders a deck as real slides with the vendored client-side renderer
(``vendor/pptx-renderer.min.js``, inside the no-same-origin artifact sandbox). Before the
panel ever hands it the bytes, ``save_file_artifact`` runs this preflight once and stamps
the verdict onto the version's ``file`` meta as ``slides: {render, reason, count}``. A deck
that fails it shows the text outline instead of being parsed in the browser at all.

It reads only the zip's central directory plus the first few KB of each image, so it is
cheap even for a big deck and never inflates a bomb: the declared sizes and compression
ratio are checked from metadata, and image pixel dimensions come from the header bytes
(PNG / GIF / JPEG / BMP / WebP) — no Pillow needed, so a lean install gets the same gate.
An over-cap image is counted, not fatal: the panel swaps in a placeholder for it.

The shell mirrors these caps (``PPTX_CAPS`` in ``shell.js``, drift-guarded by a test) and
enforces them again on the ACTUAL decoded bytes, since a hostile zip can lie in its
directory; this pass exists so the obvious cases never reach the browser.
"""

from __future__ import annotations

import io
import re
import struct
import zipfile
from pathlib import Path

PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
SLIDE_EXTS = {".pptx"}

# ── caps (mirrored by PPTX_CAPS in shell.js) ─────────────────────────────────
MAX_BYTES = 40 * 1024 * 1024  # the .pptx file itself
MAX_ENTRIES = 4000  # files in the archive
MAX_ENTRY_BYTES = 32 * 1024 * 1024  # one inflated entry
MAX_TOTAL_BYTES = 256 * 1024 * 1024  # all inflated entries together
MAX_IMAGE_PIXELS = 50_000_000  # one image's width × height (≈ 200 MB of RGBA once decoded)
MAX_SLIDES = 1000
# An entry that inflates past this ratio (and is big enough to matter) is a zip bomb, not a
# deck: real XML compresses ~5-20×, PNG/JPEG ~1×.
MAX_RATIO = 200
_RATIO_FLOOR = 1024 * 1024  # small entries may compress absurdly well (a run of spaces) — ignore
_HEAD_BYTES = 64 * 1024  # enough to find a JPEG's SOF marker past typical EXIF blocks

_SLIDE_RE = re.compile(r"^ppt/slides/slide\d+\.xml$")
_IMAGE_RE = re.compile(r"^ppt/media/[^/]+\.(png|gif|jpe?g|bmp|webp)$", re.IGNORECASE)


def is_slides(filename: str, mime: str) -> bool:
    """Does this file get the slide renderer? By extension, or by the OOXML presentation mime."""
    return Path(filename or "").suffix.lower() in SLIDE_EXTS or (mime or "").lower() == PPTX_MIME


def image_dims(head: bytes) -> tuple[int, int] | None:
    """(width, height) from an image's leading bytes, or None if unrecognised / truncated."""
    try:
        if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
            return struct.unpack(">II", head[16:24])
        if head[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", head[6:10])
        if head[:2] == b"BM":
            w, h = struct.unpack("<ii", head[18:26])
            return abs(w), abs(h)
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            kind = head[12:16]
            if kind == b"VP8X":
                w = int.from_bytes(head[24:27], "little") + 1
                h = int.from_bytes(head[27:30], "little") + 1
                return w, h
            if kind == b"VP8L":
                b = int.from_bytes(head[21:25], "little")
                return (b & 0x3FFF) + 1, ((b >> 14) & 0x3FFF) + 1
            if kind == b"VP8 ":
                w, h = struct.unpack("<HH", head[26:30])
                return w & 0x3FFF, h & 0x3FFF
            return None
        if head[:2] == b"\xff\xd8":
            i = 2
            while i + 9 < len(head):
                if head[i] != 0xFF:
                    i += 1
                    continue
                marker = head[i + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                seg = struct.unpack(">H", head[i + 2 : i + 4])[0]
                # SOF0..SOF15, excluding DHT (C4), JPG (C8) and DAC (CC)
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">HH", head[i + 5 : i + 9])
                    return w, h
                i += 2 + seg
            return None
    except struct.error:
        return None
    return None


def _verdict(render: bool, reason: str = "", count: int = 0, big_images: int = 0) -> dict:
    return {"render": render, "reason": reason, "count": count, "big_images": big_images}


def preflight(data: bytes) -> dict:
    """``{render, reason, count, big_images}`` for a .pptx's bytes — ``render`` False names the
    cap it broke. An image over MAX_IMAGE_PIXELS doesn't sink the deck: it's counted in
    ``big_images`` and the panel draws a placeholder in its place (it never decodes it).

    Never raises: anything unreadable is a ``render: False`` verdict (the outline still shows)."""
    if len(data) > MAX_BYTES:
        return _verdict(
            False,
            f"file is {len(data) // (1024 * 1024)} MB — over the {MAX_BYTES // (1024 * 1024)} MB slide-preview cap",
        )
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError, OSError):
        return _verdict(False, "not a readable .pptx (bad zip)")
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > MAX_ENTRIES:
            return _verdict(False, f"{len(infos)} files in the archive — over the {MAX_ENTRIES} cap")
        total = 0
        for i in infos:
            if i.file_size > MAX_ENTRY_BYTES:
                return _verdict(
                    False, f"{i.filename} inflates to {i.file_size // (1024 * 1024)} MB — over the per-file cap"
                )
            total += i.file_size
            if i.file_size > _RATIO_FLOOR and i.file_size > MAX_RATIO * max(i.compress_size, 1):
                return _verdict(
                    False, f"{i.filename} has a {i.file_size // max(i.compress_size, 1)}:1 compression ratio (zip bomb)"
                )
        if total > MAX_TOTAL_BYTES:
            return _verdict(
                False,
                f"archive inflates to {total // (1024 * 1024)} MB — over the {MAX_TOTAL_BYTES // (1024 * 1024)} MB cap",
            )
        names = {i.filename for i in infos}
        if "ppt/presentation.xml" not in names:
            return _verdict(False, "no ppt/presentation.xml — not a PowerPoint deck")
        count = sum(1 for n in names if _SLIDE_RE.match(n))
        if count == 0:
            return _verdict(False, "the deck has no slides")
        if count > MAX_SLIDES:
            return _verdict(False, f"{count} slides — over the {MAX_SLIDES}-slide preview cap", count)
        big = 0
        for i in infos:
            if not _IMAGE_RE.match(i.filename):
                continue
            try:
                with zf.open(i) as fh:  # streamed: reads only the head, never inflates the rest
                    head = fh.read(_HEAD_BYTES)
            except Exception:  # noqa: BLE001 — corrupt member / unsupported compression
                return _verdict(False, f"{i.filename} can't be read")
            dims = image_dims(head)
            if dims and dims[0] * dims[1] > MAX_IMAGE_PIXELS:
                big += 1
    return _verdict(True, "", count, big)
