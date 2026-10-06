"""Slide-preview preflight for ``.pptx`` file artifacts — the server-side safety gate.

The panel renders a deck as real slides with the vendored client-side renderer
(``vendor/pptx-renderer.min.js``, inside the no-same-origin artifact sandbox). Before the
panel ever hands it the bytes, ``save_file_artifact`` runs this preflight once and stamps the
verdict onto the version's ``file`` meta as ``slides: {render, reason, count, big_images}`` —
that stamp IS the per-version cache: a view never re-inflates anything. The panel renders a
deck ONLY when the stamp says ``render: True``; anything else (a refusal, or a version saved
before the preflight existed) shows the text outline.

The preflight does not trust what the zip SAYS. Every entry's raw compressed stream is
inflated here, in chunks, against a running byte budget (per entry and in total) and a wall
clock budget, independent of the declared sizes — a bomb that patches its headers to claim
100 bytes is measured by what it actually inflates to and stops at the cap, never in memory.
The real size and CRC must then match the declared ones, because the browser's unzip trusts
them: any mismatch, unsupported method, encryption, truncation or over-budget entry is
``render: False``. Image pixel dimensions come from the inflated header bytes (PNG / GIF /
JPEG / BMP / WebP — no Pillow needed); an over-cap image is counted, not fatal, and the panel
swaps in a placeholder for it.

The shell mirrors these caps (``PPTX_CAPS`` in ``shell.js``, drift-guarded by a test) and the
frame enforces them again on the bytes it inflates — defence in depth behind this gate.
"""

from __future__ import annotations

import io
import re
import struct
import time
import zipfile
import zlib
from pathlib import Path

PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
SLIDE_EXTS = {".pptx"}

# ── caps (mirrored by PPTX_CAPS in shell.js) ─────────────────────────────────
MAX_BYTES = 40 * 1024 * 1024  # the .pptx file itself
MAX_ENTRIES = 4000  # files in the archive
MAX_ENTRY_BYTES = 32 * 1024 * 1024  # one inflated entry
MAX_TOTAL_BYTES = 256 * 1024 * 1024  # all inflated entries together
MAX_IMAGE_PIXELS = 50_000_000  # one image's width × height (≈ 200 MB of RGBA once decoded)
MAX_DECK_PIXELS = 150_000_000  # all images together; images past this budget get placeholders too
MAX_SLIDES = 1000
# An entry that inflates past this ratio (and is big enough to matter) is a zip bomb, not a
# deck: real XML compresses ~5-20×, PNG/JPEG ~1×.
MAX_RATIO = 200
_RATIO_FLOOR = 1024 * 1024  # small entries may compress absurdly well (a run of spaces) — ignore
_HEAD_BYTES = 64 * 1024  # enough to find a JPEG's SOF marker past typical EXIF blocks
MAX_SCAN_SECONDS = 15.0  # wall clock for inflating the whole deck (a real 40 MB deck takes ~1 s)
_CHUNK = 256 * 1024  # compressed bytes fed per step; output per step is capped the same

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


class _Refused(Exception):
    """A cap the actual inflation broke — its message is the verdict's reason."""


def _inflate(
    data: bytes, info: zipfile.ZipInfo, budget: int, deadline: float, what: str = "deck"
) -> tuple[int, bytes]:
    """Inflate one entry's RAW stream from ``data`` (never via the declared size): ``(real size,
    first _HEAD_BYTES)``. Raises _Refused past ``budget`` bytes or ``deadline``, or when the
    stream disagrees with the directory (size, CRC, truncation) or can't be read."""
    if info.flag_bits & 0x1:
        raise _Refused(f"{info.filename} is encrypted")
    off = info.header_offset
    if data[off : off + 4] != b"PK\x03\x04":
        raise _Refused(f"{info.filename} has no local header (corrupt zip)")
    name_len, extra_len = struct.unpack("<HH", data[off + 26 : off + 30])
    start = off + 30 + name_len + extra_len
    raw = memoryview(data)[start : start + info.compress_size]
    if len(raw) != info.compress_size:
        raise _Refused(f"{info.filename} is truncated")
    cap = min(budget, MAX_ENTRY_BYTES)
    size, crc, head = 0, 0, bytearray()

    def take(chunk: bytes) -> None:
        nonlocal size, crc
        size += len(chunk)
        if size > cap:
            raise _Refused(
                f"{info.filename} inflates past the {MAX_ENTRY_BYTES // (1024 * 1024)} MB per-file cap"
                if cap == MAX_ENTRY_BYTES
                else f"archive inflates past the {MAX_TOTAL_BYTES // (1024 * 1024)} MB cap"
            )
        crc = zlib.crc32(chunk, crc)
        if len(head) < _HEAD_BYTES:
            head.extend(chunk[: _HEAD_BYTES - len(head)])

    if info.compress_type == zipfile.ZIP_STORED:
        for i in range(0, len(raw), _CHUNK):
            take(bytes(raw[i : i + _CHUNK]))
    elif info.compress_type == zipfile.ZIP_DEFLATED:
        d = zlib.decompressobj(-zlib.MAX_WBITS)
        for i in range(0, len(raw), _CHUNK):
            buf = bytes(raw[i : i + _CHUNK])
            while buf:  # bound the output of every step, so a bomb never lands in memory whole
                take(d.decompress(buf, _CHUNK))
                buf = d.unconsumed_tail
                if time.monotonic() > deadline:
                    raise _Refused(f"checking the {what} took longer than {MAX_SCAN_SECONDS:.0f}s")
            if d.eof:
                break
        take(d.flush())
        if not d.eof:
            raise _Refused(f"{info.filename} is truncated")
    else:
        raise _Refused(f"{info.filename} uses an unsupported compression method")
    if size != info.file_size or (crc & 0xFFFFFFFF) != info.CRC:
        raise _Refused(f"{info.filename} doesn't match its declared size/CRC (tampered zip)")
    if size > _RATIO_FLOOR and size > MAX_RATIO * max(info.compress_size, 1):
        raise _Refused(f"{info.filename} has a {size // max(info.compress_size, 1)}:1 compression ratio (zip bomb)")
    return size, bytes(head)


def preflight(data: bytes) -> dict:
    """``{render, reason, count, big_images}`` for a .pptx's bytes — ``render`` False names the
    cap it broke. Every entry is actually inflated (bounded — see the module doc); an image over
    MAX_IMAGE_PIXELS doesn't sink the deck: it's counted in ``big_images`` and the panel draws
    a placeholder in its place (it never decodes it).

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
        # Cheap refusals from the directory first; the inflate pass below is what's binding.
        for i in infos:
            if i.file_size > MAX_ENTRY_BYTES:
                return _verdict(
                    False, f"{i.filename} inflates to {i.file_size // (1024 * 1024)} MB — over the per-file cap"
                )
        if sum(i.file_size for i in infos) > MAX_TOTAL_BYTES:
            return _verdict(False, f"archive inflates past the {MAX_TOTAL_BYTES // (1024 * 1024)} MB cap")
        names = {i.filename for i in infos}
        if "ppt/presentation.xml" not in names:
            return _verdict(False, "no ppt/presentation.xml — not a PowerPoint deck")
        count = sum(1 for n in names if _SLIDE_RE.match(n))
        if count == 0:
            return _verdict(False, "the deck has no slides")
        if count > MAX_SLIDES:
            return _verdict(False, f"{count} slides — over the {MAX_SLIDES}-slide preview cap", count)
        deadline = time.monotonic() + MAX_SCAN_SECONDS
        total, big, pixels = 0, 0, 0
        try:
            for i in infos:
                size, head = _inflate(data, i, MAX_TOTAL_BYTES - total, deadline)
                total += size
                if _IMAGE_RE.match(i.filename):
                    dims = image_dims(head)
                    px = dims[0] * dims[1] if dims else 0
                    if px > MAX_IMAGE_PIXELS or pixels + px > MAX_DECK_PIXELS:
                        big += 1  # the frame applies the same rule, in the same entry order
                    else:
                        pixels += px
        except _Refused as e:
            return _verdict(False, str(e), count)
        except Exception:  # noqa: BLE001 — zlib.error / a malformed header: not a deck we'll parse
            return _verdict(False, "the zip is corrupt (an entry couldn't be inflated)", count)
    return _verdict(True, "", count, big)
