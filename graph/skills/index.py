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
import re
import shutil
import sqlite3
from typing import NamedTuple

log = logging.getLogger(__name__)


def _build_match_query(query: str) -> str:
    """Turn free text into a safe FTS5 prefix-OR MATCH expression.

    A bare query string is an implicit AND of its terms, so one non-matching
    word (or a morphological variant like ``calculation`` vs ``calculations``)
    zeroes the result. We instead OR each token as a prefix (``term*``), which
    matches variants and ranks by BM25. Tokenizing to ``\\w+`` also strips FTS5
    syntax characters, so arbitrary user text can't raise a query error.
    """
    terms = re.findall(r"\w+", query.lower())
    return " OR ".join(f"{t}*" for t in terms)


# Bump when FTS table columns change — triggers auto-migration
# v2: added confidence + last_used (consumed by the skill curator).
# v3: added `source` ('disk' = human-authored SKILL.md, re-seeded each boot;
#     'emitted' = agent-authored via task(), persisted + curator-managed).
# v4: added user_facing + slash (ADR 0052 — `/<slash>` chat commands).
# v5: added user_only (2026-06) — a user_facing skill withheld from agent retrieval.
_SCHEMA_VERSION = 5

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
    tools_used: tuple[str, ...] = ()


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
            cur = conn.execute("SELECT version FROM _skills_meta WHERE key = 'schema_version' LIMIT 1")
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
                "SQLite FTS5 extension not available in this build. Rebuild SQLite with FTS5 enabled."
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
                created_at UNINDEXED,
                confidence UNINDEXED,
                last_used UNINDEXED,
                source UNINDEXED,
                user_facing UNINDEXED,
                slash UNINDEXED,
                user_only UNINDEXED
            );

            CREATE TABLE _skills_meta (
                key   TEXT PRIMARY KEY,
                version INTEGER NOT NULL
            );

            INSERT INTO _skills_meta (key, version)
            VALUES ('schema_version', 5);
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

    def add_skill(self, artifact: object, source: str = "emitted") -> None:
        """Insert a SkillV1Artifact into the FTS5 index.

        Accepts any object with matching attributes so this module does not
        import graph.extensions.skills (avoiding circular dependency).
        Silently skips artifacts with empty names. ``source`` is ``'disk'`` for
        human-authored SKILL.md skills (re-seeded each boot) or ``'emitted'``
        for agent-authored ones (persisted + curator-managed).
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
        # New skills start fully confident; last_used seeds from created_at so
        # the curator's decay clock starts at emission (bumped on retrieval).
        last_used = created_at

        # User-facing slash trigger (ADR 0052). Stored as '1'/'0' text in the
        # UNINDEXED column; ``slash`` falls back to the slugified name so the
        # reader always has a non-empty token.
        user_facing = "1" if getattr(artifact, "user_facing", False) else "0"
        slash = getattr(artifact, "slash", "") or ""
        if user_facing == "1" and not slash and hasattr(artifact, "slash_token"):
            slash = artifact.slash_token()
        # User-only (v5): withheld from load_skills (agent retrieval) but still a /slash.
        user_only = "1" if getattr(artifact, "user_only", False) else "0"

        conn = self._open_conn()
        try:
            conn.execute(
                """
                INSERT INTO skills_fts
                    (name, description, prompt_template, tools_used,
                     source_session_id, created_at, confidence, last_used, source,
                     user_facing, slash, user_only)
                VALUES (?, ?, ?, ?, ?, ?, 1.0, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    description,
                    prompt_template,
                    tools_str,
                    source_session_id,
                    created_at,
                    last_used,
                    source,
                    user_facing,
                    slash,
                    user_only,
                ),
            )
            conn.commit()
            log.debug("[skills] indexed skill: %s (source=%s)", name, source)
        except sqlite3.Error as exc:
            log.error("[skills] failed to index skill %s: %s", name, exc)

    def replace_disk_skills(self, artifacts: list[object]) -> None:
        """Reset the ``disk`` source to exactly *artifacts*, leaving ``emitted``
        skills intact. Used to (re)seed human-authored SKILL.md skills on boot
        without clobbering the agent's persisted procedural memory."""
        conn = self._open_conn()
        try:
            conn.execute("DELETE FROM skills_fts WHERE source = 'disk'")
            conn.commit()
        except sqlite3.Error as exc:
            log.error("[skills] failed to clear disk skills for re-seed: %s", exc)
            return
        for artifact in artifacts:
            self.add_skill(artifact, source="disk")
        log.info("[skills] seeded %d disk skill(s)", len(artifacts))

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

        match_query = _build_match_query(query)
        if not match_query:
            return []

        conn = self._open_conn()
        try:
            cur = conn.execute(
                """
                SELECT name, description, prompt_template, tools_used,
                       bm25(skills_fts) AS score
                FROM skills_fts
                WHERE skills_fts MATCH ?
                  AND user_only = '0'
                ORDER BY score
                LIMIT ?
                """,
                (match_query, k),
            )
            rows = cur.fetchall()
            return [
                SkillRecord(
                    name=row["name"],
                    description=row["description"],
                    prompt_template=row["prompt_template"],
                    score=float(row["score"]),
                    tools_used=tuple((row["tools_used"] or "").split()),
                )
                for row in rows
            ]
        except sqlite3.OperationalError as exc:
            # Table may be empty or query syntax invalid
            log.debug("[skills] FTS5 search error (returning empty): %s", exc)
            return []

    # ── Curation surface (consumed by graph/skills/curator.py) ─────────────────

    def all_skills(self) -> list[dict]:
        """Return every skill as a dict, including the curator's bookkeeping
        fields (``id`` = rowid, ``confidence``, ``last_used``). Empty on error."""
        conn = self._open_conn()
        try:
            cur = conn.execute(
                """
                SELECT rowid AS id, name, description, prompt_template, tools_used,
                       created_at, confidence, last_used, source, user_facing, slash, user_only
                FROM skills_fts
                """
            )
            return [self._row_to_dict(row) for row in cur.fetchall()]
        except sqlite3.OperationalError as exc:
            log.debug("[skills] all_skills error (returning empty): %s", exc)
            return []

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        """Map an FTS row to a skill dict (tolerates pre-v4 rows missing the
        user_facing/slash columns)."""
        keys = row.keys()
        return {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "prompt_template": row["prompt_template"],
            "tools_used": (row["tools_used"] or "").split(),
            "created_at": row["created_at"],
            "confidence": float(row["confidence"]) if row["confidence"] is not None else 1.0,
            "last_used": row["last_used"],
            "source": (row["source"] if "source" in keys else "emitted") or "emitted",
            "user_facing": (row["user_facing"] if "user_facing" in keys else "0") == "1",
            "slash": (row["slash"] if "slash" in keys else "") or "",
            "user_only": (row["user_only"] if "user_only" in keys else "0") == "1",
        }

    def user_facing_skills(self) -> list[dict]:
        """Return only the skills flagged ``user_facing`` (ADR 0052), each as a
        dict (same shape as ``all_skills``). These back the `/<slash>` chat
        commands. Empty on error."""
        conn = self._open_conn()
        try:
            cur = conn.execute(
                """
                SELECT rowid AS id, name, description, prompt_template, tools_used,
                       created_at, confidence, last_used, source, user_facing, slash, user_only
                FROM skills_fts
                WHERE user_facing = '1'
                """
            )
            return [self._row_to_dict(row) for row in cur.fetchall()]
        except sqlite3.OperationalError as exc:
            log.debug("[skills] user_facing_skills error (returning empty): %s", exc)
            return []

    def update_confidence(self, skill_id: int, confidence: float) -> None:
        """Set a skill's confidence (used by the curator's decay pass)."""
        conn = self._open_conn()
        try:
            conn.execute(
                "UPDATE skills_fts SET confidence = ? WHERE rowid = ?",
                (float(confidence), int(skill_id)),
            )
            conn.commit()
        except sqlite3.Error as exc:
            log.error("[skills] update_confidence failed for %s: %s", skill_id, exc)

    def delete_skill(self, skill_id: int) -> None:
        """Remove a skill by rowid (used by the curator's dedup/prune passes)."""
        conn = self._open_conn()
        try:
            conn.execute("DELETE FROM skills_fts WHERE rowid = ?", (int(skill_id),))
            conn.commit()
        except sqlite3.Error as exc:
            log.error("[skills] delete_skill failed for %s: %s", skill_id, exc)

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
