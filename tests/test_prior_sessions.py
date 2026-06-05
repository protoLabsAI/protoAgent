"""ADR 0021 Phase 3: one shared <prior_sessions> loader, with read-time
reasoning stripping.

The loader is the single source of truth for both MemoryMiddleware and
KnowledgeMiddleware (previously two copy-pasted copies). It strips reasoning at
read so a session file written by an older build can't inject <scratch_pad> into
the prompt.
"""

from __future__ import annotations

import json

from graph.middleware.memory import load_prior_sessions


def _write(d, sid, messages, final_output=""):
    (d / f"{sid}.json").write_text(json.dumps({
        "session_id": sid,
        "timestamp": "2026-06-05T00:00:00Z",
        "messages": messages,
        "final_output": final_output,
    }))


def test_loader_strips_reasoning_from_dirty_old_files(tmp_path):
    # Simulate a file written before the persist-time strip existed.
    _write(
        tmp_path, "s1",
        [{"role": "assistant", "content": "<scratch_pad>secret plan</scratch_pad>The answer is 42."}],
        final_output="<scratch_pad>noise</scratch_pad>Done.",
    )
    block = load_prior_sessions(str(tmp_path))
    assert "scratch_pad" not in block
    assert "secret plan" not in block
    assert "The answer is 42." in block
    assert "Done." in block


def test_loader_empty_and_missing_dir(tmp_path):
    assert load_prior_sessions(str(tmp_path)) == "<prior_sessions/>"   # empty dir
    assert load_prior_sessions(str(tmp_path / "nope")) == ""           # missing dir


def test_loader_returns_block_for_clean_session(tmp_path):
    _write(tmp_path, "s1", [{"role": "user", "content": "my color is teal"}])
    block = load_prior_sessions(str(tmp_path))
    assert block.startswith("<prior_sessions>")
    assert "teal" in block


def test_loader_token_budget_trims(tmp_path):
    for i in range(20):
        _write(tmp_path, f"s{i:02d}", [{"role": "user", "content": "x" * 2000}])
    block = load_prior_sessions(str(tmp_path), max_tokens=400)
    # Trimmed to fit the budget (rough char/4) rather than dumping all 20.
    assert block != "<prior_sessions/>"
    assert len(block) // 4 <= 700  # budget + one-session slack


def test_both_middlewares_use_the_same_loader(tmp_path):
    """KnowledgeMiddleware.load_memory delegates to load_prior_sessions, so the
    two can't drift."""
    from graph.middleware.knowledge import KnowledgeMiddleware

    _write(tmp_path, "s1", [{"role": "user", "content": "shared loader check"}])
    mw = KnowledgeMiddleware.__new__(KnowledgeMiddleware)  # no __init__ needed
    assert mw.load_memory(str(tmp_path)) == load_prior_sessions(str(tmp_path))
