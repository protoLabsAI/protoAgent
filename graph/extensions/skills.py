"""Skill-v1 artifact schema and emission infrastructure for protoAgent.

A skill-v1 artifact captures the "recipe" of a successful subagent workflow —
which tools were used, the prompt template, and provenance metadata. These
artifacts are emitted by task() when emit_skill=True and the subagent config
permits it.

The emitted DataPart shape follows the A2A extension pattern used by cost-v1
and worldstate-delta-v1: a ``kind: "data"`` part with a ``mimeType`` metadata
field that A2A consumers (e.g. Workstacean) use to route the payload to the
matching interceptor.

Emission is stored in a ContextVar so the collector (the A2A handler or any
other runtime wrapper) can retrieve all skills emitted during a task run
without coupling the graph package to the server package.
"""

from __future__ import annotations

import contextvars
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

log = logging.getLogger(__name__)

SKILL_V1_MIME = "application/vnd.protolabs.skill-v1+json"

# ContextVar holding skills emitted during the current async context (task run).
# Initialised to None; callers must use get_pending_skills() and
# emit_skill_artifact() rather than accessing this directly.
_pending_skills: contextvars.ContextVar[list[SkillV1Artifact] | None] = (
    contextvars.ContextVar("_pending_skills", default=None)
)


@dataclass
class SkillV1Artifact:
    """Serializable record of a subagent workflow captured as a reusable skill.

    Fields
    ------
    name              Short human-readable label for the skill.
    description       What the skill does, suitable for a skill registry.
    prompt_template   The prompt that drove the subagent run.
    tools_used        Tool names actually invoked during the run.
    created_at        UTC timestamp of capture.
    source_session_id Session that produced this artifact (for provenance).
    """

    name: str
    description: str
    prompt_template: str
    tools_used: list[str] = field(default_factory=list)
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    source_session_id: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("SkillV1Artifact.name must be a non-empty string")
        if not isinstance(self.tools_used, list):
            raise TypeError("SkillV1Artifact.tools_used must be a list")
        if not isinstance(self.created_at, datetime):
            raise TypeError("SkillV1Artifact.created_at must be a datetime")

    def to_dict(self) -> dict:
        """Serialize to a JSON-compatible dict."""
        return {
            "name": self.name,
            "description": self.description,
            "prompt_template": self.prompt_template,
            "tools_used": list(self.tools_used),
            "created_at": self.created_at.isoformat(),
            "source_session_id": self.source_session_id,
        }

    def to_datapart(self) -> dict:
        """Serialize as an A2A DataPart for inclusion in artifact ``parts``."""
        return {
            "kind": "data",
            "data": self.to_dict(),
            "metadata": {"mimeType": SKILL_V1_MIME},
        }


# ── ContextVar helpers ────────────────────────────────────────────────────────


def get_pending_skills() -> list[SkillV1Artifact]:
    """Return a snapshot of skills emitted in the current async context."""
    skills = _pending_skills.get()
    return list(skills) if skills is not None else []


def emit_skill_artifact(artifact: SkillV1Artifact) -> None:
    """Append *artifact* to the pending-skills list for this async context.

    Creates the list on first call within a fresh context. Subsequent calls
    append to the same list so multiple task() invocations in one session
    accumulate correctly.
    """
    skills = _pending_skills.get()
    if skills is None:
        skills = []
        _pending_skills.set(skills)
    skills.append(artifact)
    log.debug("[skill] emitted skill artifact: %s", artifact.name)
