"""Skill-v1 artifact schema for protoAgent.

A ``SkillV1Artifact`` is the in-memory shape of a skill — the "recipe" of a
recurring workflow (name, description, the prompt that drives it, the tools it
uses) plus the ADR-0052 user-facing slash metadata. It's the value the
``SKILL.md`` loader (``graph/skills/loader.py``) parses disk skills into, and the
shape the FTS5 index (``graph/skills/index.py``) stores and retrieves.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

log = logging.getLogger(__name__)


@dataclass
class SkillV1Artifact:
    """Serializable record of a skill workflow.

    Fields
    ------
    name              Short human-readable label for the skill.
    description       What the skill does, suitable for a skill registry.
    prompt_template   The procedure / prompt body the skill teaches.
    tools_used        Advisory tool names the skill relies on.
    created_at        UTC timestamp of capture.
    source_session_id Session that produced this artifact (for provenance).
    """

    name: str
    description: str
    prompt_template: str
    tools_used: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    source_session_id: str = ""
    # User-facing skills (ADR 0052): when True, the skill is offered as a `/<slash>`
    # command in the chat composer and runs its procedure on demand (in addition to the
    # implicit retrieval-injection every skill gets). ``slash`` is the trigger token
    # (whitespace-free); blank → derived from ``name``. Off by default — only
    # deliberately-authored skills become directly invokable.
    user_facing: bool = False
    slash: str = ""
    # User-ONLY skills (2026-06): user_facing AND withheld from the agent's retrieval
    # (`load_skills`) — a `/<slash>` command the operator can run, but the agent never
    # pulls into context. Implies user_facing.
    user_only: bool = False

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("SkillV1Artifact.name must be a non-empty string")
        if not isinstance(self.tools_used, list):
            raise TypeError("SkillV1Artifact.tools_used must be a list")
        if not isinstance(self.created_at, datetime):
            raise TypeError("SkillV1Artifact.created_at must be a datetime")

    def slash_token(self) -> str:
        """The whitespace-free `/<token>` trigger — explicit ``slash`` or a slug of
        ``name`` (lowercased, non-alphanumerics → hyphens)."""
        import re

        raw = (self.slash or self.name or "").strip().lower()
        return re.sub(r"[^a-z0-9]+", "-", raw).strip("-")

    def to_dict(self) -> dict:
        """Serialize to a JSON-compatible dict."""
        return {
            "name": self.name,
            "description": self.description,
            "prompt_template": self.prompt_template,
            "tools_used": list(self.tools_used),
            "created_at": self.created_at.isoformat(),
            "source_session_id": self.source_session_id,
            "user_facing": self.user_facing,
            "slash": self.slash,
        }
