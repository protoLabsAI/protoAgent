"""Document ingestion engine — turn a source into plain text for the knowledge base.

The knowledge store ingests text (``add_document`` chunks + contextually enriches
+ embeds it, ADR 0021). This package is the *front* of that pipeline: it turns a
file or URL into that text. Each format is a small extractor; the heavy ones
(PDF parsing, YouTube transcripts) lazy-import their dependency and raise a
friendly ``MissingDependency`` if it isn't installed, so the base import is light.

Phase 1 (this module) covers the light, pure-Python formats: plain text,
Markdown, HTML / web URLs, PDF, and YouTube transcripts. Audio/video (local ASR)
is a deliberate Phase 2 — the gateway serves no transcription model.

Public API:
    extract_bytes(filename, data, content_type=None) -> ExtractResult
    extract_url(url) -> ExtractResult
    SUPPORTED_EXTENSIONS, SUPPORTED_DESCRIPTION
    IngestionError / UnsupportedSource / ExtractionError / MissingDependency
"""

from ingestion.engine import (
    SUPPORTED_DESCRIPTION,
    SUPPORTED_EXTENSIONS,
    ExtractionError,
    ExtractResult,
    IngestionError,
    MissingDependency,
    UnsupportedSource,
    extract_bytes,
    extract_url,
    youtube_id,
)

__all__ = [
    "ExtractResult",
    "IngestionError",
    "UnsupportedSource",
    "ExtractionError",
    "MissingDependency",
    "extract_bytes",
    "extract_url",
    "youtube_id",
    "SUPPORTED_EXTENSIONS",
    "SUPPORTED_DESCRIPTION",
]
