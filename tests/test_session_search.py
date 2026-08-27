"""FTS5 search over persisted session summaries (#3073)."""

from __future__ import annotations

import json
import os

import pytest

from graph.middleware.memory import session_filename
from graph.session_search import search_session_summaries


def _write(directory, session_id: str, text: str, *, timestamp: str = "2026-08-01T00:00:00Z"):
    path = directory / session_filename(session_id)
    path.write_text(
        json.dumps(
            {
                "session_id": session_id,
                "timestamp": timestamp,
                "messages": [
                    {"role": "user", "content": f"question about {text}"},
                    {"role": "assistant", "content": f"resolved {text}"},
                ],
                "tool_calls": [],
                "final_output": f"done with {text}",
            }
        ),
        encoding="utf-8",
    )
    return path


def test_search_indexes_existing_summaries_and_returns_attribution(tmp_path):
    _write(tmp_path, "chat-old", "Playwright timeout in the checkout test")
    _write(tmp_path, "a2a:other", "unrelated database migration")

    rows = search_session_summaries("Playwright timeout", memory_dir=str(tmp_path))

    assert [row["session_id"] for row in rows] == ["chat-old"]
    assert rows[0]["surface"] == "chat"
    assert "Playwright" in rows[0]["excerpt"]
    assert (tmp_path / ".session-search.sqlite3").is_file()


def test_search_reconciles_rewrites_and_deletes(tmp_path):
    path = _write(tmp_path, "chat-changing", "original lighthouse failure")
    assert search_session_summaries("lighthouse", memory_dir=str(tmp_path))

    _write(tmp_path, "chat-changing", "replacement websocket failure")
    # Defeat coarse filesystem timestamp resolution as well as the size check.
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert search_session_summaries("lighthouse", memory_dir=str(tmp_path)) == []
    assert search_session_summaries("websocket", memory_dir=str(tmp_path))[0]["session_id"] == "chat-changing"

    path.unlink()
    assert search_session_summaries("websocket", memory_dir=str(tmp_path)) == []


def test_search_strips_reasoning_and_indexes_tool_results(tmp_path):
    path = _write(tmp_path, "chat-tools", "ordinary text")
    summary = json.loads(path.read_text(encoding="utf-8"))
    summary["messages"][1]["content"] = "<scratch_pad>private token plan</scratch_pad>public answer"
    summary["tool_calls"] = [{"name": "run_command", "args": {"command": "pytest"}, "result": "rare-failure-code"}]
    path.write_text(json.dumps(summary), encoding="utf-8")

    assert search_session_summaries("private token", memory_dir=str(tmp_path)) == []
    row = search_session_summaries("rare failure code", memory_dir=str(tmp_path))[0]
    assert row["session_id"] == "chat-tools"


def test_search_filters_surface_and_excludes_current_session(tmp_path):
    _write(tmp_path, "chat-one", "shared needle")
    _write(tmp_path, "a2a:two", "shared needle")

    rows = search_session_summaries(
        "shared needle",
        memory_dir=str(tmp_path),
        surface="a2a/other",
        exclude_session_id="a2a:two",
    )
    assert rows == []
    assert search_session_summaries("shared needle", memory_dir=str(tmp_path), surface="chat")[0][
        "session_id"
    ] == "chat-one"


@pytest.mark.parametrize("query", ["", "***"])
def test_search_rejects_empty_or_operator_only_queries(tmp_path, query):
    _write(tmp_path, "chat-one", "anything")
    with pytest.raises(ValueError, match="at least one letter or number"):
        search_session_summaries(query, memory_dir=str(tmp_path))


def test_search_treats_fts_operators_as_literal_terms(tmp_path):
    _write(tmp_path, "chat-one", "anything")
    assert search_session_summaries("' OR 1=1 --", memory_dir=str(tmp_path)) == []


def test_search_redacts_credentials_before_indexing(tmp_path):
    token = "api_key=" + "redactionfixture" * 3
    _write(tmp_path, "chat-secret", f"failure used {token}")

    assert search_session_summaries(token, memory_dir=str(tmp_path)) == []
    row = search_session_summaries("REDACTED", memory_dir=str(tmp_path))[0]
    assert token not in row["excerpt"]


async def test_session_search_tool_is_registered_bounded_and_excludes_current(monkeypatch, tmp_path):
    from tools.lg_tools import MEMORY_TOOL_NAMES, get_all_tools

    monkeypatch.setenv("MEMORY_PATH", str(tmp_path))
    _write(tmp_path, "chat-current", "playwright timeout")
    _write(tmp_path, "chat-prior", "playwright timeout")
    tool = {item.name: item for item in get_all_tools(object())}["session_search"]

    assert "session_search" in MEMORY_TOOL_NAMES
    output = await tool.ainvoke(
        {"query": "playwright timeout", "limit": 99, "state": {"session_id": "chat-current"}}
    )
    assert "chat-prior" in output
    assert "chat-current" not in output
    assert "recall_session" in output


async def test_session_search_tool_reports_bad_surface(monkeypatch, tmp_path):
    from tools.lg_tools import get_all_tools

    monkeypatch.setenv("MEMORY_PATH", str(tmp_path))
    _write(tmp_path, "chat-one", "needle")
    tool = {item.name: item for item in get_all_tools(object())}["session_search"]
    output = await tool.ainvoke({"query": "needle", "surface": "email"})
    assert output.startswith("Error: unknown surface")
