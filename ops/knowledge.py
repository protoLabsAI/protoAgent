"""Knowledge ops (ADR 0075 D2) — the one op that already spanned both spines.

``ingest`` is the whole extract→store glue that the agent tool (``knowledge_ingest``) and
the console route (``POST /api/knowledge/ingest``) each used to re-implement. Now both call
this; ``ingest_preview`` shares the extraction half with a dry-run that never persists.

A source is a small union — URL, local path, uploaded bytes, or already-extracted text —
so every surface hands the op what it has and the extraction dispatch lives in one place
(the console route previously built only the STT fn, never the vision one; unifying here
fixes that gap for image uploads). Expected failures raise :class:`IngestError` with a
``kind`` the adapter maps to its surface (a tool message, an HTTP status)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from ops import OpContext, op


class IngestError(Exception):
    """An expected, legible ingest failure. ``kind`` is a stable token the adapters map:
    ``no_source`` / ``not_found`` / ``missing_dependency`` / ``unsupported`` / ``extraction``
    / ``empty``. ``str(err)`` is the underlying detail (safe to show)."""

    def __init__(self, detail: str, *, kind: str):
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


@dataclass
class IngestSource:
    """What to ingest — exactly one of url / path / (data + filename) / text is meaningful.
    Use the constructors so callers don't juggle fields."""

    url: str | None = None
    path: str | None = None
    data: bytes | None = None
    filename: str | None = None
    content_type: str | None = None
    text: str | None = None
    text_title: str | None = None
    label: str | None = None  # override the stored `source` provenance label

    @classmethod
    def from_url(cls, url: str) -> "IngestSource":
        return cls(url=url)

    @classmethod
    def from_path(cls, path: str) -> "IngestSource":
        return cls(path=path)

    @classmethod
    def from_upload(cls, data: bytes, filename: str | None, content_type: str | None = None) -> "IngestSource":
        return cls(data=data, filename=filename, content_type=content_type)

    @classmethod
    def from_text(cls, text: str, *, title: str | None = None, label: str = "console") -> "IngestSource":
        return cls(text=text, text_title=title, label=label)


@dataclass
class IngestResult:
    ids: list[int]
    chunks: int
    chars: int
    title: str | None
    source_type: str
    source: str  # provenance label the chunks were stored under


@dataclass
class PreviewResult:
    chunks: int
    chars: int
    approx_tokens: int
    title: str | None
    source_type: str
    source: str
    snippet: str
    truncated: bool


def _media_fns(graph_config):
    """Gateway STT + vision fns for media sources, or ``(None, None)`` when no model is
    configured — audio/video/image then raise a clean "not configured" error while
    text/URL/PDF paths are unaffected."""
    if graph_config is None:
        return None, None
    try:
        from graph.llm import create_describe_image_fn, create_transcribe_fn

        return create_transcribe_fn(graph_config), create_describe_image_fn(graph_config)
    except Exception:  # noqa: BLE001 — media stays optional; text/URL/PDF unaffected
        return None, None


async def _extract(source: IngestSource, ctx: OpContext):
    """Turn a source into ``(ExtractResult, provenance_label)`` off the event loop — the
    shared half of ingest + preview. Never persists. Raises :class:`IngestError` on an
    expected failure; returns ``(None, "")`` when no source was given."""
    from ingestion import ExtractResult, MissingDependency, UnsupportedSource, extract_bytes, extract_url

    transcribe, describe = _media_fns(ctx.graph_config)
    try:
        if source.text is not None:
            text = source.text
            return ExtractResult(text=text, title=source.text_title or None, source_type="text"), (source.label or "console")
        if source.url:
            return await asyncio.to_thread(extract_url, source.url, transcribe=transcribe), (source.label or source.url)
        if source.data is not None:
            name = source.filename or "upload"
            result = await asyncio.to_thread(
                extract_bytes, name, source.data, source.content_type, transcribe=transcribe, describe=describe
            )
            return result, (source.label or name)
        if source.path:
            path = Path(source.path).expanduser()
            if not path.is_file():
                raise IngestError(
                    f"No such file: {source.path} — pass an http(s) URL or an existing local file path.",
                    kind="not_found",
                )
            data = await asyncio.to_thread(path.read_bytes)
            result = await asyncio.to_thread(
                extract_bytes, path.name, data, None, transcribe=transcribe, describe=describe
            )
            return result, (source.label or str(path))
        return None, ""
    except MissingDependency as exc:
        raise IngestError(str(exc), kind="missing_dependency") from exc
    except UnsupportedSource as exc:
        raise IngestError(str(exc), kind="unsupported") from exc
    except IngestError:
        raise
    except Exception as exc:  # noqa: BLE001 — surface extraction failure, never crash the surface
        raise IngestError(str(exc), kind="extraction") from exc


@op(
    name="knowledge.ingest",
    mutates=True,
    summary="Extract a URL / file / text source and index it into long-term knowledge.",
)
async def ingest(source: IngestSource, *, domain: str = "general", title: str | None = None, ctx: OpContext) -> IngestResult:
    """Run the ingestion pipeline for one source and store it. Raises :class:`IngestError`
    (``no_source`` when nothing was given, ``empty`` when extraction yielded no text)."""
    from knowledge import add_document

    result, origin = await _extract(source, ctx)
    if result is None:
        raise IngestError("provide a url, a file, or text to ingest", kind="no_source")

    heading = (title or "").strip() or result.title or None
    dom = (domain or "general").strip() or "general"
    ids = await asyncio.to_thread(
        lambda: add_document(
            ctx.knowledge_store,
            result.text,
            domain=dom,
            heading=heading,
            source=origin,
            source_type=result.source_type,
        )
    )
    if not ids:
        raise IngestError("nothing ingested — no text could be extracted from that source", kind="empty")
    return IngestResult(
        ids=list(ids),
        chunks=len(ids),
        chars=len(result.text),
        title=heading,
        source_type=result.source_type,
        source=origin,
    )


@op(
    name="knowledge.ingest_preview",
    mutates=False,
    summary="Dry-run an ingest: extract + count chunks for a source without persisting.",
)
async def ingest_preview(source: IngestSource, *, title: str | None = None, ctx: OpContext, snippet_chars: int = 600) -> PreviewResult:
    """Extract + count chunks with the live store's knobs WITHOUT persisting (#1801) — the
    same extraction as :func:`ingest`, stopped before ``add_document``."""
    from knowledge.chunking import chunk_text

    result, origin = await _extract(source, ctx)
    if result is None:
        raise IngestError("provide a url, a file, or text to ingest", kind="no_source")

    store = ctx.knowledge_store
    chunks = chunk_text(
        result.text,
        max_chars=getattr(store, "_chunk_max_chars", 1200),
        overlap_chars=getattr(store, "_chunk_overlap_chars", 150),
        min_chars=getattr(store, "_chunk_min_chars", 200),
    )
    snippet = result.text[:snippet_chars]
    return PreviewResult(
        chunks=len(chunks),
        chars=len(result.text),
        approx_tokens=max(1, len(result.text) // 4),  # house heuristic (graph.middleware.*)
        title=(title or "").strip() or result.title or None,
        source_type=result.source_type,
        source=origin,
        snippet=snippet,
        truncated=len(result.text) > len(snippet),
    )
