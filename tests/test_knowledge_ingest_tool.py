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


class _FakeBgMgr:
    """Captures spawn_work calls without running them, so the test controls timing."""

    def __init__(self) -> None:
        self.spawned: list[dict] = []

    async def spawn_work(self, *, origin_session, kind, description, work, detail=""):
        self.spawned.append(
            {"origin_session": origin_session, "kind": kind, "description": description, "detail": detail, "work": work}
        )
        return "bg-fake123"


def _ingest_tool_bg(store, mgr):
    return {t.name: t for t in get_all_tools(store, background_mgr=mgr)}["knowledge_ingest"]


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


# ── inline vs. background (ADR 0050) ──────────────────────────────────────────


async def test_url_goes_background(monkeypatch):
    store = _FakeStore()
    mgr = _FakeBgMgr()
    out = await _ingest_tool_bg(store, mgr).ainvoke({"source": "https://youtu.be/abc", "domain": "talks"})

    assert "background" in out.lower() and "bg-fake123" in out
    assert len(mgr.spawned) == 1
    sp = mgr.spawned[0]
    assert sp["kind"] == "ingest" and sp["detail"] == "https://youtu.be/abc"
    assert store.docs == []  # detached — the work hasn't run yet


async def test_background_work_coroutine_actually_ingests(monkeypatch):
    # The coroutine handed to spawn_work is what the manager runs in the background —
    # verify it routes through the engine into the store when invoked.
    store = _FakeStore()
    mgr = _FakeBgMgr()
    monkeypatch.setattr(
        ingestion,
        "extract_url",
        lambda url, **kw: ExtractResult(text="transcript body", title="Talk", source_type="youtube"),
    )
    await _ingest_tool_bg(store, mgr).ainvoke({"source": "https://youtu.be/abc", "domain": "talks"})

    result = await mgr.spawned[0]["work"]()  # run the detached coroutine
    assert "Talk" in result and "youtube" in result and "talks" in result
    content, kw = store.docs[0]
    assert content == "transcript body" and kw["domain"] == "talks"


async def test_small_text_file_ingests_inline(tmp_path, monkeypatch):
    store = _FakeStore()
    mgr = _FakeBgMgr()
    f = tmp_path / "note.txt"
    f.write_text("small body")
    monkeypatch.setattr(
        ingestion,
        "extract_bytes",
        lambda filename, data, content_type, **kw: ExtractResult(text="small body", title=None, source_type="text"),
    )
    out = await _ingest_tool_bg(store, mgr).ainvoke({"source": str(f)})

    assert mgr.spawned == []  # stayed inline despite a manager being available
    assert "Ingested" in out
    assert store.docs and store.docs[0][0] == "small body"


async def test_media_file_goes_background(tmp_path):
    store = _FakeStore()
    mgr = _FakeBgMgr()
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"\x00fake-video")
    out = await _ingest_tool_bg(store, mgr).ainvoke({"source": str(f)})

    assert "background" in out.lower()
    assert len(mgr.spawned) == 1 and mgr.spawned[0]["detail"] == str(f)
    assert store.docs == []


async def test_large_text_file_goes_background(tmp_path):
    store = _FakeStore()
    mgr = _FakeBgMgr()
    f = tmp_path / "big.txt"
    f.write_text("x" * (64 * 1024 + 1))  # over the inline byte budget
    out = await _ingest_tool_bg(store, mgr).ainvoke({"source": str(f)})

    assert "background" in out.lower() and len(mgr.spawned) == 1
