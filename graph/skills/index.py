"""SQLite FTS5 skill index for protoAgent.

Stores emitted skill-v1 artifacts in a full-text search index so the
agent can retrieve relevant past skills at inference time.

Database location: configurable, defaults to /sandbox/skills.db.
Schema version is stamped in a metadata table; incompatible schemas
trigger a backup-and-rebuild cycle per the deviation rules.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
from typing import NamedTuple

log = logging.getLogger(__name__)

# Bump when FTS table columns change — triggers auto-migration
_SCHEMA_VERSION = 1

# Columns indexed by FTS5 (order matters for sqlite_master check)
_FTS_CONTENT_COLUMNS = (
    "name",
    "description",
    "prompt_template",
    "tools_used",
    "source_session_id",
)


class SkillRecord(NamedTuple):
    """A single result from FTS5 skill retrieval."""

    name: str
    description: str
    prompt_template: str
    score: float


class SkillsIndex:
    """SQLite FTS5-backed skill index.

    Usage::

        index = SkillsIndex("/sandbox/skills.db")
        index.add_skill(artifact)           # SkillV1Artifact from extensions.skills
        results = index.load_skills("web research", k=5)
    """

    def __init__(self, db_path: str = "/sandbox/skills.db") -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self.initialize_db()

    # ── Schema management ─────────────────────────────────────────────────────

    def initialize_db(self) -> None:
        """Create (or verify) the SQLite database and FTS5 virtual table.

        On first run: creates the DB file and table.
        On re-run with matching schema: no-op (idempotent).
        On schema mismatch: backup existing DB to .bak, drop and recreate.
        """
        db_dir = os.path.dirname(self._db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

        # Open connection — creates the file if absent
        conn = self._open_conn()

        if self._schema_compatible(conn):
            log.debug("[skills] existing schema is compatible, no migration needed")
            return

        # Schema mismatch — backup and rebuild
        conn.close()
        self._conn = None
        self._backup_and_reset()
        conn = self._open_conn()
        self._create_schema(conn)

    def _open_conn(self) -> sqlite3.Connection:
        """Open (or reuse) the SQLite connection."""
        if self._conn is not None:
            return self._conn
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        self._conn = conn
        return conn

    def _schema_compatible(self, conn: sqlite3.Connection) -> bool:
        """Return True if the DB has the expected schema at the current version."""
        try:
            cur = conn.execute(
                "SELECT version FROM _skills_meta WHERE key = 'schema_version' LIMIT 1"
            )
            row = cur.fetchone()
            if row is None:
                # Meta table exists but no version row → treat as incompatible
                return False
            return int(row[0]) == _SCHEMA_VERSION
        except sqlite3.OperationalError:
            # Table doesn't exist yet — fresh DB
            self._create_schema(conn)
            return True

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        """Create FTS5 table and metadata table from scratch."""
        # Check FTS5 availability
        try:
            conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_probe USING fts5(x)")
            conn.execute("DROP TABLE IF EXISTS _fts5_probe")
        except sqlite3.OperationalError as exc:
            raise RuntimeError(
                "SQLite FTS5 extension not available in this build. "
                "Rebuild SQLite with FTS5 enabled."
            ) from exc

        conn.executescript("""
            DROP TABLE IF EXISTS skills_fts;
            DROP TABLE IF EXISTS _skills_meta;

            CREATE VIRTUAL TABLE skills_fts USING fts5(
                name,
                description,
                prompt_template,
                tools_used,
                source_session_id,
                created_at UNINDEXED
            );

            CREATE TABLE _skills_meta (
                key   TEXT PRIMARY KEY,
                version INTEGER NOT NULL
            );

            INSERT INTO _skills_meta (key, version)
            VALUES ('schema_version', 1);
        """)
        conn.commit()
        log.info("[skills] schema created at %s", self._db_path)

    def _backup_and_reset(self) -> None:
        """Backup the existing DB file to .bak and remove the original."""
        bak_path = self._db_path + ".bak"
        if os.path.exists(self._db_path):
            try:
                shutil.copy2(self._db_path, bak_path)
                os.remove(self._db_path)
                log.warning(
                    "[skills] incompatible schema — backed up %s → %s and will rebuild",
                    self._db_path,
                    bak_path,
                )
            except OSError as exc:
                log.error("[skills] backup failed: %s — will attempt in-place schema reset", exc)

    # ── Write path ────────────────────────────────────────────────────────────

    def add_skill(self, artifact: object) -> None:
        """Insert a SkillV1Artifact into the FTS5 index.

        Accepts any object with matching attributes so this module does not
        import graph.extensions.skills (avoiding circular dependency).
        Silently skips artifacts with empty names.
        """
        name = getattr(artifact, "name", "") or ""
        if not name:
            log.debug("[skills] skipping artifact with empty name")
            return

        description = getattr(artifact, "description", "") or ""
        prompt_template = getattr(artifact, "prompt_template", "") or ""
        tools_used = getattr(artifact, "tools_used", []) or []
        source_session_id = getattr(artifact, "source_session_id", "") or ""
        created_at = str(getattr(artifact, "created_at", ""))

        tools_str = " ".join(tools_used) if isinstance(tools_used, (list, tuple)) else str(tools_used)

        conn = self._open_conn()
        try:
            conn.execute(
                """
                INSERT INTO skills_fts
                    (name, description, prompt_template, tools_used, source_session_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (name, description, prompt_template, tools_str, source_session_id, created_at),
            )
            conn.commit()
            log.debug("[skills] indexed skill: %s", name)
        except sqlite3.Error as exc:
            log.error("[skills] failed to index skill %s: %s", name, exc)

    # ── Read path ─────────────────────────────────────────────────────────────

    def load_skills(self, query: str, k: int = 5) -> list[SkillRecord]:
        """Return top-k skills matching *query* ranked by FTS5 BM25 relevance.

        Returns an empty list when the database is empty or the query has no
        FTS5 matches — callers must handle the empty case gracefully.

        BM25 scores in SQLite FTS5 are negative; lower (more negative) = more
        relevant. Results are ordered ascending so index 0 is the best match.

        Args:
            query: Free-text query string (user message + recent context).
            k:     Maximum number of results to return (default 5).

        Returns:
            List of SkillRecord named tuples ordered best-first.
        """
        if not query or not query.strip():
            return []

        conn = self._open_conn()
        try:
            cur = conn.execute(
                """
                SELECT name, description, prompt_template, bm25(skills_fts) AS score
                FROM skills_fts
                WHERE skills_fts MATCH ?
                ORDER BY score
                LIMIT ?
                """,
                (query.strip(), k),
            )
            rows = cur.fetchall()
            return [
                SkillRecord(
                    name=row["name"],
                    description=row["description"],
                    prompt_template=row["prompt_template"],
                    score=float(row["score"]),
                )
                for row in rows
            ]
        except sqlite3.OperationalError as exc:
            # Table may be empty or query syntax invalid
            log.debug("[skills] FTS5 search error (returning empty): %s", exc)
            return []

    def rebuild_index(self, artifacts: list[object]) -> None:
        """Drop all rows and re-index from *artifacts*.

        Useful after schema migration or if the index becomes inconsistent.
        """
        conn = self._open_conn()
        try:
            conn.execute("DELETE FROM skills_fts")
            conn.commit()
        except sqlite3.Error as exc:
            log.error("[skills] failed to clear FTS table for rebuild: %s", exc)
            return

        for artifact in artifacts:
            self.add_skill(artifact)

        log.info("[skills] rebuilt index with %d artifacts", len(artifacts))

    def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
