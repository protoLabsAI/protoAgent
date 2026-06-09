"""Layered skills (ADR 0041, slice 3) — read the COMMONS ∪ the PRIVATE index.

The ``layered`` tier is the "shared brain, private hands" model: an agent reads
both the shared commons skill library and its own private skills, but **writes go to
private** — so one agent's half-baked learned skills never pollute the fleet. A
proven skill is lifted into the commons explicitly via :meth:`promote` (curated, not
automatic — the commons is trusted).

This wraps two ordinary :class:`SkillsIndex` backends and presents the same surface
the middleware uses (``load_skills`` + the write methods), so it's a drop-in.
"""

from __future__ import annotations

import logging
import types

log = logging.getLogger(__name__)


class LayeredSkillsIndex:
    """A skills index whose reads union a private + a commons backend, whose writes
    target private, and which can ``promote`` a private skill into the commons."""

    def __init__(self, private, commons) -> None:
        self._private = private
        self._commons = commons

    # ── read: commons ∪ private, merged + de-duped, best-first ────────────────
    def load_skills(self, query: str, k: int = 5):
        merged: dict[str, object] = {}
        for rec in [*self._private.load_skills(query, k=k), *self._commons.load_skills(query, k=k)]:
            cur = merged.get(rec.name)
            if cur is None or rec.score < cur.score:  # BM25: lower = more relevant
                merged[rec.name] = rec
        return sorted(merged.values(), key=lambda r: r.score)[:k]

    # ── writes → private only ─────────────────────────────────────────────────
    def add_skill(self, artifact, source: str = "emitted") -> None:
        self._private.add_skill(artifact, source)

    def add_emitted_skill(self, artifact) -> None:
        self._private.add_emitted_skill(artifact)

    def replace_disk_skills(self, artifacts: list) -> None:
        self._private.replace_disk_skills(artifacts)

    def update_confidence(self, skill_id: int, confidence: float) -> None:
        self._private.update_confidence(skill_id, confidence)

    def delete_skill(self, skill_id: int) -> None:
        self._private.delete_skill(skill_id)

    def rebuild_index(self, artifacts: list) -> None:
        self._private.rebuild_index(artifacts)

    # ── introspection + promotion ─────────────────────────────────────────────
    def all_skills(self) -> list[dict]:
        return ([{**s, "tier": "private"} for s in self._private.all_skills()]
                + [{**s, "tier": "commons"} for s in self._commons.all_skills()])

    def promote(self, name: str) -> bool:
        """Copy a private skill (by name) into the commons. Returns False if no
        private skill by that name exists. Curated, explicit — the commons is trusted."""
        match = next((s for s in self._private.all_skills() if s.get("name") == name), None)
        if match is None:
            return False
        artifact = types.SimpleNamespace(
            name=match.get("name", ""),
            description=match.get("description", ""),
            prompt_template=match.get("prompt_template", ""),
            tools_used=tuple(match.get("tools_used") or ()),
            source_session_id=match.get("source_session_id", ""),
        )
        self._commons.add_skill(artifact, source="promoted")
        log.info("[skills] promoted %r to the commons", name)
        return True

    def close(self) -> None:
        self._private.close()
        self._commons.close()
