"""Unit tests for KnowledgeMiddleware.load_memory().

Covers:
- Successful loading of multiple session summaries (attributed digest, ADR 0069)
- Digest carries ids + topics but NO assistant text / verbatim bodies
- Token budget enforcement (oldest sessions truncated first)
- Missing memory directory returns empty string (not an error)
- Malformed/unreadable session file is skipped gracefully
- Empty memory directory returns <prior_sessions/>
- Disabled knowledge middleware: load_memory() still works as standalone
- Result is cached after first call (no repeated disk reads)
- before_model injects prior_sessions block into returned context
- recall_session tool expands one digest entry (and rejects traversal)
- <injected_memory> envelope wraps memory parts, not the skills block
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_middleware(knowledge_store=None):
    """Instantiate KnowledgeMiddleware with a mock store."""
    from graph.middleware.knowledge import KnowledgeMiddleware

    store = knowledge_store or MagicMock()
    store.search.return_value = []  # no knowledge hits by default
    return KnowledgeMiddleware(store, top_k=5)


def _write_session(directory: str, session_id: str, content: dict) -> str:
    """Write a session summary JSON file and return its path."""
    fpath = os.path.join(directory, f"{session_id}.json")
    with open(fpath, "w", encoding="utf-8") as fh:
        json.dump(content, fh)
    return fpath


def _sample_session(session_id: str = "s1", timestamp: str = "2024-01-01T00:00:00+00:00") -> dict:
    return {
        "session_id": session_id,
        "trace_id": f"trace-{session_id}",
        "messages": [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ],
        "tool_calls": [],
        "final_output": "Hi there!",
        "timestamp": timestamp,
    }


# ---------------------------------------------------------------------------
# 1. Missing directory — returns empty string, does not raise
# ---------------------------------------------------------------------------


def test_load_memory_missing_directory():
    mw = _make_middleware()
    result = mw.load_memory(memory_path="/tmp/nonexistent_protoagent_memory_xyz_999/")
    assert result == "", f"Expected empty string for missing dir, got: {result!r}"


# ---------------------------------------------------------------------------
# 2. Empty directory — returns <prior_sessions/>
# ---------------------------------------------------------------------------


def test_load_memory_empty_directory(tmp_path):
    mw = _make_middleware()
    result = mw.load_memory(memory_path=str(tmp_path))
    assert result == "<prior_sessions/>", f"Expected empty tag, got: {result!r}"


# ---------------------------------------------------------------------------
# 3. Single valid session — appears in output
# ---------------------------------------------------------------------------


def test_load_memory_single_session(tmp_path):
    _write_session(str(tmp_path), "sess-1", _sample_session("sess-1"))
    mw = _make_middleware()
    result = mw.load_memory(memory_path=str(tmp_path))

    assert "<prior_sessions>" in result
    assert "sess-1" in result
    assert "Hello" in result  # topic = first user message
    # ADR 0069 D1: the digest never carries assistant text or message bodies.
    assert "Hi there!" not in result
    assert "recall_session" in result  # header points at the expansion tool


# ---------------------------------------------------------------------------
# 4. Multiple sessions — all appear when within budget
# ---------------------------------------------------------------------------


def test_load_memory_multiple_sessions(tmp_path):
    for i in range(3):
        _write_session(str(tmp_path), f"sess-{i}", _sample_session(f"sess-{i}"))

    mw = _make_middleware()
    result = mw.load_memory(memory_path=str(tmp_path))

    # One digest line per session (each ends with its message count).
    assert result.count(" msgs") == 3


# ---------------------------------------------------------------------------
# 5. Token budget enforcement — oldest sessions truncated first
# ---------------------------------------------------------------------------


def test_load_memory_token_budget_drops_oldest(tmp_path):
    # Write 5 sessions with long user messages. The digest truncates each topic
    # to ~80 chars, so a digest line is ~140 chars (~35 tokens) and the framing
    # header ~60 tokens. max_tokens=100 leaves room for the header plus roughly
    # one line, so budget enforcement must drop the oldest sessions.

    for i in range(5):
        session = _sample_session(f"sess-{i}", f"2024-01-0{i + 1}T00:00:00+00:00")
        session["messages"] = [
            {"role": "user", "content": "Q" * 500},
            {"role": "assistant", "content": "A" * 500},
        ]
        session["final_output"] = "F" * 300
        fpath = _write_session(str(tmp_path), f"sess-{i}", session)
        # Space out mtimes so ordering is deterministic: sess-4 is newest
        os.utime(fpath, (1000 + i, 1000 + i))

    mw = _make_middleware()
    result = mw.load_memory(memory_path=str(tmp_path), max_sessions=5, max_tokens=100)

    # Budget must be respected (the <prior_sessions> wrapper tags sit outside
    # the loader's budget, as they always have — allow that slack).
    token_count = max(1, len(result) // 4)
    assert token_count <= 100 + len("<prior_sessions>\n\n</prior_sessions>") // 4, (
        f"Token budget exceeded: ~{token_count} tokens"
    )

    # At least one session should survive (newest)
    session_count = result.count(" msgs")
    assert session_count >= 1, "Expected at least one session within budget"

    # Fewer than 5 sessions should be present (budget enforcement dropped some)
    assert session_count < 5, f"Expected budget enforcement to drop some sessions, got {session_count}"

    # Oldest dropped first: the newest session survives.
    assert "sess-4" in result


def test_load_memory_respects_max_sessions(tmp_path):
    for i in range(15):
        _write_session(str(tmp_path), f"sess-{i}", _sample_session(f"sess-{i}"))

    mw = _make_middleware()
    result = mw.load_memory(memory_path=str(tmp_path), max_sessions=5, max_tokens=100_000)

    assert result.count(" msgs") <= 5


# ---------------------------------------------------------------------------
# 6. Malformed session file — skipped, other sessions still loaded
# ---------------------------------------------------------------------------


def test_load_memory_skips_malformed_file(tmp_path):
    # Write one good session
    _write_session(str(tmp_path), "good", _sample_session("good"))

    # Write a malformed JSON file
    bad_path = os.path.join(str(tmp_path), "bad.json")
    with open(bad_path, "w") as fh:
        fh.write("this is not json {{{")

    mw = _make_middleware()
    result = mw.load_memory(memory_path=str(tmp_path))

    assert "<prior_sessions>" in result
    assert "good" in result
    # Bad session should not appear
    assert "bad" not in result


# ---------------------------------------------------------------------------
# 7. Result is cached after first load_memory call
# ---------------------------------------------------------------------------


def test_load_memory_cache_via_before_model(tmp_path):
    _write_session(str(tmp_path), "cached-sess", _sample_session("cached-sess"))

    mw = _make_middleware()
    # Monkey-patch load_memory to count calls
    call_count = {"n": 0}
    original = mw.load_memory

    def counting_load(**kw):
        call_count["n"] += 1
        return original(**kw)

    mw.load_memory = counting_load

    state = {"messages": []}

    # Trigger before_model twice
    mw.before_model(state, runtime=None)
    mw.before_model(state, runtime=None)

    # load_memory should only have been called once (cache hit on second call)
    assert call_count["n"] == 1, f"load_memory called {call_count['n']} times — expected 1 (cached)"


def test_prior_sessions_cache_refreshes_after_ttl(tmp_path):
    """The cache is not frozen for the process lifetime — after the TTL it
    reloads, so sessions persisted after boot become visible."""
    _write_session(str(tmp_path), "ttl-sess", _sample_session("ttl-sess"))

    mw = _make_middleware()
    call_count = {"n": 0}
    original = mw.load_memory

    def counting_load(**kw):
        call_count["n"] += 1
        return original(memory_path=str(tmp_path))

    mw.load_memory = counting_load
    state = {"messages": []}

    mw.before_model(state, runtime=None)
    assert call_count["n"] == 1
    # Simulate the TTL elapsing.
    from graph.middleware.knowledge import _PRIOR_SESSIONS_TTL_S

    mw._prior_sessions_loaded_at -= _PRIOR_SESSIONS_TTL_S + 1
    mw.before_model(state, runtime=None)
    assert call_count["n"] == 2, "cache did not refresh after TTL elapsed"


# ---------------------------------------------------------------------------
# 8. before_model injects prior_sessions into returned context
# ---------------------------------------------------------------------------


def test_before_model_injects_prior_sessions(tmp_path):
    _write_session(str(tmp_path), "inject-sess", _sample_session("inject-sess"))

    mw = _make_middleware()
    # Override load_memory to use tmp_path; mark fresh so the TTL cache does
    # not immediately reload from the default (empty) path.
    import time

    mw._prior_sessions_cache = mw.load_memory(memory_path=str(tmp_path))
    mw._prior_sessions_loaded_at = time.monotonic()

    from langchain_core.messages import HumanMessage

    state = {"messages": [HumanMessage(content="What did we discuss?")]}

    result = mw.before_model(state, runtime=None)
    assert result is not None
    assert "<prior_sessions>" in result.get("context", "")
    assert "inject-sess" in result["context"]


def test_before_model_suppresses_prior_sessions_in_goal_turn(tmp_path):
    """Goal-driven turns must NOT receive cross-session prior_sessions —
    unrelated history biases the self-driving loop. The knowledge-search path
    is unaffected; only the prior_sessions block is dropped."""
    _write_session(str(tmp_path), "leak-sess", _sample_session("leak-sess"))

    mw = _make_middleware()
    import time

    mw._prior_sessions_cache = mw.load_memory(memory_path=str(tmp_path))
    mw._prior_sessions_loaded_at = time.monotonic()

    from langchain_core.messages import HumanMessage
    from graph.goals.goal_turn import goal_turn

    state = {"messages": [HumanMessage(content="continue the goal")]}

    # Normal turn injects it; goal-driven turn suppresses it.
    assert "<prior_sessions>" in (mw.before_model(state, runtime=None) or {}).get("context", "")
    with goal_turn():
        result = mw.before_model(state, runtime=None)
    ctx = (result or {}).get("context", "")
    assert "<prior_sessions>" not in ctx
    assert "leak-sess" not in ctx


# ---------------------------------------------------------------------------
# 9. Disabled memory (no sessions) yields empty block or empty string
# ---------------------------------------------------------------------------


def test_load_memory_no_sessions_yields_empty_tag(tmp_path):
    mw = _make_middleware()
    result = mw.load_memory(memory_path=str(tmp_path))
    # Empty dir → empty self-closing tag (not a full block)
    assert result == "<prior_sessions/>"


# ---------------------------------------------------------------------------
# 10. load_memory() works as a standalone call without a knowledge store
# ---------------------------------------------------------------------------


def test_load_memory_standalone_no_knowledge_store(tmp_path):
    """load_memory() does not touch self._store — it should work independently."""
    _write_session(str(tmp_path), "standalone", _sample_session("standalone"))

    # Pass a store that raises on any call to ensure load_memory doesn't use it
    broken_store = MagicMock()
    broken_store.search.side_effect = RuntimeError("store should not be called")

    from graph.middleware.knowledge import KnowledgeMiddleware

    mw = KnowledgeMiddleware(broken_store, top_k=5)
    result = mw.load_memory(memory_path=str(tmp_path))

    assert "<prior_sessions>" in result
    assert "standalone" in result


# ---------------------------------------------------------------------------
# 11. abefore_model runs the (blocking) store search OFF the event loop
# ---------------------------------------------------------------------------
# The store search embeds the query over HTTP on hybrid stores
# (HybridKnowledgeStore + create_embed_fn) — running it inline in
# abefore_model stalled the event loop before every LLM call. The async
# hook must dispatch the sync before_model via asyncio.to_thread.


async def test_abefore_model_runs_search_off_event_loop():
    import threading

    from langchain_core.messages import HumanMessage

    from graph.middleware.knowledge import KnowledgeMiddleware

    seen_threads: list[threading.Thread] = []

    store = MagicMock()
    store.get_hot_memory.return_value = ""
    store.get_hot_memory_entries.return_value = []

    def _slow_search(query, k=5):
        seen_threads.append(threading.current_thread())
        return [{"table": "chunks", "preview": "remembered fact"}]

    store.search.side_effect = _slow_search

    mw = KnowledgeMiddleware(store, top_k=5)
    state = {"messages": [HumanMessage(content="what do you remember?")]}

    result = await mw.abefore_model(state, runtime=None)

    # Same behavior as the sync path…
    assert result is not None
    assert "remembered fact" in result["context"]
    # …but the blocking search ran on a worker thread, not the event loop.
    assert seen_threads, "store.search was never called"
    assert seen_threads[0] is not threading.main_thread()


# ---------------------------------------------------------------------------
# 12. Attributed digest format (ADR 0069 D1)
# ---------------------------------------------------------------------------


def test_digest_one_attributed_line_no_assistant_text(tmp_path):
    session = _sample_session("chat-abc123")
    session["messages"] = [
        {"role": "user", "content": "plan the launch\nwith a  multi-line body"},
        {"role": "assistant", "content": "ASSISTANT-ONLY-TEXT about the launch"},
    ]
    session["final_output"] = "ASSISTANT-ONLY-TEXT about the launch"
    _write_session(str(tmp_path), "chat-abc123", session)

    from graph.middleware.memory import load_prior_sessions

    result = load_prior_sessions(str(tmp_path))

    # No assistant text, no verbatim multi-line bodies (ADR 0069 D1).
    assert "ASSISTANT-ONLY-TEXT" not in result
    lines = [ln for ln in result.split("\n") if "chat-abc123" in ln]
    assert len(lines) == 1, f"expected exactly one digest line, got {lines}"
    line = lines[0]
    # id · timestamp · surface · topic (whitespace-collapsed) · message count
    assert "2024-01-01T00:00:00+00:00" in line
    assert " chat " in line.replace("·", " ")
    assert "plan the launch with a multi-line body" in line
    assert "2 msgs" in line


def test_digest_surface_classification(tmp_path):
    cases = [
        ("chat-1", "chat"),
        ("system:activity", "activity"),
        ("palette-2", "palette"),
        ("someA2Aconsumer", "a2a/other"),
    ]
    for sid, _ in cases:
        _write_session(str(tmp_path), sid, _sample_session(sid))
    # A background WORKER summary never enters the digest (ADR 0070 D3 — the writer
    # no longer persists them; the loader filters legacy files). digest_entry itself
    # still classifies the surface (asserted below) for the memory-inspector API.
    _write_session(str(tmp_path), "background:job-7", _sample_session("background:job-7"))
    from graph.middleware.memory import digest_entry

    assert digest_entry(_sample_session("background:job-7"))["surface"] == "background"

    from graph.middleware.memory import load_prior_sessions

    result = load_prior_sessions(str(tmp_path))
    for sid, surface in cases:
        line = next(ln for ln in result.split("\n") if sid in ln)
        assert f" {surface} " in line.replace("·", " "), f"{sid} classified wrong: {line}"
    assert "background:job-7" not in result


def test_digest_excludes_encoded_background_files(tmp_path):
    """The background:* read-side filter must catch the '%3A'-encoded filename
    a Windows-safe writer produces, not just the legacy raw-':' name."""
    _write_session(str(tmp_path), "chat-ok", _sample_session("chat-ok"))
    with open(os.path.join(str(tmp_path), "background%3Ajob-9.json"), "w", encoding="utf-8") as fh:
        json.dump(_sample_session("background:job-9"), fh)

    from graph.middleware.memory import load_prior_sessions_digest

    block, ids = load_prior_sessions_digest(str(tmp_path))
    assert ids == ["chat-ok"]
    assert "background:job-9" not in block


def test_digest_topic_truncated(tmp_path):
    session = _sample_session("sess-long")
    session["messages"] = [{"role": "user", "content": "T" * 300}]
    _write_session(str(tmp_path), "sess-long", session)

    from graph.middleware.memory import load_prior_sessions

    result = load_prior_sessions(str(tmp_path))
    assert "T" * 81 not in result  # topic capped at ~80 chars
    assert "…" in result


# ---------------------------------------------------------------------------
# 13. recall_session tool — expand one digest entry (ADR 0069 D1)
# ---------------------------------------------------------------------------


def _recall_tool(monkeypatch, memory_dir):
    from tools.lg_tools import get_all_tools

    monkeypatch.setenv("MEMORY_PATH", str(memory_dir))
    store = MagicMock()
    return {t.name: t for t in get_all_tools(store)}["recall_session"]


async def test_recall_session_happy_path(monkeypatch, tmp_path):
    _write_session(str(tmp_path), "sess-9", _sample_session("sess-9"))
    out = await _recall_tool(monkeypatch, tmp_path).ainvoke({"session_id": "sess-9"})

    # Full summary: both roles + final output, unlike the digest.
    assert 'id="sess-9"' in out
    assert "Hello" in out
    assert "Hi there!" in out


async def test_recall_session_unknown_id(monkeypatch, tmp_path):
    out = await _recall_tool(monkeypatch, tmp_path).ainvoke({"session_id": "no-such-session"})
    assert "No session" in out


async def test_recall_session_reads_encoded_filename(monkeypatch, tmp_path):
    # A ':' id persists under the '%3A'-encoded name (Windows-safe filenames);
    # recall goes through the same session_filename mapper as the writer.
    with open(os.path.join(str(tmp_path), "a2a%3Asess-1.json"), "w", encoding="utf-8") as fh:
        json.dump(_sample_session("a2a:sess-1"), fh)
    out = await _recall_tool(monkeypatch, tmp_path).ainvoke({"session_id": "a2a:sess-1"})
    assert 'id="a2a:sess-1"' in out
    assert "Hello" in out


async def test_recall_session_legacy_raw_name_fallback(monkeypatch, tmp_path):
    # Pre-encoding builds wrote the raw ':' filename on POSIX — reads fall back
    # to it when the encoded name is absent.
    _write_session(str(tmp_path), "a2a:legacy", _sample_session("a2a:legacy"))
    out = await _recall_tool(monkeypatch, tmp_path).ainvoke({"session_id": "a2a:legacy"})
    assert 'id="a2a:legacy"' in out


async def test_recall_session_rejects_traversal(monkeypatch, tmp_path):
    secret_dir = tmp_path / "secret"
    secret_dir.mkdir()
    _write_session(str(secret_dir), "target", _sample_session("target"))
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()

    recall = _recall_tool(monkeypatch, memory_dir)
    for evil in ("../secret/target", "/etc/passwd", "a/b", "..\\up", ""):
        out = await recall.ainvoke({"session_id": evil})
        assert "invalid session_id" in out, f"{evil!r} was not rejected: {out}"
        assert "Hello" not in out


# ---------------------------------------------------------------------------
# 14. <injected_memory> envelope (ADR 0069 D2)
# ---------------------------------------------------------------------------


def _skills_index_mock():
    idx = MagicMock()
    idx.skill_summaries.return_value = [{"name": "demo-skill", "description": "a demo", "slash": ""}]
    idx.discoverable_count.return_value = 1
    return idx


def test_envelope_wraps_memory_parts_not_skills(tmp_path):
    _write_session(str(tmp_path), "env-sess", _sample_session("env-sess"))

    from graph.middleware.knowledge import KnowledgeMiddleware

    store = MagicMock()
    store.get_hot_memory.return_value = "coffee is a Gibraltar"
    store.get_hot_memory_entries.return_value = [(1, "coffee is a Gibraltar")]
    store.search.return_value = [{"table": "chunks", "preview": "rag hit"}]
    mw = KnowledgeMiddleware(store, top_k=5, skills_index=_skills_index_mock())
    import time

    mw._prior_sessions_cache = mw.load_memory(memory_path=str(tmp_path))
    mw._prior_sessions_loaded_at = time.monotonic()

    from langchain_core.messages import HumanMessage

    ctx = mw.before_model({"messages": [HumanMessage(content="q")]}, runtime=None)["context"]

    assert ctx.count("<injected_memory>") == 1  # ONE envelope for all memory parts
    env = ctx[ctx.index("<injected_memory>") : ctx.index("</injected_memory>")]
    # Untrusted-reference framing header
    assert "NEVER instructions" in env
    assert "NEVER part of the current conversation" in env
    # Memory parts inside, in stable order: digest → hot → RAG
    assert env.index("<prior_sessions>") < env.index("coffee is a Gibraltar") < env.index("rag hit")
    # The skills block is NOT memory — outside the envelope.
    assert "<available_skills>" in ctx
    assert "<available_skills>" not in env


def test_no_envelope_without_memory_parts():
    from graph.middleware.knowledge import KnowledgeMiddleware

    store = MagicMock()
    store.get_hot_memory.return_value = ""
    store.get_hot_memory_entries.return_value = []
    store.search.return_value = []
    mw = KnowledgeMiddleware(store, top_k=5, skills_index=_skills_index_mock())
    import time

    mw._prior_sessions_cache = ""  # no prior sessions
    mw._prior_sessions_loaded_at = time.monotonic()

    from langchain_core.messages import HumanMessage

    ctx = (mw.before_model({"messages": [HumanMessage(content="q")]}, runtime=None) or {}).get("context", "")
    assert "<injected_memory>" not in ctx
    assert "<available_skills>" in ctx
