"""In-memory SQLite FTS5 index over the bundled docs — the keyword search the agent's
`docs_search` tool (and, later, the console view) query.

Mirrors `graph/skills/index.py` (the SkillsIndex pattern: FTS5 + a prefix-OR MATCH query +
BM25 ranking), trimmed to a **read-only, rebuild-on-boot** corpus: the docs ship bundled
and never change at runtime, so the index lives in memory (`:memory:`) and is seeded once
at plugin load — no db file, no path resolution, no staleness. No embeddings (keyword BM25
is plenty for well-titled docs; a hybrid upgrade can hide behind `search()` later).
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import NamedTuple

from .corpus import doc_preview, doc_title, iter_docs

log = logging.getLogger("protoagent.plugins.docs")


def _match_query(query: str) -> str:
    """Free text → a safe FTS5 prefix-OR MATCH expr (each term as ``term*``), so variants
    match and arbitrary user text can't raise a query error. Mirrors SkillsIndex."""
    terms = re.findall(r"\w+", (query or "").lower())
    return " OR ".join(f"{t}*" for t in terms)


class DocRecord(NamedTuple):
    path: str
    title: str
    section: str
    preview: str
    score: float


class DocsIndex:
    """FTS5 keyword index over the doc corpus. Build once, query many."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root
        self._paths: set[str] = set()
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute(
                "CREATE VIRTUAL TABLE docs_fts USING fts5(path, title, section, content, preview UNINDEXED)"
            )
        except sqlite3.OperationalError as exc:
            raise RuntimeError("SQLite FTS5 not available — rebuild SQLite with FTS5.") from exc

    def seed(self) -> int:
        """Index every corpus doc. Returns the count."""
        rows: list[tuple[str, str, str, str, str]] = []
        for rel, abs_path in iter_docs(self._root):
            try:
                content = abs_path.read_text(encoding="utf-8")
            except OSError:
                continue
            rows.append((rel, doc_title(abs_path), rel.split("/", 1)[0], content, doc_preview(content)))
            self._paths.add(rel)
        if rows:
            self._conn.executemany(
                "INSERT INTO docs_fts (path, title, section, content, preview) VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            self._conn.commit()
        return len(rows)

    def search(self, query: str, k: int = 5) -> list[DocRecord]:
        """Top-k docs for *query*, BM25-ranked best-first (lower score = better)."""
        mq = _match_query(query)
        if not mq:
            return []
        try:
            cur = self._conn.execute(
                "SELECT path, title, section, preview, bm25(docs_fts) AS score "
                "FROM docs_fts WHERE docs_fts MATCH ? ORDER BY score LIMIT ?",
                (mq, max(1, int(k))),
            )
            return [
                DocRecord(r["path"], r["title"], r["section"], r["preview"] or "", float(r["score"]))
                for r in cur.fetchall()
            ]
        except sqlite3.OperationalError as exc:  # empty table / odd query
            log.debug("[docs] search error (returning empty): %s", exc)
            return []

    def has(self, path: str) -> bool:
        """Whether *path* is an indexed doc (the read-access gate)."""
        return (path or "").strip().lstrip("/") in self._paths
