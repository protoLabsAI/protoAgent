"""Ingestion engine — source → text extractors (ADR 0021).

Pure helpers (YouTube-id parsing, HTML→text, decode) are tested directly; the
dependency/network extractors (PDF, YouTube, URL fetch) are tested with injected
fetchers / monkeypatched libs so the suite stays offline and deterministic.
"""

from __future__ import annotations

import pytest

from ingestion import (
    ExtractionError,
    MissingDependency,
    UnsupportedSource,
    extract_bytes,
    extract_url,
    youtube_id,
)
from ingestion import engine


# ── youtube_id (pure) ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("url,expected", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=30s", "dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://m.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://example.com/watch?v=dQw4w9WgXcQ", None),   # not youtube
    ("https://www.youtube.com/watch?v=short", None),     # not 11 chars
    ("https://www.youtube.com/", None),
    ("not a url", None),
])
def test_youtube_id(url, expected):
    assert youtube_id(url) == expected


# ── html helpers (pure) ───────────────────────────────────────────────────────


def test_html_to_text_strips_chrome():
    html = (b"<html><head><title>T</title><style>x{}</style></head><body>"
            b"<nav>menu home</nav><script>evil()</script>"
            b"<h1>Heading</h1><p>Hello world.</p><footer>copyright</footer></body></html>")
    text = engine.html_to_text(html)
    assert "Heading" in text and "Hello world." in text
    assert "evil()" not in text and "menu home" not in text and "copyright" not in text


def test_html_title_prefers_title_then_h1():
    assert engine.html_title(b"<title>  My Doc </title><h1>H</h1>") == "My Doc"
    assert engine.html_title(b"<body><h1>Fallback</h1></body>") == "Fallback"
    assert engine.html_title(b"<p>nothing</p>") is None


def test_decode_handles_utf8_and_falls_back():
    assert engine._decode("café".encode("utf-8")) == "café"
    # Invalid UTF-8 never raises (latin-1 last resort).
    assert engine._decode(b"\xff\xfeabc")  # no exception


# ── extract_bytes ─────────────────────────────────────────────────────────────


def test_extract_bytes_text_and_markdown():
    r = extract_bytes("notes.txt", b"plain content here")
    assert r.text == "plain content here" and r.source_type == "text" and r.title == "notes"
    r2 = extract_bytes("doc.md", b"# Title\n\nbody")
    assert r2.source_type == "markdown" and "# Title" in r2.text


def test_extract_bytes_html():
    r = extract_bytes("page.html", b"<title>T</title><p>Hi there</p>")
    assert r.source_type == "html" and "Hi there" in r.text


def test_extract_bytes_unknown_but_textual():
    r = extract_bytes("README", b"just some text, no extension")
    assert r.source_type == "text" and "just some text" in r.text


def test_extract_bytes_rejects_binary():
    with pytest.raises(UnsupportedSource):
        extract_bytes("blob.bin", b"\x00\x01\x02\x03garbage")


def test_extract_bytes_empty_raises():
    with pytest.raises(ExtractionError):
        extract_bytes("empty.txt", b"   \n  ")


def test_extract_bytes_content_type_sniff():
    # No extension, but content_type says HTML.
    r = extract_bytes("download", b"<p>From header</p>", content_type="text/html; charset=utf-8")
    assert r.source_type == "html" and "From header" in r.text


# ── PDF (pypdf logic, reader monkeypatched) ───────────────────────────────────


def test_extract_pdf_joins_pages(monkeypatch):
    class _Page:
        def __init__(self, t): self._t = t
        def extract_text(self): return self._t

    class _Reader:
        def __init__(self, _stream): self.pages = [_Page("page one"), _Page(""), _Page("page two")]

    import pypdf
    monkeypatch.setattr(pypdf, "PdfReader", _Reader)
    r = extract_bytes("doc.pdf", b"%PDF-1.4 ...")
    assert r.source_type == "pdf" and r.text == "page one\n\npage two"


def test_extract_pdf_parse_error_raises_extraction_error(monkeypatch):
    import pypdf

    def _boom(_stream): raise ValueError("corrupt")
    monkeypatch.setattr(pypdf, "PdfReader", _boom)
    with pytest.raises(ExtractionError):
        extract_bytes("doc.pdf", b"not really a pdf")


# ── extract_url (injected fetch / monkeypatched transcript) ───────────────────


def test_extract_url_html_with_injected_fetch():
    def fake_fetch(url):
        return b"<title>Article</title><p>Body text here.</p>", "text/html; charset=utf-8"
    r = extract_url("https://example.com/post", fetch=fake_fetch)
    assert r.source_type == "html" and r.title == "Article" and "Body text here." in r.text


def test_extract_url_plaintext_with_injected_fetch():
    r = extract_url("https://example.com/raw.txt", fetch=lambda u: (b"raw body", "text/plain"))
    assert r.source_type == "text" and r.text == "raw body"


def test_extract_url_rejects_unknown_content_type():
    with pytest.raises(UnsupportedSource):
        extract_url("https://example.com/x.zip", fetch=lambda u: (b"PK\x03\x04", "application/zip"))


def test_extract_url_youtube(monkeypatch):
    class _Snippet:
        def __init__(self, t): self.text = t

    class _FakeApi:
        def fetch(self, video_id, *a, **k):
            assert video_id == "dQw4w9WgXcQ"
            return [_Snippet("never gonna"), _Snippet("give you up")]

    import youtube_transcript_api
    monkeypatch.setattr(youtube_transcript_api, "YouTubeTranscriptApi", _FakeApi)
    r = extract_url("https://youtu.be/dQw4w9WgXcQ")
    assert r.source_type == "youtube" and r.text == "never gonna give you up"
    assert r.meta["video_id"] == "dQw4w9WgXcQ"


def test_extract_url_empty_raises():
    with pytest.raises(UnsupportedSource):
        extract_url("   ")


# ── audio / video (gateway STT, injected transcribe + ffmpeg) ─────────────────


def test_extract_bytes_audio_transcribes():
    seen = {}

    def transcribe(data, name):
        seen["name"] = name
        return "spoken words from the clip"

    r = extract_bytes("talk.mp3", b"\x00fake-audio", transcribe=transcribe)
    assert r.source_type == "audio" and r.text == "spoken words from the clip"
    assert seen["name"] == "talk.mp3"


def test_extract_bytes_audio_by_content_type():
    r = extract_bytes("blob", b"\x00", content_type="audio/mpeg",
                      transcribe=lambda d, n: "heard it")
    assert r.source_type == "audio" and r.text == "heard it"


def test_extract_bytes_audio_without_transcribe_raises():
    with pytest.raises(MissingDependency):
        extract_bytes("a.mp3", b"\x00audio")


def test_extract_bytes_video_extracts_audio_then_transcribes(monkeypatch):
    monkeypatch.setattr(engine, "_audio_from_video", lambda data, suffix: b"AUDIO-TRACK")
    seen = {}

    def transcribe(data, name):
        seen["data"], seen["name"] = data, name
        return "the talk narration"

    r = extract_bytes("keynote.mp4", b"\x00video-bytes", transcribe=transcribe)
    assert r.source_type == "video" and r.text == "the talk narration"
    assert seen["data"] == b"AUDIO-TRACK" and seen["name"].endswith(".mp3")


def test_extract_url_audio(monkeypatch):
    r = extract_url(
        "https://example.com/podcast/ep1.mp3",
        fetch=lambda u: (b"\x00audio", "audio/mpeg"),
        transcribe=lambda data, name: "episode transcript",
    )
    assert r.source_type == "audio" and r.text == "episode transcript"


def test_audio_from_video_invokes_ffmpeg(monkeypatch):
    import shutil
    import subprocess

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ffmpeg")

    def fake_run(cmd, **kw):
        with open(cmd[-1], "wb") as f:  # ffmpeg's output path is the last arg
            f.write(b"MP3-BYTES")
        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert engine._audio_from_video(b"video-bytes", ".mp4") == b"MP3-BYTES"


def test_audio_from_video_missing_ffmpeg(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(MissingDependency):
        engine._audio_from_video(b"x", ".mp4")
