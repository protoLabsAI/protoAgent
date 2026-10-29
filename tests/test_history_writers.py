"""ADR 0125 H1d: history writers never put transcripts in knowledge chunks."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from graph.compaction_op import compact_thread
from graph.conversation_harvest import harvest_thread
from graph.middleware.compaction import CountingSummarizationMiddleware
from knowledge.history import HistoryStore
from knowledge.store import KnowledgeStore
from runtime.state import STATE


@pytest.fixture
def stores(tmp_path, monkeypatch):
    knowledge = KnowledgeStore(tmp_path / "agent.db")
    history = HistoryStore(tmp_path / "agent.db")
    monkeypatch.setattr(STATE, "knowledge_store", knowledge)
    monkeypatch.setattr(STATE, "history_store", history)
    return knowledge, history


@pytest.mark.parametrize("automatic", [False, True])
@pytest.mark.parametrize("knowledge_available", [False, True])
@pytest.mark.parametrize("multipart", [False, True])
async def test_archive_metadata_and_no_chunks(stores, monkeypatch, automatic, knowledge_available, multipart):
    knowledge, history = stores
    writer_knowledge = knowledge if knowledge_available else None
    monkeypatch.setattr(STATE, "knowledge_store", writer_knowledge)
    monkeypatch.setattr(
        "graph.conversation_harvest.message_dates",
        lambda *args, **kwargs: {"h1": "2026-08-01", "h2": "2026-08-02"},
    )
    def content(text):
        return [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": "https://example.com/image.png"}},
        ] if multipart else text

    state = {
        "session_id": "session",
        "messages": [HumanMessage(content("first"), id="h1"), AIMessage(content("last"), id="h2")],
    }
    if automatic:
        mw = CountingSummarizationMiddleware.__new__(CountingSummarizationMiddleware)
        mw._archive(state, "thread")
    else:
        graph = SimpleNamespace(aget_state=AsyncMock(return_value=SimpleNamespace(values=state)),
                                aupdate_state=AsyncMock())
        result = await compact_thread(
            graph, object(), writer_knowledge, object(), "thread", "session",
            keep_recent=0, summarizer=AsyncMock(return_value="summary"),
        )
        assert result["archived_chunks"] == 1
        graph.aupdate_state.assert_awaited_once()
    (entry,) = history.list_for_session("session")
    assert (entry.kind, entry.thread_id, entry.namespace) == ("archive", "thread", "chat-archive:session")
    assert (entry.span_start, entry.span_end) == ("2026-08-01", "2026-08-02")
    assert "first" in entry.content and "last" in entry.content
    assert "User [2026-08-01]: first" in entry.content
    assert "Assistant [2026-08-02]: last" in entry.content
    if multipart:
        assert entry.content.count("_[image_url]_") == 2
    assert knowledge.list_chunks() == []


@pytest.mark.parametrize("failure", ["missing", "raises"])
@pytest.mark.parametrize("force", [False, True])
async def test_manual_archive_failure_preserves_checkpoint_unless_forced(stores, monkeypatch, caplog, failure, force):
    knowledge, history = stores
    if failure == "missing":
        monkeypatch.setattr(STATE, "history_store", None)
    else:
        def fail(*args, **kwargs):
            raise RuntimeError("disk failure")
        monkeypatch.setattr(history, "add", fail)
    graph = SimpleNamespace(
        aget_state=AsyncMock(return_value=SimpleNamespace(values={"messages": [HumanMessage("full history")]})),
        aupdate_state=AsyncMock(),
    )
    summary = AsyncMock(return_value="summary")
    result = await compact_thread(
        graph, object(), knowledge, object(), "thread", "session",
        keep_recent=0, summarizer=summary, force=force,
    )
    assert result["archived"] is False
    assert result["refused"] is not force
    if force:
        graph.aupdate_state.assert_awaited_once()
        assert "FORCE" in caplog.text
    else:
        graph.aupdate_state.assert_not_awaited()
        summary.assert_not_awaited()
        assert result["reason"] == ("no_store" if failure == "missing" else "archive_error")
    assert knowledge.list_chunks() == []


@pytest.mark.parametrize("multipart", [False, True])
async def test_summary_uses_checkpoint_session_and_keeps_facts_in_chunks(stores, multipart):
    knowledge, history = stores
    content = [{"type": "text", "text": "I prefer teal"}] if multipart else "I prefer teal"
    checkpoint = SimpleNamespace(checkpoint={
        "ts": "2026-08-02T12:00:00Z",
        "channel_values": {"session_id": "session", "messages": [HumanMessage(content)]},
    })
    summarizer = AsyncMock(return_value="Teal preference.")
    entry_id = await harvest_thread(
        "custom:thread:goal-iter-2",
        checkpointer=SimpleNamespace(aget_tuple=AsyncMock(return_value=checkpoint)),
        knowledge_store=knowledge, config=SimpleNamespace(knowledge_facts=True),
        summarizer=summarizer,
        fact_extractor=AsyncMock(return_value=["The user prefers teal."]),
        namespace="project",
    )
    entry = history.get(entry_id)
    assert "User: I prefer teal" in summarizer.call_args.args[0]
    assert (entry.session_id, entry.thread_id, entry.kind) == ("session", "custom:thread:goal-iter-2", "summary")
    assert entry.namespace == "project"
    chunks = knowledge.list_chunks()
    assert len(chunks) == 1 and chunks[0].domain == "fact"


async def test_summary_does_not_require_a_knowledge_store(stores, monkeypatch):
    knowledge, history = stores
    monkeypatch.setattr(STATE, "knowledge_store", None)
    checkpoint = SimpleNamespace(checkpoint={
        "channel_values": {"session_id": "session", "messages": [HumanMessage("conversation")]},
    })
    extractor = AsyncMock()
    entry_id = await harvest_thread(
        "thread",
        checkpointer=SimpleNamespace(aget_tuple=AsyncMock(return_value=checkpoint)),
        knowledge_store=None, config=SimpleNamespace(knowledge_facts=True),
        summarizer=AsyncMock(return_value="Retirement summary."),
        fact_extractor=extractor,
    )
    entry = history.get(entry_id)
    assert (entry.session_id, entry.thread_id, entry.kind) == ("session", "thread", "summary")
    assert entry.content == "Retirement summary."
    extractor.assert_not_awaited()
    assert knowledge.list_chunks() == []


async def test_report_is_full_history_under_origin(stores):
    from server.a2a import _BG_WAKE_TASKS, _should_index_report, _spawn_report_index
    import asyncio

    knowledge, history = stores
    job = SimpleNamespace(id="job", status="completed", origin_session="origin", origin_incognito=False,
                          description="Research", result="Full report\n" * 100)
    assert _should_index_report(job)
    _spawn_report_index(job)
    await asyncio.gather(*list(_BG_WAKE_TASKS))
    (entry,) = history.list_for_session("origin")
    assert (entry.kind, entry.surface, entry.content) == ("report", "background", job.result)
    assert knowledge.list_chunks() == []


@pytest.mark.parametrize("incognito,thread", [(True, "thread"), (False, "background:job"), (False, "a2a:background:job")])
async def test_retirement_skips_leave_both_stores_empty(stores, incognito, thread):
    knowledge, history = stores
    cp = SimpleNamespace(aget_tuple=AsyncMock(return_value=SimpleNamespace(checkpoint={
        "channel_values": {"incognito": incognito, "messages": [HumanMessage("secret")]}
    })))
    summary = AsyncMock(return_value="secret")
    assert await harvest_thread(thread, checkpointer=cp, knowledge_store=knowledge,
                                config=object(), summarizer=summary) is None
    summary.assert_not_awaited()
    assert history.search("secret") == []
    assert knowledge.list_chunks() == []


def test_auto_resolves_history_after_construction(stores, monkeypatch):
    _, history = stores
    mw = CountingSummarizationMiddleware.__new__(CountingSummarizationMiddleware)
    # Resolving on every archive also observes a store reload.
    monkeypatch.setattr(STATE, "history_store", None)
    mw._archive({"session_id": "s", "messages": [HumanMessage("before")]})
    monkeypatch.setattr(STATE, "history_store", history)
    mw._archive({"session_id": "s", "messages": [HumanMessage("after")]})
    assert len(history.list_for_session("s")) == 1


async def test_summary_write_failure_obeys_retirement_error_policy(stores, monkeypatch):
    knowledge, history = stores

    def fail(*args, **kwargs):
        raise RuntimeError("history unavailable")

    monkeypatch.setattr(history, "add", fail)
    cp = SimpleNamespace(aget_tuple=AsyncMock(return_value=SimpleNamespace(checkpoint={
        "channel_values": {"messages": [HumanMessage("conversation")]}
    })))
    kwargs = dict(checkpointer=cp, knowledge_store=knowledge, config=object(),
                  summarizer=AsyncMock(return_value="summary"))
    assert await harvest_thread("thread", **kwargs) is None
    with pytest.raises(RuntimeError, match="history unavailable"):
        await harvest_thread("thread", raise_on_error=True, **kwargs)
    assert knowledge.list_chunks() == []


def test_report_gate_depends_only_on_history(stores, monkeypatch):
    from server.a2a import _should_index_report

    _, history = stores
    job = SimpleNamespace(status="completed", origin_session="origin", origin_incognito=False,
                          result="report" * 200)
    assert not _should_index_report(job, history_store=None)
    monkeypatch.setattr(STATE, "knowledge_store", None)
    assert _should_index_report(job)
    monkeypatch.setattr(STATE, "history_store", None)
    assert not _should_index_report(job)
    assert _should_index_report(job, history_store=history)
