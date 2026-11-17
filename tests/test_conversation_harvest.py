"""Tests for harvesting retired conversations into the knowledge base."""

from __future__ import annotations

import asyncio

import pytest

from knowledge.history import HistoryStore
from runtime.state import STATE

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, MessagesState, StateGraph

from graph.checkpoint_prune import delete_thread, find_aged_threads
from graph.checkpointer import build_sqlite_checkpointer
from graph.conversation_harvest import harvest_thread, render_transcript


@pytest.fixture(autouse=True)
def history_store(tmp_path, monkeypatch):
    store = HistoryStore(tmp_path / "kb.db")
    monkeypatch.setattr(STATE, "history_store", store)
    # ADR 0132 D3b: harvest writes a "closed" digest record under memory_path(); pin it to
    # a tmp dir so the suite never touches the real instance store.
    monkeypatch.setenv("MEMORY_PATH", str(tmp_path / "memory"))
    return store


def test_render_transcript_cleans_and_skips_noise():
    msgs = [
        HumanMessage(content="what is 2+2?"),
        AIMessage(content="<scratch_pad>add them</scratch_pad>It's 4."),
        AIMessage(content="   "),  # empty → skipped
    ]
    t = render_transcript(msgs)
    assert "User: what is 2+2?" in t
    assert "Assistant: It's 4." in t  # leaked scratch_pad dropped, native answer kept
    assert "scratch_pad" not in t


@pytest.mark.parametrize("message_type,role", [(HumanMessage, "User"), (AIMessage, "Assistant")])
def test_render_transcript_preserves_multipart_content(message_type, role):
    message = message_type(
        content=[
            {"type": "text", "text": "Caption"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,private"}},
            "More text",
            {"type": "text", "text": "<scratch_pad>hidden</scratch_pad>Answer"},
        ]
    )
    transcript = render_transcript([message])
    assert transcript.startswith(f"{role}: Caption\n\n_[image_url]_\n\nMore text")
    assert "Answer" in transcript
    assert "base64" not in transcript
    if message_type is AIMessage:
        assert "hidden" not in transcript


def test_render_transcript_preserves_attachment_only_message():
    assert (
        render_transcript(
            [
                HumanMessage(
                    content=[
                        {"type": "image_url", "image_url": {"url": "https://example.com/image.png"}},
                    ]
                )
            ]
        )
        == "User: _[image_url]_"
    )


class _FakeKnowledge:
    def __init__(self):
        self.chunks = []

    def add_chunk(self, content, domain=None, heading=None, *, source=None, source_type=None, namespace=None, **kw):
        self.chunks.append(
            {
                "content": content,
                "domain": domain,
                "heading": heading,
                "source": source,
                "source_type": source_type,
                "namespace": namespace,
            }
        )
        return f"chunk-{len(self.chunks)}"


def _seed(db, thread="a2a:chat-1", *, operator_chat_turn=True):
    """One checkpointed exchange. ``operator_chat_turn`` is the server's per-turn stamp
    (True = an authenticated operator chat); ``None`` leaves it unset, like a thread
    from before the stamp existed."""
    from graph.state import ProtoAgentState

    g = StateGraph(ProtoAgentState if operator_chat_turn is not None else MessagesState)
    g.add_node("n", lambda s: {"messages": [AIMessage(content="<output>noted</output>")]})
    g.add_edge(START, "n")
    g.add_edge("n", END)
    inputs = {"messages": [HumanMessage(content="my favorite color is teal")]}
    if operator_chat_turn is not None:
        inputs["operator_chat_turn"] = operator_chat_turn

    async def main():
        app = g.compile(checkpointer=build_sqlite_checkpointer(db))
        await app.ainvoke(inputs, {"configurable": {"thread_id": thread}})

    asyncio.run(main())


def test_harvest_thread_summarizes_into_history(tmp_path, history_store):
    db = str(tmp_path / "c.db")
    _seed(db)
    saver = build_sqlite_checkpointer(db)
    kb = _FakeKnowledge()

    async def fake_summarizer(transcript, config):
        assert transcript == "User: my favorite color is teal\nAssistant: <output>noted</output>"
        return "User prefers teal."

    cid = asyncio.run(
        harvest_thread(
            "a2a:chat-1",
            checkpointer=saver,
            knowledge_store=kb,
            config=object(),
            summarizer=fake_summarizer,
        )
    )
    row = history_store.get(cid)
    assert row.kind == "summary"
    assert "teal" in row.content
    assert row.thread_id == "a2a:chat-1"
    assert kb.chunks == []


@pytest.mark.parametrize("archived", [False, True])
def test_harvest_covers_long_chat_opening_and_live_tail(tmp_path, history_store, archived):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from knowledge.memories import MemoryStore

    opening = "The user's launch code is ORCHID.\n" + "Earlier discussion.\n" * 2500
    tail = "Recent discussion.\n" * 1000 + "The user chose a Friday launch."
    assert len(opening + tail) > 60000
    if archived:
        history_store.add(opening, kind="archive", session_id="chat-1", namespace="chat-archive:chat-1")
        history_store.add("Unrelated secret", kind="archive", session_id="other")
        history_store.add("Old summary", kind="summary", session_id="chat-1")
    messages = [HumanMessage(content=tail if archived else opening + tail)]
    saver = SimpleNamespace(aget_tuple=AsyncMock(return_value=SimpleNamespace(checkpoint={
        "channel_values": {"messages": messages, "session_id": "chat-1", "operator_chat_turn": True},
    })))
    summary_inputs, fact_inputs = [], []

    async def summarize(transcript, config):
        summary_inputs.append(transcript)
        return " ".join(word for word in ("ORCHID", "Friday") if word in transcript) or "Discussion."

    async def facts(transcript, config):
        fact_inputs.append(transcript)
        return ["The user's launch code is ORCHID."] if "ORCHID" in transcript else []

    memory = MemoryStore(tmp_path / "facts.db")
    cid = asyncio.run(harvest_thread(
        "a2a:chat-1", checkpointer=saver, knowledge_store=None, history_store=history_store,
        memory_store=memory, config=SimpleNamespace(knowledge_facts=True),
        summarizer=summarize, fact_extractor=facts, raise_on_error=True,
    ))
    assert history_store.get(cid).content == "ORCHID Friday"
    assert summary_inputs[:-1] == fact_inputs
    assert all(len(part) <= 16000 for part in summary_inputs)
    raw = "".join(fact_inputs)
    assert opening.strip() in raw and tail in raw
    assert "Unrelated secret" not in raw and "Old summary" not in raw
    assert memory.list(kind="fact")[0].content == "The user's launch code is ORCHID."


@pytest.mark.parametrize("failure", ["archive", "part"])
def test_harvest_does_not_save_partial_summary_on_read_or_part_failure(history_store, monkeypatch, failure):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    saver = SimpleNamespace(aget_tuple=AsyncMock(return_value=SimpleNamespace(checkpoint={
        "channel_values": {"messages": [HumanMessage(content="x" * 60000)]},
    })))
    if failure == "archive":
        def fail_read(*args, **kwargs):
            raise RuntimeError("archive unavailable")
        monkeypatch.setattr(history_store, "list_for_session", fail_read)
    summarizer = AsyncMock(side_effect=["First part.", RuntimeError("part unavailable")])
    with pytest.raises(RuntimeError, match="unavailable"):
        asyncio.run(harvest_thread(
            "chat", checkpointer=saver, knowledge_store=None, config=object(),
            summarizer=summarizer, raise_on_error=True,
        ))
    assert history_store.stats()["summary"] == 0


def test_harvest_extracts_facts_when_enabled(tmp_path, history_store):
    """ADR 0021 + ADR 0125 P2-D3: the session-end pass distils facts (gated on
    knowledge_facts) into the core MemoryStore — provenance='extracted', pending,
    scoped to the namespace — and writes NOTHING to the knowledge store/chunks."""
    from types import SimpleNamespace

    from knowledge.memories import MemoryStore
    from knowledge.store import KnowledgeStore

    db = str(tmp_path / "c.db")
    _seed(db)
    saver = build_sqlite_checkpointer(db)
    knowledge = KnowledgeStore(tmp_path / "kb.db")
    memory = MemoryStore(tmp_path / "agent.db")
    cfg = SimpleNamespace(knowledge_facts=True)

    async def fake_summarizer(transcript, config):
        return "User prefers teal."

    async def fake_facts(transcript, config):
        return ["The user's favorite color is teal"]

    cid = asyncio.run(
        harvest_thread(
            "a2a:chat-1",
            checkpointer=saver,
            knowledge_store=knowledge,
            memory_store=memory,
            config=cfg,
            summarizer=fake_summarizer,
            namespace="proj-x",
            fact_extractor=fake_facts,
        )
    )
    assert cid is not None
    # Episodic summary → history (namespaced); semantic fact → memories, nothing to chunks.
    summaries = history_store.list_for_session("a2a:chat-1")
    assert len(summaries) == 1 and summaries[0].namespace == "proj-x"
    assert summaries[0].thread_id == "a2a:chat-1"
    assert knowledge.list_chunks(domain="fact") == []
    assert knowledge.list_chunks(domain="conversation") == []
    facts = memory.list(kind="fact", provenance="extracted", scope="proj-x")
    assert len(facts) == 1 and "teal" in facts[0].content
    assert facts[0].review_state == "pending"
    # Provenance (ADR 0069 D5): the fact carries the originating session/thread id.
    assert facts[0].source_session == "a2a:chat-1" and facts[0].thread_id == "a2a:chat-1"


@pytest.mark.parametrize(
    "thread, operator_chat_turn",
    [
        ("a2a:peer-brief", False),  # a delegated peer agent, a /v1 client, a scheduled turn
        ("a2a:legacy-thread", None),  # retired from before the server stamped the flag
        ("a2a:system:activity", True),  # system threads never, whatever the flag says
    ],
)
def test_harvest_extracts_facts_only_from_operator_chats(tmp_path, history_store, thread, operator_chat_turn):
    """2026-11-08 audit: 121 of one agent's memories were facts mined from peer-agent and
    API threads, recording the peer as "the user". Those threads still get their history
    summary, but the extractor never runs on them."""
    from types import SimpleNamespace

    from knowledge.memories import MemoryStore

    db = str(tmp_path / "c.db")
    _seed(db, thread, operator_chat_turn=operator_chat_turn)
    memory = MemoryStore(tmp_path / "agent.db")

    async def fake_summarizer(transcript, config):
        return "A peer asked for a board card."

    calls = []

    async def peer_facts(transcript, config):  # extraction errors are swallowed, so record
        calls.append(transcript)
        return [{"subject": "designsystem-role", "content": "The user (designSystem) boards UI work"}]

    cid = asyncio.run(
        harvest_thread(
            thread,
            checkpointer=build_sqlite_checkpointer(db),
            knowledge_store=None,
            memory_store=memory,
            config=SimpleNamespace(knowledge_facts=True),
            summarizer=fake_summarizer,
            fact_extractor=peer_facts,
            raise_on_error=True,
        )
    )
    assert cid is not None and history_store.get(cid).content.endswith("A peer asked for a board card.")
    assert calls == []
    assert memory.list(kind="fact") == []


def test_default_summarizer_drops_provider_reasoning_blocks(monkeypatch):
    """A Responses provider's aux reply is a block list: an opaque reasoning block
    (``encrypted_content``) and then the text. ``str()`` of that list landed in history as
    the summary (15 such rows in the audit); only the text may.

    ADR 0133: the seam resolves its model via ``create_role_llm(config, "harvest")`` (which
    reads ``memory.harvest_model``). A single-string ``aux_model`` is a one-entry chain, so
    the role LLM is the built model unchanged — the text-extraction behaviour under test is
    unaffected."""
    import graph.model_roles
    from types import SimpleNamespace

    from graph.conversation_harvest import _default_summarizer

    reply = AIMessage(
        content=[
            {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "gAAAAABqopaque"},
            {"type": "thinking", "thinking": "", "signature": "CAQSsig"},
            {"type": "text", "text": "User asked for a one-word reply."},
        ]
    )

    class _LLM:
        async def ainvoke(self, messages):
            return reply

    monkeypatch.setattr(graph.model_roles, "create_llm", lambda config, model_name=None, **kw: _LLM())
    out = asyncio.run(_default_summarizer("User: alive?", SimpleNamespace(aux_model="aux")))
    assert out == "User asked for a one-word reply."


def test_harvest_pass_builds_from_memory_harvest_model(monkeypatch):
    """ADR 0133 fix: the session-end harvest pass resolves its model from
    ``memory.harvest_model`` — the chain Settings writes — not the aux fallback. A primary
    that rate-limits falls through to the next chain entry. On the pre-fix code the "harvest"
    role read nothing and inherited aux, so this would build ``aux`` and never touch [x, y]."""
    import graph.model_roles
    from types import SimpleNamespace

    from graph.conversation_harvest import _default_summarizer

    built: list[str] = []

    class _Transient(Exception):
        def __init__(self):
            super().__init__("transient")
            self.status_code = 429

    class _LLM:
        def __init__(self, name):
            self.name = name

        async def ainvoke(self, messages, config=None, **kwargs):
            built.append(self.name)
            if self.name == "x":
                raise _Transient()
            return AIMessage(content="harvest summary")

    monkeypatch.setattr(graph.model_roles, "_last_fallbacks", {})  # isolate M2's process-global
    monkeypatch.setattr(graph.model_roles, "_streaks", set())
    monkeypatch.setattr(graph.model_roles, "create_llm", lambda config, model_name=None, **kw: _LLM(model_name))
    cfg = SimpleNamespace(aux_model="aux", memory_harvest_model=["x", "y"])
    out = asyncio.run(_default_summarizer("User: anything to remember?", cfg))
    assert out == "harvest summary"
    assert built == ["x", "y"]  # read memory.harvest_model and fell x → y; aux never built


def test_harvest_skips_facts_when_disabled(tmp_path):
    from types import SimpleNamespace

    from knowledge.memories import MemoryStore
    from knowledge.store import KnowledgeStore

    db = str(tmp_path / "c.db")
    _seed(db)
    saver = build_sqlite_checkpointer(db)
    knowledge = KnowledgeStore(tmp_path / "kb.db")
    memory = MemoryStore(tmp_path / "agent.db")

    async def fake_summarizer(transcript, config):
        return "summary"

    async def boom_facts(transcript, config):
        raise AssertionError("fact extractor must not run when disabled")

    asyncio.run(
        harvest_thread(
            "a2a:chat-1",
            checkpointer=saver,
            knowledge_store=knowledge,
            memory_store=memory,
            config=SimpleNamespace(knowledge_facts=False),
            summarizer=fake_summarizer,
            fact_extractor=boom_facts,
        )
    )
    assert memory.list(kind="fact") == []
    assert knowledge.list_chunks(domain="fact") == []


def test_harvest_skips_facts_without_memory_store(tmp_path, caplog):
    """ADR 0125 P2-D3: with no MemoryStore (plugin hosts, tests) the session-end pass logs
    the skip and extracts no facts — it never falls back to writing memory through chunks."""
    from types import SimpleNamespace

    from knowledge.store import KnowledgeStore

    db = str(tmp_path / "c.db")
    _seed(db)
    saver = build_sqlite_checkpointer(db)
    knowledge = KnowledgeStore(tmp_path / "kb.db")

    async def fake_summarizer(transcript, config):
        return "User prefers teal."

    async def boom_facts(transcript, config):
        raise AssertionError("extraction must be skipped when there is no memory store")

    with caplog.at_level("INFO"):
        cid = asyncio.run(
            harvest_thread(
                "a2a:chat-1",
                checkpointer=saver,
                knowledge_store=knowledge,
                memory_store=None,
                config=SimpleNamespace(knowledge_facts=True),
                summarizer=fake_summarizer,
                fact_extractor=boom_facts,
            )
        )
    assert cid is not None  # the episodic summary still lands
    assert knowledge.list_chunks(domain="fact") == []  # chunks untouched — no chunk fallback
    assert "no memory store" in caplog.text


def test_harvest_does_not_re_raise_on_fact_write_failure(tmp_path, history_store):
    """ADR 0125 P2-D3: MemoryStore.add/supersede raise on SQLite errors (where add_chunk
    returned None). A fact-write failure must NOT propagate out of harvest_thread, even under
    raise_on_error — the episodic summary already wrote, so a re-raise would make the retire
    sweep keep the thread and re-harvest it. Fact capture is best-effort."""
    import sqlite3
    from types import SimpleNamespace

    db = str(tmp_path / "c.db")
    _seed(db)

    class _RaisingMemory:
        def list(self, **kw):
            return []

        def add(self, *a, **kw):
            raise sqlite3.OperationalError("database is locked")

        def supersede(self, *a, **kw):
            return False

    async def fake_summarizer(transcript, config):
        return "User prefers teal."

    async def fake_facts(transcript, config):
        return ["The user's favorite color is teal"]

    cid = asyncio.run(
        harvest_thread(
            "a2a:chat-1",
            checkpointer=build_sqlite_checkpointer(db),
            knowledge_store=None,
            memory_store=_RaisingMemory(),
            config=SimpleNamespace(knowledge_facts=True),
            summarizer=fake_summarizer,
            namespace="proj-x",
            fact_extractor=fake_facts,
            raise_on_error=True,
        )
    )
    # The summary landed and harvest did NOT re-raise despite the fact-write failure.
    assert cid is not None
    summaries = history_store.list_for_session("a2a:chat-1")
    assert len(summaries) == 1 and summaries[0].thread_id == "a2a:chat-1"


def test_harvest_noop_without_history_store(tmp_path):
    db = str(tmp_path / "c.db")
    _seed(db)
    saver = build_sqlite_checkpointer(db)
    assert (
        asyncio.run(
            harvest_thread("a2a:chat-1", checkpointer=saver, knowledge_store=None, history_store=None, config=object())
        )
        is None
    )


def test_harvest_noop_on_unknown_thread(tmp_path):
    db = str(tmp_path / "c.db")
    _seed(db)
    saver = build_sqlite_checkpointer(db)
    kb = _FakeKnowledge()

    async def _boom(transcript, config):
        raise AssertionError("should not summarize an empty/unknown thread")

    out = asyncio.run(
        harvest_thread("a2a:nope", checkpointer=saver, knowledge_store=kb, config=object(), summarizer=_boom)
    )
    assert out is None and kb.chunks == []


def test_harvest_skips_incognito_thread(tmp_path):
    """ADR 0069 D3b: incognito means NO memory trail — the retire sweep
    (harvest_enabled defaults ON) must not summarize the thread into the
    knowledge store, where RAG would re-inject it into later prompts."""
    from graph.state import ProtoAgentState

    db = str(tmp_path / "c.db")
    g = StateGraph(ProtoAgentState)
    g.add_node("n", lambda s: {"messages": [AIMessage(content="noted")]})
    g.add_edge(START, "n")
    g.add_edge("n", END)

    async def main():
        app = g.compile(checkpointer=build_sqlite_checkpointer(db))
        await app.ainvoke(
            {"messages": [HumanMessage(content="my secret color is teal")], "incognito": True},
            {"configurable": {"thread_id": "a2a:incog"}},
        )

    asyncio.run(main())
    saver = build_sqlite_checkpointer(db)
    kb = _FakeKnowledge()

    async def _boom(transcript, config):
        raise AssertionError("incognito thread must not be summarized")

    out = asyncio.run(
        harvest_thread("a2a:incog", checkpointer=saver, knowledge_store=kb, config=object(), summarizer=_boom)
    )
    assert out is None and kb.chunks == []


def test_find_aged_threads_and_delete(tmp_path):
    import sqlite3

    db = str(tmp_path / "c.db")
    _seed(db, thread="recent")
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata) "
        "VALUES (?,?,?,?,?,?,?)",
        ("stale", "", "1dc8b9f0-0000-6000-8000-000000000000", None, "", b"{}", b"{}"),
    )
    conn.commit()
    conn.close()

    aged = find_aged_threads(db, max_age_seconds=86400)
    assert aged == ["stale"]
    assert delete_thread(db, "stale") == 1
    assert find_aged_threads(db, max_age_seconds=86400) == []


# ── #2946: a FAILED harvest must not cost the conversation its knowledge ────────


def _retire_env(monkeypatch, tmp_path, harvest_behavior):
    """Point server.agent_init.STATE at a minimal retire-able world and record
    deletions. ``harvest_behavior`` is the fake harvest_thread."""
    from types import SimpleNamespace

    import graph.conversation_harvest as ch
    import server.agent_init as ai
    from graph import checkpoint_prune

    monkeypatch.setattr(ai.STATE, "graph_config", SimpleNamespace(checkpoint_harvest_enabled=True), raising=False)
    monkeypatch.setattr(ai.STATE, "checkpointer", object(), raising=False)
    monkeypatch.setattr(ai.STATE, "knowledge_store", object(), raising=False)
    monkeypatch.setattr(ai.STATE, "checkpoint_path", str(tmp_path / "ck.db"), raising=False)
    monkeypatch.setattr(ch, "harvest_thread", harvest_behavior)
    deleted = []
    monkeypatch.setattr(checkpoint_prune, "delete_thread", lambda path, tid, cascade=True: deleted.append(tid))
    ai._HARVEST_FAILURES.clear()
    return ai, deleted


async def test_sweep_retire_keeps_the_thread_when_harvest_fails(monkeypatch, tmp_path):
    """The TTL sweep path (#2946): a transient harvest failure (the shared-account
    429 burst) keeps the thread for the next sweep instead of deleting it — the old
    flow deleted anyway, permanently skipping knowledge capture."""

    async def _boom(thread_id, **kw):
        raise RuntimeError("429 burst")

    ai, deleted = _retire_env(monkeypatch, tmp_path, _boom)

    assert await ai._retire_thread("t-1") is None
    assert deleted == []  # kept for retry
    assert ai._HARVEST_FAILURES["t-1"] == 1


async def test_sweep_retire_deletes_after_the_failure_cap(monkeypatch, tmp_path):
    """A permanently-broken harvest can't pin checkpoints forever: at the cap the
    thread deletes anyway (loudly)."""

    async def _boom(thread_id, **kw):
        raise RuntimeError("still broken")

    ai, deleted = _retire_env(monkeypatch, tmp_path, _boom)

    for _ in range(ai._HARVEST_FAILURE_CAP - 1):
        await ai._retire_thread("t-1")
    assert deleted == []
    await ai._retire_thread("t-1")  # cap reached → delete proceeds
    assert deleted == ["t-1"]
    assert "t-1" not in ai._HARVEST_FAILURES  # counter cleared with the thread


async def test_explicit_delete_still_deletes_when_harvest_fails(monkeypatch, tmp_path):
    """The delete-chat dialog path (explicit harvest bool): the operator asked for
    deletion — a failed harvest is logged loudly but must not block it."""

    async def _boom(thread_id, **kw):
        raise RuntimeError("boom")

    ai, deleted = _retire_env(monkeypatch, tmp_path, _boom)

    await ai._retire_thread("t-x", harvest=True)
    assert deleted == ["t-x"]
    assert "t-x" not in ai._HARVEST_FAILURES


async def test_successful_harvest_clears_the_failure_counter(monkeypatch, tmp_path):
    """A success between failures resets the retry budget — the cap is for
    CONSECUTIVE failures, not lifetime ones."""
    calls = {"n": 0}

    async def _flaky(thread_id, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return "chunk-1"

    ai, deleted = _retire_env(monkeypatch, tmp_path, _flaky)

    await ai._retire_thread("t-2")  # fails → kept
    assert deleted == [] and ai._HARVEST_FAILURES["t-2"] == 1
    assert await ai._retire_thread("t-2") == "chunk-1"  # succeeds → harvested + deleted
    assert deleted == ["t-2"]
    assert "t-2" not in ai._HARVEST_FAILURES


async def test_harvest_thread_raise_on_error_reraises(tmp_path):
    """The flag that lets the retire path tell FAILURE from legitimately-nothing:
    default swallows (None), raise_on_error re-raises."""
    import pytest as _pytest

    from graph.conversation_harvest import harvest_thread

    class _BoomCheckpointer:
        async def aget_tuple(self, cfg):
            raise RuntimeError("checkpointer exploded")

    kwargs = dict(
        checkpointer=_BoomCheckpointer(),
        knowledge_store=object(),
        config=object(),
    )
    assert await harvest_thread("t-e", **kwargs) is None  # default: swallowed
    with _pytest.raises(RuntimeError):
        await harvest_thread("t-e", raise_on_error=True, **kwargs)


def test_harvest_dates_summary_and_facts_to_the_conversation(tmp_path, history_store):
    # Regression (2026-09-10): the TTL sweep retired August threads and stored their facts
    # undated, so "the user runs model X" was recalled weeks later as the current setup.
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from knowledge.memories import MemoryStore

    db = str(tmp_path / "c.db")
    _seed(db)
    memory = MemoryStore(tmp_path / "agent.db")

    async def fake_summarizer(transcript, config):
        return "User prefers teal."

    async def fake_facts(transcript, config):
        return ["The user prefers teal."]

    asyncio.run(
        harvest_thread(
            "a2a:chat-1",
            checkpointer=build_sqlite_checkpointer(db),
            knowledge_store=None,
            memory_store=memory,
            config=SimpleNamespace(knowledge_facts=True),
            summarizer=fake_summarizer,
            fact_extractor=fake_facts,
        )
    )
    day = datetime.now(UTC).date().isoformat()  # the seeded thread's last checkpoint
    summary = history_store.list_for_session("a2a:chat-1")[0]
    assert summary.content.startswith(f"[as of {day}] ")
    assert summary.heading.endswith(f", last active {day})")
    facts = memory.list(kind="fact", provenance="extracted")
    assert facts and facts[0].content == f"[as of {day}] The user prefers teal."


# ── ADR 0132 D3b: a harvested chat stays in the digest, marked "closed", for 14 days ──


def _seed_chat(db, thread, session_id, *, first="help me pick a teal theme"):
    """One checkpointed exchange keyed to a console ``chat-`` session id, so the harvest
    writes its closed-session digest record under that id (surface "chat")."""
    from graph.state import ProtoAgentState

    g = StateGraph(ProtoAgentState)
    g.add_node("n", lambda s: {"messages": [AIMessage(content="<output>noted</output>")]})
    g.add_edge(START, "n")
    g.add_edge("n", END)

    async def main():
        app = g.compile(checkpointer=build_sqlite_checkpointer(db))
        await app.ainvoke(
            {"messages": [HumanMessage(content=first)], "session_id": session_id, "operator_chat_turn": True},
            {"configurable": {"thread_id": thread}},
        )

    asyncio.run(main())


async def _teal_summary(transcript, config):
    return "The user wants a teal theme. They also mentioned dark mode."


def _harvest_teal(db, *, config=None):
    from types import SimpleNamespace

    return asyncio.run(
        harvest_thread(
            "a2a:chat-9",
            checkpointer=build_sqlite_checkpointer(db),
            knowledge_store=None,
            config=config if config is not None else SimpleNamespace(),
            summarizer=_teal_summary,
        )
    )


def test_harvested_chat_shows_as_closed_with_gist_then_expires(tmp_path, history_store):
    """A chat harvested with "Save a summary" stays in the digest, marked closed, with the
    FIRST sentence of its summary as the gist — and drops out once past the 14-day window."""
    import json
    from datetime import UTC, datetime, timedelta
    from pathlib import Path

    from graph.middleware.memory import load_prior_sessions_digest, memory_path, session_filename

    db = str(tmp_path / "c.db")
    _seed_chat(db, "a2a:chat-9", "chat-9")
    assert _harvest_teal(db) is not None

    base = memory_path()
    block, ids = load_prior_sessions_digest(base)
    assert "chat-9" in ids
    assert "closed" in block
    assert "The user wants a teal theme." in block  # the gist is the first sentence only
    assert "dark mode" not in block

    # Past the retention window → gone. Backdate the record's closed_at directly so the
    # read-time age filter is what drops it (not the write-time prune).
    closed_path = Path(base) / "closed" / session_filename("chat-9")
    data = json.loads(closed_path.read_text(encoding="utf-8"))
    data["closed_at"] = (datetime.now(UTC) - timedelta(days=15)).isoformat()
    closed_path.write_text(json.dumps(data), encoding="utf-8")
    block2, ids2 = load_prior_sessions_digest(base)
    assert "chat-9" not in ids2
    assert "closed" not in block2


def _retirement_state(monkeypatch, db):
    from types import SimpleNamespace

    monkeypatch.setattr(STATE, "checkpointer", build_sqlite_checkpointer(db))
    monkeypatch.setattr(STATE, "checkpoint_path", db)
    monkeypatch.setattr(STATE, "graph_config", SimpleNamespace(checkpoint_harvest_enabled=True))
    monkeypatch.setattr(STATE, "knowledge_store", None)
    monkeypatch.setattr(STATE, "memory_store", None)
    monkeypatch.setattr(STATE, "a2a_task_engine", None)


@pytest.mark.parametrize("erase", [False, True], ids=["unharvested-delete", "erase-existing-summary"])
def test_delete_route_leaves_no_closed_entry(tmp_path, history_store, monkeypatch, erase):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import operator_api.chat_routes as routes
    from graph.middleware.memory import load_closed_pool, load_digest_pool, memory_path

    db = str(tmp_path / "c.db")
    _seed_chat(db, "a2a:chat-9", "chat-9")
    if erase:
        assert _harvest_teal(db) is not None
        assert load_closed_pool(memory_path(), closed_days=14)
    _write_open_card(memory_path(), "chat-9", topic="theme", gist="Open theme discussion.")
    _retirement_state(monkeypatch, db)
    app = FastAPI()
    routes.register_chat_routes(app, ui="none")
    with TestClient(app) as client:
        response = client.delete(f"/api/chat/sessions/chat-9?harvest=false&forget={str(erase).lower()}")
    assert response.status_code == 200
    assert response.json()["harvested"] is False
    assert asyncio.run(STATE.checkpointer.aget_tuple({"configurable": {"thread_id": "a2a:chat-9"}})) is None
    assert load_closed_pool(memory_path(), closed_days=14) == []
    assert load_digest_pool(memory_path(), closed_days=14)[0] == []


def test_idle_retirement_replaces_open_card_with_closed_summary(tmp_path, history_store, monkeypatch):
    import graph.conversation_harvest as ch
    from graph.middleware.memory import load_digest_pool, memory_path
    from server.maintenance_loops import _retire_thread

    db = str(tmp_path / "c.db")
    # A custom thread id proves cleanup uses the checkpoint's session identity.
    _seed_chat(db, "custom:theme", "chat-9")
    _write_open_card(memory_path(), "chat-9", topic="theme", gist="Stale open gist.")
    _retirement_state(monkeypatch, db)

    async def summarize(thread_id, **kwargs):
        return await harvest_thread(thread_id, summarizer=_teal_summary, **kwargs)

    monkeypatch.setattr(ch, "harvest_thread", summarize)
    assert asyncio.run(_retire_thread("custom:theme")) is not None
    assert asyncio.run(STATE.checkpointer.aget_tuple({"configurable": {"thread_id": "custom:theme"}})) is None
    pool, _ = load_digest_pool(memory_path(), closed_days=14)
    assert len(pool) == 1
    assert pool[0].session_id == "chat-9"
    assert "closed" in pool[0].line
    assert "The user wants a teal theme." in pool[0].line
    assert "Stale open gist." not in pool[0].line


def test_closed_carryover_off_writes_nothing(tmp_path, history_store):
    """memory_closed_session_days=0 turns the carry-over off: no closed record is written."""
    from types import SimpleNamespace

    from graph.middleware.memory import load_closed_pool, memory_path

    db = str(tmp_path / "c.db")
    _seed_chat(db, "a2a:chat-9", "chat-9")
    assert _harvest_teal(db, config=SimpleNamespace(memory_closed_session_days=0)) is not None
    assert load_closed_pool(memory_path(), closed_days=14) == []


def _write_open_card(base, session_id, *, topic, gist, timestamp="2026-11-16T10:00:00+00:00"):
    """Write an OPEN session card (the normal per-turn persist) under memory_path() — a chat
    whose live card is on disk, which the digest must prefer over any closed record for it."""
    import json
    import os

    from graph.middleware.memory import session_filename

    os.makedirs(base, exist_ok=True)
    with open(os.path.join(base, session_filename(session_id)), "w", encoding="utf-8") as fh:
        json.dump(
            {
                "session_id": session_id,
                "messages": [{"role": "user", "content": topic}],
                "message_count": 2,
                "gist": gist,
                "timestamp": timestamp,
            },
            fh,
        )


def test_resumed_harvested_chat_lists_open_while_its_live_card_exists(tmp_path, history_store):
    """ADR 0132 D3b: the open card wins. A chat harvested (idle sweep or "Save a summary") and
    then resumed under the same id keeps a live card, so the digest lists it OPEN — once, with
    its live gist — and suppresses the stale closed record; it is never shown closed while live."""
    from graph.middleware.memory import load_digest_pool, memory_path

    db = str(tmp_path / "c.db")
    _seed_chat(db, "a2a:chat-9", "chat-9")
    assert _harvest_teal(db) is not None  # closed record, gist "The user wants a teal theme."

    base = memory_path()
    # The chat was resumed after the harvest: its live open card is back on disk, fresh gist.
    _write_open_card(base, "chat-9", topic="back to the theme", gist="Still refining the palette.")

    pool, exists = load_digest_pool(base, closed_days=14)
    assert exists
    sids = [e.session_id for e in pool]
    assert sids.count("chat-9") == 1  # listed once, not double-listed as open AND closed
    (entry,) = [e for e in pool if e.session_id == "chat-9"]
    assert "closed" not in entry.line  # listed OPEN, not closed
    assert "Still refining the palette." in entry.line  # the LIVE gist…
    assert "The user wants a teal theme." not in entry.line  # …not the stale closed gist


def test_relevant_digest_falls_back_to_newest_when_fts_finds_nothing(tmp_path, history_store, monkeypatch):
    """ADR 0108 D9 fallback, preserved under ADR 0132 D3b: with zero FTS matches but a closed
    record on disk, the relevant digest falls back to the newest OPEN pool (plus the closed
    chat) — it must not collapse to only the closed chats, dropping the newest open ones."""
    import graph.session_search as session_search
    from graph.middleware.memory import load_digest, memory_path

    db = str(tmp_path / "c.db")
    _seed_chat(db, "a2a:chat-9", "chat-9")
    assert _harvest_teal(db) is not None  # closed record for chat-9

    base = memory_path()
    _write_open_card(base, "chat-open", topic="an open chat about routing", gist="Routing options.")

    monkeypatch.setattr(session_search, "search_session_summaries", lambda *a, **k: [])

    res = load_digest(policy="relevant", query="nothing matches this", memory_dir=base, closed_days=14)
    ids = [e.session_id for e in res.entries]
    assert "chat-open" in ids  # the newest OPEN chat survived the fallback
    assert "chat-9" in ids  # the closed chat still rides along, marked closed


# ── P1f.2 Conversations in Work: public-only harvest, archives and facts (ADR 0131 §5) ──
#
# A conversation that had private operator steering is harvested on a LATER ordinary turn
# (incognito already cleared), with private canaries retained in its checkpoint and a
# legacy private archive on disk. Every derived reader — summary, closed-chat card,
# compaction archive, shared-history search, fact recall — must learn only from the public
# part. These are the executed cases #5403 lacked (proof matrix row "Every derived reader
# on a later ordinary turn").

_STEER_CANARY = "STEERSECRET acquire TargetCo before Friday"
_REPLY_CANARY = "PRIVATEREPLY understood, keeping the TargetCo plan quiet"
_ARCHIVE_CANARY = "LEGACYSECRET the TargetCo valuation is 4.2B"


def _private(message):
    """Stamp a checkpoint message as belonging to the private partition (ADR 0131 §5)."""
    from graph.conversation_audience import PRIVATE_AUDIENCE

    message.additional_kwargs["audience"] = PRIVATE_AUDIENCE
    return message


def _public(message):
    from graph.conversation_audience import PUBLIC_AUDIENCE

    message.additional_kwargs["audience"] = PUBLIC_AUDIENCE
    return message


def test_harvest_excludes_private_canaries_from_every_derived_reader(tmp_path, history_store):
    """Proof slice: a later public turn (incognito=False) with retained private messages,
    a private reply, and a legacy private archive. The canaries must be absent from the
    summary, the closed-chat card, the compaction archive, shared-history search, fact
    recall and facts — while the public part still flows through."""
    import json as _json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from knowledge.memories import MemoryStore

    from graph.conversation_audience import PRIVATE_AUDIENCE, PUBLIC_AUDIENCE
    from graph.conversation_harvest import archive_payload
    from graph.middleware.memory import memory_path, session_filename

    session_id = "chat-9"
    # A legacy private archive span, retained in the private partition on disk.
    history_store.add(
        f"[Conversation archive] {_ARCHIVE_CANARY}",
        kind="archive",
        session_id=session_id,
        namespace=f"chat-archive:{session_id}",
        audience=PRIVATE_AUDIENCE,
    )
    # A public archive span from the same chat — this one DOES belong in a public summary.
    history_store.add(
        "[Conversation archive] Earlier we compared teal and slate palettes.",
        kind="archive",
        session_id=session_id,
        namespace=f"chat-archive:{session_id}",
        audience=PUBLIC_AUDIENCE,
    )

    messages = [
        HumanMessage(content="help me pick a theme for the dashboard"),
        _private(HumanMessage(content=_STEER_CANARY)),
        _private(AIMessage(content=f"<output>{_REPLY_CANARY}</output>")),
        AIMessage(content="<output>A teal palette fits the dashboard.</output>"),
    ]
    saver = SimpleNamespace(aget_tuple=AsyncMock(return_value=SimpleNamespace(checkpoint={
        "channel_values": {
            "messages": messages,
            "session_id": session_id,
            "incognito": False,  # the later ordinary turn cleared it — protection must not rely on it
            "operator_chat_turn": True,
        },
    })))

    seen_transcripts = []

    async def summarize(transcript, config):
        seen_transcripts.append(transcript)
        return "The operator wants a teal dashboard theme."

    async def facts(transcript, config):  # must never run — provenance is mixed/unknown
        raise AssertionError("fact extraction must be disabled for mixed/unknown provenance")

    memory = MemoryStore(tmp_path / "agent.db")
    cid = asyncio.run(harvest_thread(
        "a2a:chat-9", checkpointer=saver, knowledge_store=None, history_store=history_store,
        memory_store=memory, config=SimpleNamespace(knowledge_facts=True),
        summarizer=summarize, fact_extractor=facts, raise_on_error=True,
    ))

    # The public archive + public messages reached the summarizer; no canary did.
    transcript = "".join(seen_transcripts)
    assert "teal and slate palettes" in transcript and "teal palette fits" in transcript
    for canary in (_STEER_CANARY, _REPLY_CANARY, _ARCHIVE_CANARY):
        assert canary not in transcript

    # Summary row — clean, and stamped with the public projection lineage.
    summary = history_store.get(cid)
    assert summary.kind == "summary" and summary.audience == PUBLIC_AUDIENCE and summary.source_version == 1
    for canary in (_STEER_CANARY, _REPLY_CANARY, _ARCHIVE_CANARY):
        assert canary not in summary.content

    # Shared-history search never surfaces the private archive, nor any new canary. A
    # restart (reopen the store from disk) keeps the exclusion — the columns persist.
    for store in (history_store, HistoryStore(history_store.path)):
        # Single FTS5 tokens — history search takes FTS syntax, not a raw phrase.
        for token in ("STEERSECRET", "PRIVATEREPLY", "LEGACYSECRET", "TargetCo"):
            assert store.search(token) == []
        # The public read hides the private archive; the public archive is still listed.
        public_arcs = [a.content for a in store.list_for_session(session_id, kinds=["archive"])]
        assert public_arcs == ["[Conversation archive] Earlier we compared teal and slate palettes."]
        # The private archive is retained — a console reader (include_private) still sees both.
        assert len(store.list_for_session(session_id, kinds=["archive"], include_private=True)) == 2

    # Closed-chat digest card — topic + gist come from the public part only.
    import os as _os

    closed_file = _os.path.join(memory_path(), "closed", session_filename(session_id))
    card = _json.loads(open(closed_file, encoding="utf-8").read())
    assert card["audience"] == PUBLIC_AUDIENCE and card["source_version"] == 1
    for canary in (_STEER_CANARY, _REPLY_CANARY, _ARCHIVE_CANARY):
        assert canary not in _json.dumps(card)

    # Compaction archive of the same mixed checkpoint drops the private messages.
    content, kwargs = archive_payload(messages, session_id=session_id, thread_id="a2a:chat-9", cause="auto-compaction")
    assert kwargs["audience"] == PUBLIC_AUDIENCE and kwargs["source_version"] == 1
    assert "teal palette fits" in content
    for canary in (_STEER_CANARY, _REPLY_CANARY):
        assert canary not in content

    # Facts / recall — extraction was disabled (mixed provenance) so nothing landed.
    assert memory.list(kind="fact") == []
    for canary in (_STEER_CANARY, _REPLY_CANARY, _ARCHIVE_CANARY):
        assert memory.search(canary) == []


def test_harvest_learns_from_public_part_of_clean_partition(tmp_path, history_store):
    """The goal of the slice: a FULLY-labelled partition (every message carries its
    audience, no unknown spans) has clean provenance, so facts are still extracted — from
    the public part only. Memory keeps working; the private steer never becomes a fact."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from knowledge.memories import MemoryStore

    from graph.conversation_audience import PUBLIC_AUDIENCE

    messages = [
        _public(HumanMessage(content="my favorite color is teal")),
        _private(HumanMessage(content=_STEER_CANARY)),
        _public(AIMessage(content="<output>Noted — teal it is.</output>")),
    ]
    saver = SimpleNamespace(aget_tuple=AsyncMock(return_value=SimpleNamespace(checkpoint={
        "channel_values": {"messages": messages, "session_id": "chat-7", "operator_chat_turn": True},
    })))

    async def summarize(transcript, config):
        assert _STEER_CANARY not in transcript
        return "The operator prefers teal."

    async def facts(transcript, config):
        assert _STEER_CANARY not in transcript  # the extractor only ever sees public input
        return ["The user's favorite color is teal."]

    memory = MemoryStore(tmp_path / "agent.db")
    cid = asyncio.run(harvest_thread(
        "a2a:chat-7", checkpointer=saver, knowledge_store=None, history_store=history_store,
        memory_store=memory, config=SimpleNamespace(knowledge_facts=True),
        summarizer=summarize, fact_extractor=facts, namespace="proj-x", raise_on_error=True,
    ))
    assert cid is not None
    stored = memory.list(kind="fact", provenance="extracted", scope="proj-x")
    assert len(stored) == 1 and "teal" in stored[0].content
    assert stored[0].audience == PUBLIC_AUDIENCE  # public lineage carried into storage
    assert _STEER_CANARY not in stored[0].content


def test_private_fact_never_supersedes_or_merges_with_a_shared_fact(tmp_path):
    """ADR 0131 §5: a private-lineage fact may neither supersede nor dedup-merge into a
    shared one. The shared fact stays live; the private revision is kept separate."""
    from knowledge.memories import MemoryStore

    from graph.conversation_audience import PRIVATE_AUDIENCE, PUBLIC_AUDIENCE
    from graph.memory_facts import consolidate_and_store

    memory = MemoryStore(tmp_path / "agent.db")
    shared_id = memory.add(
        "The user prefers the teal theme", kind="fact", provenance="extracted",
        subject="theme", audience=PUBLIC_AUDIENCE,
    )

    # A private fact on the SAME subject must not retire the shared row.
    private_id, replaced = memory.add_with_supersession(
        "SECRET the user actually prefers crimson", kind="fact", provenance="extracted",
        subject="theme", audience=PRIVATE_AUDIENCE,
    )
    assert replaced == []
    live_ids = {m.id for m in memory.list(kind="fact")}
    assert shared_id in live_ids and private_id in live_ids  # both live — no cross-bucket supersession
    assert memory.get(shared_id).superseded_by is None

    # A private fact near-identical to the shared one must not dedup-merge into it.
    counts = consolidate_and_store(
        memory, ["The user prefers the teal theme"], namespace=None, audience=PRIVATE_AUDIENCE,
    )
    assert counts["added"] == 1 and counts["skipped"] == 0  # added as its own private fact, not merged
    assert memory.get(shared_id).superseded_by is None  # shared fact still untouched


def test_harvest_unchanged_without_a_private_partition(tmp_path, history_store):
    """Regression guard for r4: a conversation with NO private partition harvests exactly
    as before — the whole transcript is summarized and facts extract — only now the derived
    records additionally carry the public audience/source version."""
    from types import SimpleNamespace

    from knowledge.memories import MemoryStore

    from graph.conversation_audience import PUBLIC_AUDIENCE

    db = str(tmp_path / "c.db")
    _seed(db)  # an ordinary operator chat, no audience markers anywhere
    memory = MemoryStore(tmp_path / "agent.db")
    seen = []

    async def summarize(transcript, config):
        seen.append(transcript)
        return "User prefers teal."

    async def facts(transcript, config):
        return ["The user's favorite color is teal."]

    cid = asyncio.run(harvest_thread(
        "a2a:chat-1", checkpointer=build_sqlite_checkpointer(db), knowledge_store=None,
        memory_store=memory, config=SimpleNamespace(knowledge_facts=True),
        summarizer=summarize, fact_extractor=facts, raise_on_error=True,
    ))
    # Full transcript reached the summarizer (nothing dropped) and facts were extracted.
    assert seen == ["User: my favorite color is teal\nAssistant: <output>noted</output>"]
    summary = history_store.get(cid)
    assert summary.audience == PUBLIC_AUDIENCE and summary.source_version == 1
    assert "teal" in summary.content
    facts_stored = memory.list(kind="fact", provenance="extracted")
    assert len(facts_stored) == 1 and facts_stored[0].audience == PUBLIC_AUDIENCE
