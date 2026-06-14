"""Structure-aware text chunking for knowledge ingest (ADR 0021).

The store embeds each chunk as ONE vector. A whole conversation summary or a
pasted document stored as a single chunk collapses to one diluted embedding
that represents no specific passage — so semantic recall can't surface the
precise relevant span. Splitting a document into coherent, overlapping pieces
*before* embedding fixes that: one embedding per passage, so a query lands on
the chunk that actually answers it.

Splitting is hierarchical — paragraphs first (the strongest boundary), then
sentences, then whitespace, then a hard character window as the last resort —
so a chunk almost always ends on a natural break. Short text passes through
unchanged, so callers can route everything through ``add_document`` without a
size check. Pure functions, no I/O — unit-tested directly; ``KnowledgeStore``
composes them in ``add_document``.
"""

from __future__ import annotations

import re

# Boundaries, strongest first. Paragraph = a blank line; sentence = a
# terminator followed by whitespace (kept on the left via lookbehind).
_PARA_RE = re.compile(r"\n\s*\n")
_SENT_RE = re.compile(r"(?<=[.!?])\s+")


def chunk_text(
    text: str,
    *,
    max_chars: int = 1200,
    overlap_chars: int = 150,
    min_chars: int = 200,
) -> list[str]:
    """Split ``text`` into chunks of at most ~``max_chars`` characters.

    - Text at or under ``max_chars`` returns ``[text]`` unchanged (the common
      case for facts/notes — no behavior change).
    - Adjacent chunks share an ``overlap_chars`` tail (snapped to a word
      boundary) so a span straddling a split is still wholly present in one
      chunk.
    - A trailing chunk shorter than ``min_chars`` is merged back into the
      previous one rather than embedded as a thin fragment.

    Deterministic and never raises; returns ``[]`` for empty input.
    """
    text = (text or "").strip()
    if not text:
        return []

    max_chars = max(1, int(max_chars))
    overlap_chars = max(0, min(int(overlap_chars), max_chars - 1))
    min_chars = max(0, min(int(min_chars), max_chars))

    if len(text) <= max_chars:
        return [text]

    segments = _segment(text, max_chars)

    chunks: list[str] = []
    cur = ""
    for seg in segments:
        candidate = (cur + "\n" + seg) if cur else seg
        if not cur or len(candidate) <= max_chars:
            cur = candidate
            continue
        chunks.append(cur.strip())
        tail = _overlap_tail(cur, overlap_chars)
        cur = (tail + "\n" + seg) if tail else seg
    if cur.strip():
        chunks.append(cur.strip())

    # Don't leave a thin fragment as its own embedding — fold it back.
    if len(chunks) >= 2 and len(chunks[-1]) < min_chars:
        chunks[-2] = (chunks[-2] + "\n" + chunks[-1]).strip()
        chunks.pop()

    return chunks


def _segment(text: str, max_chars: int) -> list[str]:
    """Break ``text`` into pieces each at most ``max_chars`` long, descending
    paragraph → sentence → whitespace → hard window only as far as needed."""
    out: list[str] = []
    for para in _PARA_RE.split(text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= max_chars:
            out.append(para)
        else:
            out.extend(_split_oversized(para, max_chars))
    return out


def _split_oversized(para: str, max_chars: int) -> list[str]:
    """A paragraph longer than ``max_chars`` — pack its sentences up to the
    limit, hard-splitting any single sentence that still overflows."""
    parts: list[str] = []
    buf = ""
    for sent in _SENT_RE.split(para):
        sent = sent.strip()
        if not sent:
            continue
        if len(sent) > max_chars:
            if buf:
                parts.append(buf)
                buf = ""
            parts.extend(_hard_split(sent, max_chars))
            continue
        candidate = (buf + " " + sent) if buf else sent
        if len(candidate) <= max_chars:
            buf = candidate
        else:
            parts.append(buf)
            buf = sent
    if buf:
        parts.append(buf)
    return parts


def _hard_split(s: str, max_chars: int) -> list[str]:
    """Last resort for a no-natural-boundary run: pack on whitespace, and
    window any single word longer than ``max_chars`` (e.g. a URL/base64 blob)."""
    parts: list[str] = []
    buf = ""
    for word in s.split():
        if len(word) > max_chars:
            if buf:
                parts.append(buf)
                buf = ""
            for i in range(0, len(word), max_chars):
                parts.append(word[i:i + max_chars])
            continue
        candidate = (buf + " " + word) if buf else word
        if len(candidate) <= max_chars:
            buf = candidate
        else:
            parts.append(buf)
            buf = word
    if buf:
        parts.append(buf)
    return parts


def _overlap_tail(chunk: str, overlap_chars: int) -> str:
    """The last ``overlap_chars`` of ``chunk``, snapped forward to a word
    boundary so the next chunk doesn't begin mid-word. Empty when there's no
    room for a meaningful overlap (chunk no longer than the overlap window)."""
    if overlap_chars <= 0 or len(chunk) <= overlap_chars:
        return ""
    tail = chunk[-overlap_chars:]
    space = tail.find(" ")
    if space != -1:
        tail = tail[space + 1:]
    return tail.strip()
