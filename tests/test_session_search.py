"""FTS5 search over persisted session summaries (#3073)."""

from __future__ import annotations

import json
import os
import sqlite3

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


def test_schema_reset_persists_the_declared_version(monkeypatch, tmp_path):
    import graph.session_search as session_search_module

    # A version the module does NOT declare, or this asserts nothing: `_SCHEMA_VERSION`
    # is 2 since #3322, so patching it to 2 would pass whether the write happens or not.
    monkeypatch.setattr(session_search_module, "_SCHEMA_VERSION", 3)
    _write(tmp_path, "chat-versioned", "schema version probe")
    assert search_session_summaries("schema version", memory_dir=str(tmp_path))

    with sqlite3.connect(tmp_path / ".session-search.sqlite3") as conn:
        stored = conn.execute(
            "SELECT value FROM session_search_meta WHERE key = 'schema_version'"
        ).fetchone()
    assert stored == ("3",)


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


def test_query_matches_a_plural_or_tense_variant(tmp_path):
    """The index stems (porter), so recall doesn't depend on reproducing the stored
    surface form. Before #3322 every one of these missed while the session sat
    indexed on disk — the agent searched, found nothing, and correctly said so."""
    _write(tmp_path, "chat-audit", "how long we keep audit logs before rotating them")

    for query in ("audit log", "audit logs", "rotation", "rotating"):
        assert search_session_summaries(query, memory_dir=str(tmp_path)), f"{query!r} found nothing"


def test_derivational_mismatch_still_misses(tmp_path):
    """The KNOWN limit, pinned so it shows up the day it changes. Porter stems
    inflections, not derivations: "retained" → "retain" but "retention" → "retent".
    Closing this needs semantic retrieval, which this index deliberately doesn't have —
    which is why on-demand search is not a complete substitute for the prior-session
    digest (ADR 0108 D9, the #3186 decision)."""
    _write(tmp_path, "chat-ret", "audit logs are retained for ninety days")

    assert search_session_summaries("retained", memory_dir=str(tmp_path))
    assert search_session_summaries("retention", memory_dir=str(tmp_path)) == []


def test_an_index_from_the_previous_schema_is_rebuilt_with_the_stemmer(tmp_path):
    """The upgrade path that matters: every existing install already has an index
    built by the OLD tokenizer. The version bump is the whole migration — a stale
    index must rebuild itself on first open rather than serve unstemmed results
    forever."""
    _write(tmp_path, "chat-audit", "how long we keep audit logs before rotating them")
    index = tmp_path / ".session-search.sqlite3"

    # An index exactly as v1 left it: old tokenizer, old declared version, and a row
    # already in it so a "rebuild" is observable rather than a first-time build.
    with sqlite3.connect(index) as conn:
        conn.executescript(
            """
            CREATE TABLE session_search_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE session_search_files (
                session_id TEXT PRIMARY KEY, filename TEXT NOT NULL UNIQUE,
                mtime_ns INTEGER NOT NULL, size INTEGER NOT NULL,
                timestamp TEXT NOT NULL, surface TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE session_search_fts USING fts5(
                session_id UNINDEXED, content, tokenize='unicode61'
            );
            INSERT INTO session_search_meta (key, value) VALUES ('schema_version', '1');
            """
        )
    # Sanity: under the OLD schema the singular really does miss — otherwise this test
    # would pass whether or not the rebuild happened.
    with sqlite3.connect(index) as conn:
        conn.execute(
            "INSERT INTO session_search_fts (session_id, content) VALUES ('chat-audit', 'audit logs rotating')"
        )
        stale = conn.execute(
            "SELECT count(*) FROM session_search_fts WHERE session_search_fts MATCH ?", ('"audit" AND "log"',)
        ).fetchone()[0]
    assert stale == 0

    assert search_session_summaries("audit log", memory_dir=str(tmp_path))

    with sqlite3.connect(index) as conn:
        stored = conn.execute(
            "SELECT value FROM session_search_meta WHERE key = 'schema_version'"
        ).fetchone()
    assert stored == ("2",)
