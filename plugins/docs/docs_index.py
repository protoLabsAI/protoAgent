"""In-memory SQLite FTS5 index over the bundled docs — the keyword search the agent's
`docs_search` tool and console reader query.

Mirrors `graph/skills/index.py` (the SkillsIndex pattern: FTS5 + a prefix-OR MATCH query +
BM25 ranking), trimmed to a **read-only, rebuild-on-boot** corpus: the docs ship bundled
and never change at runtime, so the index lives in memory (`:memory:`) and is seeded once
at plugin load — no db file, no path resolution, no staleness. No embeddings (keyword BM25
is plenty for well-titled docs; a hybrid upgrade can hide behind `search()` later).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from pathlib import Path
from typing import NamedTuple

from .corpus import doc_preview, doc_title, is_custom_root, iter_docs

log = logging.getLogger("protoagent.plugins.docs")

try:
    _CATALOG = json.loads(Path(__file__).with_name("catalog.json").read_text(encoding="utf-8"))
except (OSError, ValueError):
    log.warning("[docs] catalog unavailable; indexing without content status or stop words")
    _CATALOG = {}
_STOP_WORDS = frozenset(_CATALOG.get("search", {}).get("stop_words", []))


def _query_terms(query: str) -> list[str]:
    terms = re.findall(r"\w+", (query or "").lower())
    return list(dict.fromkeys(t for t in terms if t not in _STOP_WORDS)) or terms


def _match_query(query: str) -> str:
    """Free text → a safe FTS5 prefix-OR MATCH expr (each term as ``term*``), so variants
    match and arbitrary user text can't raise a query error. Mirrors SkillsIndex."""
    terms = _query_terms(query)
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
        self._bundled = root is None and not is_custom_root()
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # One shared connection, but `docs_search` dispatches `search` onto thread-pool
        # workers via `asyncio.to_thread` — and a single sqlite connection is NOT safe for
        # concurrent use across threads (`check_same_thread=False` only silences the guard,
        # it doesn't add locking). Two back-to-back searches racing on the connection
        # corrupt cursor state → a NULL bm25 score → `float(None)`. Serialize every DB touch
        # on this lock; the corpus is tiny + in-memory so contention is negligible. (A
        # `:memory:` DB can't be shared via per-thread connections — each opens its own.)
        self._lock = threading.Lock()
        try:
            self._conn.execute(
                "CREATE VIRTUAL TABLE docs_fts USING fts5(path, title, section, content, headings, preview UNINDEXED, status UNINDEXED)"
            )
        except sqlite3.OperationalError as exc:
            raise RuntimeError("SQLite FTS5 not available — rebuild SQLite with FTS5.") from exc

    def seed(self) -> int:
        """Index canonical corpus docs; keep all readable paths, including aliases."""
        catalog = _CATALOG if self._bundled else {}
        rows = []
        for rel, abs_path in iter_docs(self._root):
            try:
                content = abs_path.read_text(encoding="utf-8")
            except OSError:
                continue
            self._paths.add(rel)  # Moved pages remain readable through old links.
            section = rel.split("/", 1)[0]
            metadata = catalog.get("pages", {}).get(rel, {})
            status = metadata.get("status", catalog.get("section_status", {}).get(section, "current"))
            if status == "moved":
                continue
            title = metadata.get("title", doc_title(abs_path))
            headings = "\n".join(re.findall(r"^#{1,6} (.+)$", content, re.M))
            rows.append((rel, title, section, content, headings, doc_preview(content), status))
        if rows:
            with self._lock:
                self._conn.executemany(
                    "INSERT INTO docs_fts (path, title, section, content, headings, preview, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
                self._conn.commit()
        return len(rows)

    def search(self, query: str, k: int = 5) -> list[DocRecord]:
        """Top-k docs by weighted BM25, title coverage, and content status (lower is better)."""
        mq = _match_query(query)
        if not mq:
            return []
        # Decision records remain discoverable; explicit ADR/history queries should
        # not inherit the preference for current task instructions.
        history_query = bool(re.search(r"\b(adr|decision|history|historical|deprecated)\b", query, re.I))
        try:
            with self._lock:
                cur = self._conn.execute(
                    "SELECT path, title, section, preview, "
                    "bm25(docs_fts, 2, 6, 0, 1, 3) * "
                    "CASE WHEN ? THEN 1 WHEN status = 'decision' THEN 0.25 "
                    "WHEN status = 'historical' THEN 0.15 WHEN status = 'deprecated' THEN 0.3 "
                    "ELSE 1 END AS score "
                    "FROM docs_fts WHERE docs_fts MATCH ?",
                    (history_query, mq),
                )
                rows = cur.fetchall()
            # Prefer a title that names the whole task over incidental body hits.
            # FTS length normalization otherwise favors short references over longer
            # procedures even when their titles match every query term.
            terms = _query_terms(query)
            results = []
            for row in rows:
                title_words = re.findall(r"\w+", row["title"].lower())
                coverage = sum(any(word.startswith(term) for word in title_words) for term in terms) / len(terms)
                score = float(row["score"] or 0.0) * (1 + 3 * coverage)
                results.append(DocRecord(row["path"], row["title"], row["section"], row["preview"] or "", score))
            return sorted(results, key=lambda row: (row.score, row.path))[:max(1, int(k))]

        except Exception as exc:  # noqa: BLE001 — empty table / odd query / any sqlite hiccup: degrade to no-results, never raise into the tool
            log.debug("[docs] search error (returning empty): %s", exc)
            return []

    def has(self, path: str) -> bool:
        """Whether *path* is a readable corpus doc, including moved pages."""
        return (path or "").strip().lstrip("/") in self._paths
