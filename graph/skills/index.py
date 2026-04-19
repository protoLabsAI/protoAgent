"""SkillIndex — SQLite-backed full-text search index for skill-v1 artifacts.

Falls back to LIKE-based pattern matching with word tokenization and relevance
scoring when SQLite FTS5 is not available (deviation rule: SQLite FTS5
unavailable).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

_CREATE_SKILLS_TABLE = """
CREATE TABLE IF NOT EXISTS skills (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL,
    description       TEXT NOT NULL DEFAULT '',
    prompt_template   TEXT NOT NULL DEFAULT '',
    tools_used        TEXT NOT NULL DEFAULT '[]',
    created_at        TEXT NOT NULL DEFAULT '',
    source_session_id TEXT NOT NULL DEFAULT ''
);
"""

_CREATE_SKILLS_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS skills_fts
USING fts5(
    name,
    description,
    prompt_template,
    content=skills,
    content_rowid=id
);
"""

# Triggers to keep the FTS index in sync with the skills table.
# Split into individual statements so they can be executed one at a time.
_FTS_TRIGGERS = [
    """CREATE TRIGGER IF NOT EXISTS skills_ai AFTER INSERT ON skills BEGIN
        INSERT INTO skills_fts(rowid, name, description, prompt_template)
        VALUES (new.id, new.name, new.description, new.prompt_template);
    END""",
    """CREATE TRIGGER IF NOT EXISTS skills_ad AFTER DELETE ON skills BEGIN
        INSERT INTO skills_fts(skills_fts, rowid, name, description, prompt_template)
        VALUES ('delete', old.id, old.name, old.description, old.prompt_template);
    END""",
    """CREATE TRIGGER IF NOT EXISTS skills_au AFTER UPDATE ON skills BEGIN
        INSERT INTO skills_fts(skills_fts, rowid, name, description, prompt_template)
        VALUES ('delete', old.id, old.name, old.description, old.prompt_template);
        INSERT INTO skills_fts(rowid, name, description, prompt_template)
        VALUES (new.id, new.name, new.description, new.prompt_template);
    END""",
]


def _fts5_available() -> bool:
    """Return True if the SQLite build includes the FTS5 extension.

    Uses an in-memory database to avoid corrupting the real DB if FTS5
    is absent and the CREATE VIRTUAL TABLE statement raises.
    """
    try:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE VIRTUAL TABLE _probe USING fts5(x)")
        conn.close()
        return True
    except sqlite3.OperationalError:
        return False


class SkillIndex:
    """SQLite-backed index for skill-v1 artifacts.

    Provides full-text search via FTS5 when available; falls back to
    LIKE-based word matching with relevance scoring otherwise.

    Parameters
    ----------
    db_path:
        Path to the SQLite database file.  The parent directory is
        created automatically if it does not exist.
    """

    def __init__(self, db_path: str = "/sandbox/skills.db") -> None:
        self._db_path = db_path
        self._use_fts5: bool = False
        self._init_db()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Create the database directory and apply the schema."""
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

        conn = self._get_conn()
        try:
            # Create the base table first (outside of FTS5 check so it
            # is always available even on FTS5-less builds).
            conn.execute(_CREATE_SKILLS_TABLE)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_skills_name ON skills(name)"
            )
            conn.commit()

            if _fts5_available():
                # Track whether the FTS virtual table is new so we can
                # back-fill existing rows on migration.
                existing_fts = conn.execute(
                    "SELECT count(*) FROM sqlite_master "
                    "WHERE type='table' AND name='skills_fts'"
                ).fetchone()[0]

                conn.execute(_CREATE_SKILLS_FTS)
                for trigger_sql in _FTS_TRIGGERS:
                    conn.execute(trigger_sql)

                if not existing_fts:
                    # Back-fill any rows inserted before FTS was set up.
                    conn.execute(
                        "INSERT INTO skills_fts(rowid, name, description, prompt_template) "
                        "SELECT id, name, description, prompt_template FROM skills"
                    )

                conn.commit()
                self._use_fts5 = True
                log.debug("[skill-index] FTS5 enabled at %s", self._db_path)
            else:
                log.warning(
                    "[skill-index] FTS5 unavailable — falling back to LIKE search at %s",
                    self._db_path,
                )
                self._use_fts5 = False
        finally:
            conn.close()

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        tools_used: list = []
        try:
            tools_used = json.loads(row["tools_used"])
        except (json.JSONDecodeError, TypeError):
            pass

        keys = row.keys()
        score = float(row["score"]) if "score" in keys else 0.0

        return {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "prompt_template": row["prompt_template"],
            "tools_used": tools_used,
            "created_at": row["created_at"],
            "source_session_id": row["source_session_id"],
            "score": score,
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def index_skill(self, artifact) -> int:
        """Insert a SkillV1Artifact into the index.

        Parameters
        ----------
        artifact:
            Any object with the same fields as SkillV1Artifact (duck-typed).

        Returns
        -------
        int
            The ``id`` of the newly inserted row.
        """
        conn = self._get_conn()
        try:
            cur = conn.execute(
                """INSERT INTO skills
                       (name, description, prompt_template, tools_used,
                        created_at, source_session_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    artifact.name,
                    artifact.description,
                    artifact.prompt_template,
                    json.dumps(list(artifact.tools_used)),
                    (
                        artifact.created_at.isoformat()
                        if hasattr(artifact.created_at, "isoformat")
                        else str(artifact.created_at)
                    ),
                    artifact.source_session_id,
                ),
            )
            conn.commit()
            log.debug("[skill-index] indexed skill '%s' (id=%d)", artifact.name, cur.lastrowid)
            return cur.lastrowid
        finally:
            conn.close()

    def search(self, query: str, k: int = 5) -> list[dict]:
        """Search the index and return up to *k* results sorted by relevance.

        An empty or whitespace-only *query* falls back to the *k* most
        recently inserted skills.

        Parameters
        ----------
        query:
            Free-text search string (e.g. current user message).
        k:
            Maximum number of results to return.
        """
        if not query or not query.strip():
            return self._get_recent(k)

        if self._use_fts5:
            return self._search_fts5(query, k)
        return self._search_like(query, k)

    def count(self) -> int:
        """Return the total number of indexed skills."""
        conn = self._get_conn()
        try:
            return conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Search backends
    # ------------------------------------------------------------------

    def _search_fts5(self, query: str, k: int) -> list[dict]:
        """FTS5 BM25-ranked full-text search."""
        # Strip non-alphanumeric tokens that would break FTS5 query parsing.
        tokens = [w for w in query.split() if any(c.isalnum() for c in w)]
        safe_query = " ".join(tokens)
        if not safe_query:
            return self._get_recent(k)

        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT s.id, s.name, s.description, s.prompt_template,
                          s.tools_used, s.created_at, s.source_session_id,
                          bm25(skills_fts) AS score
                   FROM skills_fts
                   JOIN skills s ON s.id = skills_fts.rowid
                   WHERE skills_fts MATCH ?
                   ORDER BY score
                   LIMIT ?""",
                (safe_query, k),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        except sqlite3.OperationalError as exc:
            log.warning("[skill-index] FTS5 query failed, falling back to LIKE: %s", exc)
            self._use_fts5 = False
            return self._search_like(query, k)
        finally:
            conn.close()

    def _search_like(self, query: str, k: int) -> list[dict]:
        """LIKE-based fallback with word-overlap relevance scoring."""
        words = [w.lower() for w in query.split() if len(w) >= 2]
        if not words:
            return self._get_recent(k)

        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT id, name, description, prompt_template, "
                "tools_used, created_at, source_session_id FROM skills"
            ).fetchall()

            scored: list[dict] = []
            for row in rows:
                score = self._relevance_score(row, words)
                if score > 0:
                    d = self._row_to_dict(row)
                    d["score"] = score
                    scored.append(d)

            scored.sort(key=lambda x: x["score"], reverse=True)
            return scored[:k]
        finally:
            conn.close()

    def _relevance_score(self, row: sqlite3.Row, words: list[str]) -> float:
        """Word-overlap fraction in [0, 1] for LIKE-based ranking."""
        text = " ".join(
            [
                str(row["name"] or ""),
                str(row["description"] or ""),
                str(row["prompt_template"] or ""),
            ]
        ).lower()
        matches = sum(1 for w in words if w in text)
        return matches / len(words) if words else 0.0

    def _get_recent(self, k: int) -> list[dict]:
        """Return the *k* most recently inserted skills."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT id, name, description, prompt_template, "
                "tools_used, created_at, source_session_id "
                "FROM skills ORDER BY id DESC LIMIT ?",
                (k,),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            conn.close()
