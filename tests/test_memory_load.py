"""Unit tests for KnowledgeMiddleware.load_memory().

Covers:
- Successful loading of multiple session summaries
- Token budget enforcement (oldest sessions truncated first)
- Missing memory directory returns empty string (not an error)
- Malformed/unreadable session file is skipped gracefully
- Empty memory directory returns <prior_sessions/>
- Disabled knowledge middleware: load_memory() still works as standalone
- Result is cached after first call (no repeated disk reads)
- before_model injects prior_sessions block into returned context
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
    assert 'id="sess-1"' in result
    assert "Hello" in result
    assert "Hi there!" in result


# ---------------------------------------------------------------------------
# 4. Multiple sessions — all appear when within budget
# ---------------------------------------------------------------------------


def test_load_memory_multiple_sessions(tmp_path):
    for i in range(3):
        _write_session(str(tmp_path), f"sess-{i}", _sample_session(f"sess-{i}"))

    mw = _make_middleware()
    result = mw.load_memory(memory_path=str(tmp_path))

    assert result.count("<session") == 3


# ---------------------------------------------------------------------------
# 5. Token budget enforcement — oldest sessions truncated first
# ---------------------------------------------------------------------------


def test_load_memory_token_budget_drops_oldest(tmp_path):
    # Write 5 sessions with enough per-session content to trigger budget enforcement.
    # The formatter truncates final_output to 300 and each message to 500 chars.
    # Per-session formatted size: ~50 (XML tag) + 500 (user) + 500 (asst) + 300 (final) + overhead
    # ≈ 1400 chars → ~350 tokens per session.  5 sessions → ~1750 tokens.
    # We use max_tokens=700 (budget for ~2 sessions) so budget enforcement fires.

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
    result = mw.load_memory(memory_path=str(tmp_path), max_sessions=5, max_tokens=700)

    # Budget must be respected
    token_count = max(1, len(result) // 4)
    assert token_count <= 700, f"Token budget exceeded: ~{token_count} tokens"

    # At least one session should survive (newest)
    session_count = result.count("<session")
    assert session_count >= 1, "Expected at least one session within budget"

    # Fewer than 5 sessions should be present (budget enforcement dropped some)
    assert session_count < 5, f"Expected budget enforcement to drop some sessions, got {session_count}"


def test_load_memory_respects_max_sessions(tmp_path):
    for i in range(15):
        _write_session(str(tmp_path), f"sess-{i}", _sample_session(f"sess-{i}"))

    mw = _make_middleware()
    result = mw.load_memory(memory_path=str(tmp_path), max_sessions=5, max_tokens=100_000)

    assert result.count("<session") <= 5


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
    assert 'id="good"' in result
    # Bad session should not appear
    assert 'id="bad"' not in result


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
    assert 'id="inject-sess"' in result["context"]


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
    assert 'id="leak-sess"' not in ctx


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


async def test_memory_middleware_abefore_model_off_loop(tmp_path, monkeypatch):
    """SessionSummaryMiddleware.abefore_model (standalone mode, disk reads) also
    dispatches via to_thread."""
    import threading

    from langchain_core.messages import HumanMessage

    import graph.middleware.memory as memmod

    seen_threads: list[threading.Thread] = []

    def _fake_load(self):
        seen_threads.append(threading.current_thread())
        return "<prior_sessions>old</prior_sessions>"

    monkeypatch.setattr(memmod.SessionSummaryMiddleware, "_load_prior_sessions", _fake_load)
    mw = memmod.SessionSummaryMiddleware(knowledge_store=None)

    state = {"messages": [HumanMessage(content="hello")]}
    result = await mw.abefore_model(state, runtime=None)

    assert result is not None  # prior sessions injected
    assert seen_threads and seen_threads[0] is not threading.main_thread()
