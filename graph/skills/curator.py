"""Periodic skill curator agent for protoAgent.

Reads the skill index, clusters skills by similarity to identify duplicates,
applies exponential confidence decay for stale skills, and prunes those with
confidence below the configured threshold.  Writes a structured audit entry to
audit.jsonl after each run.

Usage
-----
    # Dry-run (no writes to index or audit):
    python -m graph.skills.curator --dry-run

    # Full run with default paths:
    python -m graph.skills.curator

    # Custom paths:
    python -m graph.skills.curator \\
        --index /sandbox/skills/index.jsonl \\
        --audit /sandbox/audit/curator.jsonl

Skill index format (JSONL, one JSON object per line)
-----------------------------------------------------
    {
        "id": "<uuid>",
        "name": "<short label>",
        "description": "<what the skill does>",
        "prompt_template": "<the prompt that drove the subagent run>",
        "tools_used": ["tool_a", "tool_b"],
        "confidence": 0.9,
        "created_at": "<ISO-8601 UTC>",
        "last_used": "<ISO-8601 UTC>"   // optional; falls back to created_at
    }

Confidence decay model
----------------------
Confidence decays with a 90-day half-life of inactivity:

    decay_factor = 0.5 ** (days_idle / 90)
    new_confidence = original_confidence * decay_factor

Skills whose post-decay confidence falls below PRUNE_THRESHOLD (0.2) are
removed from the index.

Duplicate detection
-------------------
Skills are clustered using token-based Jaccard similarity on the concatenation
of ``name + " " + description``.  When sentence-transformers is available it is
used instead for higher-quality clustering; a warning is logged when the
fallback is active.  Within each cluster the skill with the highest confidence
is kept; the rest are consolidated and removed.

Audit log
---------
Each run appends one JSON object to audit.jsonl:

    {
        "run_id": "<uuid>",
        "timestamp": "<ISO-8601 UTC>",
        "dry_run": false,
        "skills_before": 42,
        "skills_after": 38,
        "decay_applied": [{"id": "...", "old": 0.9, "new": 0.72}],
        "deduplicated": [{"kept": "id-a", "removed": ["id-b", "id-c"]}],
        "pruned": [{"id": "...", "name": "...", "confidence": 0.18}]
    }
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import uuid
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

HALF_LIFE_DAYS: float = 90.0
PRUNE_THRESHOLD: float = 0.2
SIMILARITY_THRESHOLD: float = 0.6
DEFAULT_INDEX_PATH: str = "/sandbox/skills/index.jsonl"
DEFAULT_AUDIT_PATH: str = "audit.jsonl"
AUDIT_MAX_BYTES: int = 100 * 1024 * 1024  # 100 MB


# ── Helpers ────────────────────────────────────────────────────────────────────


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(ts: str) -> datetime:
    """Parse an ISO-8601 string, always returning a timezone-aware datetime."""
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    """Clamp *value* to [lo, hi], handling NaN / infinity."""
    if not math.isfinite(value):
        log.warning("[curator] confidence clamped from non-finite value %s", value)
        return lo
    return max(lo, min(hi, value))


def _jaccard(text_a: str, text_b: str) -> float:
    """Token-level Jaccard similarity between two strings."""
    tokens_a = set(text_a.lower().split())
    tokens_b = set(text_b.lower().split())
    if not tokens_a and not tokens_b:
        return 1.0
    intersection = len(tokens_a & tokens_b)
    union = len(tokens_a | tokens_b)
    return intersection / union if union else 0.0


def _skill_text(skill: dict) -> str:
    """Canonical text used for similarity comparison."""
    return f"{skill.get('name', '')} {skill.get('description', '')}"


# ── Core curator ──────────────────────────────────────────────────────────────


class SkillCurator:
    """Reads, curates, and optionally persists a skill index.

    Parameters
    ----------
    index_path:
        Path to the JSONL skill index file.
    audit_path:
        Path to the audit JSONL file.
    dry_run:
        When *True* the curator computes all changes but writes nothing to disk.
    half_life_days:
        Confidence half-life in days (default 90).
    prune_threshold:
        Skills with post-decay confidence below this value are pruned (default 0.2).
    similarity_threshold:
        Jaccard / embedding similarity above which two skills are considered
        duplicates (default 0.6).
    """

    def __init__(
        self,
        index_path: str = DEFAULT_INDEX_PATH,
        audit_path: str = DEFAULT_AUDIT_PATH,
        dry_run: bool = False,
        half_life_days: float = HALF_LIFE_DAYS,
        prune_threshold: float = PRUNE_THRESHOLD,
        similarity_threshold: float = SIMILARITY_THRESHOLD,
    ) -> None:
        self.index_path = index_path
        self.audit_path = audit_path
        self.dry_run = dry_run
        self.half_life_days = half_life_days
        self.prune_threshold = prune_threshold
        self.similarity_threshold = similarity_threshold

    # ── Public API ─────────────────────────────────────────────────────────────

    def run(self) -> dict:
        """Execute a full curation cycle.

        Returns the audit entry that was (or would be) written.
        """
        run_id = str(uuid.uuid4())
        started_at = _now_utc()
        log.info("[curator] starting run %s (dry_run=%s)", run_id, self.dry_run)

        skills = self._load_index()
        skills_before = len(skills)

        decay_report = self._apply_decay(skills)
        dedup_report = self._deduplicate(skills)
        prune_report, skills = self._prune(skills)

        skills_after = len(skills)

        audit_entry = {
            "run_id": run_id,
            "timestamp": started_at.isoformat(),
            "dry_run": self.dry_run,
            "skills_before": skills_before,
            "skills_after": skills_after,
            "decay_applied": decay_report,
            "deduplicated": dedup_report,
            "pruned": prune_report,
        }

        if not self.dry_run:
            self._save_index(skills)
            self._append_audit(audit_entry)

        log.info(
            "[curator] run %s complete — before=%d after=%d decay=%d dedup_clusters=%d pruned=%d",
            run_id,
            skills_before,
            skills_after,
            len(decay_report),
            len(dedup_report),
            len(prune_report),
        )
        return audit_entry

    # ── Loading / saving ───────────────────────────────────────────────────────

    def _load_index(self) -> list[dict]:
        """Load skills from the JSONL index.  Returns [] if file is absent."""
        if not os.path.exists(self.index_path):
            log.info("[curator] index not found at %s — starting with empty set", self.index_path)
            return []

        skills: list[dict] = []
        with open(self.index_path, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    skill = json.loads(line)
                    if not isinstance(skill, dict):
                        raise ValueError("expected a JSON object")
                    # Ensure required fields exist
                    if "id" not in skill:
                        skill["id"] = str(uuid.uuid4())
                    if "confidence" not in skill:
                        skill["confidence"] = 1.0
                    skills.append(skill)
                except (json.JSONDecodeError, ValueError) as exc:
                    log.error(
                        "[curator] skipping malformed entry at line %d in %s: %s",
                        lineno,
                        self.index_path,
                        exc,
                    )
        log.info("[curator] loaded %d skills from %s", len(skills), self.index_path)
        return skills

    def _save_index(self, skills: list[dict]) -> None:
        """Persist *skills* back to the JSONL index."""
        os.makedirs(os.path.dirname(self.index_path) or ".", exist_ok=True)
        with open(self.index_path, "w", encoding="utf-8") as fh:
            for skill in skills:
                fh.write(json.dumps(skill, default=str) + "\n")
        log.info("[curator] saved %d skills to %s", len(skills), self.index_path)

    # ── Confidence decay ───────────────────────────────────────────────────────

    def _days_idle(self, skill: dict) -> float:
        """Return the number of days since the skill was last used."""
        now = _now_utc()
        # Prefer last_used; fall back to created_at as proxy
        ts_str = skill.get("last_used") or skill.get("created_at")
        if not ts_str:
            log.debug(
                "[curator] skill %s has no timestamp — using 0 days idle", skill.get("id")
            )
            return 0.0
        try:
            last = _parse_iso(ts_str)
            delta = now - last
            return max(0.0, delta.total_seconds() / 86400.0)
        except (ValueError, TypeError) as exc:
            log.warning(
                "[curator] cannot parse timestamp for skill %s: %s — treating as 0 days idle",
                skill.get("id"),
                exc,
            )
            return 0.0

    def _decay_factor(self, days_idle: float) -> float:
        """Compute the multiplicative decay factor for *days_idle* days of inactivity."""
        return 0.5 ** (days_idle / self.half_life_days)

    def _apply_decay(self, skills: list[dict]) -> list[dict]:
        """Apply confidence decay in-place.  Returns the decay report."""
        report: list[dict] = []
        for skill in skills:
            old_confidence = _clamp(float(skill.get("confidence", 1.0)))
            days = self._days_idle(skill)
            if days <= 0:
                continue
            factor = self._decay_factor(days)
            new_confidence = _clamp(old_confidence * factor)
            if new_confidence != old_confidence:
                report.append(
                    {
                        "id": skill.get("id"),
                        "days_idle": round(days, 1),
                        "old": round(old_confidence, 4),
                        "new": round(new_confidence, 4),
                    }
                )
                skill["confidence"] = new_confidence
        log.info("[curator] decay applied to %d skills", len(report))
        return report

    # ── Deduplication ──────────────────────────────────────────────────────────

    def _build_similarity_matrix(
        self, skills: list[dict]
    ) -> list[list[float]]:
        """Return an n×n similarity matrix using Jaccard similarity."""
        # Try sentence-transformers first; fall back to Jaccard with a warning.
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore

            model = SentenceTransformer("all-MiniLM-L6-v2")
            texts = [_skill_text(s) for s in skills]
            embeddings = model.encode(texts, normalize_embeddings=True)
            # Cosine similarity via dot product (embeddings are normalised)
            import numpy as np  # type: ignore

            matrix = (embeddings @ embeddings.T).tolist()
            log.debug("[curator] similarity matrix built with sentence-transformers")
            return matrix
        except ImportError:
            log.warning(
                "[curator] sentence-transformers not available — "
                "falling back to token Jaccard similarity"
            )

        n = len(skills)
        texts = [_skill_text(s) for s in skills]
        matrix = [[0.0] * n for _ in range(n)]
        for i in range(n):
            for j in range(i, n):
                sim = _jaccard(texts[i], texts[j])
                matrix[i][j] = sim
                matrix[j][i] = sim
        return matrix

    def _cluster_skills(self, skills: list[dict]) -> list[list[int]]:
        """Return clusters (lists of indices) using greedy single-linkage."""
        if not skills:
            return []

        matrix = self._build_similarity_matrix(skills)
        n = len(skills)
        assigned = [False] * n
        clusters: list[list[int]] = []

        for i in range(n):
            if assigned[i]:
                continue
            cluster = [i]
            assigned[i] = True
            for j in range(i + 1, n):
                if not assigned[j] and matrix[i][j] >= self.similarity_threshold:
                    cluster.append(j)
                    assigned[j] = True
            clusters.append(cluster)

        return clusters

    def _deduplicate(self, skills: list[dict]) -> list[dict]:
        """Remove duplicates in-place.  Returns the deduplication report.

        Within each cluster the skill with the highest confidence is kept;
        the rest are removed.  Modifies *skills* in-place.
        """
        if not skills:
            return []

        clusters = self._cluster_skills(skills)
        report: list[dict] = []
        ids_to_remove: set[str] = set()

        for cluster in clusters:
            if len(cluster) <= 1:
                continue
            # Pick the skill with the highest confidence as the canonical one
            best_idx = max(cluster, key=lambda i: float(skills[i].get("confidence", 0)))
            removed_ids = [
                skills[i]["id"] for i in cluster if i != best_idx
            ]
            report.append(
                {"kept": skills[best_idx]["id"], "removed": removed_ids}
            )
            ids_to_remove.update(removed_ids)

        if ids_to_remove:
            skills[:] = [s for s in skills if s.get("id") not in ids_to_remove]
            log.info("[curator] deduplicated: removed %d skills across %d clusters", len(ids_to_remove), len(report))
        return report

    # ── Pruning ────────────────────────────────────────────────────────────────

    def _prune(self, skills: list[dict]) -> tuple[list[dict], list[dict]]:
        """Remove skills with confidence below prune_threshold.

        Returns ``(prune_report, surviving_skills)``.
        """
        surviving: list[dict] = []
        report: list[dict] = []

        for skill in skills:
            confidence = _clamp(float(skill.get("confidence", 1.0)))
            if confidence < self.prune_threshold:
                report.append(
                    {
                        "id": skill.get("id"),
                        "name": skill.get("name", ""),
                        "confidence": round(confidence, 4),
                    }
                )
            else:
                surviving.append(skill)

        if report:
            log.info("[curator] pruned %d skills below threshold %.2f", len(report), self.prune_threshold)
        return report, surviving

    # ── Audit log ─────────────────────────────────────────────────────────────

    def _append_audit(self, entry: dict) -> None:
        """Append *entry* as a JSON line to the audit file.

        Archives the audit file when it exceeds AUDIT_MAX_BYTES.
        """
        audit_dir = os.path.dirname(self.audit_path) or "."
        os.makedirs(audit_dir, exist_ok=True)

        # Archive if over size limit
        if os.path.exists(self.audit_path):
            size = os.path.getsize(self.audit_path)
            if size > AUDIT_MAX_BYTES:
                ts = _now_utc().strftime("%Y-%m-%d")
                archive = os.path.join(
                    audit_dir, f"audit-{ts}.jsonl"
                )
                os.rename(self.audit_path, archive)
                log.warning(
                    "[curator] audit.jsonl exceeded 100 MB — archived to %s", archive
                )

        with open(self.audit_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
        log.info("[curator] audit entry written to %s", self.audit_path)


# ── CLI entry point ────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m graph.skills.curator",
        description=(
            "Periodic skill curator — deduplicates, decays confidence, and "
            "prunes the protoAgent skill index."
        ),
    )
    p.add_argument(
        "--index",
        default=DEFAULT_INDEX_PATH,
        metavar="PATH",
        help=f"Path to the JSONL skill index (default: {DEFAULT_INDEX_PATH})",
    )
    p.add_argument(
        "--audit",
        default=DEFAULT_AUDIT_PATH,
        metavar="PATH",
        help=f"Path to the audit JSONL file (default: {DEFAULT_AUDIT_PATH})",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute changes but do not write to disk",
    )
    p.add_argument(
        "--half-life",
        type=float,
        default=HALF_LIFE_DAYS,
        metavar="DAYS",
        help=f"Confidence half-life in days (default: {HALF_LIFE_DAYS})",
    )
    p.add_argument(
        "--prune-threshold",
        type=float,
        default=PRUNE_THRESHOLD,
        metavar="FLOAT",
        help=f"Prune skills below this confidence (default: {PRUNE_THRESHOLD})",
    )
    p.add_argument(
        "--similarity-threshold",
        type=float,
        default=SIMILARITY_THRESHOLD,
        metavar="FLOAT",
        help=f"Jaccard similarity above which skills are duplicates (default: {SIMILARITY_THRESHOLD})",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO)",
    )
    return p


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    curator = SkillCurator(
        index_path=args.index,
        audit_path=args.audit,
        dry_run=args.dry_run,
        half_life_days=args.half_life,
        prune_threshold=args.prune_threshold,
        similarity_threshold=args.similarity_threshold,
    )
    audit_entry = curator.run()

    if args.dry_run:
        print("[dry-run] audit entry that would be written:")
        print(json.dumps(audit_entry, indent=2, default=str))


if __name__ == "__main__":
    main()
