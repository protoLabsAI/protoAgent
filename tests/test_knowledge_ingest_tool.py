"""knowledge_ingest tool — the agent-facing half of the console
``/api/knowledge/ingest`` route. The ingestion engine itself is covered by
test_ingestion.py; here we test the *tool wiring*: get_all_tools exposes it when
a store is present, URLs and files route through the engine into the store, and
the missing-file / media-not-configured paths degrade with a clear message
instead of crashing the turn.
"""

from __future__ import annotations

import ingestion
from ingestion import ExtractResult, MissingDependency
from tools.lg_tools import MEMORY_TOOL_NAMES, get_all_tools


class _FakeStore:
    """Minimal KnowledgeStore stand-in: records add_document calls."""

    def __init__(self) -> None:
        self.docs: list[tuple[str, dict]] = []

    def add_document(self, content, **kw):
        self.docs.append((content, kw))
        return [1, 2]  # pretend two chunks landed


def _ingest_tool(store):
    return {t.name: t for t in get_all_tools(store)}["knowledge_ingest"]


def test_get_all_tools_exposes_knowledge_ingest_when_store_wired():
    names = {t.name for t in get_all_tools(_FakeStore())}
    assert "knowledge_ingest" in names
    # also advertised in the pre-setup roster list (config_io._extra_tool_names)
    assert "knowledge_ingest" in MEMORY_TOOL_NAMES


def test_knowledge_ingest_absent_without_store():
    assert "knowledge_ingest" not in {t.name for t in get_all_tools(None)}


async def test_knowledge_ingest_url_routes_through_engine(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(
        ingestion,
        "extract_url",
        lambda url, **kw: ExtractResult(text="article body", title="An Article", source_type="html"),
    )
    out = await _ingest_tool(store).ainvoke({"source": "https://example.com/post", "domain": "research"})

    assert "An Article" in out and "html" in out and "research" in out
    content, kw = store.docs[0]
    assert content == "article body"
    assert kw["domain"] == "research"
    assert kw["source"] == "https://example.com/post"
    assert kw["heading"] == "An Article"
    assert kw["source_type"] == "html"


async def test_knowledge_ingest_file_routes_through_engine(tmp_path, monkeypatch):
    store = _FakeStore()
    f = tmp_path / "notes.txt"
    f.write_text("file body")
    captured: dict = {}

    def fake_extract_bytes(filename, data, content_type, **kw):
        captured.update(filename=filename, data=data)
        return ExtractResult(text="file body", title=None, source_type="text")

    monkeypatch.setattr(ingestion, "extract_bytes", fake_extract_bytes)
    out = await _ingest_tool(store).ainvoke({"source": str(f)})

    assert "text" in out
    assert captured["filename"] == "notes.txt"
    assert captured["data"] == b"file body"
    # title falls back to None → heading is None, source is the resolved path
    _content, kw = store.docs[0]
    assert kw["source"] == str(f) and kw["heading"] is None


async def test_knowledge_ingest_missing_file_gives_clear_message():
    store = _FakeStore()
    out = await _ingest_tool(store).ainvoke({"source": "/no/such/file.pdf"})
    assert "no such file" in out.lower()
    assert store.docs == []  # nothing written


async def test_knowledge_ingest_media_not_configured_message(monkeypatch):
    store = _FakeStore()

    def boom(url, **kw):
        raise MissingDependency("audio/video needs knowledge.transcribe_model")

    monkeypatch.setattr(ingestion, "extract_url", boom)
    out = await _ingest_tool(store).ainvoke({"source": "https://example.com/talk.mp3"})
    assert "transcribe_model" in out
    assert store.docs == []


async def test_knowledge_ingest_empty_source_rejected():
    store = _FakeStore()
    out = await _ingest_tool(store).ainvoke({"source": "   "})
    assert "Error" in out
    assert store.docs == []
