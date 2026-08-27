"""Lazy FTS5 index over persisted session-summary JSON files.

Session summaries remain the source of truth.  The SQLite database beside them is a
disposable derived index: the first search after an upgrade indexes existing files,
and later searches reconcile additions, rewrites, and deletions by filename metadata.
Keeping indexing off the session-write path means a missing FTS5 extension or a locked
index can never make an otherwise successful agent turn fail.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

from graph.middleware.memory import digest_entry, is_safe_session_id, memory_path
from graph.middleware.redaction import redact
from graph.output_format import strip_reasoning


_INDEX_FILENAME = ".session-search.sqlite3"
_SCHEMA_VERSION = 1
_MAX_INDEXED_CHARS = 256_000
_QUERY_TERM_RE = re.compile(r"\w+", re.UNICODE)
SESSION_SEARCH_SURFACES = frozenset({"chat", "a2a/other", "activity", "palette", "background"})


class SessionSearchUnavailable(RuntimeError):
    """The derived session index could not be opened or queried."""


def _index_path(memory_dir: str) -> str:
    return str(Path(memory_dir) / _INDEX_FILENAME)


def _reset_schema(conn: sqlite3.Connection) -> None:
    """Replace the disposable derived schema with the current version."""
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _session_fts5_probe USING fts5(value)")
        conn.execute("DROP TABLE IF EXISTS _session_fts5_probe")
    except sqlite3.OperationalError as exc:
        raise SessionSearchUnavailable("SQLite FTS5 is unavailable in this build") from exc

    conn.executescript(
        """
        DROP TABLE IF EXISTS session_search_fts;
        DROP TABLE IF EXISTS session_search_files;
        DROP TABLE IF EXISTS session_search_meta;

        CREATE TABLE session_search_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE session_search_files (
            session_id TEXT PRIMARY KEY,
            filename TEXT NOT NULL UNIQUE,
            mtime_ns INTEGER NOT NULL,
            size INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            surface TEXT NOT NULL
        );

        CREATE VIRTUAL TABLE session_search_fts USING fts5(
            session_id UNINDEXED,
            content,
            tokenize='unicode61'
        );
        """
    )
    conn.execute(
        "INSERT INTO session_search_meta (key, value) VALUES ('schema_version', ?)",
        (str(_SCHEMA_VERSION),),
    )
    conn.commit()


def _open_index(memory_dir: str) -> sqlite3.Connection:
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(_index_path(memory_dir), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            row = conn.execute(
                "SELECT value FROM session_search_meta WHERE key = 'schema_version'"
            ).fetchone()
            compatible = row is not None and int(row[0]) == _SCHEMA_VERSION
        except (sqlite3.Error, TypeError, ValueError):
            compatible = False
        if not compatible:
            _reset_schema(conn)
        return conn
    except SessionSearchUnavailable:
        if conn is not None:
            conn.close()
        raise
    except sqlite3.Error as exc:
        if conn is not None:
            conn.close()
        raise SessionSearchUnavailable(f"could not open the session index: {exc}") from exc


def _text(value: object) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def _summary_content(summary: dict) -> str:
    """Reasoning-stripped, credential-redacted text from one persisted summary."""
    parts: list[str] = []
    for message in summary.get("messages", []) or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "message")
        content = strip_reasoning(_text(message.get("content") or ""))
        if content:
            parts.append(f"{role}: {content}")
    for call in summary.get("tool_calls", []) or []:
        if not isinstance(call, dict):
            continue
        name = str(call.get("name") or "tool")
        args = strip_reasoning(_text(call.get("args") or ""))
        result = strip_reasoning(_text(call.get("result") or ""))
        parts.append(f"tool {name}: {args}\nresult: {result}")
    final = strip_reasoning(_text(summary.get("final_output") or ""))
    if final:
        parts.append(f"final: {final}")
    # Tool args/results are useful search material (often the only place an exact
    # failure appears), but are also the likeliest place for a credential to have
    # been echoed. Reuse the audit/trace redactor before this new model-visible read
    # surface can return a matching snippet.
    return str(redact("\n".join(parts)))[:_MAX_INDEXED_CHARS]


def _remove_session(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute("DELETE FROM session_search_fts WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM session_search_files WHERE session_id = ?", (session_id,))


def _sync_index(conn: sqlite3.Connection, memory_dir: str) -> None:
    """Reconcile the derived index with summary files without rereading unchanged JSON."""
    try:
        filenames = sorted(name for name in os.listdir(memory_dir) if name.endswith(".json"))
    except OSError as exc:
        raise SessionSearchUnavailable(f"could not list session summaries: {exc}") from exc

    try:
        conn.execute("BEGIN IMMEDIATE")
        cached = {
            row["filename"]: row
            for row in conn.execute(
                "SELECT session_id, filename, mtime_ns, size FROM session_search_files"
            ).fetchall()
        }
        seen: set[str] = set()

        for filename in filenames:
            fpath = os.path.join(memory_dir, filename)
            try:
                stat = os.stat(fpath)
            except OSError:
                continue
            seen.add(filename)
            old = cached.get(filename)
            if old is not None and old["mtime_ns"] == stat.st_mtime_ns and old["size"] == stat.st_size:
                continue

            try:
                with open(fpath, encoding="utf-8") as fh:
                    summary = json.load(fh)
                if not isinstance(summary, dict):
                    raise ValueError("session summary is not an object")
                session_id = str(summary.get("session_id") or "")
                if not is_safe_session_id(session_id):
                    raise ValueError("unsafe or empty session id")
            except (OSError, json.JSONDecodeError, ValueError, TypeError):
                if old is not None:
                    _remove_session(conn, str(old["session_id"]))
                continue

            # A rewritten file can change session id, and a legacy raw-name file can
            # coexist briefly with its encoded successor. Session id is canonical.
            if old is not None and old["session_id"] != session_id:
                _remove_session(conn, str(old["session_id"]))
            _remove_session(conn, session_id)
            entry = digest_entry(summary)
            conn.execute(
                """
                INSERT INTO session_search_files
                    (session_id, filename, mtime_ns, size, timestamp, surface)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    filename,
                    stat.st_mtime_ns,
                    stat.st_size,
                    str(entry["timestamp"]),
                    str(entry["surface"]),
                ),
            )
            conn.execute(
                "INSERT INTO session_search_fts (session_id, content) VALUES (?, ?)",
                (session_id, _summary_content(summary)),
            )

        for filename, row in cached.items():
            if filename not in seen:
                _remove_session(conn, str(row["session_id"]))
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        raise SessionSearchUnavailable(f"could not update the session index: {exc}") from exc


def _match_query(query: str) -> str:
    """Turn natural text into a literal AND query, never raw FTS syntax."""
    terms = _QUERY_TERM_RE.findall((query or "")[:500])
    if not terms:
        raise ValueError("query must contain at least one letter or number")
    return " AND ".join(f'"{term}"' for term in terms[:32])


def search_session_summaries(
    query: str,
    *,
    memory_dir: str | None = None,
    limit: int = 5,
    surface: str | None = None,
    exclude_session_id: str = "",
) -> list[dict]:
    """Search persisted session content and return bounded attributed excerpts.

    The index is derived and synchronized before every query. Results are ordered by
    FTS5 relevance, then newest timestamp, and never include ``exclude_session_id``.
    """
    base = memory_dir or memory_path()
    if not os.path.isdir(base):
        return []
    selected_surface = (surface or "").strip().lower()
    if selected_surface and selected_surface not in SESSION_SEARCH_SURFACES:
        allowed = ", ".join(sorted(SESSION_SEARCH_SURFACES))
        raise ValueError(f"unknown surface {surface!r}; use one of: {allowed}")
    match = _match_query(query)
    clamped_limit = max(1, min(int(limit), 20))

    conn = _open_index(base)
    try:
        _sync_index(conn, base)
        clauses = ["session_search_fts MATCH ?"]
        params: list[object] = [match]
        if selected_surface:
            clauses.append("files.surface = ?")
            params.append(selected_surface)
        if exclude_session_id:
            clauses.append("files.session_id != ?")
            params.append(exclude_session_id)
        params.append(clamped_limit)
        rows = conn.execute(
            f"""
            SELECT files.session_id, files.timestamp, files.surface,
                   snippet(session_search_fts, 1, '', '', ' … ', 32) AS excerpt
            FROM session_search_fts
            JOIN session_search_files AS files
              ON files.session_id = session_search_fts.session_id
            WHERE {' AND '.join(clauses)}
            ORDER BY bm25(session_search_fts), files.timestamp DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error as exc:
        raise SessionSearchUnavailable(f"session search failed: {exc}") from exc
    finally:
        conn.close()
