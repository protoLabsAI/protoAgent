"""Layered skills (ADR 0041, slice 3) — read the COMMONS ∪ the PRIVATE index.

The ``layered`` tier is the "shared brain, private hands" model: an agent reads
both the shared commons skill library and its own private skills, but **writes go to
private** — so one agent's half-baked learned skills never pollute the fleet. A
proven skill is lifted into the commons explicitly via :meth:`promote` (curated, not
automatic — the commons is trusted).

This wraps two ordinary :class:`SkillsIndex` backends and presents the same surface
the middleware uses (the ``skill_summaries`` index + ``get_skill`` + the write
methods), so it's a drop-in.
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

    # ── read: the always-on index — commons ∪ private, de-duped (private wins) ──
    def skill_summaries(self, limit: int | None = None) -> list[dict]:
        """Union the two tiers' lightweight ``{name, description, slash}`` index,
        de-duped by name (private shadows commons), then cap at ``limit``. Ordering
        follows each backend (most-recently-used first); private entries lead."""
        merged: dict[str, dict] = {}
        for backend in (self._commons, self._private):  # private listed last → wins
            for rec in backend.skill_summaries():
                merged[rec["name"]] = rec
        rows = list(merged.values())
        return rows[:limit] if limit is not None else rows

    def discoverable_count(self) -> int:
        """Distinct discoverable skills across both tiers (de-duped by name)."""
        names: set[str] = set()
        for backend in (self._private, self._commons):
            names.update(r["name"] for r in backend.skill_summaries())
        return len(names)

    def get_skill(self, name: str) -> dict | None:
        """Resolve one skill's full procedure by name — private shadows commons."""
        return self._private.get_skill(name) or self._commons.get_skill(name)

    # ── writes → private only ─────────────────────────────────────────────────
    def add_skill(self, artifact, source: str = "emitted") -> None:
        self._private.add_skill(artifact, source)

    def replace_disk_skills(self, artifacts: list) -> None:
        self._private.replace_disk_skills(artifacts)

    def update_confidence(self, skill_id: int, confidence: float) -> None:
        self._private.update_confidence(skill_id, confidence)

    def delete_skill(self, skill_id: int) -> None:
        self._private.delete_skill(skill_id)

    def rebuild_index(self, artifacts: list) -> None:
        self._private.rebuild_index(artifacts)

    # ── commons curation — the inverse of promote ─────────────────────────────
    @staticmethod
    def _forget_from_backend(backend, name: str) -> bool:
        """Delete every row named *name* from one backend. Returns True if any went."""
        removed = False
        for s in backend.all_skills():
            if s.get("name") == name and s.get("id") is not None:
                backend.delete_skill(s["id"])
                removed = True
        return removed

    def forget_from_commons(self, name: str) -> bool:
        """Remove a skill from the shared commons by name — the inverse of
        :meth:`promote`, and the only way to curate the otherwise curator-immutable
        commons (the curator writes private-only). Returns False when no commons
        skill by that name exists. Never touches the private tier."""
        return self._forget_from_backend(self._commons, name)

    # ── introspection + promotion ─────────────────────────────────────────────
    def all_skills(self) -> list[dict]:
        return [{**s, "tier": "private"} for s in self._private.all_skills()] + [
            {**s, "tier": "commons"} for s in self._commons.all_skills()
        ]

    def user_facing_skills(self) -> list[dict]:
        """User-facing skills (ADR 0052) from both tiers, de-duped by slash token
        (private wins). Backs the `/<slash>` chat commands."""
        merged: dict[str, dict] = {}
        for tier, backend in (("commons", self._commons), ("private", self._private)):
            reader = getattr(backend, "user_facing_skills", None)
            if reader is None:
                continue
            for s in reader():
                token = (s.get("slash") or s.get("name") or "").strip().lower()
                merged[token] = {**s, "tier": tier}  # private listed last → wins
        return list(merged.values())

    def promote(self, name: str) -> bool:
        """Lift a private skill (by name) into the commons. **Upsert**: re-promoting
        refreshes the commons copy instead of leaving a duplicate row (``add_skill``
        has no dedup of its own). Returns False if no private skill by that name
        exists, or if the commons write didn't land (e.g. an unwritable commons path,
        which ``add_skill`` would otherwise swallow). Curated, explicit — the commons
        is trusted."""
        match = next((s for s in self._private.all_skills() if s.get("name") == name), None)
        if match is None:
            return False
        artifact = types.SimpleNamespace(
            name=match.get("name", ""),
            description=match.get("description", ""),
            prompt_template=match.get("prompt_template", ""),
            tools_used=tuple(match.get("tools_used") or ()),
            source_session_id=match.get("source_session_id", ""),
            user_facing=bool(match.get("user_facing", False)),
            slash=match.get("slash", "") or "",
            # Preserve user_only across promotion — a /slash-only private skill must
            # stay /slash-only in the commons, not become agent-discoverable.
            user_only=bool(match.get("user_only", False)),
        )
        # Upsert: drop any prior commons copy first so re-promoting refreshes rather
        # than duplicating (the layered read de-dups by name, hiding the dupes in-app).
        self._forget_from_backend(self._commons, name)
        self._commons.add_skill(artifact, source="promoted")
        # add_skill swallows write errors; confirm the row actually landed so an
        # unwritable commons surfaces as a failure instead of a false success.
        if self._commons.get_skill(name) is None:
            log.error("[skills] promote(%r): commons write did not land — is the commons writable?", name)
            return False
        log.info("[skills] promoted %r to the commons", name)
        return True

    def close(self) -> None:
        self._private.close()
        self._commons.close()
