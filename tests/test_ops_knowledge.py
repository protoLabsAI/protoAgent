"""ops.knowledge (ADR 0075 D2) — the shared ingest op both the agent tool and the console
route now call. Extraction (the ingestion engine) is covered by test_ingestion.py; these
test the op itself: source dispatch, result shape, typed error kinds, the dry-run preview,
and the op-registry metadata that seeds the safe-operator profile + operations catalog."""

from __future__ import annotations

import ingestion
import pytest
from ingestion import ExtractResult, MissingDependency, UnsupportedSource

from ops import OpContext, registry
from ops.knowledge import IngestError, IngestSource, ingest, ingest_preview


class _FakeStore:
    def __init__(self):
        self.docs: list[tuple[str, dict]] = []
        self._chunk_max_chars = 1000
        self._chunk_overlap_chars = 100
        self._chunk_min_chars = 100

    def add_document(self, content, **kw):
        self.docs.append((content, kw))
        return [1, 2, 3]


def _ctx(store=None):
    return OpContext(knowledge_store=store if store is not None else _FakeStore(), graph_config=None)


async def test_ingest_url_stores_and_returns_result(monkeypatch):
    monkeypatch.setattr(
        ingestion, "extract_url", lambda url, **kw: ExtractResult(text="body", title="T", source_type="html")
    )
    store = _FakeStore()
    res = await ingest(IngestSource.from_url("https://x/post"), domain="research", ctx=_ctx(store))
    assert res.ids == [1, 2, 3] and res.chunks == 3 and res.chars == 4
    assert res.title == "T" and res.source_type == "html" and res.source == "https://x/post"
    content, kw = store.docs[0]
    assert content == "body" and kw["domain"] == "research" and kw["source"] == "https://x/post"


async def test_ingest_title_override_wins_over_extracted():
    res = await ingest(IngestSource.from_text("hello world", title="Extracted"), title="Override", ctx=_ctx())
    assert res.title == "Override" and res.source == "console" and res.source_type == "text"


async def test_ingest_upload_dispatches_to_extract_bytes(monkeypatch):
    seen: dict = {}

    def _xb(name, data, ctype, **kw):
        seen.update(name=name, data=data, ctype=ctype, has_describe="describe" in kw)
        return ExtractResult(text="pdf text", title=None, source_type="pdf")

    monkeypatch.setattr(ingestion, "extract_bytes", _xb)
    res = await ingest(IngestSource.from_upload(b"%PDF-1.7", "doc.pdf", "application/pdf"), ctx=_ctx())
    assert seen["name"] == "doc.pdf" and seen["data"] == b"%PDF-1.7" and seen["ctype"] == "application/pdf"
    assert res.source == "doc.pdf" and res.source_type == "pdf" and res.title is None


async def test_ingest_missing_file_is_not_found():
    with pytest.raises(IngestError) as ei:
        await ingest(IngestSource.from_path("/no/such/file-xyz.txt"), ctx=_ctx())
    assert ei.value.kind == "not_found"


async def test_ingest_no_source_raises():
    with pytest.raises(IngestError) as ei:
        await ingest(IngestSource(), ctx=_ctx())
    assert ei.value.kind == "no_source"


async def test_ingest_empty_when_nothing_lands(monkeypatch):
    monkeypatch.setattr(
        ingestion, "extract_url", lambda url, **kw: ExtractResult(text="body", title=None, source_type="html")
    )
    store = _FakeStore()
    store.add_document = lambda content, **kw: []  # store rejected / produced no chunks
    with pytest.raises(IngestError) as ei:
        await ingest(IngestSource.from_url("https://x"), ctx=_ctx(store))
    assert ei.value.kind == "empty"


async def test_extract_error_kinds_map(monkeypatch):
    def _missing(url, **kw):
        raise MissingDependency("need yt-dlp")

    monkeypatch.setattr(ingestion, "extract_url", _missing)
    with pytest.raises(IngestError) as ei:
        await ingest(IngestSource.from_url("https://y"), ctx=_ctx())
    assert ei.value.kind == "missing_dependency" and "yt-dlp" in str(ei.value)

    def _unsupported(url, **kw):
        raise UnsupportedSource(".xyz")

    monkeypatch.setattr(ingestion, "extract_url", _unsupported)
    with pytest.raises(IngestError) as ei:
        await ingest(IngestSource.from_url("https://z"), ctx=_ctx())
    assert ei.value.kind == "unsupported"


async def test_preview_counts_without_storing(monkeypatch):
    monkeypatch.setattr(
        ingestion, "extract_url", lambda url, **kw: ExtractResult(text="x" * 2500, title="Big", source_type="html")
    )
    store = _FakeStore()
    prev = await ingest_preview(IngestSource.from_url("https://x"), ctx=_ctx(store), snippet_chars=100)
    assert store.docs == []  # nothing persisted — dry run
    assert prev.chunks >= 1 and prev.chars == 2500 and prev.truncated is True and len(prev.snippet) == 100
    assert prev.title == "Big" and prev.source == "https://x"


def test_registry_seeds_read_write_metadata():
    reg = registry()
    assert reg["knowledge.ingest"].mutates is True
    assert reg["knowledge.ingest_preview"].mutates is False  # dry run — admissible to read-only
    assert reg["knowledge.ingest"].summary  # non-empty one-liner for the catalog
