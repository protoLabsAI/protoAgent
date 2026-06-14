"""Source → text extractors for the ingestion engine (ADR 0021).

Pure-Python, dependency-light. Network (URL fetch) and optional deps (pypdf,
youtube-transcript-api) are isolated to their own extractors so the parsing
helpers (HTML→text, YouTube-id parsing, decode) stay unit-testable offline.
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


class IngestionError(Exception):
    """Base for ingestion failures (kept distinct so routes can map to HTTP codes)."""


class UnsupportedSource(IngestionError):
    """The file type / URL isn't something we know how to extract."""


class ExtractionError(IngestionError):
    """The source is a known type but yielded no usable text."""


class MissingDependency(IngestionError):
    """A format needs an optional package that isn't installed."""


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
# Audio → transcribed directly via the gateway STT endpoint.
_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".oga", ".opus", ".aac",
               ".wma", ".aiff", ".aif"}
# Video → audio track extracted with ffmpeg, then transcribed.
_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpeg", ".mpg", ".wmv"}

SUPPORTED_EXTENSIONS = sorted(
    _TEXT_EXTS | _MD_EXTS | _HTML_EXTS | _PDF_EXTS | _AUDIO_EXTS | _VIDEO_EXTS)
SUPPORTED_DESCRIPTION = (
    "text, Markdown, HTML, PDF, audio + video files, and web/YouTube URLs")


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
    for tag in soup(["script", "style", "noscript", "template", "svg",
                     "nav", "footer", "aside", "form"]):
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


_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
             "youtu.be", "www.youtu.be"}


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
            cand = u.path[len(prefix):].split("/")[0]
            return cand if _valid_yt_id(cand) else None
    return None


def _valid_yt_id(cand: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{11}", cand or ""))


# ── format extractors ────────────────────────────────────────────────────────


def _extract_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise MissingDependency(
            "PDF ingestion needs the 'pypdf' package (pip install pypdf).") from exc
    import io

    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [(p.extract_text() or "").strip() for p in reader.pages]
    except Exception as exc:  # noqa: BLE001 — pypdf raises a zoo of errors on bad PDFs
        raise ExtractionError(f"could not parse PDF: {exc}") from exc
    return "\n\n".join(p for p in pages if p)


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
            "YouTube ingestion needs the 'youtube-transcript-api' package "
            "(pip install youtube-transcript-api).") from exc
    try:
        # 1.x instance API (`fetch`); FetchedTranscript is iterable of snippets.
        fetched = YouTubeTranscriptApi().fetch(video_id)
    except Exception as exc:  # noqa: BLE001 — no captions / disabled / unavailable
        raise ExtractionError(
            f"no transcript available for video {video_id}: {exc}") from exc
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
        raise MissingDependency(
            "video ingestion needs ffmpeg on PATH (brew install ffmpeg / apt install ffmpeg)")
    src = tempfile.NamedTemporaryFile(suffix=suffix or ".mp4", delete=False)
    out_path = src.name + ".mp3"
    try:
        src.write(data)
        src.flush()
        src.close()
        subprocess.run(
            ["ffmpeg", "-y", "-i", src.name, "-vn", "-acodec", "libmp3lame", "-q:a", "4", out_path],
            check=True, capture_output=True, timeout=_FFMPEG_TIMEOUT_S,
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
            "knowledge.transcribe_model and a gateway that serves it (e.g. whisper-1)")
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


def extract_bytes(
    filename: str,
    data: bytes,
    content_type: str | None = None,
    *,
    transcribe=None,
) -> ExtractResult:
    """Extract text from an uploaded file's bytes, dispatched by extension then
    content-type. ``filename`` provides the extension + a default title.
    ``transcribe`` (bytes, filename) -> text powers audio/video (gateway STT)."""
    ext = Path(filename or "").suffix.lower()
    ct = (content_type or "").split(";")[0].strip().lower()
    title = (Path(filename).stem if filename else None) or None

    if ext in _PDF_EXTS or ct == "application/pdf":
        text, source_type = _extract_pdf(data), "pdf"
    elif ext in _HTML_EXTS or "html" in ct:
        text, source_type = html_to_text(data), "html"
    elif ext in _MD_EXTS:
        text, source_type = _decode(data), "markdown"
    elif ext in _AUDIO_EXTS or ct.startswith("audio/"):
        text, source_type = _transcribe_media(data, filename, transcribe, video=False), "audio"
    elif ext in _VIDEO_EXTS or ct.startswith("video/"):
        text, source_type = _transcribe_media(data, filename, transcribe, video=True), "video"
    elif ext in _TEXT_EXTS or ct.startswith("text/"):
        text, source_type = _decode(data), "text"
    elif not ext and not ct and _looks_textual(data):
        text, source_type = _decode(data), "text"
    else:
        raise UnsupportedSource(
            f"unsupported file type {ext or ct or 'unknown'!r}; supported: {SUPPORTED_DESCRIPTION}")

    if not text.strip():
        raise ExtractionError("no extractable text in the file")
    return ExtractResult(text=text, title=title, source_type=source_type,
                         meta={"filename": filename})


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
        return ExtractResult(text=text, title=f"YouTube transcript ({vid})",
                             source_type="youtube", meta={"url": url, "video_id": vid})

    data, ct = (fetch or _http_fetch)(url)
    ct = (ct or "").split(";")[0].strip().lower()
    url_ext = Path(urlparse(url).path).suffix.lower()

    if "pdf" in ct or url_ext in _PDF_EXTS:
        text, source_type, title = _extract_pdf(data), "pdf", url
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
    return ExtractResult(text=text, title=title, source_type=source_type,
                         meta={"url": url, "content_type": ct})


def _media_filename(url: str, url_ext: str, default_ext: str) -> str:
    """A filename WITH an extension for a media URL — so ffmpeg/STT see the
    format. Uses the URL's basename when it has one, else ``media<ext>``."""
    name = Path(urlparse(url).path).name
    if name and Path(name).suffix:
        return name
    return f"media{url_ext or default_ext}"


def _http_fetch(url: str) -> tuple[bytes, str]:
    import httpx

    with httpx.Client(follow_redirects=True, timeout=_FETCH_TIMEOUT_S,
                      headers={"User-Agent": _FETCH_UA}) as client:
        resp = client.get(url)
        resp.raise_for_status()
        content = resp.content
        if len(content) > _MAX_FETCH_BYTES:
            raise ExtractionError(
                f"document too large ({len(content)} bytes > {_MAX_FETCH_BYTES})")
        return content, resp.headers.get("content-type", "")
