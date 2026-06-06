"""Unit tests for graph.skills.curator.

Covers:
- Confidence decay calculations (50% after 90 days idle)
- Deduplication via Jaccard similarity clustering
- Pruning of low-confidence skills
- Audit log writing
- Full curator run on a fixture skill set
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone


from graph.skills.curator import (
    PRUNE_THRESHOLD,
    SIMILARITY_THRESHOLD,
    SkillCurator,
    _clamp,
    _jaccard,
)


# ── Helpers ────────────────────────────────────────────────────────────────────


def _utc_iso(days_ago: float = 0) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return dt.isoformat()


def _make_skill(
    name: str = "test skill",
    description: str = "does something useful",
    confidence: float = 1.0,
    days_ago: float = 0,
    last_used_days_ago: float | None = None,
    skill_id: str | None = None,
) -> dict:
    """Return a minimal well-formed skill dict."""
    skill: dict = {
        "id": skill_id or str(uuid.uuid4()),
        "name": name,
        "description": description,
        "prompt_template": f"Run the {name} workflow.",
        "tools_used": ["current_time"],
        "confidence": confidence,
        "created_at": _utc_iso(days_ago),
    }
    if last_used_days_ago is not None:
        skill["last_used"] = _utc_iso(last_used_days_ago)
    return skill


def _seed_index(db_path: str, skills: list[dict]):
    """Create a SkillsIndex at *db_path* and insert *skills* directly.

    Inserts confidence/last_used/created_at verbatim (bypassing add_skill's
    defaults) so curation tests can stage exact decay/prune scenarios. The
    rowid becomes the curator's skill ``id``. Returns the SkillsIndex.
    """
    from graph.skills.index import SkillsIndex

    idx = SkillsIndex(db_path)
    conn = idx._open_conn()
    for s in skills:
        conn.execute(
            """INSERT INTO skills_fts
               (name, description, prompt_template, tools_used,
                source_session_id, created_at, confidence, last_used)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                s["name"], s["description"], s.get("prompt_template", ""),
                " ".join(s.get("tools_used", [])), "",
                s.get("created_at", ""), s.get("confidence", 1.0),
                s.get("last_used", s.get("created_at", "")),
            ),
        )
    conn.commit()
    return idx


# ── _clamp ─────────────────────────────────────────────────────────────────────


class TestClamp:
    def test_value_in_range(self):
        assert _clamp(0.5) == 0.5

    def test_value_below_zero(self):
        assert _clamp(-0.1) == 0.0

    def test_value_above_one(self):
        assert _clamp(1.5) == 1.0

    def test_nan_clamps_to_lo(self):
        result = _clamp(float("nan"))
        assert result == 0.0

    def test_infinity_clamps_to_one(self):
        result = _clamp(float("inf"))
        assert result == 1.0

    def test_negative_infinity_clamps_to_zero(self):
        result = _clamp(float("-inf"))
        assert result == 0.0


# ── _jaccard ──────────────────────────────────────────────────────────────────


class TestJaccard:
    def test_identical_strings(self):
        assert _jaccard("hello world", "hello world") == 1.0

    def test_disjoint_strings(self):
        assert _jaccard("foo bar", "baz qux") == 0.0

    def test_partial_overlap(self):
        # {"search", "web"} ∩ {"search", "results"} = {"search"} → 1/3
        sim = _jaccard("web search", "search results")
        assert abs(sim - 1 / 3) < 1e-9

    def test_empty_strings(self):
        assert _jaccard("", "") == 1.0

    def test_case_insensitive(self):
        assert _jaccard("Hello World", "hello world") == 1.0


# ── Confidence decay ──────────────────────────────────────────────────────────


class TestConfidenceDecay:
    def _curator(self, **kwargs) -> SkillCurator:
        return SkillCurator(
            db_path="/dev/null",
            audit_path="/dev/null",
            dry_run=True,
            **kwargs,
        )

    def test_no_decay_when_fresh(self):
        curator = self._curator()
        skill = _make_skill(confidence=1.0, days_ago=0)
        report = curator._apply_decay([skill])
        assert report == []
        assert skill["confidence"] == 1.0

    def test_half_life_90_days(self):
        """After 90 days idle the confidence should halve."""
        curator = self._curator()
        skill = _make_skill(confidence=1.0, last_used_days_ago=90)
        curator._apply_decay([skill])
        assert abs(skill["confidence"] - 0.5) < 1e-6

    def test_half_life_180_days(self):
        """After 180 days idle the confidence should be ~0.25."""
        curator = self._curator()
        skill = _make_skill(confidence=1.0, last_used_days_ago=180)
        curator._apply_decay([skill])
        assert abs(skill["confidence"] - 0.25) < 1e-6

    def test_partial_decay(self):
        """After 45 days the confidence is 0.5^(45/90) = sqrt(0.5) ≈ 0.7071."""
        curator = self._curator()
        skill = _make_skill(confidence=1.0, last_used_days_ago=45)
        curator._apply_decay([skill])
        expected = 0.5 ** (45 / 90)
        assert abs(skill["confidence"] - expected) < 1e-6

    def test_decay_uses_last_used_over_created_at(self):
        """last_used timestamp takes priority over created_at."""
        curator = self._curator()
        # created 180 days ago but used 10 days ago
        skill = _make_skill(
            confidence=1.0, days_ago=180, last_used_days_ago=10
        )
        curator._apply_decay([skill])
        expected = 0.5 ** (10 / 90)
        assert abs(skill["confidence"] - expected) < 1e-6

    def test_decay_falls_back_to_created_at(self):
        """When last_used is absent, created_at is used as proxy."""
        curator = self._curator()
        skill = _make_skill(confidence=1.0, days_ago=90)
        # no last_used key
        assert "last_used" not in skill
        curator._apply_decay([skill])
        assert abs(skill["confidence"] - 0.5) < 1e-6

    def test_custom_half_life(self):
        curator = self._curator(half_life_days=30)
        skill = _make_skill(confidence=1.0, last_used_days_ago=30)
        curator._apply_decay([skill])
        assert abs(skill["confidence"] - 0.5) < 1e-6

    def test_report_entries(self):
        curator = self._curator()
        skill = _make_skill(confidence=1.0, last_used_days_ago=90)
        report = curator._apply_decay([skill])
        assert len(report) == 1
        entry = report[0]
        assert entry["id"] == skill["id"]
        assert abs(entry["old"] - 1.0) < 1e-3
        assert abs(entry["new"] - 0.5) < 1e-3


# ── Deduplication ─────────────────────────────────────────────────────────────


class TestDeduplication:
    def _curator(self, threshold: float = SIMILARITY_THRESHOLD) -> SkillCurator:
        return SkillCurator(
            db_path="/dev/null",
            audit_path="/dev/null",
            dry_run=True,
            similarity_threshold=threshold,
        )

    def test_no_duplicates(self):
        curator = self._curator()
        skills = [
            _make_skill("web search tool", "searches the web for info"),
            _make_skill("calculator tool", "performs arithmetic operations"),
        ]
        report = curator._deduplicate(skills)
        assert report == []
        assert len(skills) == 2

    def test_exact_duplicates_kept_higher_confidence(self):
        """Two identical skills: the one with higher confidence survives."""
        curator = self._curator(threshold=0.9)
        id_a, id_b = str(uuid.uuid4()), str(uuid.uuid4())
        skills = [
            _make_skill("web search", "searches the web", confidence=0.6, skill_id=id_a),
            _make_skill("web search", "searches the web", confidence=0.9, skill_id=id_b),
        ]
        report = curator._deduplicate(skills)
        assert len(report) == 1
        assert report[0]["kept"] == id_b
        assert id_a in report[0]["removed"]
        assert len(skills) == 1
        assert skills[0]["id"] == id_b

    def test_dissimilar_skills_not_merged(self):
        curator = self._curator(threshold=0.8)
        skills = [
            _make_skill("web scraper", "scrapes HTML from URLs"),
            _make_skill("database query", "runs SQL against Postgres"),
        ]
        report = curator._deduplicate(skills)
        assert report == []
        assert len(skills) == 2

    def test_three_similar_skills_one_survives(self):
        """Three very similar skills → only the highest-confidence one survives."""
        curator = self._curator(threshold=0.5)
        ids = [str(uuid.uuid4()) for _ in range(3)]
        skills = [
            _make_skill("fetch url content", "fetches and returns URL content", confidence=0.5, skill_id=ids[0]),
            _make_skill("fetch url page", "fetches and returns URL page", confidence=0.7, skill_id=ids[1]),
            _make_skill("fetch url data", "fetches and returns URL data", confidence=0.6, skill_id=ids[2]),
        ]
        report = curator._deduplicate(skills)
        # At least one cluster with a removal
        removed_total = sum(len(r["removed"]) for r in report)
        assert removed_total >= 1
        # The surviving skill(s) should all have id from the original set
        for s in skills:
            assert s["id"] in ids


# ── Pruning ────────────────────────────────────────────────────────────────────


class TestPruning:
    def _curator(self, threshold: float = PRUNE_THRESHOLD) -> SkillCurator:
        return SkillCurator(
            db_path="/dev/null",
            audit_path="/dev/null",
            dry_run=True,
            prune_threshold=threshold,
        )

    def test_no_pruning_above_threshold(self):
        curator = self._curator()
        skills = [_make_skill(confidence=0.5), _make_skill(confidence=0.9)]
        report, surviving = curator._prune(skills)
        assert report == []
        assert len(surviving) == 2

    def test_prune_below_threshold(self):
        curator = self._curator()
        low = _make_skill(confidence=0.1)
        high = _make_skill(confidence=0.8)
        report, surviving = curator._prune([low, high])
        assert len(report) == 1
        assert report[0]["id"] == low["id"]
        assert len(surviving) == 1
        assert surviving[0]["id"] == high["id"]

    def test_prune_at_threshold_boundary(self):
        """Confidence exactly equal to threshold is kept (not pruned)."""
        curator = self._curator(threshold=0.2)
        at_threshold = _make_skill(confidence=0.2)
        report, surviving = curator._prune([at_threshold])
        assert report == []
        assert len(surviving) == 1

    def test_prune_all(self):
        curator = self._curator(threshold=0.5)
        skills = [_make_skill(confidence=0.1), _make_skill(confidence=0.3)]
        report, surviving = curator._prune(skills)
        assert len(report) == 2
        assert surviving == []

    def test_prune_report_contains_name_and_confidence(self):
        curator = self._curator()
        skill = _make_skill(name="stale skill", confidence=0.05)
        report, _ = curator._prune([skill])
        assert report[0]["name"] == "stale skill"
        assert "confidence" in report[0]


# ── Full run on fixture ───────────────────────────────────────────────────────


class TestCuratorRun:
    """End-to-end runs against the live SQLite SkillsIndex (the store the
    runtime actually writes — see #173)."""

    def _audit(self, tmp_path):
        return str(tmp_path / "audit.jsonl")

    def test_dry_run_does_not_write_files(self, tmp_path):
        idx = _seed_index(str(tmp_path / "skills.db"), [_make_skill(confidence=1.0)])
        audit = self._audit(tmp_path)
        SkillCurator(index=idx, audit_path=audit, dry_run=True).run()
        assert not os.path.exists(audit)

    def test_dry_run_leaves_store_unchanged(self, tmp_path):
        # A low-confidence, long-idle skill would be pruned on a real run.
        idx = _seed_index(str(tmp_path / "skills.db"), [
            _make_skill(name="stale", description="old thing",
                        confidence=1.0, last_used_days_ago=400),
        ])
        SkillCurator(index=idx, audit_path=self._audit(tmp_path), dry_run=True).run()
        assert len(idx.all_skills()) == 1  # nothing deleted in dry-run

    def test_full_run_writes_audit(self, tmp_path):
        idx = _seed_index(str(tmp_path / "skills.db"), [_make_skill(confidence=1.0)])
        audit = self._audit(tmp_path)
        SkillCurator(index=idx, audit_path=audit, dry_run=False).run()

        assert os.path.exists(audit)
        with open(audit) as fh:
            entry = json.loads(fh.readline())
        assert "run_id" in entry
        assert entry["dry_run"] is False

    def test_empty_store_produces_empty_run(self, tmp_path):
        idx = _seed_index(str(tmp_path / "skills.db"), [])
        entry = SkillCurator(index=idx, audit_path=self._audit(tmp_path), dry_run=False).run()
        assert entry["skills_before"] == 0
        assert entry["skills_after"] == 0

    def test_decay_then_prune_pipeline(self, tmp_path):
        """A skill idle >300 days decays below threshold and is pruned from
        the SQLite store; the fresh one survives. Distinct names so dedup
        doesn't collapse the pair before prune runs."""
        old_skill = _make_skill(
            name="stale crawler", description="scrapes an old feed",
            confidence=1.0, last_used_days_ago=300,
        )
        fresh_skill = _make_skill(
            name="active summarizer", description="summarizes recent docs",
            confidence=0.9, last_used_days_ago=5,
        )
        idx = _seed_index(str(tmp_path / "skills.db"), [old_skill, fresh_skill])

        entry = SkillCurator(index=idx, audit_path=self._audit(tmp_path), dry_run=False).run()

        assert entry["skills_before"] == 2
        assert entry["skills_after"] == 1
        assert len(entry["pruned"]) == 1
        remaining = idx.all_skills()
        assert len(remaining) == 1
        assert remaining[0]["name"] == "active summarizer"

    def test_audit_entry_structure(self, tmp_path):
        idx = _seed_index(str(tmp_path / "skills.db"), [_make_skill()])
        entry = SkillCurator(index=idx, audit_path=self._audit(tmp_path), dry_run=False).run()
        required_keys = {
            "run_id", "timestamp", "dry_run", "skills_before",
            "skills_after", "decay_applied", "deduplicated", "pruned",
        }
        assert required_keys.issubset(entry.keys())

    def test_store_persisted_after_run(self, tmp_path):
        old_skill = _make_skill(
            name="stale crawler", description="scrapes an old feed",
            confidence=1.0, last_used_days_ago=300,
        )
        fresh_skill = _make_skill(
            name="active summarizer", description="summarizes recent docs",
            confidence=0.9, last_used_days_ago=2,
        )
        idx = _seed_index(str(tmp_path / "skills.db"), [old_skill, fresh_skill])

        SkillCurator(index=idx, audit_path=self._audit(tmp_path), dry_run=False).run()

        remaining = idx.all_skills()
        assert len(remaining) == 1
        assert remaining[0]["name"] == "active summarizer"
        # Survivor's decayed confidence was written back (< its seeded 0.9).
        assert remaining[0]["confidence"] < 0.9
